"""Bounded, resumable indexing pipeline.

The design constraints this file exists to satisfy:

* **Memory is bounded by the window, not the corpus.** Discovery is a generator
  and reconciliation uses a SQLite generation counter, so indexing a million
  files costs the same working set as indexing a thousand.
* **The writer lock is held per flush, never across the crawl.** Extraction,
  hashing, OCR, and embedding all happen outside the lock, so search is never
  waiting on indexing.
* **A committed batch is durable.** Vectors are written first, then cache rows
  in one transaction. If the cache write fails the run fails loudly, because the
  alternative is silently forgetting files whose vectors exist.
* **Pause and cancel are different.** Pause flushes and stops; cancel also skips
  deletion reconciliation, because an interrupted crawl never proved absence.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from haydar.config import (
    ALL_INDEXABLE_EXTENSIONS,
    IMAGE_EXTENSIONS,
    HaydarConfig,
    get_size_category,
    is_excluded,
)
from haydar.indexer.cache import CacheEntry, CacheWriteError, FileCache
from haydar.indexer.extractors import Disposition, extract
from haydar.search.store import VectorStore

logger = logging.getLogger(__name__)

# Flush when either bound is hit, so a few very large files cannot inflate a
# batch past what fits comfortably in memory.
DEFAULT_MAX_BATCH_BYTES = 32 * 1024 * 1024

# How long cancel waits for in-flight extraction before abandoning results.
CANCEL_DRAIN_SECONDS = 5.0


class JobKind(Enum):
    INITIAL = "initial"
    INCREMENTAL = "incremental"
    OCR_BACKFILL = "ocr_backfill"
    REBUILD = "rebuild"


class JobPhase(Enum):
    STARTING = "starting"
    DISCOVERING = "discovering"
    EXTRACTING = "extracting"
    EMBEDDING = "embedding"
    COMMITTING = "committing"
    RECONCILING = "reconciling"
    PAUSING = "pausing"
    CANCELLING = "cancelling"
    COMPLETE = "complete"
    FAILED = "failed"


class JobOutcome(Enum):
    """How a run ended. Distinguishable without inspecting a stats dictionary."""

    COMPLETE = "complete"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class IndexSnapshot:
    """A point-in-time view of one run, safe to render directly in the UI."""

    run_id: str = ""
    kind: JobKind = JobKind.INITIAL
    phase: JobPhase = JobPhase.STARTING
    outcome: JobOutcome | None = None
    discovered: int = 0
    examined: int = 0
    committed_files: int = 0
    committed_chunks: int = 0
    skipped_unchanged: int = 0
    skipped_size: int = 0
    skipped_unsupported: int = 0
    errors: int = 0
    ocr_deferred: int = 0
    deleted: int = 0
    current_path: str = ""
    elapsed_seconds: float = 0.0
    discovery_complete: bool = False
    error_message: str = ""

    @property
    def resumable(self) -> bool:
        return self.outcome in (JobOutcome.PAUSED, JobOutcome.CANCELLED, JobOutcome.FAILED)

    @property
    def has_stable_total(self) -> bool:
        """Whether a determinate percentage is honest yet.

        During open-ended discovery the denominator is still moving, so the UI
        must show an indeterminate indicator plus a discovered count instead of
        a percentage computed from a number that keeps growing.
        """
        return self.discovery_complete

    def to_stats(self) -> dict:
        """Render legacy stat keys for CLI output and existing callers."""
        return {
            "files_indexed": self.committed_files,
            "chunks_stored": self.committed_chunks,
            "files_skipped_size": self.skipped_size,
            "files_skipped_error": self.errors,
            "files_skipped_unchanged": self.skipped_unchanged,
            "files_deleted": self.deleted,
            "ocr_deferred": self.ocr_deferred,
            "total_text_bytes": self._text_bytes,
            "outcome": self.outcome.value if self.outcome else None,
            "run_id": self.run_id,
        }

    _text_bytes: int = 0


@dataclass
class DiscoveredFile:
    """One indexable path plus the stat data discovery already paid for."""

    path: Path
    mtime: float
    size: int

    @property
    def key(self) -> str:
        return str(self.path)


@dataclass
class ExtractionResult:
    filepath: str
    file_hash: str | None
    chunks: list[dict]
    disposition: Disposition
    detail: str = ""
    ocr_engine_version: str = ""
    mtime: float = 0.0
    size: int = 0
    # Retained for backwards compatibility with existing callers/tests.
    error: str | None = None
    skipped_reason: str | None = None


class JobControl:
    """Cooperative pause/cancel signalling shared with extraction workers.

    Pause and cancel are separate because they mean different things to the
    data: a pause keeps everything committed and resumes later, while a cancel
    additionally forfeits the right to reconcile deletions.
    """

    def __init__(self) -> None:
        self._pause = threading.Event()
        self._cancel = threading.Event()

    def request_pause(self) -> None:
        self._pause.set()

    def request_cancel(self) -> None:
        self._cancel.set()

    def clear(self) -> None:
        self._pause.clear()
        self._cancel.clear()

    @property
    def pause_requested(self) -> bool:
        return self._pause.is_set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    @property
    def should_stop(self) -> bool:
        return self._pause.is_set() or self._cancel.is_set()


class _LegacyCancelAdapter(JobControl):
    """Treat a plain ``threading.Event`` as a cancel request.

    Keeps the historical ``index_all(cancel_event=...)`` signature working.
    """

    def __init__(self, event) -> None:
        super().__init__()
        self._event = event

    @property
    def cancel_requested(self) -> bool:
        return bool(self._event is not None and self._event.is_set())

    @property
    def should_stop(self) -> bool:
        return self.cancel_requested or self.pause_requested


class IndexingEngine:
    def __init__(self, config: HaydarConfig, allow_download: bool = False):
        self.config = config
        self._allow_download = allow_download
        self._store: VectorStore | None = None
        self.cache = FileCache()

    @property
    def store(self) -> VectorStore:
        if self._store is None:
            self._store = VectorStore(self.config, allow_download=self._allow_download)
        return self._store

    @contextmanager
    def _acquire_index_lock(self, *, blocking: bool):
        """Process-exclusive lock around every write path.

        Held per flush rather than for a whole crawl, so a long index never
        blocks the watcher or a second writer for longer than one batch. Search
        reads never acquire this lock at all.

        ``blocking=False`` fails fast so a duplicate full index reports a clear
        message; ``blocking=True`` waits so watcher updates serialize instead of
        being dropped.
        """
        import msvcrt

        from haydar.config import INDEX_LOCK

        INDEX_LOCK.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(INDEX_LOCK, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(lock_fd)
            raise RuntimeError("Indexing already in progress.") from exc

        try:
            yield
        finally:
            with suppress(OSError):
                msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
            os.close(lock_fd)
            with suppress(OSError):
                INDEX_LOCK.unlink()

    # -- discovery ---------------------------------------------------------

    def _configured_roots(self) -> list[str]:
        """Normalized configured folders, for containment checks."""
        return [FileCache._normalize(folder) for folder in self.config.folders]

    def discover_pending_images(
        self,
        control: JobControl,
        *,
        ocr_version: str = "",
        only_extensions: frozenset[str] | None = None,
        on_discovered: Callable[[int], None] | None = None,
    ) -> Iterator[DiscoveredFile]:
        """Yield the images an OCR backfill should revisit, straight from the cache.

        The eligible set is already recorded — images deferred for want of OCR,
        and images whose text came from an older engine — so the backfill reads
        that work list instead of re-walking the corpus. Nothing but those paths
        is visited, which is what keeps non-image records untouched and stops a
        backfill from turning into a full reindex.

        Paths that have since been deleted or excluded, or that fall outside the
        currently configured folders, are skipped rather than removed: a backfill
        never proved absence and has no reconciliation authority.
        """
        extensions = only_extensions or frozenset(IMAGE_EXTENSIONS)
        roots = self._configured_roots()
        discovered = 0

        for abs_path in self.cache.iter_pending_images(
            extensions, ocr_version=ocr_version
        ):
            if control.should_stop:
                return
            if roots:
                normalized = FileCache._normalize(abs_path)
                if not any(normalized.startswith(root) for root in roots):
                    continue

            filepath = Path(abs_path)
            if is_excluded(filepath, self.config.excluded_patterns):
                continue
            try:
                file_stat = filepath.stat()
            except OSError:
                # Gone or unreadable. Leave the record alone.
                continue
            if not filepath.is_file():
                continue

            discovered += 1
            if on_discovered is not None:
                on_discovered(discovered)
            yield DiscoveredFile(
                path=filepath, mtime=file_stat.st_mtime, size=file_stat.st_size
            )

    def discover(
        self,
        control: JobControl,
        *,
        force: bool = False,
        only_extensions: frozenset[str] | None = None,
        ocr_version: str = "",
        on_discovered: Callable[[int], None] | None = None,
        on_skipped_unchanged: Callable[[], None] | None = None,
        generation: int | None = None,
    ) -> Iterator[DiscoveredFile]:
        """Yield indexable files one at a time, never materializing the corpus.

        Unchanged, already-complete files are marked as seen for this generation
        and skipped. ``ocr_deferred`` files are deliberately *not* skipped: they
        are unfinished work, not finished work.
        """
        extensions = only_extensions or ALL_INDEXABLE_EXTENSIONS
        discovered = 0
        seen_batch: list[str] = []

        def flush_seen() -> None:
            if seen_batch and generation is not None:
                self.cache.mark_seen(seen_batch, generation)
                seen_batch.clear()

        for folder_path in self.config.folders:
            folder = Path(folder_path)
            if not folder.exists() or not folder.is_dir():
                continue

            def _on_error(exc):
                logger.debug("Skipping inaccessible path: %s", exc.filename)

            for root, dirs, files in os.walk(folder, onerror=_on_error):
                # Checked at every directory boundary so a pause during a deep
                # crawl is acknowledged promptly.
                if control.should_stop:
                    flush_seen()
                    return
                dirs[:] = [
                    d for d in dirs
                    if not is_excluded(Path(root) / d, self.config.excluded_patterns)
                ]

                for file in files:
                    if control.should_stop:
                        flush_seen()
                        return
                    filepath = Path(root) / file
                    ext = filepath.suffix.lower()
                    if ext not in extensions:
                        continue
                    if is_excluded(filepath, self.config.excluded_patterns):
                        continue

                    try:
                        file_stat = filepath.stat()
                    except OSError:
                        continue
                    if not filepath.is_file():
                        continue

                    abs_path = str(filepath.absolute())
                    seen_batch.append(abs_path)
                    if len(seen_batch) >= 500:
                        flush_seen()

                    if not force and self.cache.is_unchanged_and_complete(
                        abs_path,
                        file_stat.st_mtime,
                        file_stat.st_size,
                        ocr_version=ocr_version if ext in IMAGE_EXTENSIONS else "",
                    ):
                        if on_skipped_unchanged is not None:
                            on_skipped_unchanged()
                        continue

                    discovered += 1
                    if on_discovered is not None:
                        on_discovered(discovered)
                    yield DiscoveredFile(
                        path=filepath,
                        mtime=file_stat.st_mtime,
                        size=file_stat.st_size,
                    )
        flush_seen()

    # -- extraction --------------------------------------------------------

    def _extract_worker(
        self,
        discovered: DiscoveredFile,
        config: HaydarConfig,
        force: bool,
        control: JobControl | None = None,
        *,
        refresh_ocr: bool = False,
    ) -> ExtractionResult:
        filepath = discovered.path
        filepath_str = str(filepath)
        try:
            if control is not None and control.cancel_requested:
                return ExtractionResult(
                    filepath_str, None, [], Disposition.TRANSIENT_ERROR,
                    detail="cancelled", mtime=discovered.mtime, size=discovered.size,
                )

            ext = filepath.suffix.lower()
            category = get_size_category(ext)
            limit = config.size_limits.get(category, 0)
            if discovered.size > limit:
                return ExtractionResult(
                    filepath_str, None, [], Disposition.TOO_LARGE,
                    detail=f"Exceeds {category} limit",
                    mtime=discovered.mtime, size=discovered.size,
                    skipped_reason=f"Exceeds {category} limit",
                )

            try:
                file_hash = self._compute_hash(filepath)
            except OSError as e:
                return ExtractionResult(
                    filepath_str, None, [], Disposition.TRANSIENT_ERROR,
                    detail=f"Could not hash: {e}",
                    mtime=discovered.mtime, size=discovered.size,
                    skipped_reason=f"Could not hash: {e}",
                )

            if control is not None and control.cancel_requested:
                return ExtractionResult(
                    filepath_str, file_hash, [], Disposition.TRANSIENT_ERROR,
                    detail="cancelled", mtime=discovered.mtime, size=discovered.size,
                )

            outcome = extract(filepath, file_hash=file_hash, refresh_ocr=refresh_ocr)
            if outcome.disposition is not Disposition.CONTENT:
                return ExtractionResult(
                    filepath_str, file_hash, [], outcome.disposition,
                    detail=outcome.detail,
                    ocr_engine_version=outcome.ocr_engine_version,
                    mtime=discovered.mtime, size=discovered.size,
                )

            chunks = self._chunk_text(
                outcome.text, config.chunk_size, config.chunk_overlap
            )
            return ExtractionResult(
                filepath_str, file_hash, chunks,
                Disposition.CONTENT if chunks else Disposition.EMPTY,
                ocr_engine_version=outcome.ocr_engine_version,
                mtime=discovered.mtime, size=discovered.size,
            )
        except Exception as e:
            return ExtractionResult(
                filepath_str, None, [], Disposition.PERMANENT_ERROR,
                detail=str(e), mtime=discovered.mtime, size=discovered.size,
                error=str(e),
            )

    # -- the run -----------------------------------------------------------

    def run_job(
        self,
        *,
        kind: JobKind = JobKind.INITIAL,
        force: bool = False,
        control: JobControl | None = None,
        on_snapshot: Callable[[IndexSnapshot], None] | None = None,
        on_committed: Callable[[IndexSnapshot], None] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        only_extensions: frozenset[str] | None = None,
        ocr_version: str = "",
        run_id: str | None = None,
    ) -> IndexSnapshot:
        """Execute one indexing run and return how it ended.

        The returned snapshot carries an explicit :class:`JobOutcome`, so callers
        never have to infer "was this cancelled?" from a stats dictionary.
        """
        control = control or JobControl()
        snapshot = IndexSnapshot(
            run_id=run_id or secrets.token_hex(4), kind=kind, phase=JobPhase.STARTING
        )
        started = time.monotonic()
        # Every record from this run carries its id, so interleaved runs and
        # watcher writes stay distinguishable in the log.
        log = logging.LoggerAdapter(logger, {"run_id": snapshot.run_id})

        def emit(phase: JobPhase | None = None, *, committed: bool = False) -> None:
            if phase is not None:
                snapshot.phase = phase
            snapshot.elapsed_seconds = time.monotonic() - started
            if on_snapshot is not None:
                on_snapshot(snapshot)
            if committed and on_committed is not None:
                on_committed(snapshot)

        log.info("Starting %s index run.", kind.value)
        from haydar.indexer.extractors import prune_extraction_cache

        prune_extraction_cache()

        try:
            generation = self.cache.next_generation()
        except CacheWriteError as exc:
            snapshot.outcome = JobOutcome.FAILED
            snapshot.error_message = str(exc)
            emit(JobPhase.FAILED)
            return snapshot

        batch = _Batch(self.config)
        max_workers = min(32, (os.cpu_count() or 1) + 4)
        submission_window = max_workers * 4
        # A re-OCR must not be served from the content-keyed text cache: the
        # bytes are unchanged, but the engine reading them is not.
        refresh_ocr = kind is JobKind.OCR_BACKFILL

        try:
            emit(JobPhase.DISCOVERING)

            def note_skipped() -> None:
                snapshot.skipped_unchanged += 1
                # A resumed run is mostly skips, so they must count as progress
                # or the bar would sit at zero while real work is happening.
                if progress_callback is not None:
                    progress_callback(
                        snapshot.examined + snapshot.skipped_unchanged,
                        max(
                            snapshot.discovered + snapshot.skipped_unchanged,
                            snapshot.examined + snapshot.skipped_unchanged,
                        ),
                    )

            discovery = self._discovery_for(
                kind,
                control,
                force=force,
                only_extensions=only_extensions,
                ocr_version=ocr_version,
                generation=generation,
                on_discovered=lambda count: setattr(snapshot, "discovered", count),
                on_skipped_unchanged=note_skipped,
            )

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers
            ) as executor:
                pending: set[concurrent.futures.Future] = set()
                discovery_exhausted = False

                while True:
                    # Keep the submission window full but bounded, so the number
                    # of in-flight extractions never scales with corpus size.
                    while not discovery_exhausted and len(pending) < submission_window:
                        if control.should_stop:
                            break
                        try:
                            item = next(discovery)
                        except StopIteration:
                            discovery_exhausted = True
                            snapshot.discovery_complete = True
                            break
                        pending.add(
                            executor.submit(
                                self._extract_worker,
                                item,
                                self.config,
                                force,
                                control,
                                refresh_ocr=refresh_ocr,
                            )
                        )

                    if not pending:
                        break

                    done, pending = concurrent.futures.wait(
                        pending,
                        timeout=0.5,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )

                    for future in done:
                        snapshot.examined += 1
                        try:
                            result = future.result()
                        except Exception as e:
                            log.error("Extraction worker failed: %s", e)
                            snapshot.errors += 1
                            continue
                        needs_preflush = self._record(
                            snapshot, result, batch, snapshot.run_id
                        )
                        if needs_preflush:
                            # This file would cross a bound, so commit what is
                            # already batched and start a fresh one with it.
                            emit(JobPhase.COMMITTING)
                            self._flush(batch, snapshot, generation)
                            emit(JobPhase.EXTRACTING, committed=True)
                            self._record(snapshot, result, batch, snapshot.run_id)
                        if progress_callback is not None:
                            done_count = snapshot.examined + snapshot.skipped_unchanged
                            total = max(
                                snapshot.discovered + snapshot.skipped_unchanged,
                                done_count,
                            )
                            progress_callback(done_count, total)
                        # Checked per file, not per wait() batch: a single large
                        # document can cross the ceiling on its own, and the
                        # batch must never exceed the configured bound.
                        if batch.should_flush():
                            emit(JobPhase.COMMITTING)
                            self._flush(batch, snapshot, generation)
                            emit(JobPhase.EXTRACTING, committed=True)

                    if control.cancel_requested:
                        # Bounded drain: keep results already computed, abandon
                        # the rest rather than waiting on the whole queue.
                        emit(JobPhase.CANCELLING)
                        self._drain(
                            pending, snapshot, batch, snapshot.run_id, generation
                        )
                        pending.clear()
                        break
                    if control.pause_requested:
                        emit(JobPhase.PAUSING)
                        self._drain(
                            pending, snapshot, batch, snapshot.run_id, generation
                        )
                        pending.clear()
                        break

                    emit(JobPhase.EXTRACTING)

            if batch.pending:
                emit(JobPhase.COMMITTING)
                self._flush(batch, snapshot, generation)
                emit(committed=True)

            if control.cancel_requested:
                snapshot.outcome = JobOutcome.CANCELLED
                emit(JobPhase.CANCELLING)
                return snapshot
            if control.pause_requested:
                snapshot.outcome = JobOutcome.PAUSED
                emit(JobPhase.PAUSING)
                return snapshot

            # Reconciliation only after a discovery pass that actually finished.
            # Anything else never proved a file was gone.
            if snapshot.discovery_complete and kind in (JobKind.INITIAL, JobKind.REBUILD):
                emit(JobPhase.RECONCILING)
                self._reconcile(generation, snapshot)

            snapshot.outcome = JobOutcome.COMPLETE
            emit(JobPhase.COMPLETE)
            return snapshot

        except CacheWriteError as exc:
            # Vectors were written but their cache rows were not. Fail loudly:
            # a later run re-upserts the same deterministic ids safely.
            log.error("Cache write failed after a vector commit: %s", exc)
            snapshot.outcome = JobOutcome.FAILED
            snapshot.error_message = str(exc)
            emit(JobPhase.FAILED)
            return snapshot
        except Exception as exc:
            log.exception("Index run failed")
            snapshot.outcome = JobOutcome.FAILED
            snapshot.error_message = str(exc)
            emit(JobPhase.FAILED)
            return snapshot

    def _discovery_for(
        self,
        kind: JobKind,
        control: JobControl,
        *,
        force: bool,
        only_extensions: frozenset[str] | None,
        ocr_version: str,
        generation: int,
        on_discovered: Callable[[int], None] | None,
        on_skipped_unchanged: Callable[[], None] | None,
    ) -> Iterator[DiscoveredFile]:
        """Choose the work list for this run kind.

        An OCR backfill visits the cache's known-eligible images; every other
        kind crawls the configured folders. Keeping the choice here means the
        rest of the pipeline — batching, the per-flush lock, commit ordering —
        is identical for both.
        """
        if kind is JobKind.OCR_BACKFILL:
            return self.discover_pending_images(
                control,
                ocr_version=ocr_version,
                only_extensions=only_extensions,
                on_discovered=on_discovered,
            )
        return self.discover(
            control,
            force=force,
            only_extensions=only_extensions,
            ocr_version=ocr_version,
            generation=generation,
            on_discovered=on_discovered,
            on_skipped_unchanged=on_skipped_unchanged,
        )

    def _drain(
        self,
        pending: set,
        snapshot: IndexSnapshot,
        batch: _Batch,
        run_id: str,
        generation: int,
    ) -> None:
        """Collect already-finished work, then abandon the rest promptly.

        A thread pool that waits for every queued task after a cancel would make
        cancellation unbounded, so pending futures are cancelled and only what
        already finished within the drain window is kept.
        """
        for future in list(pending):
            future.cancel()
        done, _ = concurrent.futures.wait(pending, timeout=CANCEL_DRAIN_SECONDS)
        for future in done:
            if future.cancelled():
                continue
            try:
                result = future.result()
            except Exception:
                continue
            snapshot.examined += 1
            if self._record(snapshot, result, batch, run_id):
                self._flush(batch, snapshot, generation)
                self._record(snapshot, result, batch, run_id)
            if batch.should_flush():
                self._flush(batch, snapshot, generation)

    def _record(
        self,
        snapshot: IndexSnapshot,
        result: ExtractionResult,
        batch: _Batch,
        run_id: str,
    ) -> bool:
        """Fold one extraction outcome into the pending batch and counters.

        Returns ``True`` when the batch must be flushed *before* this file is
        added, so a file's chunks and its cache row always commit together.
        """
        snapshot.current_path = _redact(result.filepath)
        disposition = result.disposition

        if disposition is Disposition.TOO_LARGE:
            snapshot.skipped_size += 1
            return False
        if disposition is Disposition.UNSUPPORTED:
            snapshot.skipped_unsupported += 1
            return False
        if disposition is Disposition.OCR_DEFERRED:
            # Recorded so a later backfill can find it, but never as complete.
            snapshot.ocr_deferred += 1
            batch.add_cache_only(
                CacheEntry(
                    filepath=result.filepath,
                    mtime=result.mtime,
                    size=result.size,
                    file_hash=result.file_hash,
                    chunk_count=0,
                    disposition="ocr_deferred",
                    ocr_engine_version=result.ocr_engine_version,
                    committed_run_id=run_id,
                )
            )
            return False
        if disposition in (Disposition.TRANSIENT_ERROR, Disposition.PERMANENT_ERROR):
            if result.detail != "cancelled":
                snapshot.errors += 1
                logger.debug("Skipping %s: %s", result.filepath, result.detail)
            return False

        if batch.would_exceed(result):
            return True
        batch.add_file(result, run_id)
        return False

    def _flush(
        self, batch: _Batch, snapshot: IndexSnapshot, generation: int
    ) -> None:
        """Commit one batch: vectors first, then all cache rows in one transaction.

        The writer lock covers only this section. Extraction and embedding
        preparation happened outside it, and search never takes it at all.
        """
        if not batch.pending:
            return

        payload = batch.take()
        with self._acquire_index_lock(blocking=True):
            if payload.deletions:
                self.store.delete_by_filepaths(payload.deletions)
            if payload.ids:
                self.store.add_documents(
                    ids=payload.ids,
                    documents=payload.documents,
                    metadatas=payload.metadatas,
                )
            # Raises CacheWriteError on failure; the run then fails rather than
            # reporting files as committed when their record did not persist.
            self.cache.set_many(payload.cache_entries, generation=generation)

        snapshot.committed_chunks += len(payload.ids)
        snapshot.committed_files += payload.indexed_files
        snapshot._text_bytes += payload.text_bytes

    def _reconcile(self, generation: int, snapshot: IndexSnapshot) -> None:
        """Remove entries for files that are gone, after a proven-complete crawl."""
        stale = self.cache.stale_filepaths(generation, self.config.folders)
        if not stale:
            return
        logger.info("Removing %d deleted files from the index.", len(stale))
        with self._acquire_index_lock(blocking=True):
            self.store.delete_by_filepaths(stale)
            self.cache.remove_many(stale)
        snapshot.deleted = len(stale)

    # -- compatibility surface --------------------------------------------

    def index_all(
        self,
        force: bool = False,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_event=None,
    ) -> dict:
        """Run a full index and return legacy stat keys.

        Retained for the CLI and existing tests. New callers should prefer
        :meth:`run_job`, which reports a typed outcome.
        """
        control = _LegacyCancelAdapter(cancel_event) if cancel_event is not None else JobControl()
        with self._acquire_run_guard():
            snapshot = self.run_job(
                kind=JobKind.REBUILD if force else JobKind.INITIAL,
                force=force,
                control=control,
                progress_callback=progress_callback,
            )
        return snapshot.to_stats()

    @contextmanager
    def _acquire_run_guard(self):
        """Fail fast if another full index already owns the writer lock.

        This is a short probe, not a lock held across the run: batches take the
        lock individually so the watcher and search are never blocked for long.
        """
        with self._acquire_index_lock(blocking=False):
            pass
        yield

    def index_file(self, filepath: Path) -> bool:
        """Index or re-index a single file (watcher path)."""
        if not filepath.exists() or not filepath.is_file():
            return False

        ext = filepath.suffix.lower()
        if ext not in ALL_INDEXABLE_EXTENSIONS:
            return False

        try:
            file_stat = filepath.stat()
            discovered = DiscoveredFile(
                path=filepath, mtime=file_stat.st_mtime, size=file_stat.st_size
            )
            result = self._extract_worker(discovered, self.config, force=True)
            if result.disposition is Disposition.OCR_DEFERRED:
                with self._acquire_index_lock(blocking=True):
                    self.cache.set(
                        str(filepath.absolute()), result.mtime, result.size,
                        result.file_hash, 0, disposition="ocr_deferred",
                        ocr_engine_version=result.ocr_engine_version,
                    )
                return False
            if result.disposition not in (Disposition.CONTENT, Disposition.EMPTY):
                return False

            abs_path = str(filepath.absolute())
            ids, documents, metadatas = _build_chunk_payload(
                abs_path, result, result.mtime
            )

            with self._acquire_index_lock(blocking=True):
                self.store.delete_by_filepath(abs_path)
                batch_size = 100
                for i in range(0, len(ids), batch_size):
                    self.store.add_documents(
                        ids=ids[i:i + batch_size],
                        documents=documents[i:i + batch_size],
                        metadatas=metadatas[i:i + batch_size],
                    )
                self.cache.set(
                    abs_path, result.mtime, result.size, result.file_hash, len(ids),
                    disposition="indexed" if ids else "empty",
                    ocr_engine_version=result.ocr_engine_version,
                )
            return bool(ids)
        except Exception:
            logger.exception("Could not index %s", filepath)
            return False

    def remove_file(self, filepath: Path) -> None:
        """Remove a file from the index."""
        abs_path = str(filepath.absolute())
        try:
            with self._acquire_index_lock(blocking=True):
                self.store.delete_by_filepath(abs_path)
                self.cache.remove(abs_path)
        except RuntimeError:
            logger.warning("Deferred removal of %s: indexing busy.", abs_path)
        except CacheWriteError:
            logger.exception("Could not remove the cache row for %s", abs_path)

    @staticmethod
    def estimate_unindexed_count(
        folders: list[str],
        config: HaydarConfig,
        cap: int = 10_000,
    ) -> int:
        """Count supported files newer than the newest cached file.

        Bounded so this launch-time check stays cheap on large folders. An empty
        cache means there is no baseline yet, so nothing is reported as stale.
        """
        if cap <= 0:
            return 0

        cache = FileCache()
        try:
            newest_mtime = cache.newest_mtime()
        finally:
            cache.close()
        if newest_mtime is None:
            return 0

        unindexed = 0
        visited = 0
        for folder_path in folders:
            folder = Path(folder_path)
            if not folder.is_dir():
                continue

            for root, dirs, files in os.walk(folder):
                dirs[:] = [
                    name for name in dirs
                    if not is_excluded(Path(root) / name, config.excluded_patterns)
                ]
                for name in files:
                    if visited >= cap:
                        return unindexed
                    visited += 1
                    filepath = Path(root) / name
                    if filepath.suffix.lower() not in ALL_INDEXABLE_EXTENSIONS:
                        continue
                    if is_excluded(filepath, config.excluded_patterns):
                        continue
                    try:
                        if filepath.stat().st_mtime > newest_mtime:
                            unindexed += 1
                    except OSError:
                        continue

        return unindexed

    def close(self) -> None:
        """Release resources (the SQLite cache connection)."""
        with suppress(Exception):
            self.cache.close()

    def __enter__(self) -> IndexingEngine:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[dict]:
        """Split text into overlapping word chunks, retaining original offsets."""
        import re
        word_spans = [(m.start(), m.end(), m.group()) for m in re.finditer(r'\S+', text)]
        chunks = []

        if not word_spans:
            return chunks

        i = 0
        while i < len(word_spans):
            end_idx = min(i + chunk_size, len(word_spans))
            chunk_words = word_spans[i:end_idx]

            # Drop only a negligible trailing remainder; keep short file tails
            # so the end of a multi-chunk file isn't silently unindexed.
            if len(chunk_words) < 5 and chunks:
                break

            start_char = chunk_words[0][0]
            end_char = chunk_words[-1][1]

            chunks.append({
                "text": text[start_char:end_char],
                "start_char": start_char,
                "end_char": end_char
            })

            if end_idx == len(word_spans):
                break

            i += (chunk_size - overlap)

        return chunks

    @staticmethod
    def _compute_hash(filepath: Path) -> str:
        """Compute MD5 hash of first 8KB concatenated with file size."""
        file_size = filepath.stat().st_size
        hasher = hashlib.md5()

        with open(filepath, 'rb') as f:
            chunk = f.read(8192)
            hasher.update(chunk)

        hasher.update(str(file_size).encode('utf-8'))
        return hasher.hexdigest()


@dataclass
class _Payload:
    ids: list[str]
    documents: list[str]
    metadatas: list[dict]
    deletions: list[str]
    cache_entries: list[CacheEntry]
    indexed_files: int
    text_bytes: int


class _Batch:
    """Accumulates one flush worth of work, bounded by both chunks and bytes."""

    def __init__(self, config: HaydarConfig) -> None:
        self.config = config
        self.max_chunks = max(1, config.embedding_batch_size)
        self.max_bytes = getattr(
            config, "embedding_batch_max_bytes", DEFAULT_MAX_BATCH_BYTES
        )
        self.ids: list[str] = []
        self.documents: list[str] = []
        self.metadatas: list[dict] = []
        self.deletions: list[str] = []
        self.cache_entries: list[CacheEntry] = []
        self.indexed_files = 0
        self.text_bytes = 0
        self._batch_bytes = 0

    @property
    def pending(self) -> bool:
        return bool(self.ids or self.deletions or self.cache_entries)

    def should_flush(self) -> bool:
        # Either bound triggers a flush: a handful of very large documents can
        # exceed the memory ceiling long before the chunk count does.
        return len(self.ids) >= self.max_chunks or self._batch_bytes >= self.max_bytes

    def would_exceed(self, result: ExtractionResult) -> bool:
        """Whether adding this file to the current batch would cross a bound.

        A file's chunks and its cache row must commit together, so a file is
        never split across batches. Instead the batch is flushed first, which
        keeps every batch within the bound unless one single file exceeds it.
        """
        if not self.ids:
            return False
        added_chunks = len(result.chunks)
        added_bytes = sum(
            len(chunk["text"].encode("utf-8")) for chunk in result.chunks
        )
        return (
            len(self.ids) + added_chunks > self.max_chunks
            or self._batch_bytes + added_bytes > self.max_bytes
        )

    def add_cache_only(self, entry: CacheEntry) -> None:
        self.cache_entries.append(entry)

    def add_file(self, result: ExtractionResult, run_id: str) -> None:
        self.deletions.append(result.filepath)
        ids, documents, metadatas = _build_chunk_payload(
            result.filepath, result, result.mtime
        )
        for chunk_id, document, metadata in zip(ids, documents, metadatas, strict=True):
            self.ids.append(chunk_id)
            self.documents.append(document)
            self.metadatas.append(metadata)
            size = len(document.encode("utf-8"))
            self.text_bytes += size
            self._batch_bytes += size

        self.cache_entries.append(
            CacheEntry(
                filepath=result.filepath,
                mtime=result.mtime,
                size=result.size,
                file_hash=result.file_hash,
                chunk_count=len(ids),
                disposition="indexed" if ids else "empty",
                ocr_engine_version=result.ocr_engine_version,
                committed_run_id=run_id,
            )
        )
        self.indexed_files += 1

    def take(self) -> _Payload:
        payload = _Payload(
            ids=list(self.ids),
            documents=list(self.documents),
            metadatas=list(self.metadatas),
            deletions=list(self.deletions),
            cache_entries=list(self.cache_entries),
            indexed_files=self.indexed_files,
            text_bytes=self.text_bytes,
        )
        self.ids.clear()
        self.documents.clear()
        self.metadatas.clear()
        self.deletions.clear()
        self.cache_entries.clear()
        self.indexed_files = 0
        self.text_bytes = 0
        self._batch_bytes = 0
        return payload


def _build_chunk_payload(
    abs_path: str, result: ExtractionResult, mtime: float
) -> tuple[list[str], list[str], list[dict]]:
    """Build deterministic chunk ids and metadata for one file version.

    Ids are derived from the canonical path, the content hash, and the chunk
    index, so re-processing a file after a failure upserts over its own chunks
    rather than duplicating them.
    """
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    path_hash = hashlib.md5(abs_path.encode("utf-8")).hexdigest()
    suffix = Path(abs_path).suffix.lower()
    filename = Path(abs_path).name

    for index, chunk in enumerate(result.chunks):
        ids.append(f"{path_hash}_{index}")
        documents.append(chunk["text"])
        metadatas.append({
            "file_path": abs_path,
            "filepath": abs_path,
            "file_type": suffix,
            "chunk_index": index,
            "start_char": chunk["start_char"],
            "end_char": chunk["end_char"],
            "file_hash": result.file_hash,
            "modified_time": mtime,
            "filename": filename,
        })
    return ids, documents, metadatas


def _redact(path: str, limit: int = 80) -> str:
    """Bound a path for display so a deep path cannot distort the status band."""
    if len(path) <= limit:
        return path
    name = Path(path).name
    return f"…{os.sep}{name}" if len(name) < limit else f"…{name[-limit:]}"

"""SQLite-backed file cache with explicit transactions and crawl generations.

Two properties matter here and both are about honesty:

* **Write failures are never suppressed.** A cache row is the record that a
  file's vectors were committed. Swallowing a write error would make a later run
  skip a file whose vectors may not exist, so write paths raise.
* **Dispositions are recorded, not inferred.** A file skipped because OCR was
  unavailable is not the same as a file that genuinely contained no text, so the
  two are stored distinctly and only one of them counts as done.

Reads stay tolerant: a failed read degrades to "unknown", which costs a
reprocess rather than silent data loss.
"""

from __future__ import annotations

import builtins
import contextlib
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from haydar.config import DB_DIR

logger = logging.getLogger(__name__)

# Dispositions that mean "this file version is fully processed"; anything else
# stays eligible for a later run.
COMPLETE_DISPOSITIONS: frozenset[str] = frozenset({"indexed", "empty", "unsupported"})

# A file whose text could not be read yet. These are deliberately re-attempted:
# `ocr_deferred` becomes indexable once OCR is installed.
PENDING_DISPOSITIONS: frozenset[str] = frozenset({"ocr_deferred", "transient_error"})


class CacheWriteError(Exception):
    """A cache write failed after vectors were already committed.

    The run must fail rather than continue: the vector store and the cache have
    diverged, and only a run that knows it failed will reprocess those files.
    """


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so a suffix matches literally.

    A file extension is ordinary data, but ``%`` and ``_`` are wildcards to
    SQLite, and ``_`` in particular appears in real suffixes.
    """
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


@dataclass(frozen=True)
class CacheEntry:
    """One file version's recorded processing outcome."""

    filepath: str
    mtime: float
    size: int
    file_hash: str | None
    chunk_count: int
    disposition: str = "indexed"
    ocr_engine_version: str = ""
    committed_run_id: str = ""

    @property
    def is_complete(self) -> bool:
        return self.disposition in COMPLETE_DISPOSITIONS


class FileCache:
    """SQLite cache of per-file processing state, keyed by absolute path."""

    def __init__(self) -> None:
        self.db_path = DB_DIR / "haydar_cache.db"
        self._conn: sqlite3.Connection | None = None
        # One connection is shared across extraction threads, so writes are
        # serialized here rather than relying on SQLite's own locking.
        self._write_lock = threading.RLock()
        self._init_db()

    # -- connection --------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            DB_DIR.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # WAL lets a reader continue while a writer commits, which is the
            # same property search relies on at the vector-store layer.
            with contextlib.suppress(sqlite3.DatabaseError):
                self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        try:
            self._create_schema()
        except sqlite3.DatabaseError as e:
            logger.warning(
                "Cache database is corrupted or inaccessible: %s. Rebuilding...", e
            )
            if self._conn:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None
            if self.db_path.exists():
                with contextlib.suppress(OSError):
                    self.db_path.unlink()
            self._create_schema()

    def _create_schema(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_cache (
                filepath TEXT PRIMARY KEY,
                mtime REAL,
                size INTEGER,
                file_hash TEXT,
                chunk_count INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_state (
                key TEXT PRIMARY KEY,
                value INTEGER
            )
        """)
        # Added incrementally so an existing cache upgrades in place instead of
        # forcing users to re-index everything they already have.
        self._add_column_if_missing(conn, "seen_generation", "INTEGER")
        self._add_column_if_missing(
            conn, "disposition", "TEXT NOT NULL DEFAULT 'indexed'"
        )
        self._add_column_if_missing(conn, "committed_run_id", "TEXT")
        self._add_column_if_missing(conn, "ocr_engine_version", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_cache_generation "
            "ON file_cache(seen_generation)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_cache_disposition "
            "ON file_cache(disposition)"
        )
        conn.commit()

    @staticmethod
    def _add_column_if_missing(
        conn: sqlite3.Connection, column: str, definition: str
    ) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(file_cache)")}
        if column not in existing:
            conn.execute(f"ALTER TABLE file_cache ADD COLUMN {column} {definition}")

    # -- generations -------------------------------------------------------

    def next_generation(self) -> int:
        """Allocate a monotonically increasing crawl generation.

        Reconciliation compares generations in SQLite rather than holding a set
        of every path seen this run in process memory, so a multi-million-file
        corpus costs no more than a small one.
        """
        with self._write_lock:
            conn = self._get_conn()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO crawl_state (key, value) VALUES ('generation', 1) "
                        "ON CONFLICT(key) DO UPDATE SET value = value + 1"
                    )
                row = conn.execute(
                    "SELECT value FROM crawl_state WHERE key = 'generation'"
                ).fetchone()
            except sqlite3.Error as exc:
                raise CacheWriteError(f"Could not allocate a crawl generation: {exc}") from exc
            return int(row[0]) if row else 1

    def current_generation(self) -> int:
        try:
            row = self._get_conn().execute(
                "SELECT value FROM crawl_state WHERE key = 'generation'"
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error:
            return 0

    def mark_seen(self, filepaths: Sequence[str], generation: int) -> None:
        """Record that these paths exist on disk in this crawl generation."""
        if not filepaths:
            return
        with self._write_lock:
            conn = self._get_conn()
            try:
                with conn:
                    conn.executemany(
                        "UPDATE file_cache SET seen_generation = ? WHERE filepath = ?",
                        [(generation, path) for path in filepaths],
                    )
            except sqlite3.Error as exc:
                raise CacheWriteError(f"Could not record crawl generation: {exc}") from exc

    def stale_filepaths(self, generation: int, roots: Sequence[str]) -> list[str]:
        """Return cached paths under ``roots`` not seen in ``generation``.

        Only ever called after a *complete* discovery pass. After a cancelled or
        failed crawl, absence was never proven, so callers must not reconcile.
        """
        if not roots:
            return []
        try:
            cur = self._get_conn().execute(
                "SELECT filepath FROM file_cache "
                "WHERE seen_generation IS NULL OR seen_generation < ?",
                (generation,),
            )
        except sqlite3.Error as e:
            logger.debug("Cache generation read error: %s", e)
            return []

        normalized_roots = [self._normalize(root) for root in roots]
        stale = []
        for row in cur.fetchall():
            path = row[0]
            normalized = self._normalize(path)
            if any(normalized.startswith(root) for root in normalized_roots):
                stale.append(path)
        return stale

    @staticmethod
    def _normalize(path: str) -> str:
        import os

        normalized = os.path.normcase(os.path.abspath(path))
        return normalized if normalized.endswith(os.sep) else normalized + os.sep

    # -- reads -------------------------------------------------------------

    def get(self, filepath: str) -> dict[str, Any] | None:
        """Return the cached row for a file, or ``None`` when unknown."""
        try:
            cur = self._get_conn().cursor()
            cur.execute(
                "SELECT mtime, size, file_hash, chunk_count, disposition, "
                "ocr_engine_version, committed_run_id "
                "FROM file_cache WHERE filepath = ?",
                (filepath,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
        except sqlite3.Error as e:
            logger.debug("Cache read error for %s: %s", filepath, e)
        return None

    def is_unchanged_and_complete(
        self, filepath: str, mtime: float, size: int, *, ocr_version: str = ""
    ) -> bool:
        """Whether this exact file version is already fully processed.

        A deferred image is *not* unchanged-complete: it must stay eligible so
        installing OCR later can pick it up. An image processed by an older OCR
        engine is likewise eligible again after an upgrade.
        """
        cached = self.get(filepath)
        if cached is None:
            return False
        if cached["mtime"] != mtime or cached["size"] != size:
            return False
        if (cached["disposition"] or "indexed") not in COMPLETE_DISPOSITIONS:
            return False
        return not ocr_version or (cached["ocr_engine_version"] or "") == ocr_version

    def get_all_filepaths(self) -> builtins.set[str]:
        try:
            cur = self._get_conn().cursor()
            cur.execute("SELECT filepath FROM file_cache")
            return {row[0] for row in cur.fetchall()}
        except sqlite3.Error as e:
            logger.debug("Cache list error: %s", e)
            return set()

    def iter_pending_images(
        self, extensions: Iterable[str], *, ocr_version: str = ""
    ) -> Iterator[str]:
        """Yield image paths eligible for an OCR backfill.

        Eligible means deferred for want of OCR, or processed by an older OCR
        engine than the one now installed. This is the backfill's work list: it
        is what lets an OCR install revisit exactly the known-eligible images
        instead of re-crawling the corpus.

        Selection happens in SQL and rows stream from the cursor, so the caller's
        memory is bounded by the batch it is building rather than by the number
        of cached files.
        """
        suffixes = tuple(dict.fromkeys(ext.lower() for ext in extensions))
        if not suffixes:
            return
        # LIKE treats `%` and `_` as wildcards, so a suffix is escaped rather
        # than trusted. SQLite's LIKE is already case-insensitive for ASCII.
        like_clause = " OR ".join("filepath LIKE ? ESCAPE '\\'" for _ in suffixes)
        params: list[Any] = [f"%{_escape_like(suffix)}" for suffix in suffixes]

        if ocr_version:
            eligible = (
                "(COALESCE(disposition, 'indexed') = 'ocr_deferred' "
                "OR COALESCE(ocr_engine_version, '') <> ?)"
            )
            params.append(ocr_version)
        else:
            # Without a known engine version, only deferral proves eligibility.
            eligible = "COALESCE(disposition, 'indexed') = 'ocr_deferred'"

        try:
            cursor = self._get_conn().execute(
                f"SELECT filepath FROM file_cache WHERE ({like_clause}) AND {eligible}",
                params,
            )
            for row in cursor:
                yield row[0]
        except sqlite3.Error as e:
            logger.debug("Cache backfill read error: %s", e)
            return

    def newest_mtime(self) -> float | None:
        try:
            row = self._get_conn().execute(
                "SELECT MAX(mtime) FROM file_cache"
            ).fetchone()
            return None if row is None or row[0] is None else float(row[0])
        except sqlite3.Error as e:
            logger.debug("Cache newest-mtime read error: %s", e)
            return None

    def count_by_disposition(self) -> dict[str, int]:
        try:
            cur = self._get_conn().execute(
                "SELECT COALESCE(disposition, 'indexed'), COUNT(*) "
                "FROM file_cache GROUP BY 1"
            )
            return {row[0]: int(row[1]) for row in cur.fetchall()}
        except sqlite3.Error:
            return {}

    # -- writes ------------------------------------------------------------

    def set(
        self,
        filepath: str,
        mtime: float,
        size: int,
        file_hash: str | None,
        chunk_count: int,
        *,
        disposition: str = "indexed",
        ocr_engine_version: str = "",
        committed_run_id: str = "",
        generation: int | None = None,
    ) -> None:
        """Write one file's outcome. Raises :class:`CacheWriteError` on failure."""
        self.set_many(
            [
                CacheEntry(
                    filepath=filepath,
                    mtime=mtime,
                    size=size,
                    file_hash=file_hash,
                    chunk_count=chunk_count,
                    disposition=disposition,
                    ocr_engine_version=ocr_engine_version,
                    committed_run_id=committed_run_id,
                )
            ],
            generation=generation,
        )

    def set_many(
        self, entries: Sequence[CacheEntry], *, generation: int | None = None
    ) -> None:
        """Commit a batch of file outcomes in one transaction.

        All rows for a committed vector batch land together or none do, so a
        partially written batch can never be mistaken for completed work.
        """
        if not entries:
            return
        with self._write_lock:
            conn = self._get_conn()
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO file_cache (
                            filepath, mtime, size, file_hash, chunk_count,
                            disposition, ocr_engine_version, committed_run_id,
                            seen_generation
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                entry.filepath,
                                entry.mtime,
                                entry.size,
                                entry.file_hash,
                                entry.chunk_count,
                                entry.disposition,
                                entry.ocr_engine_version,
                                entry.committed_run_id,
                                generation,
                            )
                            for entry in entries
                        ],
                    )
            except sqlite3.Error as exc:
                # Never suppressed: the caller has already written vectors, so
                # the run must be marked failed and those files reprocessed.
                raise CacheWriteError(
                    f"Could not record {len(entries)} committed file(s): {exc}"
                ) from exc

    def remove(self, filepath: str) -> None:
        self.remove_many([filepath])

    def remove_many(self, filepaths: Sequence[str]) -> None:
        if not filepaths:
            return
        with self._write_lock:
            conn = self._get_conn()
            try:
                with conn:
                    conn.executemany(
                        "DELETE FROM file_cache WHERE filepath = ?",
                        [(f,) for f in filepaths],
                    )
            except sqlite3.Error as exc:
                raise CacheWriteError(f"Could not remove cache rows: {exc}") from exc

    def clear(self) -> None:
        with self._write_lock:
            conn = self._get_conn()
            try:
                with conn:
                    conn.execute("DELETE FROM file_cache")
            except sqlite3.Error as exc:
                raise CacheWriteError(f"Could not clear the cache: {exc}") from exc

    def close(self) -> None:
        if self._conn:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None

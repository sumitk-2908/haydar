"""
Main indexing orchestrator for Haydar.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from rich.progress import BarColumn, Progress, TextColumn

from haydar.config import (
    ALL_INDEXABLE_EXTENSIONS,
    HaydarConfig,
    get_size_category,
    is_excluded,
)
from haydar.indexer.cache import FileCache
from haydar.indexer.extractors import extract_text
from haydar.search.store import VectorStore

logger = logging.getLogger(__name__)

@dataclass
class ExtractionResult:
    filepath: str
    file_hash: str | None
    chunks: list[dict]
    error: str | None
    skipped_reason: str | None

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
        """Process-exclusive lock around every ChromaDB write path.

        A single lock file (``INDEX_LOCK``) serialises `index_all`, `index_file`
        and `remove_file` so a full reindex can never interleave upserts with the
        watcher's single-file updates (and two full indexes can't run at once).

        ``blocking=False`` (full index): raise ``RuntimeError`` immediately if the
        lock is held, so `reindex`/`init` fail fast with a friendly message.
        ``blocking=True`` (watcher single-file ops): wait for the holder so the
        update serialises instead of being dropped. Windows-only (``msvcrt``),
        consistent with the rest of the app.
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

    def _extract_worker(self, filepath: Path, config: HaydarConfig, force: bool) -> ExtractionResult:
        try:
            try:
                file_size = filepath.stat().st_size
            except OSError as e:
                return ExtractionResult(str(filepath), None, [], None, f"Could not read (locked/permission): {e}")

            ext = filepath.suffix.lower()
            category = get_size_category(ext)
            limit = config.size_limits.get(category, 0)
            if file_size > limit:
                return ExtractionResult(str(filepath), None, [], None, f"Exceeds {category} limit")

            try:
                file_hash = self._compute_hash(filepath)
            except OSError as e:
                return ExtractionResult(str(filepath), None, [], None, f"Could not hash: {e}")

            extracted = extract_text(filepath, file_hash=file_hash)
            if not extracted or not extracted.text.strip():
                return ExtractionResult(str(filepath), file_hash, [], None, None) # Zero chunks

            chunks = self._chunk_text(extracted.text, config.chunk_size, config.chunk_overlap)
            return ExtractionResult(str(filepath), file_hash, chunks, None, None)
        except Exception as e:
            return ExtractionResult(str(filepath), None, [], str(e), None)

    def index_all(self, force: bool = False) -> dict:
        """Index all files using a producer-consumer pipeline with batching."""
        with self._acquire_index_lock(blocking=False):
            return self._index_all_internal(force)

    def _index_all_internal(self, force: bool = False) -> dict:
        # Bound the on-disk extraction cache before a full index run.
        from haydar.indexer.extractors import prune_extraction_cache
        prune_extraction_cache()

        stats = {
            "files_indexed": 0,
            "chunks_stored": 0,
            "files_skipped_size": 0,
            "files_skipped_error": 0,
            "files_skipped_unchanged": 0,
            "total_text_bytes": 0
        }

        # 1. Crawl filesystem and pre-filter
        files_to_process = []
        current_disk_paths = set()

        for folder_path in self.config.folders:
            folder = Path(folder_path)
            if not folder.exists() or not folder.is_dir():
                continue

            def _on_error(exc):
                logger.debug(f"Skipping inaccessible path: {exc.filename}")

            for root, dirs, files in os.walk(folder, onerror=_on_error):
                dirs[:] = [d for d in dirs if not is_excluded(Path(root) / d, self.config.excluded_patterns)]

                for file in files:
                    filepath = Path(root) / file
                    try:
                        if not filepath.is_file():
                            continue
                        file_stat = filepath.stat()
                        mtime = file_stat.st_mtime
                        size = file_stat.st_size
                    except OSError:
                        continue

                    ext = filepath.suffix.lower()
                    if ext not in ALL_INDEXABLE_EXTENSIONS:
                        continue
                    if is_excluded(filepath, self.config.excluded_patterns):
                        continue

                    abs_path_str = str(filepath.absolute())
                    current_disk_paths.add(abs_path_str)

                    if not force:
                        cached = self.cache.get(abs_path_str)
                        if cached and cached["mtime"] == mtime and cached["size"] == size:
                            stats["files_skipped_unchanged"] += 1
                            continue

                    files_to_process.append(filepath)

        # 2. Invalidation Pass
        all_cached = self.cache.get_all_filepaths()
        deleted_files = list(all_cached - current_disk_paths)
        if deleted_files:
            logger.info(f"Removing {len(deleted_files)} deleted files from index.")
            self.store.delete_by_filepaths(deleted_files)
            self.cache.remove_many(deleted_files)

        if not files_to_process:
            return stats

        # 3. Producer-Consumer Pipeline
        batch_ids = []
        batch_documents = []
        batch_metadatas = []
        batch_deletions = []
        cache_updates = []

        def flush_batch():
            nonlocal batch_ids, batch_documents, batch_metadatas, batch_deletions, cache_updates
            if not batch_ids and not batch_deletions:
                return

            try:
                if batch_deletions:
                    self.store.delete_by_filepaths(batch_deletions)
                if batch_ids:
                    self.store.add_documents(ids=batch_ids, documents=batch_documents, metadatas=batch_metadatas)
                    stats["chunks_stored"] += len(batch_ids)

                # Update cache only after successful flush
                for update in cache_updates:
                    self.cache.set(*update)
                    stats["files_indexed"] += 1
            except Exception as e:
                logger.error(f"Failed to flush batch. Affected files: {batch_deletions}. Error: {e}")
                stats["files_skipped_error"] += len(cache_updates)
            finally:
                batch_ids.clear()
                batch_documents.clear()
                batch_metadatas.clear()
                batch_deletions.clear()
                cache_updates.clear()

        max_workers = min(32, (os.cpu_count() or 1) + 4)
        submission_window = max_workers * 4

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
        ) as progress:
            task = progress.add_task("[cyan]Indexing files...", total=len(files_to_process))

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Chunked submission loop
                for i in range(0, len(files_to_process), submission_window):
                    batch_files = files_to_process[i:i + submission_window]
                    futures = {
                        executor.submit(self._extract_worker, f, self.config, force): f
                        for f in batch_files
                    }

                    for future in concurrent.futures.as_completed(futures):
                        try:
                            result = future.result()
                        except Exception as e:
                            logger.error(f"Catastrophic worker failure: {e}")
                            stats["files_skipped_error"] += 1
                            progress.advance(task)
                            continue

                        filepath_str = result.filepath
                        progress.advance(task)

                        if result.skipped_reason:
                            logger.debug(f"Skipped {filepath_str}: {result.skipped_reason}")
                            if "Exceeds" in result.skipped_reason:
                                stats["files_skipped_size"] += 1
                            continue

                        if result.error:
                            logger.error(f"Error extracting {filepath_str}: {result.error}")
                            stats["files_skipped_error"] += 1
                            continue

                        # It's a valid file. Get stat to cache.
                        try:
                            f_stat = os.stat(filepath_str)
                            mtime = f_stat.st_mtime
                            size = f_stat.st_size
                        except OSError:
                            continue

                        batch_deletions.append(filepath_str)

                        if not result.chunks:
                            # Zero-chunk file (empty or unreadable valid extension)
                            cache_updates.append((filepath_str, mtime, size, result.file_hash, 0))
                            continue

                        # Create a unique prefix for this file's chunks to avoid ChromaDB ID collisions for identical files
                        path_hash = hashlib.md5(filepath_str.encode('utf-8')).hexdigest()

                        for i, chunk_info in enumerate(result.chunks):
                            batch_ids.append(f"{path_hash}_{i}")
                            batch_documents.append(chunk_info["text"])
                            batch_metadatas.append({
                                "file_path": filepath_str,
                                "filepath": filepath_str,
                                "file_type": Path(filepath_str).suffix.lower(),
                                "chunk_index": i,
                                "start_char": chunk_info["start_char"],
                                "end_char": chunk_info["end_char"],
                                "file_hash": result.file_hash,
                                "modified_time": mtime,
                                "filename": Path(filepath_str).name
                            })
                            stats["total_text_bytes"] += len(chunk_info["text"].encode('utf-8'))

                        cache_updates.append((filepath_str, mtime, size, result.file_hash, len(result.chunks)))

                        if len(batch_ids) >= self.config.embedding_batch_size:
                            flush_batch()

        # Final flush for any remaining chunks
        flush_batch()
        return stats

    def index_file(self, filepath: Path) -> bool:
        """Index or re-index a single file."""
        if not filepath.exists() or not filepath.is_file():
            return False

        ext = filepath.suffix.lower()
        if ext not in ALL_INDEXABLE_EXTENSIONS:
            return False

        try:
            file_hash = self._compute_hash(filepath)
            extracted = extract_text(filepath, file_hash=file_hash)
            if not extracted or not extracted.text.strip():
                return False

            chunks = self._chunk_text(extracted.text, self.config.chunk_size, self.config.chunk_overlap)
            if not chunks:
                return False

            ids = []
            documents = []
            metadatas = []
            mod_time = os.path.getmtime(filepath)

            path_hash = hashlib.md5(str(filepath.absolute()).encode('utf-8')).hexdigest()
            for i, chunk_info in enumerate(chunks):
                ids.append(f"{path_hash}_{i}")
                documents.append(chunk_info["text"])
                metadatas.append({
                    "file_path": str(filepath.absolute()),
                    "file_type": ext,
                    "chunk_index": i,
                    "start_char": chunk_info["start_char"],
                    "end_char": chunk_info["end_char"],
                    "file_hash": file_hash,
                    "modified_time": mod_time,
                    "filename": filepath.name
                })

            batch_size = 100
            with self._acquire_index_lock(blocking=True):
                self.store.delete_by_filepath(str(filepath.absolute()))
                for i in range(0, len(ids), batch_size):
                    self.store.add_documents(
                        ids=ids[i:i + batch_size],
                        documents=documents[i:i + batch_size],
                        metadatas=metadatas[i:i + batch_size]
                    )
                self.cache.set(str(filepath.absolute()), mod_time, filepath.stat().st_size, file_hash, len(chunks))
            return True
        except Exception:
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
        """Split text into overlapping chunks of words, retaining original char offsets."""
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

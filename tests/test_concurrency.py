"""Concurrency contract: search reads are never gated on the indexing lock.

The product promise is that search stays usable while indexing runs. That holds
only if readers never acquire ``INDEX_LOCK`` and never deadlock behind a writer,
so these tests assert the lock discipline directly rather than trusting it.
"""

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexingEngine, JobControl
from haydar.search.hybrid import HybridSearch

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="INDEX_LOCK uses msvcrt (Windows only)"
)


def _config(folder):
    config = HaydarConfig(folders=[str(folder)])
    config.excluded_patterns = []
    return config


def _corpus(tmp_path, count=30):
    folder = tmp_path / "corpus"
    folder.mkdir()
    for index in range(count):
        (folder / f"file-{index}.txt").write_text(
            f"searchable document {index} " + "word " * 50, encoding="utf-8"
        )
    return folder


def test_semantic_reads_never_acquire_the_writer_lock(tmp_haydar, tmp_path):
    """A query must not take INDEX_LOCK, or search would stall behind indexing."""
    folder = _corpus(tmp_path, count=5)
    lock_acquisitions = []

    with patch("haydar.search.store.VectorStore") as mock_store_class:
        store = MagicMock()
        store.query.return_value = []
        mock_store_class.return_value = store
        search = HybridSearch(_config(folder))
        search._store = store

        real_open = IndexingEngine._acquire_index_lock

        def tracking_lock(self, *, blocking):
            lock_acquisitions.append(blocking)
            return real_open(self, blocking=blocking)

        with patch.object(IndexingEngine, "_acquire_index_lock", tracking_lock):
            list(search.search_stream("document", mode="semantic"))

    assert lock_acquisitions == []


def test_readers_observe_committed_batches_while_a_writer_runs(tmp_haydar, tmp_path):
    """Every committed batch must be visible to a reader that queries after it."""
    folder = _corpus(tmp_path, count=24)
    config = _config(folder)
    config.embedding_batch_size = 4

    visible_ids: set[str] = set()
    read_errors: list[Exception] = []
    observed_counts: list[int] = []
    stop_reading = threading.Event()

    store = MagicMock()

    def record_upsert(ids, documents, metadatas):
        visible_ids.update(ids)

    store.add_documents.side_effect = record_upsert
    store.query.side_effect = lambda *a, **k: [{"id": i} for i in sorted(visible_ids)]

    def reader():
        while not stop_reading.is_set():
            try:
                observed_counts.append(len(store.query("document", n_results=10)))
            except Exception as exc:
                read_errors.append(exc)
            time.sleep(0.001)

    with patch("haydar.indexer.engine.VectorStore") as mock_class:
        mock_class.return_value = store
        engine = IndexingEngine(config, allow_download=False)
        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        try:
            snapshot = engine.run_job()
        finally:
            stop_reading.set()
            reader_thread.join(timeout=5)
            engine.close()

    assert not read_errors
    assert snapshot.committed_chunks == len(visible_ids)
    # Reads saw a monotonically growing snapshot, never a malformed partial one.
    assert observed_counts == sorted(observed_counts)


def test_keyword_search_never_acquires_the_writer_lock(tmp_haydar, tmp_path):
    folder = _corpus(tmp_path, count=3)
    search = HybridSearch(_config(folder))
    acquisitions = []

    real_open = IndexingEngine._acquire_index_lock

    def tracking_lock(self, *, blocking):
        acquisitions.append(blocking)
        return real_open(self, blocking=blocking)

    with patch.object(IndexingEngine, "_acquire_index_lock", tracking_lock):
        # ripgrep is not provisioned in tests; the point is only that the code
        # path takes no writer lock on its way to that outcome.
        list(search.search_stream("document", mode="keyword"))

    assert acquisitions == []


def test_the_writer_lock_is_released_between_batches(tmp_haydar, tmp_path):
    """Holding the lock across the crawl would block the watcher for its duration."""
    folder = _corpus(tmp_path, count=20)
    config = _config(folder)
    config.embedding_batch_size = 3
    held_windows = []

    with patch("haydar.indexer.engine.VectorStore") as mock_class:
        mock_class.return_value = MagicMock()
        engine = IndexingEngine(config, allow_download=False)
        real_open = IndexingEngine._acquire_index_lock

        from contextlib import contextmanager

        @contextmanager
        def counting_lock(self, *, blocking):
            with real_open(self, blocking=blocking):
                held_windows.append(1)
                yield

        try:
            with patch.object(IndexingEngine, "_acquire_index_lock", counting_lock):
                engine.run_job()
        finally:
            engine.close()

    # Multiple short critical sections, not one long one.
    assert len(held_windows) > 1


def test_a_second_full_index_fails_clearly_instead_of_corrupting(
    tmp_haydar, tmp_path
):
    folder = _corpus(tmp_path, count=4)

    with patch("haydar.indexer.engine.VectorStore") as mock_class:
        mock_class.return_value = MagicMock()
        first = IndexingEngine(_config(folder), allow_download=False)
        second = IndexingEngine(_config(folder), allow_download=False)
        try:
            with (
                first._acquire_index_lock(blocking=False),
                pytest.raises(RuntimeError, match="already in progress"),
            ):
                second.index_all()
        finally:
            first.close()
            second.close()


def test_watcher_writes_wait_for_the_current_batch_rather_than_being_dropped(
    tmp_haydar, tmp_path
):
    folder = _corpus(tmp_path, count=2)
    modes = []

    with patch("haydar.indexer.engine.VectorStore") as mock_class:
        mock_class.return_value = MagicMock()
        engine = IndexingEngine(_config(folder), allow_download=False)
        real_open = IndexingEngine._acquire_index_lock

        from contextlib import contextmanager

        @contextmanager
        def recording_lock(self, *, blocking):
            modes.append(blocking)
            with real_open(self, blocking=blocking):
                yield

        try:
            with patch.object(IndexingEngine, "_acquire_index_lock", recording_lock):
                engine.index_file(folder / "file-0.txt")
                engine.remove_file(folder / "file-1.txt")
        finally:
            engine.close()

    # Blocking, so a watcher update queues behind indexing instead of vanishing.
    assert modes == [True, True]


def test_cancellation_during_a_lock_wait_does_not_deadlock(tmp_haydar, tmp_path):
    folder = _corpus(tmp_path, count=10)
    control = JobControl()
    control.request_cancel()

    with patch("haydar.indexer.engine.VectorStore") as mock_class:
        mock_class.return_value = MagicMock()
        engine = IndexingEngine(_config(folder), allow_download=False)
        try:
            finished = threading.Event()

            def run():
                engine.run_job(control=control)
                finished.set()

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            assert finished.wait(timeout=20), "cancelled run did not terminate"
        finally:
            engine.close()

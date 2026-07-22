"""Concurrency-lock tests for IndexingEngine (T1b-1).

Verify that the single process-exclusive index lock covers *every* ChromaDB
write path -- the full index (`index_all`) and the watcher's single-file
updates (`index_file`, `remove_file`) -- so a running reindex can never
interleave upserts with live watcher events.

Windows-only: the lock uses ``msvcrt``.
"""

import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

import haydar.indexer.engine as engine_mod
from haydar.config import HaydarConfig

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="index lock uses msvcrt (Windows-only)")

if sys.platform == "win32":
    import msvcrt


def _engine(monkeypatch) -> engine_mod.IndexingEngine:
    """An engine whose VectorStore is mocked, so no embedding model is needed."""
    monkeypatch.setattr(engine_mod, "VectorStore", MagicMock())
    config = HaydarConfig()
    config.folders = []
    return engine_mod.IndexingEngine(config)


def test_index_all_fails_fast_when_lock_held(tmp_haydar, monkeypatch):
    """A second full index refuses immediately while the lock is held."""
    eng = _engine(monkeypatch)
    from haydar.config import INDEX_LOCK

    fd = os.open(INDEX_LOCK, os.O_RDWR | os.O_CREAT, 0o666)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    try:
        with pytest.raises(RuntimeError, match="already in progress"):
            eng.index_all()
    finally:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
        eng.close()


def test_index_all_releases_lock_on_completion(tmp_haydar, monkeypatch):
    """After a run finishes the lock is free for the next acquirer."""
    eng = _engine(monkeypatch)
    eng.index_all()  # empty folders -> returns quickly, must release the lock

    from haydar.config import INDEX_LOCK

    fd = os.open(INDEX_LOCK, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        # Succeeds only if index_all released it.
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)
    eng.close()


def test_watcher_write_paths_acquire_the_shared_lock(tmp_haydar, monkeypatch, tmp_path):
    """index_file and remove_file both go through the blocking index lock.

    This is the fix for the T1b-1 gap: previously only index_all was guarded,
    so the watcher's per-file upserts could race a reindex.
    """
    eng = _engine(monkeypatch)

    calls = []
    real = engine_mod.IndexingEngine._acquire_index_lock

    @contextmanager
    def spy(self, *, blocking):
        calls.append(blocking)
        with real(self, blocking=blocking):
            yield

    monkeypatch.setattr(engine_mod.IndexingEngine, "_acquire_index_lock", spy)

    f = tmp_path / "note.txt"
    f.write_text("word " * 50, encoding="utf-8")

    assert eng.index_file(f) is True
    eng.remove_file(f)

    # Both single-file write paths serialise via the blocking lock.
    assert calls == [True, True]
    eng.close()

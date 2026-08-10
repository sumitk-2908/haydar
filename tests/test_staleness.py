import os
from unittest.mock import MagicMock

from haydar.config import HaydarConfig
from haydar.indexer.cache import FileCache
from haydar.indexer.engine import IndexingEngine


def _config(folder):
    config = HaydarConfig(folders=[str(folder)], initialized=True)
    config.excluded_patterns = []
    return config


def test_estimate_returns_zero_when_all_cached(tmp_haydar, tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    files = [corpus / f"file-{index}.txt" for index in range(5)]
    for filepath in files:
        filepath.write_text("cached content", encoding="utf-8")

    config = _config(corpus)
    engine = IndexingEngine(config)
    engine._store = MagicMock()
    engine.index_all()

    assert IndexingEngine.estimate_unindexed_count([str(corpus)], config) == 0
    engine.close()


def test_estimate_returns_nonzero_for_new_files(tmp_haydar, tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for index in range(5):
        (corpus / f"file-{index}.txt").write_text("cached content", encoding="utf-8")

    config = _config(corpus)
    engine = IndexingEngine(config)
    engine._store = MagicMock()
    engine.index_all()

    new_file = corpus / "new.txt"
    new_file.write_text("new content", encoding="utf-8")
    future_mtime = new_file.stat().st_mtime + 60
    new_file.touch()
    os.utime(new_file, (future_mtime, future_mtime))

    assert IndexingEngine.estimate_unindexed_count([str(corpus)], config) > 0
    engine.close()


def test_estimate_skips_excluded_and_unsupported_files(tmp_haydar, tmp_path):
    config = HaydarConfig(folders=[str(tmp_path)], initialized=True)
    cache = FileCache()
    cache.set(str(tmp_path / "cached.txt"), 100.0, 1, "hash", 1)
    (tmp_path / "cached.txt").write_text("content", encoding="utf-8")
    (tmp_path / "new.bin").write_bytes(b"content")
    excluded = tmp_path / "node_modules"
    excluded.mkdir()
    (excluded / "new.txt").write_text("content", encoding="utf-8")

    assert IndexingEngine.estimate_unindexed_count([str(tmp_path)], config) == 0
    cache.close()

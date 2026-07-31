import threading
from unittest.mock import patch

import pytest

from haydar.config import HaydarConfig, HaydarConfigError
from haydar.search.hybrid import HybridSearch
from haydar.search.store import VectorStoreError


def test_keyword_search_without_rg_friendly_error(caplog):
    config = HaydarConfig()
    hybrid = HybridSearch(config)

    with patch("haydar.config.get_rg_path", side_effect=HaydarConfigError("rg.exe not found")):
        results = list(hybrid._stream_ripgrep("query", limit=10, cancel_event=None, worker=None))
        assert len(results) == 0

    assert "rg.exe not found" in caplog.text

def test_dedup_by_file():
    config = HaydarConfig()
    hybrid = HybridSearch(config)

    semantic = [
        {"id": "chunk1", "distance": 0.2, "metadata": {"file_path": "test.txt", "start_char": 0, "end_char": 5}},
        {"id": "chunk2", "distance": 0.1, "metadata": {"file_path": "test.txt", "start_char": 10, "end_char": 15}},
    ]
    keyword = [
        {"id": "chunk1", "distance": 0.0, "metadata": {"file_path": "test.txt", "start_char": 0, "end_char": 5}}
    ]

    results = hybrid._merge_and_format(keyword_results=keyword, semantic_results=semantic, query="test", limit=10)

    # Should be deduped to 1 result per file
    assert len(results) == 1
    assert results[0].file_path == "test.txt"
    # Score is the best per-file match. chunk1: semantic distance 0.2 -> 0.8,
    # keyword-boosted x1.1 -> 0.88. chunk2: semantic distance 0.1 -> 0.9 (no keyword hit).
    # best_per_file keeps chunk2 at 0.9.
    assert results[0].score == pytest.approx(0.9)

def test_snippet_centering():
    doc = "A "*20 + "TARGET" + " B"*20
    snippet = HybridSearch._extract_snippet(doc, "TARGET", max_length=15)

    assert "TARGET" in snippet
    assert len(snippet) <= 25 # 15 + '...' padding


def test_semantic_search_surfaces_store_error(tmp_haydar, caplog, monkeypatch):
    config = HaydarConfig()
    hybrid = HybridSearch(config)

    monkeypatch.setattr(HybridSearch, "store", property(lambda self: (_ for _ in ()).throw(VectorStoreError("boom"))))

    results = list(hybrid.search_stream("query", mode="semantic"))
    assert results == [[]]
    assert "boom" in caplog.text


def test_stream_ripgrep_process_killed_on_cancel(tmp_haydar):
    config = HaydarConfig()
    config.folders = [str(tmp_haydar)]
    hybrid = HybridSearch(config)
    cancel_event = threading.Event()
    cancel_event.set()

    gen = hybrid._stream_ripgrep("query", limit=10, cancel_event=cancel_event, worker=None)
    results = list(gen)

    assert results == []

    assert not hasattr(hybrid, "_rg_process")


def test_merge_and_format(tmp_haydar):
    config = HaydarConfig()
    hybrid = HybridSearch(config)

    keyword_results = [
        {"id": "doc1", "distance": 0.2, "metadata": {"file_path": "a.txt"}},
        {"id": "doc2", "distance": None, "metadata": {"file_path": "b.txt", "start_char": 0, "end_char": 5}, "document": "hello world"}
    ]
    semantic_results = [
        {"id": "doc1", "distance": 0.1, "metadata": {"file_path": "a.txt"}},
        {"id": "doc3", "distance": 0.5, "metadata": {"file_path": "c.txt"}}
    ]

    results = hybrid._merge_and_format(keyword_results, semantic_results, "hello", 10)
    assert len(results) == 3
    files = {r.file_path for r in results}
    assert files == {"a.txt", "b.txt", "c.txt"}


def test_extract_snippet(tmp_haydar):
    config = HaydarConfig()
    hybrid = HybridSearch(config)

    snippet = hybrid._extract_snippet("hello beautiful world, this is a test.", "beautiful test")
    assert "beautiful" in snippet

    # Empty doc
    assert hybrid._extract_snippet("", "query") == ""

    # Word not found
    assert "not_found" not in hybrid._extract_snippet("hello world", "not_found")


def test_merge_and_format_edge_cases():
    config = HaydarConfig()
    hybrid = HybridSearch(config)

    keyword = [{"distance": 0.5}, {"id": "x"}]
    semantic = [{"distance": None}, {"id": "y", "metadata": {"file_path": "a.txt"}}]

    results = hybrid._merge_and_format(keyword, semantic, "q", 10)
    assert len(results) == 1
    assert results[0].file_path == "a.txt"


def test_extract_snippet_from_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("A" * 50 + "HELLO" + "B" * 50)
    snippet = HybridSearch._extract_snippet("ignore", "HELLO", max_length=10, file_path=str(f), start_char=50, end_char=55)
    assert "HELLO" in snippet

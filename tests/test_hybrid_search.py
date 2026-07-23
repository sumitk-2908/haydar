from unittest.mock import patch

import pytest

from haydar.config import HaydarConfig, HaydarConfigError
from haydar.search.hybrid import HybridSearch


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

from pathlib import Path

from haydar.indexer.engine import IndexingEngine


def test_chunk_text_empty():
    assert IndexingEngine._chunk_text("", 100, 10) == []
    assert IndexingEngine._chunk_text("   \n  ", 100, 10) == []


def test_chunk_text_single_chunk():
    text = "word " * 30
    chunks = IndexingEngine._chunk_text(text, chunk_size=100, overlap=10)
    assert len(chunks) == 1
    assert chunks[0]["start_char"] == 0
    # Extracted substring matches the recorded offsets.
    assert chunks[0]["text"] == text[chunks[0]["start_char"]:chunks[0]["end_char"]]


def test_chunk_text_overlap_and_offsets():
    words = [f"w{i}" for i in range(250)]
    text = " ".join(words)
    chunks = IndexingEngine._chunk_text(text, chunk_size=100, overlap=20)
    # 250 words, step = 80 -> chunks starting at 0, 80, 160, 240(tail<20 dropped)
    assert len(chunks) >= 2
    for c in chunks:
        assert c["start_char"] < c["end_char"]
        assert text[c["start_char"]:c["end_char"]] == c["text"]


def test_compute_hash_changes_with_content(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("hello world", encoding="utf-8")
    b.write_text("hello worlD", encoding="utf-8")
    ha = IndexingEngine._compute_hash(a)
    hb = IndexingEngine._compute_hash(b)
    assert ha != hb
    # Stable for identical content.
    assert ha == IndexingEngine._compute_hash(a)


def test_engine_lazy_store_initialization(tmp_path):
    from unittest.mock import patch
    from haydar.config import HaydarConfig
    
    config = HaydarConfig()
    config.folders = [str(tmp_path)]
    
    with patch("haydar.indexer.engine.VectorStore") as mock_store:
        engine = IndexingEngine(config, allow_download=False)
        # VectorStore should not be instantiated yet
        mock_store.assert_not_called()
        
        # Accessing .store should instantiate it exactly once
        store1 = engine.store
        mock_store.assert_called_once_with(config, allow_download=False)
        
        # Accessing it again should return the same cached object
        store2 = engine.store
        assert store1 is store2
        assert mock_store.call_count == 1
        
        engine.close()


def test_engine_missing_model_deferred_error(tmp_path):
    from unittest.mock import patch
    import pytest
    from haydar.config import HaydarConfig
    from haydar.search.store import VectorStoreError
    
    config = HaydarConfig()
    config.folders = [str(tmp_path)]
    
    with patch("haydar.indexer.engine.VectorStore", side_effect=VectorStoreError("Missing model")):
        # Should not raise during init
        engine = IndexingEngine(config, allow_download=False)
        
        # Should raise on first access
        with pytest.raises(VectorStoreError, match="Missing model"):
            _ = engine.store
            
        engine.close()

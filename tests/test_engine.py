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

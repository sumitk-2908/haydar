from hypothesis import given, settings
from hypothesis.strategies import integers, text

from haydar.indexer.engine import IndexingEngine

_chunk_text = IndexingEngine._chunk_text

@settings(max_examples=200)
@given(content=text(), chunk_size=integers(min_value=10, max_value=500), overlap=integers(min_value=0, max_value=49))
def test_chunk_offsets_always_valid(content, chunk_size, overlap):
    chunks = _chunk_text(content, chunk_size=chunk_size, overlap=overlap)
    for chunk in chunks:
        assert chunk["start_char"] < chunk["end_char"]
        assert content[chunk["start_char"]:chunk["end_char"]] == chunk["text"]
    for i in range(len(chunks) - 1):
        assert chunks[i]["start_char"] != chunks[i+1]["start_char"]

@settings(max_examples=200)
@given(content=text(min_size=1).filter(lambda s: bool(s.split())))
def test_chunk_covers_all_words(content):
    chunks = _chunk_text(content, chunk_size=500, overlap=50)
    covered = " ".join(c["text"] for c in chunks)
    for token in content.split():
        assert token in covered

@settings(max_examples=200)
@given(content=text(alphabet=" \t\n\r"))
def test_empty_and_whitespace_returns_empty_list(content):
    assert _chunk_text(content, chunk_size=500, overlap=50) == []

@settings(max_examples=200)
@given(content=text(min_size=1).filter(lambda s: len(s.split()) == 1))
def test_single_word_text_produces_one_chunk(content):
    assert len(_chunk_text(content, chunk_size=500, overlap=50)) == 1

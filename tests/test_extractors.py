from pathlib import Path
from haydar.indexer.extractors import extract_text

def test_extract_text_plain(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("Hello, world!", encoding="utf-8")
    
    result = extract_text(f)
    assert result is not None
    assert "Hello, world!" in result.text

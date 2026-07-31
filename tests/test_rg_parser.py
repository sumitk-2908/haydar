import json

from hypothesis import given, settings
from hypothesis.strategies import text

from haydar.search.hybrid import SearchResult, _parse_rg_line


def test_parse_valid_match_line():
    line = json.dumps({
        "type": "match",
        "data": {
            "path": {"text": "/home/user/doc.txt"},
            "lines": {"text": "hello world"},
            "line_number": 1,
            "absolute_offset": 0,
            "submatches": []
        }
    })
    result = _parse_rg_line(line)
    assert isinstance(result, SearchResult)
    assert result.file_path == "/home/user/doc.txt"
    assert result.snippet == "hello world"
    assert result.score == 1.0


def test_parse_non_match_type_returns_none():
    line = json.dumps({"type": "begin", "data": {}})
    assert _parse_rg_line(line) is None


def test_parse_malformed_json_returns_none():
    assert _parse_rg_line("not json {") is None


def test_parse_missing_keys_returns_none():
    line = json.dumps({"type": "match", "data": {}})
    assert _parse_rg_line(line) is None


@given(line=text())
@settings(max_examples=500)
def test_parse_rg_line_never_raises(line):
    result = _parse_rg_line(line)
    assert result is None or isinstance(result, SearchResult)

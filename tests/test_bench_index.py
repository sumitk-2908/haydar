from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "bench_index.py"
SPEC = importlib.util.spec_from_file_location("bench_index", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
bench_index = importlib.util.module_from_spec(SPEC)
sys.modules["bench_index"] = bench_index
SPEC.loader.exec_module(bench_index)

_PATH_NAMES = (
    "HAYDAR_DIR",
    "CONFIG_PATH",
    "DB_DIR",
    "LOG_DIR",
    "MODELS_DIR",
    "CACHE_DIR",
    "RIPGREP_DIR",
    "INDEX_LOCK",
)


def _restore_haydar_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo ``configure_data_dir``'s module-level rebinding after the test."""
    import haydar.config as config_module
    import haydar.indexer.cache as cache_module
    import haydar.indexer.extractors as extractors_module
    import haydar.search.store as store_module

    for name in _PATH_NAMES:
        monkeypatch.setattr(config_module, name, getattr(config_module, name))
    monkeypatch.setattr(cache_module, "DB_DIR", cache_module.DB_DIR)
    monkeypatch.setattr(extractors_module, "CACHE_DIR", extractors_module.CACHE_DIR)
    monkeypatch.setattr(store_module, "DB_DIR", store_module.DB_DIR)
    monkeypatch.setattr(store_module, "MODELS_DIR", store_module.MODELS_DIR)


def test_percentile_interpolates_and_rejects_empty():
    assert bench_index.percentile([10.0, 20.0, 30.0], 50) == 20.0
    assert bench_index.percentile([10.0, 20.0], 95) == pytest.approx(19.5)
    with pytest.raises(ValueError):
        bench_index.percentile([], 95)


def test_generate_corpus_is_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    bench_index.generate_corpus(first, files=3, seed=7)
    bench_index.generate_corpus(second, files=3, seed=7)

    assert [path.read_bytes() for path in sorted(first.iterdir())] == [
        path.read_bytes() for path in sorted(second.iterdir())
    ]
    assert bench_index.corpus_stats(first) == bench_index.corpus_stats(second)


def test_load_queries_uses_defaults_and_rejects_empty(tmp_path):
    assert bench_index.load_queries(None) == list(bench_index.DEFAULT_QUERIES)

    query_file = tmp_path / "queries.txt"
    query_file.write_text("first query\n\n second query \n", encoding="utf-8")
    assert bench_index.load_queries(query_file) == ["first query", "second query"]

    query_file.write_text("\n  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        bench_index.load_queries(query_file)


def test_validate_args_requires_isolated_data_dir_for_real_mode():
    parser = bench_index.build_parser()
    args = argparse.Namespace(
        files=10,
        repeat=1,
        embedding_batch_size=100,
        corpus=None,
        queries=None,
        store="real",
        data_dir=None,
    )

    with pytest.raises(SystemExit):
        bench_index.validate_args(args, parser)


def test_mocked_main_writes_machine_readable_result(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "one.txt").write_text("word " * 20, encoding="utf-8")
    output = tmp_path / "result.json"

    # ``bench_index.main`` calls ``configure_data_dir``, which rebinds the
    # storage constants with bare ``setattr`` — deliberately, since the harness
    # is a standalone script. Inside pytest that mutation would outlive this test
    # and leave every later test pointing at a deleted temp directory, so the
    # originals are restored on teardown.
    _restore_haydar_paths(monkeypatch)

    monkeypatch.setattr(bench_index, "HAS_PSUTIL", False)
    exit_code = bench_index.main(
        [
            "--store",
            "mocked",
            "--corpus",
            str(corpus),
            "--repeat",
            "1",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = __import__("json").loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "mocked"
    assert payload["corpus"]["files"] == 1
    assert payload["runs"][0]["files_indexed"] == 1
    assert payload["runs"][0]["cold_search_ms"] is None

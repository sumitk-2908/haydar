import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from haydar.config import HaydarConfig, is_excluded
from haydar.indexer.engine import IndexingEngine


def _assert_directory_pruned(tmp_haydar, excluded_name: str, *, custom: bool = False):
    """Assert an excluded subtree is absent while its sibling is indexed."""
    corpus = tmp_haydar / f"corpus-{excluded_name.strip('.')}"
    excluded = corpus / excluded_name
    included = corpus / "documents"
    excluded.mkdir(parents=True)
    included.mkdir(parents=True)
    excluded_file = excluded / "secret.txt"
    included_file = included / "visible.txt"
    excluded_file.write_text("must not be indexed", encoding="utf-8")
    included_file.write_text("positive control content", encoding="utf-8")

    config = HaydarConfig(initialized=True, folders=[str(corpus)])
    if custom:
        config.excluded_patterns.append(excluded_name)

    # Evaluate paths relative to the configured corpus. This avoids Windows'
    # pytest temp ancestor (AppData/Temp) becoming an unrelated exclusion.
    def corpus_is_excluded(path, patterns):
        return is_excluded(Path(path).relative_to(corpus), patterns)

    submitted_metadata = []

    def capture_documents(**kwargs):
        submitted_metadata.extend(dict(item) for item in kwargs["metadatas"])

    with (
        patch("haydar.indexer.engine.is_excluded", side_effect=corpus_is_excluded),
        patch("haydar.indexer.engine.VectorStore") as store_cls,
    ):
        store = store_cls.return_value
        store.add_documents.side_effect = capture_documents
        with IndexingEngine(config, allow_download=False) as engine:
            stats = engine.index_all()

    assert stats["files_indexed"] == 1
    store.add_documents.assert_called_once()
    metadata = submitted_metadata
    submitted_paths = {
        item.get("file_path") or item.get("filepath")
        for item in metadata
    }
    assert str(included_file.absolute()) in submitted_paths
    assert str(excluded_file.absolute()) not in submitted_paths


def test_node_modules_not_crawled(tmp_haydar):
    _assert_directory_pruned(tmp_haydar, "node_modules")


def test_appdata_not_crawled(tmp_haydar):
    _assert_directory_pruned(tmp_haydar, "AppData")


def test_git_dir_not_crawled(tmp_haydar):
    _assert_directory_pruned(tmp_haydar, ".git")


def test_custom_exclusion_pattern_respected(tmp_haydar):
    _assert_directory_pruned(tmp_haydar, "scratch", custom=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_unc_path_root_exclusion_skipped_gracefully():
    assert is_excluded(Path("//server/share/file.txt"), []) is False

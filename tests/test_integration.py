from unittest.mock import MagicMock, patch

import pytest

from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexingEngine


@pytest.fixture
def temp_tree(tmp_path):
    d = tmp_path / "tree"
    d.mkdir()
    (d / "file1.txt").write_text("Hello world this is a test file for integration. " * 5)
    (d / "file2.txt").write_text("Another file with different content for testing. " * 5)
    return d

@patch('haydar.indexer.engine.VectorStore')
def test_integration_index_and_invalidate(MockStore, tmp_haydar, temp_tree):
    mock_store_instance = MagicMock()
    MockStore.return_value = mock_store_instance

    config = HaydarConfig()
    config.folders = [str(temp_tree)]
    # pytest's tmp_path lives under ...\AppData\Local\Temp\..., and both "appdata"
    # and "temp" are default excluded patterns that would prune the whole tree
    # during the crawl. Clear exclusions so the temp tree is actually indexed.
    config.excluded_patterns = []

    engine = IndexingEngine(config, allow_download=False)

    # 1. First run
    engine.index_all()

    # Assert chunks landed in the store
    first_add_count = mock_store_instance.add_documents.call_count
    assert first_add_count > 0

    # 2. Second run, should skip unchanged files
    mock_store_instance.add_documents.reset_mock()
    engine.index_all()
    assert mock_store_instance.add_documents.call_count == 0

    # 3. Delete a file and run invalidation pass
    (temp_tree / "file1.txt").unlink()

    mock_store_instance.delete_by_filepaths.reset_mock()
    engine.index_all()

    # Should remove the deleted file from the store (invalidation pass)
    removed = [
        p
        for call in mock_store_instance.delete_by_filepaths.call_args_list
        for p in call.args[0]
    ]
    assert any("file1.txt" in p for p in removed)

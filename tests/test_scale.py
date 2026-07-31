from unittest.mock import patch

import pytest

from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexingEngine


@pytest.mark.slow
def test_index_5k_files_no_exception(tmp_haydar):
    config = HaydarConfig(initialized=True)
    config.excluded_patterns = []
    temp_dir = tmp_haydar / "project"
    temp_dir.mkdir()
    config.folders = [str(temp_dir)]

    # Generate 5000 files
    for i in range(5000):
        file_path = temp_dir / f"file_{i}.txt"
        file_path.write_text("word " * 20, encoding="utf-8")

    with patch("haydar.indexer.engine.VectorStore") as mock_store_cls:
        mock_store = mock_store_cls.return_value

        engine = IndexingEngine(config, allow_download=False)
        stats = engine.index_all()

        assert stats["files_indexed"] == 5000
        assert stats.get("files_skipped_error", 0) == 0
        batches = [call.kwargs["ids"] for call in mock_store.add_documents.call_args_list]
        assert batches
        assert max(map(len, batches)) <= config.embedding_batch_size

@pytest.mark.slow
def test_incremental_reindex_skips_unchanged(tmp_haydar):
    config = HaydarConfig(initialized=True)
    config.excluded_patterns = []
    temp_dir = tmp_haydar / "project"
    temp_dir.mkdir()
    config.folders = [str(temp_dir)]

    for i in range(5000):
        file_path = temp_dir / f"file_{i}.txt"
        file_path.write_text("word " * 20, encoding="utf-8")

    with patch("haydar.indexer.engine.VectorStore") as mock_store_cls:
        mock_store = mock_store_cls.return_value

        engine = IndexingEngine(config, allow_download=False)
        engine.index_all()  # first run

        mock_store.add_documents.reset_mock()
        engine.index_all()  # second run

        mock_store.add_documents.assert_not_called()

@pytest.mark.slow
def test_deleted_file_invalidation_at_scale(tmp_haydar):
    config = HaydarConfig(initialized=True)
    config.excluded_patterns = []
    temp_dir = tmp_haydar / "project"
    temp_dir.mkdir()
    config.folders = [str(temp_dir)]

    for i in range(5000):
        file_path = temp_dir / f"file_{i}.txt"
        file_path.write_text("word " * 20, encoding="utf-8")

    with patch("haydar.indexer.engine.VectorStore") as mock_store_cls:
        mock_store = mock_store_cls.return_value

        engine = IndexingEngine(config, allow_download=False)
        engine.index_all()

        deleted_paths = []
        for i in range(500):
            file_path = temp_dir / f"file_{i}.txt"
            file_path.unlink()
            deleted_paths.append(str(file_path))

        mock_store.delete_by_filepaths.reset_mock()
        engine.index_all()

        mock_store.delete_by_filepaths.assert_called_once()
        args, _ = mock_store.delete_by_filepaths.call_args
        called_paths = args[0]
        assert set(called_paths) == set(deleted_paths)

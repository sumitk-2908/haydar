from unittest.mock import MagicMock, patch

from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexingEngine


def _config(folder):
    config = HaydarConfig(folders=[str(folder)], initialized=True)
    config.excluded_patterns = []
    return config


def test_progress_callback_called_for_each_file(tmp_haydar, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for index in range(10):
        (corpus / f"file-{index}.txt").write_text(f"content {index}")

    calls = []
    with patch("haydar.indexer.engine.VectorStore") as mock_store:
        mock_store.return_value = MagicMock()
        engine = IndexingEngine(_config(corpus), allow_download=False)
        try:
            engine.index_all(progress_callback=lambda completed, total: calls.append((completed, total)))
        finally:
            engine.close()

    assert len(calls) == 10
    assert calls[-1] == (10, 10)


def test_progress_callback_called_for_skipped_files(tmp_haydar, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for index in range(5):
        (corpus / f"file-{index}.txt").write_text(f"content {index}")

    with patch("haydar.indexer.engine.VectorStore") as mock_store:
        mock_store.return_value = MagicMock()
        engine = IndexingEngine(_config(corpus), allow_download=False)
        try:
            engine.index_all()
            assert all(
                engine.cache.get(str(path.absolute())) is not None
                for path in corpus.glob("*.txt")
            )
            calls = []
            engine.index_all(
                progress_callback=lambda completed, total: calls.append((completed, total))
            )
        finally:
            engine.close()

    assert len(calls) == 5
    assert calls[-1][0] == calls[-1][1]

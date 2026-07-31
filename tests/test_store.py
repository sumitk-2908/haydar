from unittest.mock import MagicMock

import pytest

from haydar.config import HaydarConfig
from haydar.search.store import VectorStore, VectorStoreError, _model_cache_name


def _cache_model(tmp_haydar, config: HaydarConfig) -> None:
    snapshot = (
        tmp_haydar
        / "models"
        / _model_cache_name(config.embedding_model)
        / "snapshots"
        / "test-revision"
    )
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")


def test_store_raises_when_model_missing(tmp_haydar):
    """Regression: VectorStore must raise VectorStoreError (not sys.exit) when
    the embedding model is absent, so callers can handle it gracefully."""
    config = HaydarConfig()
    with pytest.raises(VectorStoreError) as excinfo:
        VectorStore(config, allow_download=False)
    # Friendly hint is attached for the CLI/UI to surface.
    assert excinfo.value.hint
    assert "init" in excinfo.value.hint.lower()


def test_store_raises_on_corrupt_db(tmp_haydar):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    db_dir = tmp_haydar / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "chroma.sqlite3").write_bytes(b"not a sqlite database")
    config = HaydarConfig()
    with pytest.raises(VectorStoreError) as exc:
        VectorStore(config, allow_download=False)
    assert "reindex" in exc.value.hint.lower()


def test_add_documents_empty_list_is_noop(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    store.add_documents([], [], [])
    assert mock_collection.upsert.call_count == 0


def test_delete_by_filepaths_empty_list_is_noop(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    store.delete_by_filepaths([])
    assert mock_collection.delete.call_count == 0


def test_get_stats_empty_collection(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()
    mock_collection.count.return_value = 0
    mock_collection.get.return_value = {"metadatas": []}

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    stats = store.get_stats()
    assert stats == {"files_indexed": 0, "chunks_stored": 0, "db_size_bytes": 0}


def test_delete_by_filepath(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()
    mock_collection.get.return_value = {"ids": ["1", "2"]}

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    store.delete_by_filepath("test.txt")
    mock_collection.delete.assert_called_once_with(ids=["1", "2"])


def test_query(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()
    mock_collection.query.return_value = {
        "ids": [["1"]],
        "documents": [["doc"]],
        "metadatas": [[{"file_path": "a"}]],
        "distances": [[0.1]]
    }

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    res = store.query("test")
    assert res == [{"id": "1", "document": "doc", "metadata": {"file_path": "a"}, "distance": 0.1}]


def test_clear(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_client = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=mock_client))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    store.clear()
    mock_client.delete_collection.assert_called_with("haydar_files")
    assert mock_client.get_or_create_collection.call_count == 2


def test_get_all_file_paths(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()
    mock_collection.get.return_value = {"metadatas": [{"file_path": "a.txt"}, {"file_path": "b.txt"}]}

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    assert store.get_all_file_paths() == {"a.txt", "b.txt"}


def test_get_file_hash(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()
    mock_collection.get.return_value = {"metadatas": [{"file_hash": "hash123"}]}

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    assert store.get_file_hash("test") == "hash123"


def test_embedding_function_error(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock())
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock(side_effect=Exception("network error")))
        with pytest.raises(VectorStoreError) as exc:
            VectorStore(config, allow_download=False)
        assert "could not be loaded" in str(exc.value)
        assert "network" not in str(exc.value).lower()


def test_collection_creation_error(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_client = MagicMock()
    mock_client.get_or_create_collection.side_effect = Exception("db lock")

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=mock_client))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        with pytest.raises(VectorStoreError) as exc:
            VectorStore(config, allow_download=False)
        assert "currently in use" in str(exc.value)
        assert "db lock" not in str(exc.value)


def test_delete_by_filepaths_happy_path(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()
    mock_collection.get.return_value = {"ids": ["1", "2"]}

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    store.delete_by_filepaths(["test.txt", "test2.txt"])
    mock_collection.delete.assert_called_once_with(ids=["1", "2"])


def test_query_with_filter(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()
    mock_collection.query.return_value = {
        "ids": [["1"]],
        "documents": [["doc"]],
        "metadatas": [[{"file_path": "a"}]],
        "distances": [[0.1]]
    }

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    res = store.query_with_filter("test", where_document={"$contains": "foo"})
    assert res == [{"id": "1", "document": "doc", "metadata": {"file_path": "a"}, "distance": 0.1}]


def test_query_empty_results(tmp_haydar, monkeypatch):
    config = HaydarConfig()
    _cache_model(tmp_haydar, config)
    mock_collection = MagicMock()
    mock_collection.query.return_value = {}

    with monkeypatch.context() as m:
        m.setattr("chromadb.PersistentClient", MagicMock(return_value=MagicMock(get_or_create_collection=MagicMock(return_value=mock_collection))))
        m.setattr("haydar.search.store.SentenceTransformerEmbeddingFunction", MagicMock())
        store = VectorStore(config, allow_download=False)

    assert store.query("test") == []



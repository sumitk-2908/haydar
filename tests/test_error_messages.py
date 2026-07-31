from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haydar.config import HaydarConfig, HaydarConfigError, get_rg_path
from haydar.search.store import VectorStore, VectorStoreError


def test_haydar_config_error_contract():
    exc = HaydarConfigError("The message.", hint="The hint.")
    assert str(exc) == "The message."
    assert exc.hint == "The hint."

def test_vector_store_error_contract():
    exc = VectorStoreError("The message.", hint="The hint.")
    assert str(exc) == "The message."
    assert exc.hint == "The hint."

def test_get_rg_path_error(monkeypatch):
    monkeypatch.setattr("haydar.config.RIPGREP_DIR", Path("/nonexistent/dir"))
    monkeypatch.setattr("sys._MEIPASS", "/nonexistent", raising=False)

    # Mock development path check
    with (
        patch("pathlib.Path.exists", return_value=False),
        pytest.raises(HaydarConfigError) as exc_info,
    ):
        get_rg_path()

    assert "could not be found" in str(exc_info.value)
    assert exc_info.value.hint is not None
    assert "haydar-cli.exe init" in exc_info.value.hint

def test_vector_store_missing_model_error(tmp_path):
    config = HaydarConfig()
    with (
        pytest.raises(VectorStoreError) as exc_info,
        patch("haydar.search.store.MODELS_DIR", tmp_path),
    ):
        VectorStore(config, allow_download=False)

    assert "embedding model" in str(exc_info.value)
    assert "not found locally" in str(exc_info.value)
    assert exc_info.value.hint is not None
    assert "haydar-cli.exe init" in exc_info.value.hint

def test_vector_store_database_corruption_error(tmp_path):
    config = HaydarConfig()
    snapshot = tmp_path / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "rev"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    with (
        pytest.raises(VectorStoreError) as exc_info,
        patch("chromadb.PersistentClient", side_effect=Exception("mock corruption")),
        patch("haydar.search.store.MODELS_DIR", tmp_path),
    ):
        VectorStore(config, allow_download=False)

    assert "is corrupt" in str(exc_info.value)
    assert exc_info.value.hint is not None
    assert "haydar-cli.exe reindex" in exc_info.value.hint
    # Check chaining
    assert isinstance(exc_info.value.__cause__, Exception)
    assert str(exc_info.value.__cause__) == "mock corruption"

def test_vector_store_embedding_model_error(tmp_path):
    config = HaydarConfig()
    snapshot = tmp_path / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "rev"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    with (
        pytest.raises(VectorStoreError) as exc_info,
        patch("chromadb.PersistentClient"),
        patch(
            "haydar.search.store.SentenceTransformerEmbeddingFunction",
            side_effect=Exception("mock model load fail"),
        ),
        patch("haydar.search.store.MODELS_DIR", tmp_path),
    ):
        VectorStore(config, allow_download=False)

    assert "could not be loaded" in str(exc_info.value)
    assert exc_info.value.hint is not None
    assert "haydar-cli.exe init" in exc_info.value.hint
    assert isinstance(exc_info.value.__cause__, Exception)

def test_vector_store_collection_init_error(tmp_path):
    config = HaydarConfig()
    snapshot = tmp_path / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "rev"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    with pytest.raises(VectorStoreError) as exc_info:
        mock_client = MagicMock()
        mock_client.get_or_create_collection.side_effect = Exception("mock collection fail")
        with (
            patch("chromadb.PersistentClient", return_value=mock_client),
            patch("haydar.search.store.SentenceTransformerEmbeddingFunction"),
            patch("haydar.search.store.MODELS_DIR", tmp_path),
        ):
            VectorStore(config, allow_download=False)

    assert "collection initialization failed" in str(exc_info.value)
    assert exc_info.value.hint is not None
    assert "full log" in exc_info.value.hint
    assert isinstance(exc_info.value.__cause__, Exception)

def test_cli_fail_appends_log_path(capsys, monkeypatch):
    from pathlib import Path

    import typer

    from haydar.cli import _fail
    from haydar.config import HaydarConfigError

    monkeypatch.setattr("haydar.config.get_log_path", lambda: Path("/mock/haydar.log"))

    exc = HaydarConfigError("Test error.", hint="Test hint.")

    with pytest.raises(typer.Exit):
        _fail(exc)

    captured = capsys.readouterr()
    assert "Test error." in captured.out
    assert "Test hint." in captured.out
    assert "Full log:" in captured.out
    assert "haydar.log" in captured.out

def test_cli_fail_suppresses_duplicate_log_path(capsys, monkeypatch):
    from pathlib import Path

    import typer

    from haydar.cli import _fail
    from haydar.config import HaydarConfigError

    monkeypatch.setattr("haydar.config.get_log_path", lambda: Path("/mock/haydar.log"))

    exc = HaydarConfigError("Test error. Full log: /mock/haydar.log")

    with pytest.raises(typer.Exit):
        _fail(exc)

    captured = capsys.readouterr()
    assert "Test error. Full log: /mock/haydar.log" in captured.out
    assert captured.out.count("Full log:") == 1

def test_ui_on_search_error_appends_log_path(monkeypatch):
    from pathlib import Path

    class DummyWindow:
        def __init__(self):
            class StatusLabel:
                def setText(self, text):
                    self.text = text
                def setAccessibleDescription(self, text):
                    self.acc = text
                def setStyleSheet(self, s):
                    pass
                def show(self):
                    pass
            self.status_label = StatusLabel()
            self.BASE_ERROR_HEIGHT = 160

        def _set_content_height(self, h):
            pass

    monkeypatch.setattr("haydar.config.get_log_path", lambda: Path("/mock/haydar.log"))

    from haydar.ui.window import SearchWindow
    dummy = DummyWindow()
    SearchWindow.on_search_error(dummy, "Search failed.")

    assert "Search failed." in dummy.status_label.text
    assert "Full log:" in dummy.status_label.text
    assert "haydar.log" in dummy.status_label.text

def test_gui_main_fatal_error_messagebox(monkeypatch):

    from haydar.gui_main import main

    monkeypatch.setattr("haydar.config.HaydarConfig.load", MagicMock(side_effect=Exception("mock config fail")))
    monkeypatch.setattr("haydar.gui_main._enable_windows_dpi_awareness", lambda: None)
    monkeypatch.setattr("haydar.logging_setup.setup_logging", lambda **kw: None)

    from pathlib import Path
    monkeypatch.setattr("haydar.config.get_log_path", lambda: Path("/mock/haydar.log"))

    mock_msgbox = MagicMock()
    import ctypes
    monkeypatch.setattr(ctypes, "windll", MagicMock(), raising=False)
    if hasattr(ctypes, "windll"):
        ctypes.windll.user32.MessageBoxW = mock_msgbox

    with pytest.raises(SystemExit):
        main()

    if hasattr(ctypes, "windll"):
        mock_msgbox.assert_called_once()
        args = mock_msgbox.call_args[0]
        assert "Full log:" in args[1]
        assert "haydar.log" in args[1]


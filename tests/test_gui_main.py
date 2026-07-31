import sys
from unittest.mock import patch

import pytest

from haydar.config import CURRENT_SCHEMA_VERSION, HaydarConfig
from haydar.gui_main import _show_error_dialog, main


@pytest.fixture
def mock_config():
    with patch("haydar.config.HaydarConfig.load") as mock_load:
        config = HaydarConfig(initialized=True, folders=[])
        config.schema_version = CURRENT_SCHEMA_VERSION
        mock_load.return_value = config
        yield config


@pytest.fixture
def mock_show_error():
    with patch("haydar.gui_main._show_error_dialog") as mock_dialog:
        yield mock_dialog


@pytest.fixture
def mock_launch_window():
    with patch("haydar.ui.window.launch_search_window") as mock_launch:
        yield mock_launch


def test_main_uninitialized_shows_dialog(mock_config, mock_show_error, mock_launch_window):
    mock_config.initialized = False
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    mock_show_error.assert_called_once()
    title, msg = mock_show_error.call_args[0]
    assert "Setup Required" in title
    assert "haydar-cli.exe init" in msg
    assert "haydar.log" in msg
    mock_launch_window.assert_not_called()


def test_main_schema_mismatch_shows_dialog(mock_config, mock_show_error, mock_launch_window):
    mock_config.schema_version = CURRENT_SCHEMA_VERSION - 1
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    mock_show_error.assert_called_once()
    title, msg = mock_show_error.call_args[0]
    assert "Update Required" in title
    assert "haydar-cli.exe reindex" in msg
    assert "haydar.log" in msg
    mock_launch_window.assert_not_called()


def test_main_fatal_error_shows_dialog(mock_config, mock_show_error, mock_launch_window):
    mock_launch_window.side_effect = RuntimeError("boom")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    mock_show_error.assert_called_once()
    title, msg = mock_show_error.call_args[0]
    assert "Fatal Error" in title
    assert "haydar.log" in msg


def test_main_successful_launch(mock_config, mock_show_error, mock_launch_window):
    # Should not raise
    main()
    mock_show_error.assert_not_called()
    mock_launch_window.assert_called_once_with(mock_config)


def test_show_error_dialog_failure_fallback(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows only")

    # Mock ctypes.windll.user32.MessageBoxW to raise
    import ctypes
    class MockWindll:
        class user32:
            @staticmethod
            def MessageBoxW(*args, **kwargs):
                raise OSError("No window station")

    monkeypatch.setattr(ctypes, "windll", MockWindll)

    # Should ignore failure and return gracefully
    _show_error_dialog("Title", "Message")

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
def mock_gui_application():
    with patch("haydar.ui.application.run_gui_application") as mock_run:
        yield mock_run


def test_main_uninitialized_enters_gui_setup(
    mock_config, mock_show_error, mock_gui_application
):
    mock_config.initialized = False
    mock_config.search_ready = False

    main()

    mock_show_error.assert_not_called()
    mock_gui_application.assert_called_once_with(mock_config)


def test_main_schema_mismatch_shows_dialog(mock_config, mock_show_error, mock_gui_application):
    mock_config.schema_version = CURRENT_SCHEMA_VERSION - 1
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    mock_show_error.assert_called_once()
    title, msg = mock_show_error.call_args[0]
    assert "Update Required" in title
    # Recovery is in-app: a GUI user is never told to run the expert CLI.
    assert "haydar-cli.exe" not in msg
    assert "Rebuild index" in msg
    assert "haydar.log" in msg
    mock_gui_application.assert_not_called()


def test_main_future_schema_directs_to_the_newer_version(
    mock_config, mock_show_error, mock_gui_application
):
    mock_config.schema_version = CURRENT_SCHEMA_VERSION + 1
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    title, msg = mock_show_error.call_args[0]
    assert "Version Required" in title
    assert "newer Haydar version" in msg
    mock_gui_application.assert_not_called()


def test_main_future_config_format_fails_closed(
    mock_config, mock_show_error, mock_gui_application
):
    """A config from a newer build must not be rewritten by this one."""
    from haydar.config import ConfigFormatError

    with (
        patch(
            "haydar.config.HaydarConfig.load",
            side_effect=ConfigFormatError(
                "newer config", hint="Install the newer version."
            ),
        ),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 1
    mock_show_error.assert_called_once()
    mock_gui_application.assert_not_called()


def test_main_fatal_error_shows_dialog(mock_config, mock_show_error, mock_gui_application):
    mock_gui_application.side_effect = RuntimeError("boom")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    mock_show_error.assert_called_once()
    title, msg = mock_show_error.call_args[0]
    assert "Fatal Error" in title
    assert "haydar.log" in msg


def test_main_successful_launch(mock_config, mock_show_error, mock_gui_application):
    # Should not raise
    main()
    mock_show_error.assert_not_called()
    mock_gui_application.assert_called_once_with(mock_config)


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

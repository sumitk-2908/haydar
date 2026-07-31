from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt

import haydar
from haydar.config import HaydarConfig
from haydar.search.hybrid import SearchResult
from haydar.search.store import VectorStoreError
from haydar.ui.window import SearchWindow


@pytest.fixture(autouse=True)
def disable_update_check(monkeypatch):
    """Keep focused window tests deterministic and free of one-shot Qt threads."""
    monkeypatch.setattr(SearchWindow, "_start_update_check", lambda self: None)


@pytest.fixture
def config():
    return HaydarConfig(folders=[], initialized=True)

def test_window_constructs_without_model(qtbot, tmp_haydar, config, monkeypatch):
    def mock_init(self, config):
        raise VectorStoreError("model missing", hint="run haydar init")

    monkeypatch.setattr("haydar.ui.window.HybridSearch.__init__", mock_init)

    window = SearchWindow(config)
    qtbot.addWidget(window)

    assert window is not None
    assert window.engine_error is not None
    assert "model missing" in window.engine_error
    assert window.isVisible() is False
    window.close()

def test_empty_query_emits_no_search(qtbot, tmp_haydar, config, monkeypatch):
    def mock_init(self, config):
        pass

    monkeypatch.setattr("haydar.ui.window.HybridSearch.__init__", mock_init)
    window = SearchWindow(config)
    qtbot.addWidget(window)

    window.search_input.setText("")

    mock_do_search = MagicMock()
    monkeypatch.setattr(window.search_worker, "do_search", mock_do_search)

    window._trigger_search()

    mock_do_search.assert_not_called()
    window.close()

def test_escape_hides_window(qtbot, tmp_haydar, config, monkeypatch):
    def mock_init(self, config):
        pass
    monkeypatch.setattr("haydar.ui.window.HybridSearch.__init__", mock_init)

    window = SearchWindow(config)
    qtbot.addWidget(window)
    window.show()

    qtbot.keyClick(window, Qt.Key_Escape)

    assert window.isVisible() is False
    window.close()

def test_results_resize_window(qtbot, tmp_haydar, config, monkeypatch):
    def mock_init(self, config):
        pass
    monkeypatch.setattr("haydar.ui.window.HybridSearch.__init__", mock_init)

    window = SearchWindow(config)
    qtbot.addWidget(window)

    results = [SearchResult(
        file_path="dummy.txt",
        filename="dummy.txt",
        folder="",
        file_type=".txt",
        snippet="dummy snippet",
        score=1.0,
        modified_time=0.0
    )]

    window.on_search_results(results)

    assert window.height() == 530
    window.close()

def test_settings_integration(qtbot, tmp_haydar, config, monkeypatch):
    def mock_init(self, config):
        pass
    monkeypatch.setattr("haydar.ui.window.HybridSearch.__init__", mock_init)

    from haydar.ui.settings import SettingsWindow
    window = SearchWindow(config)
    settings_window = SettingsWindow(config)
    window.set_settings_window(settings_window)
    qtbot.addWidget(window)
    qtbot.addWidget(settings_window)

    # Verify the gear button is layout-owned and remains part of the search row.
    assert window.settings_btn.text() == "⚙"
    assert window.settings_btn.parentWidget() is window.container
    search_layout = window.settings_btn.parentWidget().layout().itemAt(1).layout()
    assert search_layout.indexOf(window.settings_btn) >= 0

    # Verify button click shows settings window
    assert not settings_window.isVisible()
    qtbot.mouseClick(window.settings_btn, Qt.LeftButton)
    assert settings_window.isVisible()

    # Hide and verify Ctrl+, shortcut
    settings_window.hide()
    assert not settings_window.isVisible()

    # Simulate Ctrl+,
    # Pyside6 requires passing the exact key modifier
    qtbot.keyClick(window, Qt.Key_Comma, modifier=Qt.ControlModifier)
    assert settings_window.isVisible()

    window.close()
    settings_window.close()

def _disable_search_engine(monkeypatch):
    def unavailable(self, config):
        raise VectorStoreError("search disabled for focused window test")

    monkeypatch.setattr("haydar.ui.window.HybridSearch.__init__", unavailable)


def test_whatsnew_banner_shown_after_update(qtbot, tmp_haydar, monkeypatch):
    config = HaydarConfig(folders=[], initialized=True, last_seen_version="0.1.0")
    monkeypatch.setattr(haydar, "__version__", "0.2.0")
    _disable_search_engine(monkeypatch)

    window = SearchWindow(config)
    qtbot.addWidget(window)
    try:
        window.show()

        assert window._whatsnew_banner is not None
        assert window._whatsnew_banner.isVisible()
        assert window._whatsnew_label.textFormat() == Qt.PlainText
        assert window._see_whatsnew_btn.accessibleName() == "See what's new"
        assert window._dismiss_whatsnew_btn.objectName() == "dismissWhatsNewButton"
    finally:
        window.close()


def test_first_install_save_failure_restores_empty_version(
    qtbot, tmp_haydar, monkeypatch
):
    config = HaydarConfig(folders=[], initialized=True, last_seen_version="")
    monkeypatch.setattr(config, "save", MagicMock(side_effect=OSError("read only")))
    _disable_search_engine(monkeypatch)

    window = SearchWindow(config)
    qtbot.addWidget(window)
    try:
        assert window._whatsnew_banner is None
        assert config.last_seen_version == ""
    finally:
        window.close()


def test_whatsnew_banner_not_shown_on_first_install(qtbot, tmp_haydar, monkeypatch):
    config = HaydarConfig(folders=[], initialized=True, last_seen_version="")
    _disable_search_engine(monkeypatch)

    window = SearchWindow(config)
    qtbot.addWidget(window)
    try:
        assert window._whatsnew_banner is None
        assert window.config.last_seen_version == haydar.__version__
    finally:
        window.close()


def test_whatsnew_banner_launch_and_dismiss_preserve_base_height(
    qtbot, tmp_haydar, monkeypatch
):
    config = HaydarConfig(folders=[], initialized=True, last_seen_version="0.1.0")
    monkeypatch.setattr(haydar, "__version__", "0.2.0")
    _disable_search_engine(monkeypatch)

    window = SearchWindow(config)
    qtbot.addWidget(window)
    try:
        assert window.height() == SearchWindow.BASE_EMPTY_HEIGHT + 40

        window.toggle()
        assert window.height() == SearchWindow.BASE_EMPTY_HEIGHT + 40
        qtbot.mouseClick(window._dismiss_whatsnew_btn, Qt.LeftButton)

        assert window.height() == SearchWindow.BASE_EMPTY_HEIGHT
        assert window._whatsnew_banner is not None
        assert not window._whatsnew_banner.isVisible()
        assert HaydarConfig.load().last_seen_version == "0.2.0"
    finally:
        window.close()


def test_see_whats_new_opens_target_then_marks_seen(qtbot, tmp_haydar, monkeypatch):
    config = HaydarConfig(folders=[], initialized=True, last_seen_version="0.1.0")
    monkeypatch.setattr(haydar, "__version__", "0.2.0")
    _disable_search_engine(monkeypatch)
    settings = MagicMock()

    window = SearchWindow(config)
    window.set_settings_window(settings)
    qtbot.addWidget(window)
    try:
        qtbot.mouseClick(window._see_whatsnew_btn, Qt.LeftButton)

        settings.show_whats_new.assert_called_once_with()
        assert config.last_seen_version == "0.2.0"
        assert window._whatsnew_banner is not None
        assert window._whatsnew_banner.isHidden()
    finally:
        window.close()


def test_see_whats_new_navigation_failure_does_not_mark_seen(
    qtbot, tmp_haydar, monkeypatch
):
    config = HaydarConfig(folders=[], initialized=True, last_seen_version="0.1.0")
    monkeypatch.setattr(haydar, "__version__", "0.2.0")
    _disable_search_engine(monkeypatch)
    settings = MagicMock()
    settings.show_whats_new.side_effect = RuntimeError("navigation failed")

    window = SearchWindow(config)
    window.set_settings_window(settings)
    qtbot.addWidget(window)
    try:
        qtbot.mouseClick(window._see_whatsnew_btn, Qt.LeftButton)

        assert config.last_seen_version == "0.1.0"
        assert window._whatsnew_banner is not None
        assert not window._whatsnew_banner.isHidden()
    finally:
        window.close()


def test_see_whats_new_without_settings_does_not_mark_seen(
    qtbot, tmp_haydar, monkeypatch
):
    config = HaydarConfig(folders=[], initialized=True, last_seen_version="0.1.0")
    monkeypatch.setattr(haydar, "__version__", "0.2.0")
    _disable_search_engine(monkeypatch)

    window = SearchWindow(config)
    qtbot.addWidget(window)
    try:
        qtbot.mouseClick(window._see_whatsnew_btn, Qt.LeftButton)

        assert config.last_seen_version == "0.1.0"
        assert window._whatsnew_banner is not None
        assert not window._whatsnew_banner.isHidden()
    finally:
        window.close()


def test_whatsnew_height_tracks_all_base_states_and_update_banner(
    qtbot, tmp_haydar, monkeypatch
):
    config = HaydarConfig(folders=[], initialized=True, last_seen_version="0.1.0")
    monkeypatch.setattr(haydar, "__version__", "0.2.0")
    _disable_search_engine(monkeypatch)
    window = SearchWindow(config)
    qtbot.addWidget(window)
    try:
        window.show()
        whats_new_extra = 40

        window.on_search_results([])
        assert window.height() == SearchWindow.BASE_EMPTY_HEIGHT + whats_new_extra

        result = SearchResult(
            file_path="dummy.txt",
            filename="dummy.txt",
            folder="",
            file_type=".txt",
            snippet="snippet",
            score=1.0,
            modified_time=0.0,
        )
        window.on_search_results([result])
        assert window.height() == SearchWindow.BASE_RESULTS_HEIGHT + whats_new_extra

        window.on_search_error("Boom")
        assert window.height() == SearchWindow.BASE_ERROR_HEIGHT + whats_new_extra

        window.on_update_available("9.9.9")
        update_extra = window._update_banner.sizeHint().height() + 12
        expected = SearchWindow.BASE_ERROR_HEIGHT + whats_new_extra + update_extra
        assert window.height() == expected
        window._set_content_height(SearchWindow.BASE_ERROR_HEIGHT)
        assert window.height() == expected

        window._hide_whats_new_banner()
        assert window.height() == SearchWindow.BASE_ERROR_HEIGHT + update_extra
    finally:
        window.close()


def test_whatsnew_save_failure_keeps_banner_and_previous_version(
    qtbot, tmp_haydar, monkeypatch
):
    config = HaydarConfig(folders=[], initialized=True, last_seen_version="0.1.0")
    monkeypatch.setattr(haydar, "__version__", "0.2.0")
    monkeypatch.setattr(config, "save", MagicMock(side_effect=OSError("read only")))
    _disable_search_engine(monkeypatch)

    window = SearchWindow(config)
    qtbot.addWidget(window)
    try:
        window.show()
        qtbot.mouseClick(window._dismiss_whatsnew_btn, Qt.LeftButton)

        assert config.last_seen_version == "0.1.0"
        assert window._whatsnew_banner is not None
        assert window._whatsnew_banner.isVisible()
    finally:
        window.close()

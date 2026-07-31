"""D1-3 Multi-monitor and Display-Scaling Regression Suite.

MANUAL VALIDATION RECORD:
--------------------------------------------------------------------------------
The following manual checks MUST be performed on a real Windows desktop before
a release, as offscreen CI cannot reliably simulate native OS DPI scaling:

[ ] 100% Scaling Check: Verify the window layout, text, and shadows appear
    correctly without clipping at 96 DPI.
[ ] 125% Scaling Check: Verify layout and text sizing.
[ ] 150% Scaling Check: Verify layout and text sizing.
[ ] 200% (4K) Scaling Check: Verify layout, text, and shadow bounds are not
    truncated or clipped.
[ ] Two-Monitor Placement Check: With monitors at different scaling factors
    (e.g., 100% and 150%), invoke the hotkey with the cursor on each monitor
    and verify the window centers correctly without crossing screen boundaries.
[ ] Taskbar Edge Check: Ensure window avoids taskbars on top/bottom/left/right.
--------------------------------------------------------------------------------
"""

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from haydar.config import HaydarConfig
from haydar.search.hybrid import SearchResult
from haydar.ui.results import ResultItem
from haydar.ui.window import SearchWindow


@pytest.fixture
def config():
    return HaydarConfig(folders=[], initialized=True)


def _disable_engine(monkeypatch):
    monkeypatch.setattr("haydar.ui.window.HybridSearch.__init__", lambda self, config: None)


def test_rounding_policy_uses_qt6_pass_through(monkeypatch):
    from haydar.ui.window import _configure_qt_dpi_policy

    calls = []
    monkeypatch.setattr(
        QApplication,
        "setHighDpiScaleFactorRoundingPolicy",
        lambda policy: calls.append(policy),
    )
    _configure_qt_dpi_policy()
    assert calls == [Qt.HighDpiScaleFactorRoundingPolicy.PassThrough]


def test_window_uses_logical_base_dimensions(qtbot, tmp_haydar, config, monkeypatch):
    _disable_engine(monkeypatch)
    window = SearchWindow(config)
    qtbot.addWidget(window)

    assert window.width() == SearchWindow.BASE_WIDTH
    assert window.settings_btn.minimumSize().width() == 24
    assert window.settings_btn.parentWidget() is window.container
    assert window.settings_btn.geometry().right() < window.container.width()
    search_layout = window.container.layout().itemAt(1).layout()
    assert search_layout.indexOf(window.settings_btn) >= 0
    window.close()


def test_result_item_expands_instead_of_fixed_card_height(qtbot):
    result = SearchResult(
        file_path="C:/docs/example.txt",
        filename="example.txt",
        folder="C:/docs",
        file_type=".txt",
        snippet="A long snippet that can wrap when the available logical width is small.",
        score=1.0,
        modified_time=0.0,
    )
    item = ResultItem(result, "example")
    qtbot.addWidget(item)

    assert item.minimumHeight() == 0
    assert item.sizePolicy().verticalPolicy().name == "Minimum"
    assert item.sizeHint().height() > 0
    assert item.icon_label.minimumWidth() == 0


def test_result_layout_accepts_small_logical_width(qtbot, tmp_haydar, config, monkeypatch):
    _disable_engine(monkeypatch)
    window = SearchWindow(config)
    qtbot.addWidget(window)
    window._set_logical_size(320, SearchWindow.BASE_EMPTY_HEIGHT)
    window.show()
    QApplication.processEvents()

    assert window.width() == 320
    assert window.settings_btn.geometry().right() <= window.width() - 1
    window.close()


def test_logical_sizing_helper_fractional_values(qtbot, tmp_haydar, config, monkeypatch):
    _disable_engine(monkeypatch)
    window = SearchWindow(config)
    qtbot.addWidget(window)

    # Test that fractional values are safely truncated to int logical dimensions
    window._set_logical_size(700.5, 110.8)
    assert window.width() == 700
    assert window.height() == 110
    window.close()


def test_layout_states_no_double_deltas(qtbot, tmp_haydar, config, monkeypatch):
    _disable_engine(monkeypatch)
    window = SearchWindow(config)
    qtbot.addWidget(window)
    window.show()
    QApplication.processEvents()

    # 1. No result state
    window.on_search_results([])
    assert window.height() == SearchWindow.BASE_EMPTY_HEIGHT

    # 2. Result state
    results = [SearchResult(
        file_path="a", filename="b", folder="c", file_type="d",
        snippet="e", score=1.0, modified_time=0.0
    )]
    window.on_search_results(results)
    assert window.height() == SearchWindow.BASE_RESULTS_HEIGHT

    # 3. Error state
    window.on_search_error("Boom")
    assert window.height() == SearchWindow.BASE_ERROR_HEIGHT

    # 4. Update banner expands by its layout-owned hint and spacing.
    window.on_update_available("0.9.9")
    banner_extra = window._update_banner.sizeHint().height() + 12
    assert window.height() == SearchWindow.BASE_ERROR_HEIGHT + banner_extra

    # 5. Switching to results while banner is active applies the same delta once.
    window.on_search_results(results)
    assert window.height() == SearchWindow.BASE_RESULTS_HEIGHT + banner_extra

    window.close()


def test_gui_dpi_awareness_is_safe_without_windows_api(monkeypatch):
    import haydar.gui_main as gui_main

    monkeypatch.setattr(gui_main.sys, "platform", "win32")
    monkeypatch.setitem(gui_main.__dict__, "ctypes", MagicMock())
    gui_main._enable_windows_dpi_awareness()


def test_cursor_monitor_positioning(qtbot, tmp_haydar, config, monkeypatch):
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QCursor

    _disable_engine(monkeypatch)
    window = SearchWindow(config)
    qtbot.addWidget(window)

    mock_screen = MagicMock()
    mock_screen.availableGeometry.return_value = QRect(-1000, 20, 1000, 1000)

    monkeypatch.setattr(QApplication, "screenAt", lambda pos: mock_screen)
    monkeypatch.setattr(QCursor, "pos", lambda: QPoint(-500, 500))

    window.toggle()

    assert window.x() == -850
    assert window.y() == 265
    window.close()


def test_cursor_monitor_positioning_fallback_to_primary(qtbot, tmp_haydar, config, monkeypatch):
    from PySide6.QtCore import QRect

    _disable_engine(monkeypatch)
    window = SearchWindow(config)
    qtbot.addWidget(window)

    mock_primary = MagicMock()
    mock_primary.availableGeometry.return_value = QRect(0, 0, 1920, 1080)

    monkeypatch.setattr(QApplication, "screenAt", lambda pos: None)
    monkeypatch.setattr(QApplication, "primaryScreen", lambda: mock_primary)

    window.toggle()

    assert window.x() == 610
    assert window.y() == 285
    window.close()


def test_cursor_monitor_positioning_clamping(qtbot, tmp_haydar, config, monkeypatch):
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QCursor

    _disable_engine(monkeypatch)
    window = SearchWindow(config)
    qtbot.addWidget(window)

    mock_screen = MagicMock()
    mock_screen.availableGeometry.return_value = QRect(100, 100, 700, 300)

    monkeypatch.setattr(QApplication, "screenAt", lambda pos: mock_screen)
    monkeypatch.setattr(QCursor, "pos", lambda: QPoint(150, 150))

    window.toggle()

    available = mock_screen.availableGeometry()
    assert window.geometry().left() >= available.left()
    assert window.geometry().top() >= available.top()
    assert window.geometry().right() <= available.right()
    assert window.geometry().bottom() <= available.bottom()
    window.close()


def test_cursor_monitor_positioning_no_screen(qtbot, tmp_haydar, config, monkeypatch):
    _disable_engine(monkeypatch)
    window = SearchWindow(config)
    qtbot.addWidget(window)
    window.move(123, 456)

    monkeypatch.setattr(QApplication, "screenAt", lambda pos: None)
    monkeypatch.setattr(QApplication, "primaryScreen", lambda: None)

    window.toggle()

    assert window.x() == 123
    assert window.y() == 456
    window.close()

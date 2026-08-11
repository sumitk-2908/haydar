import threading
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QLabel, QMessageBox

from haydar.config import HaydarConfig
from haydar.ocr import TesseractInfo, TesseractStatus
from haydar.ui.settings import SettingsWindow


def test_settings_constructs(qtbot, tmp_haydar):
    w = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    qtbot.addWidget(w)

    assert w.tab_widget.count() == 5
    assert w.tab_widget.tabText(0) == "Folders"
    assert w.tab_widget.tabText(1) == "Search"
    assert w.tab_widget.tabText(2) == "Appearance"
    assert w.tab_widget.tabText(3) == "Advanced"
    assert w.tab_widget.tabText(4) == "What's New"

    assert w.windowTitle() == "Haydar Settings"
    assert w.width() == 600
    assert w.height() == 520

    assert bool(w.windowFlags() & Qt.Window)
    assert bool(w.windowFlags() & Qt.WindowStaysOnTopHint)

def test_settings_ocr_detection_is_nonblocking(qtbot, tmp_haydar, monkeypatch):
    """Constructing the window must not serialize on Tesseract detection.

    Asserted structurally rather than against a wall-clock budget. An
    ``elapsed < 0.1`` bound also timed Qt building five tabs of widgets, so it
    tripped on a loaded runner even though detection was properly off-thread.
    Here detection parks until the test releases it, so construction returning
    before ``slow_detection`` returns is itself the proof.
    """
    release = threading.Event()
    detection_returned = threading.Event()

    def slow_detection():
        release.wait(10)
        detection_returned.set()
        return TesseractInfo(TesseractStatus.NOT_FOUND, None, None)

    monkeypatch.setattr("haydar.ui.settings.detect_tesseract", slow_detection)
    w = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    qtbot.addWidget(w)

    assert not detection_returned.is_set(), "construction blocked on detection"
    assert w.ocr_status_label.text() == "Checking..."
    release.set()
    qtbot.waitUntil(lambda: "executable not found" in w.ocr_status_label.text())


def test_settings_can_close_before_ocr_worker_finishes(qtbot, tmp_haydar, monkeypatch):
    """Destroying the window mid-detection must still retire its worker.

    Regression: ``worker.finished -> thread.quit`` used the default
    AutoConnection, and because the ``QThread`` object itself lives in the GUI
    thread that made the quit a *queued* call the GUI thread had to deliver.
    Detection deliberately outlives the window, so once the window was destroyed
    nothing guaranteed that call was ever delivered: the thread stayed parked in
    ``exec()``, never emitted ``finished``, and never retired its job.

    ``QThread.wait()`` is the assertion that pins this down, because it blocks
    *without* processing events: it can only return if the detection thread ends
    its own event loop. Polling with ``waitUntil`` instead would pump the queue
    by hand and pass against either wiring.

    The window is deliberately not registered with ``qtbot``: this test destroys
    it, and pytest-qt's teardown would call ``close()`` on the freed C++ object,
    erroring both the teardown and the next test's setup.
    """
    release = threading.Event()

    def slow_detection():
        release.wait(1)
        return TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe")

    monkeypatch.setattr("haydar.ui.settings.detect_tesseract", slow_detection)
    before = set(SettingsWindow._active_ocr_jobs)
    w = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    job = SettingsWindow._active_ocr_jobs - before
    assert job, "constructing the window must register a detection job"
    thread = w._ocr_thread

    w.close()
    w.deleteLater()
    release.set()

    assert thread.wait(5000), "detection thread never ended its own event loop"
    assert thread.isFinished()
    # Retiring the registry entry is a queued hand-off to the GUI thread, so it
    # lands on the next pass of the event loop rather than inside wait() above.
    # This waits on *this* window's job rather than on ``_active_ocr_jobs`` being
    # empty. The registry is class-level, every ``SettingsWindow`` in the suite
    # adds to it, and several tests return before their detection thread retires —
    # so asserting global emptiness waits on unrelated tests' threads and fails
    # whenever CI scheduling leaves one in flight.
    qtbot.waitUntil(lambda: not (job & SettingsWindow._active_ocr_jobs))


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"), "image OCR enabled"),
        (TesseractInfo(TesseractStatus.PYTHON_PACKAGE_MISSING, None, None), "adapter not installed"),
        (TesseractInfo(TesseractStatus.NOT_FOUND, None, None), "executable not found"),
        (TesseractInfo(TesseractStatus.WRONG_VERSION, "3.05", "C:/Tesseract/tesseract.exe"), "v4+ required"),
        (TesseractInfo(TesseractStatus.ERROR, None, "C:/Tesseract/tesseract.exe", "timed out"), "could not be verified"),
    ],
)
def test_settings_renders_each_ocr_state(qtbot, tmp_haydar, monkeypatch, info, expected):
    monkeypatch.setattr("haydar.ui.settings.detect_tesseract", lambda: info)
    w = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    qtbot.addWidget(w)

    qtbot.waitUntil(lambda: expected in w.ocr_status_label.text())
    assert w.ocr_status_label.accessibleName() == "OCR status"
    assert w.ocr_install_btn.accessibleName() == "Install OCR instructions"


def test_ocr_instruction_dialog_uses_plain_text(qtbot, tmp_haydar, monkeypatch):
    monkeypatch.setattr(
        "haydar.ui.settings.detect_tesseract",
        lambda: TesseractInfo(TesseractStatus.NOT_FOUND, None, None),
    )
    monkeypatch.setattr(QMessageBox, "exec", MagicMock(return_value=QMessageBox.Ok))
    w = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    qtbot.addWidget(w)

    w._show_ocr_install_instructions()

    dialog = w.findChild(QMessageBox)
    assert dialog is not None
    assert dialog.textFormat() == pytest.importorskip("PySide6.QtCore").Qt.PlainText
    # §19: never sends a normal user to pip, Winget, PATH, or the CLI. The
    # shipped manifest is unreviewed, so this dialog explains how to install the
    # engine directly (§19 amended 2026-08-11) rather than offering a one-click
    # install that would always fail.
    lowered = dialog.text().lower()
    assert "tesseract" in lowered
    assert "never uploaded" in lowered
    for forbidden in ("pip install", "winget", "path", "haydar-cli"):
        assert forbidden not in lowered


def test_add_folder_updates_pending(qtbot, tmp_haydar, tmp_path, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args, **kwargs: str(tmp_path))
    w = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    qtbot.addWidget(w)

    qtbot.mouseClick(w.add_folder_btn, pytest.importorskip("PySide6.QtCore").Qt.LeftButton)
    assert str(tmp_path) in w._pending_folders

def test_save_writes_config(qtbot, tmp_haydar, tmp_path):
    config = HaydarConfig(folders=[str(tmp_path)], initialized=True)
    w = SettingsWindow(config)
    qtbot.addWidget(w)

    w._apply()

    loaded_config = HaydarConfig.load()
    assert loaded_config.folders == [str(tmp_path)]

def test_cancel_does_not_write(qtbot, tmp_haydar, tmp_path):
    config = HaydarConfig(folders=[str(tmp_path)], initialized=True)
    config.save()
    w = SettingsWindow(config)
    qtbot.addWidget(w)

    w._pending_folders = []
    w._revert()

    loaded_config = HaydarConfig.load()
    assert loaded_config.folders == [str(tmp_path)]

def test_empty_folders_blocked(qtbot, tmp_haydar, monkeypatch):
    w = SettingsWindow(HaydarConfig(folders=["/some/path"], initialized=True))
    qtbot.addWidget(w)

    w._pending_folders = []
    mock_save = MagicMock()
    monkeypatch.setattr(w.config, "save", mock_save)
    monkeypatch.setattr(QMessageBox, "warning", MagicMock())

    w._apply()

    assert mock_save.call_count == 0

def test_config_changed_signal_emitted(qtbot, tmp_haydar, tmp_path):
    config = HaydarConfig(folders=[str(tmp_path)], initialized=True)
    w = SettingsWindow(config)
    qtbot.addWidget(w)

    collector = []
    w.config_changed.connect(lambda cfg: collector.append(cfg))

    w._apply()

    assert len(collector) == 1
    assert collector[0].folders == [str(tmp_path)]

def test_overlap_validation_blocks_save(qtbot, tmp_haydar, monkeypatch):
    w = SettingsWindow(HaydarConfig(folders=["/some/path"], initialized=True))
    qtbot.addWidget(w)
    w.tab_widget.setCurrentIndex(3)
    w.show()

    w.chunk_size_spin.setValue(100)
    w.chunk_overlap_spin.setValue(100)

    mock_save = MagicMock()
    monkeypatch.setattr(w.config, "save", mock_save)

    w._apply()

    assert mock_save.call_count == 0
    assert w.overlap_error_label.isVisible()

def test_search_tab_inputs_and_hotkey_validation(qtbot, tmp_haydar, monkeypatch):
    config = HaydarConfig(folders=["/some/path"], initialized=True)
    w = SettingsWindow(config)
    qtbot.addWidget(w)
    w.tab_widget.setCurrentIndex(1)  # Search tab
    w.show()

    w.hotkey_edit.setText("invalid hotkey")
    w.debounce_spin.setValue(1.5)
    w.limit_spin.setValue(20)

    mock_save = MagicMock()
    monkeypatch.setattr(w.config, "save", mock_save)
    monkeypatch.setattr(w, "hide", MagicMock())

    w._apply()

    assert mock_save.call_count == 1
    assert not w.hotkey_error_label.isHidden()
    assert w.config.hotkey == "invalid hotkey"
    assert w.config.watcher_debounce_seconds == 1.5
    assert w.config.results_limit == 20

def test_appearance_tab_live_updates(qtbot, tmp_haydar):
    config = HaydarConfig(folders=[], initialized=True, window_opacity=92, always_on_top=True)
    w = SettingsWindow(config)
    qtbot.addWidget(w)
    w.tab_widget.setCurrentIndex(2)  # Appearance tab
    w.show()

    collector = []
    w.config_changed.connect(lambda cfg: collector.append(cfg))

    # Test opacity live update
    w.opacity_slider.setValue(80)
    assert len(collector) == 1
    assert collector[-1].window_opacity == 80
    assert collector[-1].always_on_top  # preserved

    # Test always on top live update
    w.always_on_top_check.setChecked(False)
    assert len(collector) == 2
    assert collector[-1].window_opacity == 80  # preserved
    assert not collector[-1].always_on_top

def test_advanced_tab_reindex_warning(qtbot, tmp_haydar, monkeypatch):
    config = HaydarConfig(folders=["/some/path"], initialized=True, chunk_size=500, embedding_model="old-model")
    w = SettingsWindow(config)
    qtbot.addWidget(w)
    w.tab_widget.setCurrentIndex(3)
    w.show()

    w.chunk_size_spin.setValue(1000)
    w.model_edit.setText("new-model")

    mock_save = MagicMock()
    mock_msgbox = MagicMock()
    monkeypatch.setattr(w.config, "save", mock_save)
    monkeypatch.setattr(QMessageBox, "information", mock_msgbox)
    monkeypatch.setattr(w, "hide", MagicMock())

    w._apply()

    assert mock_save.call_count == 1
    assert mock_msgbox.call_count == 1

    # Check that the config values were actually updated
    assert w.config.chunk_size == 1000
    assert w.config.embedding_model == "new-model"


def test_whats_new_unavailable_for_missing_or_empty_changelog(
    qtbot, tmp_haydar, tmp_path, monkeypatch
):
    empty = tmp_path / "CHANGELOG.md"
    empty.write_text("# Changelog\n", encoding="utf-8")
    monkeypatch.setattr("haydar.changelog.find_changelog", lambda: empty)
    monkeypatch.setattr(SettingsWindow, "_start_ocr_check", lambda self: None)

    window = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    qtbot.addWidget(window)

    labels = window.whats_new_tab.findChildren(QLabel)
    assert any(label.text() == "Changelog not available." for label in labels)


def test_whats_new_renders_three_then_lazily_creates_remaining_versions(
    qtbot, tmp_haydar, tmp_path, monkeypatch
):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join(
            f"## [{version}.0.0] - 2026-01-0{version}\n### Added\n- Feature {version}"
            for version in range(1, 6)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("haydar.changelog.find_changelog", lambda: changelog)
    monkeypatch.setattr(SettingsWindow, "_start_ocr_check", lambda self: None)

    window = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    qtbot.addWidget(window)
    window.show_whats_new()

    initial_texts = [label.text() for label in window.whats_new_tab.findChildren(QLabel)]
    assert "v1.0.0  —  2026-01-01" in initial_texts
    assert "v3.0.0  —  2026-01-03" in initial_texts
    assert "v4.0.0  —  2026-01-04" not in initial_texts
    assert len(window._hidden_version_entries) == 2
    assert window._whats_new_show_all_btn is not None

    qtbot.mouseClick(window._whats_new_show_all_btn, Qt.LeftButton)

    all_texts = [label.text() for label in window.whats_new_tab.findChildren(QLabel)]
    assert "v4.0.0  —  2026-01-04" in all_texts
    assert "v5.0.0  —  2026-01-05" in all_texts
    assert window._hidden_version_entries == []
    assert window._whats_new_show_all_btn.isHidden()

    window._show_all_versions()
    repeated_texts = [
        label.text() for label in window.whats_new_tab.findChildren(QLabel)
    ]
    assert repeated_texts == all_texts


def test_whats_new_renders_items_as_plain_text(
    qtbot, tmp_haydar, tmp_path, monkeypatch
):
    changelog = tmp_path / "CHANGELOG.md"
    hostile_item = "<img src=x onerror=alert(1)>"
    changelog.write_text(
        f"## [1.0.0] - 2026-01-01\n### Added\n- {hostile_item}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("haydar.changelog.find_changelog", lambda: changelog)
    monkeypatch.setattr(SettingsWindow, "_start_ocr_check", lambda self: None)

    window = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    qtbot.addWidget(window)

    labels = window.whats_new_tab.findChildren(QLabel)
    item = next(label for label in labels if hostile_item in label.text())
    assert item.text() == f"• {hostile_item}"
    assert item.textFormat() == Qt.PlainText
    assert all(label.textFormat() == Qt.PlainText for label in labels)


def test_show_whats_new_selects_fifth_tab(qtbot, tmp_haydar, monkeypatch):
    monkeypatch.setattr(SettingsWindow, "_start_ocr_check", lambda self: None)
    window = SettingsWindow(HaydarConfig(folders=[], initialized=True))
    qtbot.addWidget(window)

    window.show_whats_new()

    assert window.isVisible()
    assert window.tab_widget.currentIndex() == 4

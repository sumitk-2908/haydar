"""Qt lifecycle and partial-index UI contract tests.

The properties under test: exactly one QApplication, an onboarding-to-search
handoff that never exits the event loop, and search plus settings staying usable
in every indexing state.
"""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from haydar.application import ApplicationService
from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexSnapshot, JobKind, JobOutcome, JobPhase
from haydar.indexer.jobs import IndexJobCoordinator
from haydar.ui.application import GuiApplicationController
from haydar.ui.index_status import IndexStatusBand


class _FakeEngine:
    def __init__(self):
        self.calls = []

    def run_job(self, **kwargs):
        self.calls.append(kwargs)
        return IndexSnapshot(outcome=JobOutcome.COMPLETE)

    def close(self):
        pass


def _ready_config(**kwargs):
    options = {
        "folders": [r"C:\Docs"],
        "folders_configured": True,
        "search_ready": True,
    }
    options.update(kwargs)
    return HaydarConfig(**options)


def _service(config, watcher=None):
    return ApplicationService(
        config,
        job_coordinator=IndexJobCoordinator(
            config, engine_factory=lambda _c: _FakeEngine()
        ),
        watcher_factory=(lambda _c: watcher) if watcher is not None else None,
    )


@pytest.fixture
def controller(qtbot, tmp_haydar, monkeypatch):
    """Build a controller over stubbed search/settings windows.

    The stub is installed with monkeypatch rather than a context manager so it
    stays active for the whole test: constructing a real SearchWindow would
    start hotkey, staleness, and update threads.
    """
    created = []

    def make(config, watcher=None):
        window = MagicMock()
        window.index_band = IndexStatusBand()
        qtbot.addWidget(window.index_band)
        window._hotkey_listener = None
        monkeypatch.setattr(
            "haydar.ui.application.create_search_window", lambda _config: window
        )
        ctrl = GuiApplicationController(
            QApplication.instance(), config, service=_service(config, watcher)
        )
        created.append(ctrl)
        return ctrl, window

    yield make

    for ctrl in created:
        if ctrl._subscription is not None:
            ctrl._subscription.cancel()


# -- one lifecycle ----------------------------------------------------------


def test_exactly_one_qapplication_is_used(controller):
    """The handoff must never construct a second application."""
    before = QApplication.instance()
    ctrl, _window = controller(_ready_config())
    ctrl.start()

    assert QApplication.instance() is before


def test_a_ready_config_bypasses_onboarding(controller):
    ctrl, window = controller(_ready_config(initial_index_state="complete"))
    ctrl.start()

    assert ctrl.setup_window is None
    assert ctrl.search_window is window


def test_a_fresh_install_shows_onboarding_first(qtbot, tmp_haydar):
    config = HaydarConfig(folders=[], search_ready=False)
    setup_window = MagicMock()
    with patch("haydar.ui.application.SetupWindow", return_value=setup_window):
        ctrl = GuiApplicationController(
            QApplication.instance(), config, service=_service(config)
        )
        ctrl.start()

    assert ctrl.search_window is None
    setup_window.show.assert_called_once()


def test_handoff_shows_search_before_closing_onboarding(qtbot, tmp_haydar):
    """Closing the last window before another exists would end the event loop."""
    config = HaydarConfig(folders=[r"C:\Docs"], search_ready=False)
    order = []
    setup_window = MagicMock()
    setup_window.close.side_effect = lambda: order.append("close_setup")
    search_window = MagicMock()
    search_window.index_band = None
    search_window.toggle.side_effect = lambda: order.append("show_search")

    with (
        patch("haydar.ui.application.SetupWindow", return_value=setup_window),
        patch(
            "haydar.ui.application.create_search_window", return_value=search_window
        ),
    ):
        ctrl = GuiApplicationController(
            QApplication.instance(), config, service=_service(config)
        )
        ctrl.start()
        ctrl._on_search_ready(_ready_config())

    assert order == ["show_search", "close_setup"]


# -- partial-index states ---------------------------------------------------


@pytest.mark.parametrize(
    "state", ["not_started", "running", "paused", "cancelled", "failed", "complete"]
)
def test_search_window_opens_in_every_index_state(controller, state):
    ctrl, window = controller(_ready_config(initial_index_state=state))
    ctrl.start()

    assert ctrl.search_window is window
    window.toggle.assert_called_once()


def test_search_opens_before_the_initial_index_finishes(controller):
    ctrl, window = controller(_ready_config(initial_index_state="not_started"))
    ctrl.start()

    # The window exists and is shown while the crawl is still pending.
    assert ctrl.search_window is window
    assert ctrl.config.initial_index_state != "complete"


# -- status band ------------------------------------------------------------


@pytest.fixture
def band(qtbot):
    widget = IndexStatusBand()
    qtbot.addWidget(widget)
    return widget


def test_running_state_offers_pause_and_cancel(band):
    band.update_snapshot(
        IndexSnapshot(phase=JobPhase.EXTRACTING, discovered=10, examined=4)
    )

    assert "Indexing in background" in band.message_label.text()
    assert band.pause_button.isVisible()
    assert band.cancel_button.isVisible()
    assert not band.resume_button.isVisible()


def test_paused_state_offers_resume_and_cancel(band):
    band.update_snapshot(IndexSnapshot(outcome=JobOutcome.PAUSED))

    assert "paused" in band.message_label.text().lower()
    assert "remain searchable" in band.message_label.text()
    assert band.resume_button.isVisible()


def test_cancelled_state_offers_resume(band):
    band.update_snapshot(IndexSnapshot(outcome=JobOutcome.CANCELLED))

    assert "cancelled" in band.message_label.text().lower()
    assert band.resume_button.isVisible()


def test_failed_state_offers_retry_and_log(band):
    band.update_snapshot(
        IndexSnapshot(outcome=JobOutcome.FAILED, error_message="disk full")
    )

    assert band.retry_button.isVisible()
    assert band.log_button.isVisible()
    assert "disk full" in band.message_label.text()


def test_complete_state_reports_completion(band):
    band.update_snapshot(IndexSnapshot(outcome=JobOutcome.COMPLETE))

    assert "All configured folders indexed" in band.message_label.text()
    assert not band.pause_button.isVisible()


def test_pausing_state_only_offers_cancel(band):
    band.update_snapshot(IndexSnapshot(phase=JobPhase.PAUSING))

    assert "Pausing" in band.message_label.text()
    assert not band.pause_button.isVisible()
    assert band.cancel_button.isVisible()


def test_ocr_deferred_offers_a_one_click_install(band):
    band.update_snapshot(IndexSnapshot(outcome=JobOutcome.COMPLETE, ocr_deferred=7))

    assert "7 images are waiting for OCR" in band.message_label.text()
    assert band.ocr_button.isVisible()
    # Never instructs the user to install anything by hand.
    text = band.message_label.text().lower()
    assert "pip" not in text and "winget" not in text and "path" not in text


def test_ocr_backfill_reports_its_own_progress(band):
    band.update_snapshot(
        IndexSnapshot(kind=JobKind.OCR_BACKFILL, committed_files=3)
    )

    assert "Adding image text" in band.message_label.text()
    assert band.pause_button.isVisible()


def test_progress_is_indeterminate_until_discovery_completes(band):
    band.update_snapshot(IndexSnapshot(discovered=42, discovery_complete=False))

    # An indeterminate range is Qt's "unknown total"; a percentage here would be
    # computed from a denominator that is still growing.
    assert (band.progress.minimum(), band.progress.maximum()) == (0, 0)
    assert "42" in band.message_label.text()


def test_progress_becomes_determinate_once_the_total_is_stable(band):
    band.update_snapshot(
        IndexSnapshot(discovered=10, examined=4, discovery_complete=True)
    )

    assert band.progress.maximum() == 10
    assert band.progress.value() == 4


def test_controls_emit_intent_and_disable_only_until_acknowledged(band, qtbot):
    with qtbot.waitSignal(band.pause_requested, timeout=1000):
        band.update_snapshot(IndexSnapshot(phase=JobPhase.EXTRACTING))
        band.pause_button.click()

    assert band.pause_button.isEnabled() is False

    # The acknowledgement re-enables the affected control.
    band.update_snapshot(IndexSnapshot(outcome=JobOutcome.PAUSED))
    assert band.resume_button.isEnabled() is True


def test_every_control_has_an_accessible_name_and_hit_target(band):
    band.update_snapshot(
        IndexSnapshot(outcome=JobOutcome.FAILED, error_message="x", ocr_deferred=1)
    )

    for button in band._all_buttons():
        assert button.accessibleName()
        assert button.accessibleDescription()
        assert button.minimumWidth() >= 24
        assert button.minimumHeight() >= 24


def test_the_status_message_is_exposed_to_screen_readers(band):
    band.update_snapshot(IndexSnapshot(outcome=JobOutcome.COMPLETE))

    assert band.message_label.accessibleName()
    assert band.message_label.accessibleDescription() == band.message_label.text()


# -- OCR entry point --------------------------------------------------------


def test_ocr_install_surfaces_a_plain_failure_reason(qtbot, tmp_haydar):
    """A refusal must read as product copy, not as a stack trace."""
    from haydar.ui.application import _OcrInstallWorker

    worker = _OcrInstallWorker()
    messages = []
    worker.failed.connect(messages.append)

    with patch(
        "haydar.ocr.detect_tesseract",
        return_value=MagicMock(status=None, path=None),
    ):
        worker.run()

    assert len(messages) == 1
    lowered = messages[0].lower()
    assert "traceback" not in lowered and "importerror" not in lowered
    # The shipped asset is unreviewed, so provisioning declines rather than
    # downloading something it cannot verify.
    assert "not available in this build" in lowered


def test_ocr_install_reports_readiness_when_an_engine_already_exists(
    qtbot, tmp_haydar
):
    from haydar.ocr import OcrInstallResult, OcrPhase
    from haydar.ui.application import _OcrInstallWorker

    worker = _OcrInstallWorker()
    results = []
    worker.finished.connect(results.append)

    ready = OcrInstallResult(
        phase=OcrPhase.COMPLETE, version="5.4.0", executable_path=r"C:\ocr\t.exe"
    )
    with (
        patch("haydar.ui.application.install_ocr", create=True),
        patch("haydar.ocr.install_ocr", return_value=ready),
    ):
        worker.run()

    assert [r.version_token for r in results] == ["tesseract-5.4.0"]


def test_a_successful_install_starts_an_image_only_backfill(controller):
    """§12.4: non-image records are untouched and no full reindex runs."""
    from haydar.ocr import OcrInstallResult, OcrPhase

    ctrl, _window = controller(_ready_config(initial_index_state="complete"))
    ctrl.start()
    ctrl.service.jobs.start_ocr_backfill = MagicMock(return_value="run-1")

    ctrl._on_ocr_installed(
        OcrInstallResult(phase=OcrPhase.COMPLETE, version="5.4.0")
    )

    ctrl.service.jobs.start_ocr_backfill.assert_called_once_with("tesseract-5.4.0")


def test_ocr_progress_is_narrated_in_the_status_band(controller):
    ctrl, _window = controller(_ready_config())
    ctrl.start()

    ctrl._on_ocr_progress("Verifying the download…")

    assert "Verifying" in ctrl.search_window.index_band.message_label.text()


# -- deferred callbacks outliving their widget -------------------------------


def _controller_with_disposable_band(config, qtbot):
    """Build a controller whose band this test owns and will destroy.

    The shared ``controller`` fixture registers its band with ``qtbot``, whose
    teardown would then trip over the very deletion these tests need.
    """
    window = MagicMock()
    window.index_band = IndexStatusBand()
    window._hotkey_listener = None
    with patch("haydar.ui.application.create_search_window", lambda _c: window):
        ctrl = GuiApplicationController(
            QApplication.instance(), config, service=_service(config)
        )
        ctrl.start()
    return ctrl, window


def _destroy_band(ctrl, qtbot):
    """Destroy the band's C++ side the way closing the window would.

    ``deleteLater`` plus a spin of the event loop is the real teardown path, so
    the Python wrapper survives while the C++ object does not — exactly the
    state a late callback finds.
    """
    ctrl.search_window.index_band.deleteLater()
    qtbot.wait(10)


def test_the_completion_collapse_survives_a_closed_window(qtbot, tmp_haydar):
    """The collapse fires six seconds late; the window may be gone by then.

    Touching a destroyed widget raises inside the Qt event loop, where there is
    nowhere useful for the exception to go.
    """
    ctrl, _window = _controller_with_disposable_band(_ready_config(), qtbot)
    ctrl.service.snapshot = lambda: IndexSnapshot(outcome=JobOutcome.COMPLETE)
    _destroy_band(ctrl, qtbot)

    # The pending timer callback must be a no-op, not a crash.
    ctrl._collapse_status()

    if ctrl._subscription is not None:
        ctrl._subscription.cancel()


def test_a_destroyed_controller_drops_its_deferred_initial_index(controller, monkeypatch):
    """Deferred work must not outlive the controller that scheduled it.

    ``_show_search`` defers the initial index to the next event-loop turn. Given a
    ``QTimer.singleShot`` with no context object, that pending call is bound to
    nothing and still fires after the controller's C++ side is destroyed. The
    guards inside the callbacks cannot help: the call never gets that far.

    It also lands wherever the loop is next pumped, which in a test session is
    pytest-qt's post-test ``processEvents``. That reported an access violation
    against an unrelated test in tests/test_qt_lifecycle.py on CI (3.11,
    2026-08-11) rather than against whichever test leaked the timer.
    """
    from shiboken6 import delete

    started = []
    monkeypatch.setattr(
        GuiApplicationController,
        "_start_initial_index",
        lambda self: started.append(True),
    )
    ctrl, _window = controller(_ready_config(initial_index_state="complete"))
    ctrl.start()
    assert started == [], "the initial index must be deferred, not run inline"

    if ctrl._subscription is not None:
        ctrl._subscription.cancel()
    delete(ctrl)
    QApplication.instance().processEvents()

    assert started == [], "a destroyed controller still ran its deferred start"


def test_a_late_snapshot_does_not_touch_a_destroyed_band(qtbot, tmp_haydar):
    """Snapshots arrive from a worker thread and can lose the race with close."""
    ctrl, _window = _controller_with_disposable_band(_ready_config(), qtbot)
    _destroy_band(ctrl, qtbot)

    ctrl._on_snapshot(IndexSnapshot(outcome=JobOutcome.COMPLETE, committed_files=3))

    if ctrl._subscription is not None:
        ctrl._subscription.cancel()


def test_late_ocr_callbacks_do_not_touch_a_destroyed_band(qtbot, tmp_haydar):
    """OCR provisioning outlives the window that started it."""
    ctrl, _window = _controller_with_disposable_band(_ready_config(), qtbot)
    _destroy_band(ctrl, qtbot)

    ctrl._on_ocr_progress("Downloading text recognition…")
    ctrl._on_ocr_failed("something went wrong")

    if ctrl._subscription is not None:
        ctrl._subscription.cancel()


# -- architecture boundary --------------------------------------------------


def test_ui_modules_do_not_import_indexer_internals():
    """`.agents/AGENTS.md`: ui may import only from search/ or config."""
    import ast
    from pathlib import Path

    ui_dir = Path(__file__).resolve().parent.parent / "src" / "haydar" / "ui"
    offenders = []
    for path in ui_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            else:
                continue
            if module.startswith("haydar.indexer"):
                offenders.append(f"{path.name}: {module}")

    assert offenders == []

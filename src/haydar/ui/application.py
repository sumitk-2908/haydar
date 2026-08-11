"""Single-lifecycle Qt application controller for onboarding and search.

This is an adapter, not a decision-maker. It converts
:class:`haydar.application.ApplicationService` callbacks into Qt signals and owns
threads and windows; the sequencing rules (when search may open, when the crawl
starts, when the watcher is safe to start) live in the service so they can be
tested without a display.

One ``QApplication`` exists per process. Onboarding and search are top-level
views within that single event loop — the handoff never exits the loop or
constructs a second application.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from haydar.application import ApplicationService, default_watcher_factory
from haydar.config import HaydarConfig
from haydar.search.indexing_status import IndexSnapshot, JobOutcome
from haydar.ui.onboarding import SetupWindow
from haydar.ui.window import (
    SearchWindow,
    _configure_qt_dpi_policy,
    create_search_window,
)

logger = logging.getLogger(__name__)

# How long a completed status stays on screen before the band collapses.
COMPLETE_COLLAPSE_MS = 6000

# Packaged-startup probe cadence. The poll is short so setup is cancelled soon
# after it reports its first phase; the deadline bounds a hung startup.
STARTUP_PROBE_POLL_MS = 50
STARTUP_PROBE_DEADLINE_MS = 120_000


def _is_alive(widget: object) -> bool:
    """Whether a Qt object's underlying C++ instance still exists.

    Deferred callbacks (``QTimer.singleShot``, queued signals) can outlive the
    widget they target: Python still holds a wrapper after Qt has destroyed the
    C++ side, and touching it raises inside the event loop. Checking is cheaper
    than reasoning about every possible teardown order.
    """
    try:
        from shiboken6 import isValid
    except ImportError:  # pragma: no cover - shiboken ships with PySide6
        return widget is not None
    try:
        return isValid(widget)
    except (RuntimeError, TypeError):
        return False


class _OcrInstallWorker(QObject):
    """Provision the OCR engine off the GUI thread."""

    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        from haydar.ocr import OcrProvisionError, install_ocr

        try:
            result = install_ocr(
                cancel_event=self.cancel_event,
                progress_callback=lambda event: self.progress.emit(event.message),
            )
        except OcrProvisionError as exc:
            # Already phrased for a normal user, and never mentions pip, PATH,
            # or a manual download.
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            logger.exception("OCR provisioning failed")
            self.failed.emit(f"Text recognition setup could not finish. {exc}")
            return

        if not result.ready:
            self.failed.emit(result.message or "Setup did not finish.")
            return
        self.finished.emit(result)

    def cancel(self) -> None:
        self.cancel_event.set()


class GuiApplicationController(QObject):
    """Own all top-level windows and workers for one Qt event loop."""

    snapshot_received = Signal(object)

    def __init__(
        self,
        app: QApplication,
        config: HaydarConfig,
        *,
        service: ApplicationService | None = None,
    ) -> None:
        super().__init__()
        self.app = app
        self.config = config
        self.service = service or _build_service(config)
        self.setup_window: SetupWindow | None = None
        self.search_window: SearchWindow | None = None
        self._got_sigint = False
        self._subscription = None
        self._ocr_thread: QThread | None = None
        self._ocr_worker: _OcrInstallWorker | None = None

        # Job snapshots arrive on a worker thread; this signal marshals them
        # onto the GUI thread, which is the only place widgets may be touched.
        self.snapshot_received.connect(self._on_snapshot)

        self._signal_timer = QTimer(self)
        self._signal_timer.timeout.connect(lambda: None)
        self._signal_timer.start(200)

        signal.signal(signal.SIGINT, self._handle_sigint)
        self.app.aboutToQuit.connect(self.shutdown)

    # -- startup -----------------------------------------------------------

    def start(self) -> None:
        """Show onboarding or go straight to search, depending on readiness."""
        if not self.service.needs_onboarding:
            # A migrated legacy install lands here: their folders and existing
            # index are used as-is, with no onboarding.
            self._show_search(self.config)
            return

        self.setup_window = SetupWindow(self.config)
        self.setup_window.ready.connect(self._on_search_ready)
        self.setup_window.show()

    @Slot(object)
    def _on_search_ready(self, config: HaydarConfig) -> None:
        self.config = config
        self.service.config = config
        # Search opens first, then onboarding closes. Closing the last visible
        # window before another exists would end the event loop.
        self._show_search(config)
        if self.setup_window is not None:
            self.setup_window.close()
            self.setup_window = None

    def _show_search(self, config: HaydarConfig) -> None:
        if self.search_window is not None:
            return
        self.search_window = create_search_window(config)
        self._wire_status_controls(self.search_window)
        self.search_window.toggle()

        self._subscription = self.service.subscribe(self.snapshot_received.emit)
        # Start on the next event-loop turn so the window is interactive first.
        # `self` is passed as the timer's context object so Qt cancels the pending
        # call if this controller is destroyed first. Without a context the call
        # is bound to nothing and still fires after the C++ side is gone, which
        # is a use-after-free rather than an exception the guards below can catch.
        QTimer.singleShot(0, self, self._start_initial_index)

    def _wire_status_controls(self, window: SearchWindow) -> None:
        band = getattr(window, "index_band", None)
        if band is None:
            return
        band.pause_requested.connect(self.service.pause_index)
        band.cancel_requested.connect(self.service.cancel_index)
        band.resume_requested.connect(self._resume_index)
        band.retry_requested.connect(self._retry_index)
        band.install_ocr_requested.connect(self.install_ocr)
        band.view_log_requested.connect(self._open_log)

    @Slot()
    def _start_initial_index(self) -> None:
        try:
            self.service.start_initial_index_if_due()
        except Exception:
            logger.exception("Could not start the initial index")
        self._maybe_start_watcher()

    # -- job status --------------------------------------------------------

    @Slot(object)
    def _on_snapshot(self, snapshot: IndexSnapshot) -> None:
        window = self.search_window
        if window is None:
            return
        band = getattr(window, "index_band", None)
        # Snapshots originate on a worker thread, so the window may have closed
        # between the emit and this slot running.
        if band is not None and _is_alive(band):
            band.update_snapshot(snapshot)
            window.refresh_layout()

        if snapshot.outcome is not None:
            if snapshot.outcome is JobOutcome.COMPLETE:
                # Context object, so a controller destroyed inside this six-second
                # window cancels the callback instead of being called after free.
                # `_collapse_status` still guards the band, which can die on its own.
                QTimer.singleShot(COMPLETE_COLLAPSE_MS, self, self._collapse_status)
            # A terminal state may have made the watcher eligible.
            self._maybe_start_watcher()

    @Slot()
    def _collapse_status(self) -> None:
        window = self.search_window
        if window is None:
            return
        band = getattr(window, "index_band", None)
        snapshot = self.service.snapshot()
        # Only collapse if still complete: a new run may have started since.
        if band is None or snapshot.outcome is not JobOutcome.COMPLETE:
            return
        # This runs six seconds after completion, so the window may have been
        # closed and its C++ side destroyed while the timer was pending. Touching
        # a freed widget raises inside the Qt event loop, which Qt cannot route
        # anywhere useful.
        if not _is_alive(band):
            return
        band.collapse()
        window.refresh_layout()

    @Slot()
    def _resume_index(self) -> None:
        try:
            self.service.resume_index()
        except Exception:
            logger.exception("Could not resume indexing")

    @Slot()
    def _retry_index(self) -> None:
        try:
            self.service.retry_index()
        except Exception:
            logger.exception("Could not retry indexing")

    @Slot()
    def _open_log(self) -> None:
        import os

        from haydar.config import get_log_path

        try:
            os.startfile(str(get_log_path()))
        except OSError:
            logger.exception("Could not open the log file")

    def _maybe_start_watcher(self) -> None:
        """Start the watcher only once the service says it is safe."""
        try:
            self.service.start_watcher_if_eligible()
        except Exception:
            logger.exception("Could not start the file watcher")

    def _live_band(self):
        """Return the status band only while it is safe to touch.

        OCR provisioning runs on a worker thread and can report progress long
        after the user closed the window, so every band access from those
        callbacks goes through here.
        """
        window = self.search_window
        if window is None:
            return None
        band = getattr(window, "index_band", None)
        return band if band is not None and _is_alive(band) else None

    # -- OCR ---------------------------------------------------------------

    @Slot()
    def install_ocr(self) -> None:
        """Provision OCR in one click, then backfill deferred images."""
        if self._ocr_thread is not None and self._ocr_thread.isRunning():
            return
        band = self._live_band()
        if band is not None:
            band.show_message("Installing image text recognition…")

        thread = QThread(self)
        worker = _OcrInstallWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_ocr_progress)
        worker.finished.connect(self._on_ocr_installed)
        worker.failed.connect(self._on_ocr_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_ocr_thread_finished)
        self._ocr_thread = thread
        self._ocr_worker = worker
        thread.start()

    @Slot(str)
    def _on_ocr_progress(self, message: str) -> None:
        band = self._live_band()
        if band is not None:
            band.show_message(message)

    @Slot(object)
    def _on_ocr_installed(self, result) -> None:
        version = getattr(result, "version_token", "") or ""
        try:
            # Image-only: non-image cache and vector records are untouched, and
            # a completed initial crawl is not regressed.
            self.service.jobs.start_ocr_backfill(version)
        except Exception:
            logger.exception("Could not start the OCR backfill")

    @Slot(str)
    def _on_ocr_failed(self, message: str) -> None:
        band = self._live_band()
        if band is not None:
            band.show_message(f"OCR installation failed. {message[:160]}")

    @Slot()
    def _on_ocr_thread_finished(self) -> None:
        if self._ocr_worker is not None:
            self._ocr_worker.deleteLater()
        self._ocr_worker = None
        self._ocr_thread = None

    # -- shutdown ----------------------------------------------------------

    def _handle_sigint(self, _signum, _frame) -> None:
        self._got_sigint = True
        self.app.quit()

    @Slot()
    def shutdown(self) -> None:
        """Cooperatively stop work, then release stores and the one event loop."""
        if self.setup_window is not None and self.setup_window.thread.isRunning():
            self.setup_window.worker.cancel()
            self.setup_window.thread.quit()
            self.setup_window.thread.wait(3000)

        if self._ocr_worker is not None:
            self._ocr_worker.cancel()
        if self._ocr_thread is not None and self._ocr_thread.isRunning():
            self._ocr_thread.quit()
            self._ocr_thread.wait(3000)

        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

        try:
            # On timeout this leaves `running` persisted on purpose: next launch
            # recovers it to `paused` and resumes safely.
            self.service.shutdown()
        except Exception:
            logger.exception("Error during application shutdown")

        if (
            self.search_window is not None
            and self.search_window._hotkey_listener is not None
        ):
            self.search_window._hotkey_listener.stop()

    @property
    def interrupted(self) -> bool:
        return self._got_sigint

    # -- packaged startup probe --------------------------------------------

    def startup_report(self):
        """Describe what this controller reached, for the packaged-startup probe.

        Read from the controller's own windows rather than from a flag set at
        launch, so the report reflects what was actually constructed.
        """
        from haydar.startup_probe import build_report

        if self.search_window is not None:
            view = "search"
        elif self.setup_window is not None:
            view = "onboarding"
        else:
            view = "none"

        phase = ""
        detail = ""
        window = self.setup_window
        if window is not None and _is_alive(window):
            progress = window.last_progress
            if progress is not None:
                phase = getattr(progress.phase, "value", str(progress.phase))
                detail = progress.message
            if window.failure_message:
                detail = window.failure_message
        return build_report(
            view=view,
            setup_started=bool(phase),
            setup_phase=phase,
            setup_detail=detail,
        )


def _run_startup_probe(app: QApplication, controller: GuiApplicationController) -> None:
    """Report what the packaged GUI reached, then end the one event loop.

    The probe drives the run rather than sleeping through it: it waits for the
    setup worker to report its first phase, cancels it, waits for the worker
    thread to actually finish, and only then writes the report and quits.

    Both halves matter. Waiting for a phase is what makes "onboarding reached
    setup" an observation instead of a guess. Cancelling immediately afterwards
    stops the pipeline at its next checkpoint, which is well before the phase
    that downloads the embedding model — a probe must not need the network, and
    quitting while that download held a worker thread is what made an early
    version of this probe tear the process down with a fast-fail exit code.
    """
    runner = _StartupProbeRunner(app, controller)
    runner.start()
    # Parented to the app so it outlives this function without a module global.
    runner.setParent(app)


class _StartupProbeRunner(QObject):
    """Poll the controller until startup can be reported, then quit the app."""

    def __init__(self, app: QApplication, controller: GuiApplicationController) -> None:
        super().__init__()
        self._app = app
        self._controller = controller
        self._elapsed_ms = 0
        self._cancel_requested = False
        self._timer = QTimer(self)
        self._timer.setInterval(STARTUP_PROBE_POLL_MS)
        self._timer.timeout.connect(self._poll)

    def start(self) -> None:
        self._timer.start()

    def _setup_window(self):
        window = self._controller.setup_window
        return window if window is not None and _is_alive(window) else None

    @Slot()
    def _poll(self) -> None:
        self._elapsed_ms += STARTUP_PROBE_POLL_MS
        timed_out = self._elapsed_ms >= STARTUP_PROBE_DEADLINE_MS

        # A migrated ready profile never shows onboarding; there is no setup
        # worker to wind down, so the observation is complete immediately.
        if self._controller.search_window is not None:
            self._finish([])
            return

        window = self._setup_window()
        if window is None:
            if timed_out:
                self._finish(["no top-level window appeared before the deadline"])
            return

        if not self._cancel_requested:
            reached = window.last_progress is not None or bool(window.failure_message)
            if not reached:
                if timed_out:
                    self._finish(["setup never reported a phase before the deadline"])
                return
            window.worker.cancel()
            window.thread.quit()
            self._cancel_requested = True
            return

        if window.thread.isFinished():
            self._finish([])
        elif timed_out:
            self._finish(["the setup worker did not stop before the deadline"])

    def _finish(self, errors: list[str]) -> None:
        from haydar.startup_probe import StartupReport, write_report

        self._timer.stop()
        try:
            report = self._controller.startup_report()
            report.errors.extend(errors)
        except Exception as exc:
            logger.exception("Startup probe could not build its report")
            report = StartupReport(errors=[*errors, f"{type(exc).__name__}: {exc}"])
        write_report(report)
        self._app.quit()


def _build_service(config: HaydarConfig) -> ApplicationService:
    """Construct the application service with its real collaborators.

    The watcher factory lives in the service layer, so this module needs no
    dependency on the indexer package beyond the value types re-exported
    through ``search``.
    """
    return ApplicationService(config, watcher_factory=default_watcher_factory)


def run_gui_application(config: HaydarConfig) -> None:
    """Create one Qt application and run onboarding/search within it."""
    from haydar.startup_probe import is_probing

    _configure_qt_dpi_policy()
    # Reusing an existing instance supports tests and embedded callers;
    # production always constructs exactly one.
    app = QApplication.instance() or QApplication(sys.argv)
    app.setFont(QFont("Inter", 10))
    controller = GuiApplicationController(app, config)
    # Armed before start() so a probe run still ends on its own if startup
    # raises after the windows are constructed.
    if is_probing():
        _run_startup_probe(app, controller)
    controller.start()
    app.exec()
    if controller.interrupted:
        raise KeyboardInterrupt

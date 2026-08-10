"""First-run setup UI for the windowed Haydar application."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from haydar.config import HaydarConfig
from haydar.setup import SetupCancelled, SetupCoordinator, SetupProgress


class SetupWorker(QObject):
    """Run first-run provisioning away from the Qt GUI thread."""

    progress = Signal(object)
    ready = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, config: HaydarConfig) -> None:
        super().__init__()
        self.config = config
        self.cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            result = SetupCoordinator(
                self.config,
            ).prepare_search(
                progress_callback=self.progress.emit,
                cancel_event=self.cancel_event,
            )
            self.ready.emit(result)
        except SetupCancelled:
            self.failed.emit("Setup was cancelled. Launch Haydar again to resume.")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def cancel(self) -> None:
        self.cancel_event.set()


class SetupWindow(QWidget):
    """Small non-modal setup view that hands off to search when ready."""

    ready = Signal(object)
    cancelled = Signal()

    def __init__(self, config: HaydarConfig) -> None:
        super().__init__()
        self.config = config
        # The most recent event from the worker, and the failure text if it
        # stopped. Held as state rather than only rendered into a label so a
        # caller can ask how far setup got without racing the signal.
        self.last_progress: SetupProgress | None = None
        self.failure_message: str = ""
        self.setWindowTitle("Haydar setup")
        self.setMinimumWidth(440)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        self.title_label = QLabel("Preparing Haydar")
        self.title_label.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.status_label = QLabel("Setting up search…")
        self.status_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.cancel_button)

        self.thread = QThread(self)
        self.worker = SetupWorker(config)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.ready.connect(self._on_ready)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_finished)
        self.thread.start()

    @Slot(object)
    def _on_progress(self, progress: SetupProgress) -> None:
        self.last_progress = progress
        self.status_label.setText(progress.message)

    @Slot(object)
    def _on_ready(self, config: HaydarConfig) -> None:
        self.cancel_button.setEnabled(False)
        self.ready.emit(config)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.failure_message = message
        self.status_label.setText(message)
        self.cancel_button.setText("Close")
        self.cancel_button.setEnabled(True)
        self.cancel_button.clicked.disconnect()
        self.cancel_button.clicked.connect(self.close)

    @Slot()
    def _cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Stopping setup…")
        self.worker.cancel()
        self.cancelled.emit()

    @Slot()
    def _on_thread_finished(self) -> None:
        self.worker.deleteLater()

    def closeEvent(self, event) -> None:
        if self.thread.isRunning():
            self.worker.cancel()
            self.thread.quit()
            self.thread.wait(3000)
        super().closeEvent(event)

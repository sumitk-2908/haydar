"""Compact status band for partial-index state.

A full-width band rather than a modal: the product contract is that search and
settings stay usable in every indexing state, so this can never block input. It
renders whatever snapshot it is handed and emits user intent as signals; it does
not decide what a state means.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QWidget,
)

from haydar.search.indexing_status import (
    IndexSnapshot,
    JobKind,
    JobOutcome,
    JobPhase,
    deferred_message,
    describe,
)
from haydar.ui.theme import ThemeColors

# Minimum logical hit target for the icon-style controls.
_MIN_BUTTON = 24


class IndexStatusBand(QWidget):
    """Presentation and user commands for background indexing."""

    pause_requested = Signal()
    resume_requested = Signal()
    cancel_requested = Signal()
    retry_requested = Signal()
    install_ocr_requested = Signal()
    view_log_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("indexStatusBand")
        self._snapshot: IndexSnapshot | None = None
        self._resumed = False
        # Controls are disabled only while the transition they requested is
        # still awaiting acknowledgement, never as a general "busy" state.
        self._awaiting: str | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.message_label = QLabel()
        self.message_label.setTextFormat(Qt.PlainText)
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet(
            f"color: {ThemeColors.TEXT_INFO_ALPHA}; font-size: 11px; border: none;"
        )
        self.message_label.setAccessibleName("Background indexing status")
        layout.addWidget(self.message_label, 1)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Indexing progress")
        self.progress.setStyleSheet(
            "QProgressBar { background: transparent; border: none; } "
            "QProgressBar::chunk { background: #6366f1; }"
        )
        self.progress.hide()

        self.pause_button = self._make_button(
            "Pause", "Pause indexing", "Pause background indexing"
        )
        self.pause_button.clicked.connect(self._on_pause)
        self.resume_button = self._make_button(
            "Resume", "Resume indexing", "Resume background indexing"
        )
        self.resume_button.clicked.connect(self._on_resume)
        self.cancel_button = self._make_button(
            "Cancel", "Cancel indexing", "Stop indexing; indexed files stay searchable"
        )
        self.cancel_button.clicked.connect(self._on_cancel)
        self.retry_button = self._make_button(
            "Retry", "Retry indexing", "Try background indexing again"
        )
        self.retry_button.clicked.connect(self.retry_requested.emit)
        self.log_button = self._make_button(
            "View log", "View log", "Open the Haydar log file"
        )
        self.log_button.clicked.connect(self.view_log_requested.emit)
        self.ocr_button = self._make_button(
            "Install OCR", "Install OCR", "Install image text recognition"
        )
        self.ocr_button.clicked.connect(self.install_ocr_requested.emit)

        for button in self._all_buttons():
            layout.addWidget(button)
            button.hide()

        self.hide()

    def _make_button(self, text: str, name: str, description: str) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumSize(_MIN_BUTTON, _MIN_BUTTON)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(description)
        button.setAccessibleName(name)
        button.setAccessibleDescription(description)
        button.setStyleSheet(
            "QPushButton { border: 1px solid rgba(255,255,255,0.2); "
            "border-radius: 4px; padding: 2px 8px; background: transparent; "
            f"color: {ThemeColors.TEXT_PRIMARY}; font-size: 11px; }} "
            "QPushButton:hover, QPushButton:focus { border-color: #00d4ff; } "
            "QPushButton:disabled { color: rgba(255,255,255,0.35); }"
        )
        return button

    def _all_buttons(self) -> tuple[QPushButton, ...]:
        return (
            self.pause_button,
            self.resume_button,
            self.cancel_button,
            self.retry_button,
            self.log_button,
            self.ocr_button,
        )

    # -- commands ----------------------------------------------------------

    def _on_pause(self) -> None:
        self._awaiting = "pause"
        self.pause_button.setEnabled(False)
        self.pause_requested.emit()

    def _on_resume(self) -> None:
        self._awaiting = "resume"
        self.resume_button.setEnabled(False)
        self._resumed = True
        self.resume_requested.emit()

    def _on_cancel(self) -> None:
        self._awaiting = "cancel"
        self.cancel_button.setEnabled(False)
        self.cancel_requested.emit()

    # -- rendering ---------------------------------------------------------

    def update_snapshot(self, snapshot: IndexSnapshot) -> None:
        """Render a job snapshot. Never disables search or settings."""
        self._snapshot = snapshot
        message = describe(snapshot, resumed=self._resumed and snapshot.outcome is None)
        self.message_label.setText(message)
        self.message_label.setAccessibleDescription(message)

        outcome = snapshot.outcome
        active = outcome is None

        # A percentage is only honest once discovery has finished; until then an
        # indeterminate bar plus a discovered count is the truthful display.
        if active:
            self.progress.show()
            if snapshot.has_stable_total:
                total = snapshot.discovered + snapshot.skipped_unchanged
                done = snapshot.examined + snapshot.skipped_unchanged
                self.progress.setRange(0, max(total, 1))
                self.progress.setValue(min(done, max(total, 1)))
            else:
                self.progress.setRange(0, 0)
        else:
            self.progress.hide()

        pausing = snapshot.phase in (JobPhase.PAUSING, JobPhase.CANCELLING)
        self._set_visible(
            pause=active and not pausing,
            resume=outcome in (JobOutcome.PAUSED, JobOutcome.CANCELLED),
            cancel=active,
            retry=outcome is JobOutcome.FAILED,
            log=outcome is JobOutcome.FAILED,
            ocr=snapshot.ocr_deferred > 0 and snapshot.kind is not JobKind.OCR_BACKFILL,
        )

        if snapshot.ocr_deferred > 0 and snapshot.kind is not JobKind.OCR_BACKFILL:
            suffix = deferred_message(snapshot.ocr_deferred)
            combined = f"{message} {suffix}"
            self.message_label.setText(combined)
            self.message_label.setAccessibleDescription(combined)

        # The acknowledgement arrived, so controls become live again.
        if outcome is not None or pausing:
            self._awaiting = None
        if outcome is not None:
            self._resumed = False

        for button in self._all_buttons():
            if button.isVisible():
                button.setEnabled(True)
        if self._awaiting == "pause":
            self.pause_button.setEnabled(False)
        elif self._awaiting == "cancel":
            self.cancel_button.setEnabled(False)

        self.show()

    def _set_visible(
        self,
        *,
        pause: bool,
        resume: bool,
        cancel: bool,
        retry: bool,
        log: bool,
        ocr: bool,
    ) -> None:
        self.pause_button.setVisible(pause)
        self.resume_button.setVisible(resume)
        self.cancel_button.setVisible(cancel)
        self.retry_button.setVisible(retry)
        self.log_button.setVisible(log)
        self.ocr_button.setVisible(ocr)

    def show_message(self, message: str) -> None:
        """Show a plain message with no controls (used for setup handoff)."""
        self.message_label.setText(message)
        self.message_label.setAccessibleDescription(message)
        self.progress.hide()
        self._set_visible(
            pause=False, resume=False, cancel=False, retry=False, log=False, ocr=False
        )
        self.show()

    def collapse(self) -> None:
        """Hide the band once a completed state has been shown briefly."""
        self.hide()

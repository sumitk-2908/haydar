import logging
import os
import signal
import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import haydar
from haydar.config import HaydarConfig
from haydar.search.hybrid import HybridSearch, SearchResult
from haydar.search.staleness import estimate_unindexed_count
from haydar.ui.hotkey import HotkeyListener
from haydar.ui.index_status import IndexStatusBand
from haydar.ui.results import ResultsList
from haydar.ui.theme import ThemeColors
from haydar.updater import get_latest_version, get_release_url, is_newer


def _configure_qt_dpi_policy() -> None:
    """Configure Qt 6 fractional scaling before QApplication construction."""
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )


class UpdateCheckWorker(QObject):
    update_available = Signal(str)   # emits latest version string
    checked = Signal(float)
    finished = Signal()

    def __init__(self, config: HaydarConfig):
        super().__init__()
        self.config = config

    def check(self):
        try:
            if self.config.update_check_interval_hours == 0:
                return
            if not getattr(sys, "frozen", False):
                return
            now = time.time()
            if now < self.config.update_check_snoozed_until:
                return
            elapsed = now - self.config.last_update_check
            if 0 <= elapsed < self.config.update_check_interval_hours * 3600:
                return
            latest = get_latest_version()
            if latest is None:
                return
            if is_newer(latest, haydar.__version__):
                self.update_available.emit(latest)
            # Persist on the GUI thread so a background write cannot race with
            # settings changes or overwrite newer in-memory configuration.
            self.checked.emit(time.time())
        except Exception:
            logging.getLogger(__name__).exception("Background update check failed")
        finally:
            self.finished.emit()

class _StalenessWorker(QObject):
    result_ready = Signal(int)
    finished = Signal()

    def __init__(self, config: HaydarConfig):
        super().__init__()
        self.config = config

    def run(self) -> None:
        try:
            count = estimate_unindexed_count(self.config.folders, self.config)
            self.result_ready.emit(count)
        except Exception:
            logging.getLogger(__name__).exception("Background staleness check failed")
        finally:
            self.finished.emit()


class SearchWorker(QObject):
    finished = Signal(list)
    skipped_files = Signal(list)
    error_occurred = Signal(str)

    def __init__(self, search_engine: HybridSearch):
        super().__init__()
        self.search_engine = search_engine
        self.cancel_event = threading.Event()
        self.rg_process = None

    def cancel(self):
        self.cancel_event.set()
        if self.rg_process:
            try:
                self.rg_process.kill()
                if self.rg_process.stdout:
                    self.rg_process.stdout.close()
                self.rg_process.wait(timeout=1.0)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Error killing ripgrep: {e}")
            self.rg_process = None

    def do_search(self, query: str, mode: str, limit: int):
        self.cancel_event.clear()
        if not query.strip():
            self.finished.emit([])
            return
        try:
            for results in self.search_engine.search_stream(query, limit=limit, mode=mode, cancel_event=self.cancel_event, worker=self):
                if self.cancel_event.is_set():
                    break
                self.finished.emit(results)
        except Exception as e:
            if not self.cancel_event.is_set():
                import logging
                logging.getLogger(__name__).error(f"Search failed: {e}")
                hint = getattr(e, "hint", None)
                msg = f"{e}\n{hint}" if hint else str(e)
                self.error_occurred.emit(msg)
                self.finished.emit([])

class SearchWindow(QWidget):
    """Floating search window using Qt logical pixels for all geometry.

    Qt widget coordinates are device-independent (logical) pixels.  Native
    Win32 DPI awareness is configured separately at process startup; widget
    dimensions must not be multiplied by ``devicePixelRatio()``.
    """

    BASE_WIDTH = 700
    MIN_WIDTH = 320
    BASE_EMPTY_HEIGHT = 110
    BASE_RESULTS_HEIGHT = 530
    BASE_ERROR_HEIGHT = 160
    SCREEN_MARGIN = 16
    VERTICAL_OFFSET = 200

    search_requested = Signal(str, str, int)
    toggle_requested = Signal()

    def __init__(self, config: HaydarConfig):
        super().__init__()
        self.config = config
        self.toggle_requested.connect(self.toggle)
        self._always_on_top = config.always_on_top
        self._hotkey_listener = None
        self._settings_window = None
        self._whatsnew_banner: QWidget | None = None
        self._staleness_thread: QThread | None = None
        self._staleness_worker: _StalenessWorker | None = None

        # Setup UI properties
        flags = Qt.FramelessWindowHint | Qt.Tool
        if self._always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setWindowTitle("Haydar — File Search")
        self.setWindowOpacity(self.config.window_opacity / 100.0)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # A temporary fixed construction size is deliberately avoided. The
        # layout establishes the initial size after all controls are present.
        self.resize(self.BASE_WIDTH, self.BASE_EMPTY_HEIGHT)

        # Search engine & threading
        self.search_mode = "semantic"  # default mode
        self.engine_error: str | None = None
        try:
            self.search_engine = HybridSearch(self.config)
        except Exception as exc:
            # A missing model or corrupt DB must not crash the Qt process.
            # Keep the window alive and surface the error on first search.
            import logging
            logging.getLogger(__name__).error("Failed to init search engine: %s", exc)
            hint = getattr(exc, "hint", None)
            self.engine_error = f"{exc}" + (f"\n{hint}" if hint else "")
            self.search_engine = None

        self.search_thread = None
        self.search_worker = None
        if self.search_engine is not None:
            self.search_thread = QThread()
            self.search_worker = SearchWorker(self.search_engine)
            self.search_worker.moveToThread(self.search_thread)
            self.search_requested.connect(self.search_worker.do_search)
            self.search_worker.finished.connect(self.on_search_results)
            self.search_worker.skipped_files.connect(self.on_skipped_files)
            self.search_worker.error_occurred.connect(self.on_search_error)
            self.search_thread.start()

        self.setup_ui()
        self._start_staleness_check()

        self._update_thread: QThread | None = None
        self._update_worker: UpdateCheckWorker | None = None
        self._start_update_check()

        # Debounce timer
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(int(self.config.watcher_debounce_seconds * 1000) if hasattr(self, 'config') else 300)
        self.search_timer.timeout.connect(self._trigger_search)

        # Drag state
        self.drag_pos = None

        from haydar import __version__

        if self.config.last_seen_version == "":
            # First install — record silently, no banner. A persistence failure
            # must not prevent the application from starting.
            self.config.last_seen_version = __version__
            try:
                self.config.save()
            except OSError:
                self.config.last_seen_version = ""
                logging.getLogger(__name__).exception(
                    "Could not persist the first-launch version"
                )
        elif self.config.last_seen_version != __version__:
            self._show_whatsnew_banner(__version__)

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)

        # Container
        self.container = QFrame()
        # Transparent background so it doesn't cover the window's paintEvent
        self.container.setStyleSheet("""
            QFrame {
                background: transparent;
                border: none;
            }
        """)

        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 153)) # 60% opacity
        shadow.setOffset(0, 10)
        self.container.setGraphicsEffect(shadow)

        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(16, 16, 16, 16)
        container_layout.setSpacing(12)

        # Update Banner
        self._update_banner = QWidget()
        self._update_banner.setStyleSheet("border: 1px solid rgba(245, 158, 11, 0.5); border-radius: 6px; padding: 4px 8px;")
        banner_layout = QHBoxLayout(self._update_banner)
        banner_layout.setContentsMargins(0, 0, 0, 0)

        self._update_label = QLabel()
        self._update_label.setTextFormat(Qt.PlainText)
        self._update_label.setWordWrap(True)
        self._update_label.setStyleSheet(f"border: none; color: {ThemeColors.TEXT_PRIMARY};")
        self._update_label.setAccessibleName("Update available status")

        self._download_btn = QPushButton("Download")
        self._download_btn.setStyleSheet(f"border: 1px solid rgba(245, 158, 11, 0.5); border-radius: 4px; padding: 2px 8px; background: transparent; color: {ThemeColors.TEXT_PRIMARY};")
        self._download_btn.setAccessibleName("Download update")
        self._download_btn.setAccessibleDescription("Download the latest release from GitHub")

        self._dismiss_btn = QPushButton("×")
        self._dismiss_btn.setStyleSheet(f"border: none; font-weight: bold; font-size: 16px; padding: 0 4px; color: {ThemeColors.TEXT_INFO_ALPHA}; background: transparent;")
        self._dismiss_btn.setAccessibleName("Dismiss update")
        self._dismiss_btn.setAccessibleDescription("Hide this update notification")

        banner_layout.addWidget(self._update_label)
        banner_layout.addStretch()
        banner_layout.addWidget(self._download_btn)
        banner_layout.addWidget(self._dismiss_btn)
        self._update_banner.hide()
        container_layout.addWidget(self._update_banner)

        self._staleness_banner = QWidget()
        self._staleness_banner.setObjectName("stalenessBanner")
        self._staleness_banner.setStyleSheet(
            "border: 1px solid rgba(234,179,8,0.4); "
            "border-radius: 6px; padding: 4px 8px;"
        )
        staleness_layout = QHBoxLayout(self._staleness_banner)
        staleness_layout.setContentsMargins(0, 0, 0, 0)
        self._staleness_label = QLabel()
        self._staleness_label.setTextFormat(Qt.PlainText)
        self._staleness_label.setWordWrap(True)
        self._staleness_label.setStyleSheet("border: none;")
        self._staleness_label.setAccessibleName("Index freshness status")
        self._dismiss_staleness_btn = QPushButton("×")
        self._dismiss_staleness_btn.setObjectName("dismissStalenessButton")
        self._dismiss_staleness_btn.setAccessibleName("Dismiss index freshness warning")
        self._dismiss_staleness_btn.setStyleSheet(
            "border: none; font-weight: bold; font-size: 16px; "
            "padding: 0 4px; background: transparent;"
        )
        self._dismiss_staleness_btn.clicked.connect(self._dismiss_staleness)
        staleness_layout.addWidget(self._staleness_label)
        staleness_layout.addStretch()
        staleness_layout.addWidget(self._dismiss_staleness_btn)
        self._staleness_banner.hide()

        # Search input, mode, and settings are layout-owned so system font and
        # style changes cannot cause the settings control to overlap content.
        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Search your files...")
        self.search_input.setAccessibleName("Search query")
        self.search_input.setAccessibleDescription("Enter text to search files")
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 12px;
                color: white;
                font-family: 'Inter', sans-serif;
                font-size: 18px;
                padding: 10px 14px;
            }
            QLineEdit:focus {
                border: 1px solid rgba(0, 212, 255, 0.5);
                background-color: rgba(255, 255, 255, 0.08);
            }
        """)
        self.search_input.textChanged.connect(self.on_text_changed)
        search_layout.addWidget(self.search_input)

        self.mode_btn = QPushButton("Semantic")
        self.mode_btn.setCursor(Qt.PointingHandCursor)
        self.mode_btn.setAccessibleName("Search mode")
        self.mode_btn.setAccessibleDescription("Current mode is Semantic. Press to toggle.")
        self.mode_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(147, 51, 234, 0.2);
                border: 1px solid rgba(147, 51, 234, 0.5);
                border-radius: 12px;
                color: {ThemeColors.MODE_SEMANTIC};
                font-weight: bold;
                padding: 10px 15px;
            }}
            QPushButton:hover {{
                background-color: rgba(147, 51, 234, 0.3);
            }}
        """)
        self.mode_btn.clicked.connect(self.toggle_search_mode)
        search_layout.addWidget(self.mode_btn)

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setMinimumSize(24, 24)
        self.settings_btn.setMaximumSize(32, 32)
        self.settings_btn.setToolTip("Open Haydar settings (Ctrl+,)")
        self.settings_btn.setAccessibleName("Settings")
        self.settings_btn.setAccessibleDescription("Open Haydar configuration window")
        self.settings_btn.setStyleSheet("""
            QPushButton {
                border: 1px solid transparent;
                background: transparent;
                font-size: 14px;
            }
            QPushButton:hover, QPushButton:focus {
                color: #d8b4fe;
                border-color: #00d4ff;
            }
        """)
        self.settings_btn.clicked.connect(self._show_settings)
        search_layout.addWidget(self.settings_btn)

        container_layout.addLayout(search_layout)
        container_layout.addWidget(self._staleness_banner)

        self.index_band = IndexStatusBand()
        self.index_band.hide()
        container_layout.addWidget(self.index_band)

        # Scroll area for results
        self.scroll_area = QScrollArea()
        self.scroll_area.setAccessibleName("Search results area")
        self.scroll_area.setAccessibleDescription("Scrollable area containing matching files")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.2);
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.3);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        self.results_list = ResultsList()
        self.results_list.result_activated.connect(self.open_file)
        self.scroll_area.setWidget(self.results_list)
        self.scroll_area.hide()
        container_layout.addWidget(self.scroll_area)

        # Status and Skipped
        status_layout = QHBoxLayout()

        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setStyleSheet(f"color: {ThemeColors.TEXT_INFO_ALPHA}; font-size: 11px;")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Search status")
        self.status_label.hide()

        self.skipped_label = QLabel()
        self.skipped_label.setTextFormat(Qt.PlainText)
        self.skipped_label.setStyleSheet(f"color: {ThemeColors.TEXT_ERROR_ALPHA}; font-size: 11px;")
        self.skipped_label.setAccessibleName("Skipped files warning")
        self.skipped_label.hide()

        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        status_layout.addWidget(self.skipped_label)

        container_layout.addLayout(status_layout)

        main_layout.addWidget(self.container)

        # The band owns its own progress indicator, driven by job snapshots
        # rather than by polling for a lock file.
        main_layout.addWidget(self.index_band.progress)

        QWidget.setTabOrder(self.search_input, self.mode_btn)
        QWidget.setTabOrder(self.mode_btn, self._download_btn)
        QWidget.setTabOrder(self._download_btn, self._dismiss_btn)
        QWidget.setTabOrder(self._dismiss_btn, self.settings_btn)

    def _target_screen(self):
        """Return the screen relevant to this invocation, never an unrelated primary."""
        screen = QApplication.screenAt(QCursor.pos())
        if screen is None and self.windowHandle() is not None:
            screen = self.windowHandle().screen()
        return screen or QApplication.primaryScreen()

    def _logical_width(self, screen=None) -> int:
        """Return design width clamped to the target work area in logical pixels."""
        screen = screen or self._target_screen()
        if screen is None:
            return self.BASE_WIDTH
        available_width = max(1, screen.availableGeometry().width() - 2 * self.SCREEN_MARGIN)
        return min(self.BASE_WIDTH, available_width)

    def _set_logical_size(self, width: int, height: int, screen=None) -> None:
        """Fit logical dimensions to the target screen without DPR multiplication."""
        logical_width = max(1, int(width))
        logical_height = max(1, int(height))
        screen = screen or self._target_screen()
        if screen is not None:
            available = screen.availableGeometry()
            logical_width = min(logical_width, max(1, available.width()))
            logical_height = min(logical_height, max(1, available.height()))
        self.setFixedSize(logical_width, logical_height)

    def _set_content_height(self, base_height: int, screen=None) -> None:
        """Size the active state while allowing wrapped dynamic text to expand."""
        self._current_base_height = base_height
        self.layout().activate()
        extra_height = 0
        # isVisible() is false while the top-level window is hidden, even when a
        # child is intended to appear at the next show. isHidden() tracks the
        # child's explicit state and therefore also works during construction.
        if not self._update_banner.isHidden():
            extra_height += self._update_banner.sizeHint().height() + 12
        if not self._staleness_banner.isHidden():
            extra_height += 40
        if self._whatsnew_banner is not None and not self._whatsnew_banner.isHidden():
            extra_height += 40
        if not self.index_band.isHidden():
            extra_height += self.index_band.sizeHint().height() + 8
        if not self.status_label.isHidden():
            one_line_height = self.status_label.fontMetrics().lineSpacing()
            # The error baseline reserves room for the message, log path, and
            # one platform-dependent wrap line. Longer text may still expand it.
            baseline_lines = 3 if base_height == self.BASE_ERROR_HEIGHT else 1
            status_height_budget = baseline_lines * one_line_height + max(
                0, base_height - self.BASE_EMPTY_HEIGHT
            )
            extra_height += max(
                0, self.status_label.sizeHint().height() - status_height_budget
            )
        self._set_logical_size(
            self._logical_width(screen), base_height + extra_height, screen
        )

    def set_indexing_status(self, message: str | None) -> None:
        """Show a plain indexing message.

        Retained for callers that only need to display text; structured job
        state goes through ``index_band.update_snapshot`` instead.
        """
        if message:
            self.index_band.show_message(message)
        else:
            self.index_band.collapse()
        self.refresh_layout()

    def refresh_layout(self) -> None:
        """Recompute window height after a banner or the status band changes."""
        self._set_content_height(
            getattr(self, "_current_base_height", self.BASE_EMPTY_HEIGHT)
        )

    def _start_staleness_check(self) -> None:
        """Estimate index staleness without delaying window construction."""
        thread = QThread(self)
        worker = _StalenessWorker(self.config)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result_ready.connect(self._on_staleness_result)
        worker.finished.connect(thread.quit)
        self._staleness_thread = thread
        self._staleness_worker = worker
        thread.start()

    def _on_staleness_result(self, count: int) -> None:
        if count <= 0:
            return
        message = (
            f"~{count} files may be unindexed. "
            "Run haydar watch to keep results current."
        )
        self._staleness_label.setText(message)
        self._staleness_label.setAccessibleDescription(message)
        self._staleness_banner.show()
        self._set_content_height(
            getattr(self, "_current_base_height", self.BASE_EMPTY_HEIGHT)
        )

    def _dismiss_staleness(self) -> None:
        self._staleness_banner.hide()
        self._set_content_height(
            getattr(self, "_current_base_height", self.BASE_EMPTY_HEIGHT)
        )

    def _start_update_check(self) -> None:
        """Start the one-shot update worker behind a patchable lifecycle boundary."""
        thread = QThread(self)
        worker = UpdateCheckWorker(self.config)
        worker.moveToThread(thread)
        thread.started.connect(worker.check)
        worker.update_available.connect(self.on_update_available)
        worker.checked.connect(self._record_update_check)
        worker.finished.connect(thread.quit)
        self._update_thread = thread
        self._update_worker = worker
        thread.start()

    def _record_update_check(self, checked_at: float) -> None:
        self.config.last_update_check = checked_at
        try:
            self.config.save()
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not persist the update-check timestamp"
            )


    def on_update_available(self, version: str) -> None:
        self._available_version = version
        self._update_label.setText(f"Haydar {version} is available.")
        self._update_label.setAccessibleDescription(f"Version {version} is available for download")

        if not getattr(self, "_update_actions_connected", False):
            self._download_btn.clicked.connect(self._download_update)
            self._dismiss_btn.clicked.connect(self._dismiss_update)
            self._update_actions_connected = True

        self._current_base_height = getattr(self, "_current_base_height", self.BASE_EMPTY_HEIGHT)
        self._update_banner.show()
        self._set_content_height(self._current_base_height)

    def _download_update(self) -> None:
        try:
            os.startfile(get_release_url(self._available_version))
        except OSError:
            logging.getLogger(__name__).exception("Could not open release page")

    def _dismiss_update(self) -> None:
        self._update_banner.hide()
        self.config.update_check_snoozed_until = time.time() + 7 * 86400
        self.config.save()
        self._set_content_height(self._current_base_height)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.fillRect(self.rect(), Qt.transparent)

        rect = self.rect().adjusted(15, 15, -15, -15)

        bg_color = QColor(20, 20, 30, 235)
        painter.setBrush(QBrush(bg_color))

        pen = QPen(QColor(255, 255, 255, 20))
        pen.setWidth(1)
        painter.setPen(pen)

        painter.drawRoundedRect(rect, 16, 16)

    def toggle_search_mode(self):
        if self.search_mode == "semantic":
            self.search_mode = "keyword"
            self.mode_btn.setText("Keyword")
            self.mode_btn.setAccessibleName("Search mode: Keyword")
            self.mode_btn.setAccessibleDescription("Current mode is Keyword. Press to toggle.")
            self.mode_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba(16, 185, 129, 0.2);
                    border: 1px solid rgba(16, 185, 129, 0.5);
                    border-radius: 12px;
                    color: {ThemeColors.MODE_KEYWORD};
                    font-weight: bold;
                    padding: 10px 15px;
                }}
                QPushButton:hover {{
                    background-color: rgba(16, 185, 129, 0.3);
                }}
            """)
        else:
            self.search_mode = "semantic"
            self.mode_btn.setText("Semantic")
            self.mode_btn.setAccessibleName("Search mode: Semantic")
            self.mode_btn.setAccessibleDescription("Current mode is Semantic. Press to toggle.")
            self.mode_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba(147, 51, 234, 0.2);
                    border: 1px solid rgba(147, 51, 234, 0.5);
                    border-radius: 12px;
                    color: {ThemeColors.MODE_SEMANTIC};
                    font-weight: bold;
                    padding: 10px 15px;
                }}
                QPushButton:hover {{
                    background-color: rgba(147, 51, 234, 0.3);
                }}
            """)
        self._trigger_search()
        self.search_input.setFocus()

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.search_input.clear()
            self.results_list.set_results([], "")
            self.scroll_area.hide()
            self.status_label.hide()
            self.skipped_label.hide()
            screen = self._target_screen()
            self._set_content_height(self.BASE_EMPTY_HEIGHT, screen)

            if screen is not None:
                available = screen.availableGeometry()
                # Sizing happens against this same work area, so the full window
                # is reachable even on portrait/tiny or heterogeneous monitors.
                fitted_x = available.left() + (available.width() - self.width()) // 2
                fitted_y = available.top() + (available.height() - self.height()) // 2
                max_x = available.right() - self.width() + 1
                max_y = available.bottom() - self.height() + 1
                x = min(max(fitted_x, available.left()), max_x)
                y = min(max(fitted_y, available.top()), max_y)
                # Apply the preferred upward offset only within safe bounds.
                y = max(available.top(), y - self.VERTICAL_OFFSET)
                self.move(x, y)

            self.show()
            self.activateWindow()
            self.search_input.setFocus()

    def on_text_changed(self, text: str):
        if not text.strip():
            self.search_timer.stop()
            if self.search_worker is not None:
                self.search_worker.cancel()
            self.results_list.set_results([], "")
            self.scroll_area.hide()
            self.status_label.hide()
            self.skipped_label.hide()
            self._set_content_height(self.BASE_EMPTY_HEIGHT)
        else:
            self.search_timer.start()

    def _trigger_search(self):
        query = self.search_input.text().strip()

        # If the search engine failed to initialize, surface the error instead.
        if self.search_worker is None:
            if query and self.engine_error:
                self.on_search_error(self.engine_error)
            return

        # Cancel any inflight search
        self.search_worker.cancel()

        if query:
            self.search_requested.emit(query, self.search_mode, self.config.results_limit)

    def on_search_results(self, results: list[SearchResult]):
        query = self.search_input.text().strip()
        self.results_list.set_results(results, query)

        # Reset status label styling in case of previous errors
        self.status_label.setStyleSheet(f"color: {ThemeColors.TEXT_INFO_ALPHA}; font-size: 11px;")

        if results:
            self.scroll_area.show()
            self.status_label.setText(f"{len(results)} results found")
            self.status_label.setAccessibleDescription(f"{len(results)} results found")
            self.status_label.show()
            self._set_content_height(self.BASE_RESULTS_HEIGHT)
        else:
            self.scroll_area.hide()
            # A successful empty search replaces any previous error state.
            self.status_label.setText("No results found")
            self.status_label.setAccessibleDescription("No results found")
            self.status_label.show()
            self._set_content_height(self.BASE_EMPTY_HEIGHT)

    def on_search_error(self, message: str):
        """Handle errors from the search worker."""
        from haydar.config import get_log_path

        log_msg = f"Full log: {get_log_path()}"
        if log_msg not in message:
            message = f"{message}\n{log_msg}"

        self.status_label.setText(f"Error: {message}")
        self.status_label.setAccessibleDescription("Error: " + message)
        self.status_label.setStyleSheet(f"color: {ThemeColors.TEXT_ERROR_ALPHA}; font-size: 12px; font-weight: bold;")
        self.status_label.show()

        # Adjust size to fit multiline error text
        self._set_content_height(self.BASE_ERROR_HEIGHT)

    def on_skipped_files(self, skipped: list[str]):
        if skipped:
            msg = f"{len(skipped)} files skipped (e.g. permission denied)"
            self.skipped_label.setText(msg)
            self.skipped_label.setAccessibleDescription(msg)
            self.skipped_label.show()
            self.skipped_label.setToolTip("\n".join(skipped[:10]) + ("\n..." if len(skipped) > 10 else ""))
        else:
            self.skipped_label.hide()

    def open_file(self, file_path: str):
        if os.path.exists(file_path):
            os.startfile(file_path)
            self.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Comma and event.modifiers() == Qt.ControlModifier:
            self._show_settings()
        elif event.key() == Qt.Key_Escape:
            self.hide()
        elif event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter or event.key() == Qt.Key_Space:
            focus_widget = QApplication.focusWidget()
            if isinstance(focus_widget, QPushButton) or self.search_input.hasFocus():
                super().keyPressEvent(event)
            else:
                result = self.results_list.get_selected_result()
                if result:
                    self.open_file(result.file_path)
        elif event.key() == Qt.Key_Down or (event.key() == Qt.Key_N and event.modifiers() == Qt.ControlModifier):
            self.results_list.select_next()
            self.ensure_selected_visible()
        elif event.key() == Qt.Key_Up or (event.key() == Qt.Key_P and event.modifiers() == Qt.ControlModifier):
            self.results_list.select_previous()
            self.ensure_selected_visible()
        else:
            super().keyPressEvent(event)

    def ensure_selected_visible(self):
        if self.results_list.selected_index >= 0 and self.results_list.items:
            item = self.results_list.items[self.results_list.selected_index]
            self.scroll_area.ensureWidgetVisible(item, 0, 0)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_pos is not None and event.buttons() == Qt.LeftButton:
            delta = event.globalPosition().toPoint() - self.drag_pos
            self.move(self.pos() + delta)
            self.drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None
        event.accept()

    def closeEvent(self, event):
        if self._staleness_thread is not None:
            self._staleness_thread.quit()
            self._staleness_thread.wait()
            if self._staleness_worker is not None:
                self._staleness_worker.deleteLater()
            self._staleness_thread.deleteLater()
            self._staleness_worker = None
            self._staleness_thread = None
        if self._update_thread is not None:
            self._update_thread.quit()
            self._update_thread.wait()
            if self._update_worker is not None:
                self._update_worker.deleteLater()
            self._update_thread.deleteLater()
            self._update_worker = None
            self._update_thread = None
        if self.search_thread is not None:
            self.search_thread.quit()
            self.search_thread.wait()
            self.search_worker.deleteLater()
            self.search_thread.deleteLater()
            self.search_worker = None
            self.search_thread = None
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
        super().closeEvent(event)

    def set_settings_window(self, window):
        self._settings_window = window

    def _show_settings(self):
        if self._settings_window:
            self._settings_window.show_settings()

    def on_config_changed(self, config: HaydarConfig) -> None:
        self.config = config
        self._restart_hotkey(config.hotkey)
        self.setWindowOpacity(config.window_opacity / 100.0)
        self.search_timer.setInterval(int(config.watcher_debounce_seconds * 1000))
        if config.always_on_top != self._always_on_top:
            # Reapplying window flags requires hide() and show(), causing a one-frame flicker.
            self.hide()
            flags = self.windowFlags()
            if config.always_on_top:
                flags |= Qt.WindowStaysOnTopHint
            else:
                flags &= ~Qt.WindowStaysOnTopHint
            self.setWindowFlags(flags)
            self.show()
            self._always_on_top = config.always_on_top

    def _restart_hotkey(self, hotkey: str) -> None:
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
        self._hotkey_listener = HotkeyListener(hotkey, self.toggle_requested.emit)
        self._hotkey_listener.start()

    def _show_whatsnew_banner(self, version: str) -> None:
        self._whatsnew_banner = QWidget()
        self._whatsnew_banner.setObjectName("whatsNewBanner")
        self._whatsnew_banner.setStyleSheet(
            "border: 1px solid rgba(99,102,241,0.5); "
            "border-radius: 6px; padding: 4px 8px;"
        )
        layout = QHBoxLayout(self._whatsnew_banner)
        layout.setContentsMargins(0, 0, 0, 0)

        self._whatsnew_label = QLabel(f"Updated to Haydar {version}!")
        self._whatsnew_label.setTextFormat(Qt.PlainText)
        self._whatsnew_label.setStyleSheet("border: none;")
        self._whatsnew_label.setAccessibleName("Haydar update status")
        self._whatsnew_label.setAccessibleDescription(
            f"Haydar was updated to version {version}"
        )

        self._see_whatsnew_btn = QPushButton("See what's new")
        self._see_whatsnew_btn.setObjectName("seeWhatsNewButton")
        self._see_whatsnew_btn.setAccessibleName("See what's new")
        self._see_whatsnew_btn.setAccessibleDescription(
            "Open settings to view changes in this Haydar version"
        )
        self._see_whatsnew_btn.setStyleSheet(
            "border: 1px solid rgba(99,102,241,0.5); border-radius: 4px; "
            "padding: 2px 8px; background: transparent;"
        )
        self._see_whatsnew_btn.clicked.connect(self._open_whats_new)

        self._dismiss_whatsnew_btn = QPushButton("×")
        self._dismiss_whatsnew_btn.setObjectName("dismissWhatsNewButton")
        self._dismiss_whatsnew_btn.setAccessibleName("Dismiss what's new")
        self._dismiss_whatsnew_btn.setAccessibleDescription(
            "Hide this version notification"
        )
        self._dismiss_whatsnew_btn.setStyleSheet(
            "border: none; font-weight: bold; font-size: 16px; "
            "padding: 0 4px; background: transparent;"
        )
        self._dismiss_whatsnew_btn.clicked.connect(self._dismiss_whats_new)

        layout.addWidget(self._whatsnew_label)
        layout.addStretch()
        layout.addWidget(self._see_whatsnew_btn)
        layout.addWidget(self._dismiss_whatsnew_btn)
        self.container.layout().insertWidget(1, self._whatsnew_banner)
        self._set_content_height(
            getattr(self, "_current_base_height", self.BASE_EMPTY_HEIGHT)
        )

    def _open_whats_new(self) -> None:
        if self._settings_window is None:
            logging.getLogger(__name__).warning(
                "Cannot open What's New because settings is unavailable"
            )
            return
        try:
            self._settings_window.show_whats_new()
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not open the What's New settings tab"
            )
            return
        if self._mark_seen():
            self._hide_whats_new_banner()

    def _dismiss_whats_new(self) -> None:
        if self._mark_seen():
            self._hide_whats_new_banner()

    def _hide_whats_new_banner(self) -> None:
        if self._whatsnew_banner is None:
            return
        self._whatsnew_banner.hide()
        self._set_content_height(
            getattr(self, "_current_base_height", self.BASE_EMPTY_HEIGHT)
        )

    def _mark_seen(self) -> bool:
        from haydar import __version__

        previous_version = self.config.last_seen_version
        self.config.last_seen_version = __version__
        try:
            self.config.save()
        except OSError:
            self.config.last_seen_version = previous_version
            logging.getLogger(__name__).exception(
                "Could not persist the acknowledged version"
            )
            return False
        return True


def create_search_window(config: HaydarConfig) -> SearchWindow:
    """Construct and wire search/settings UI inside an existing Qt application."""
    window = SearchWindow(config)

    from haydar.ui.settings import SettingsWindow

    settings_window = SettingsWindow(config)
    settings_window.config_changed.connect(window.on_config_changed)
    window.set_settings_window(settings_window)
    window._restart_hotkey(config.hotkey)
    return window


def launch_search_window(config: HaydarConfig):
    """Compatibility launcher for callers that do not own a Qt event loop."""
    from haydar.logging_setup import setup_logging

    setup_logging(console=False)
    logger = logging.getLogger(__name__)

    try:
        _configure_qt_dpi_policy()
        app = QApplication.instance() or QApplication(sys.argv)
        app.setFont(QFont("Inter", 10))
        window = create_search_window(config)

        got_sigint = False

        def sigint_handler(signum, frame):
            nonlocal got_sigint
            got_sigint = True
            app.quit()

        signal.signal(signal.SIGINT, sigint_handler)

        timer = QTimer()
        timer.timeout.connect(lambda: None)
        timer.start(200)
        window.toggle()

        try:
            app.exec()
        finally:
            if window._hotkey_listener:
                window._hotkey_listener.stop()

        if got_sigint:
            raise KeyboardInterrupt()
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.exception("Fatal error in search window")
        raise

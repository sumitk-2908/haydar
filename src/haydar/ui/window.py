import os
import sys
import signal
from PySide6.QtWidgets import (
    QApplication, QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLineEdit, 
    QPushButton, QScrollArea, QLabel, QGraphicsDropShadowEffect
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject, QPoint
from PySide6.QtGui import QPainter, QBrush, QColor, QFont, QPen

from haydar.config import HaydarConfig
from haydar.search.hybrid import HybridSearch, SearchResult
from haydar.ui.results import ResultsList
from haydar.ui.hotkey import HotkeyListener


import threading
import subprocess

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
        
    def do_search(self, query: str, mode: str):
        self.cancel_event.clear()
        if not query.strip():
            self.finished.emit([])
            return
        try:
            for results in self.search_engine.search_stream(query, mode=mode, cancel_event=self.cancel_event, worker=self):
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
    search_requested = Signal(str, str)
    toggle_requested = Signal()
    
    def __init__(self, config: HaydarConfig):
        super().__init__()
        self.config = config
        self.toggle_requested.connect(self.toggle)
        
        # Setup UI properties
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(700, 80)
        
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
        
        # Debounce timer
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self._trigger_search)
        
        # Drag state
        self.drag_pos = None

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
        
        # Search Input & Toggle
        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(10)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Search your files...")
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
        self.mode_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(147, 51, 234, 0.2);
                border: 1px solid rgba(147, 51, 234, 0.5);
                border-radius: 12px;
                color: #d8b4fe;
                font-weight: bold;
                padding: 10px 15px;
            }
            QPushButton:hover {
                background-color: rgba(147, 51, 234, 0.3);
            }
        """)
        self.mode_btn.clicked.connect(self.toggle_search_mode)
        search_layout.addWidget(self.mode_btn)
        
        container_layout.addLayout(search_layout)
        
        # Scroll area for results
        self.scroll_area = QScrollArea()
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
        self.status_label.setStyleSheet("color: rgba(255, 255, 255, 0.5); font-size: 11px;")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        
        self.skipped_label = QLabel()
        self.skipped_label.setStyleSheet("color: rgba(239, 68, 68, 0.7); font-size: 11px;")
        self.skipped_label.hide()
        
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        status_layout.addWidget(self.skipped_label)
        
        container_layout.addLayout(status_layout)
        
        main_layout.addWidget(self.container)
        
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
            self.mode_btn.setStyleSheet("""
                QPushButton {
                    background-color: rgba(16, 185, 129, 0.2);
                    border: 1px solid rgba(16, 185, 129, 0.5);
                    border-radius: 12px;
                    color: #6ee7b7;
                    font-weight: bold;
                    padding: 10px 15px;
                }
                QPushButton:hover {
                    background-color: rgba(16, 185, 129, 0.3);
                }
            """)
        else:
            self.search_mode = "semantic"
            self.mode_btn.setText("Semantic")
            self.mode_btn.setStyleSheet("""
                QPushButton {
                    background-color: rgba(147, 51, 234, 0.2);
                    border: 1px solid rgba(147, 51, 234, 0.5);
                    border-radius: 12px;
                    color: #d8b4fe;
                    font-weight: bold;
                    padding: 10px 15px;
                }
                QPushButton:hover {
                    background-color: rgba(147, 51, 234, 0.3);
                }
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
            self.setFixedSize(700, 80 + 30) # extra space for margins
            
            screen = QApplication.primaryScreen().geometry()
            self.move((screen.width() - self.width()) // 2, (screen.height() - self.height()) // 2 - 200)
            
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
            self.setFixedSize(700, 80 + 30)
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
            self.search_requested.emit(query, self.search_mode)
            
    def on_search_results(self, results: list[SearchResult]):
        query = self.search_input.text().strip()
        self.results_list.set_results(results, query)
        
        # Reset status label styling in case of previous errors
        self.status_label.setStyleSheet("color: rgba(255, 255, 255, 0.5); font-size: 11px;")
        
        if results:
            self.scroll_area.show()
            self.status_label.setText(f"{len(results)} results found")
            self.status_label.show()
            self.setFixedSize(700, 500 + 30)
        else:
            self.scroll_area.hide()
            # Only set "No results" if it's not currently displaying an error
            if "⚠️" not in self.status_label.text():
                self.status_label.setText("No results found")
                self.status_label.show()
            self.setFixedSize(700, 110 + 30)

    def on_search_error(self, message: str):
        self.scroll_area.hide()
        self.skipped_label.hide()
        self.results_list.set_results([], "")
        
        self.status_label.setText(f"⚠️ {message}")
        self.status_label.setStyleSheet("color: rgba(239, 68, 68, 0.9); font-size: 12px; font-weight: bold;")
        self.status_label.show()
        
        # Adjust size to fit multiline error text
        self.setFixedSize(700, 130 + 30)

    def on_skipped_files(self, skipped: list[str]):
        if skipped:
            self.skipped_label.setText(f"{len(skipped)} files skipped (e.g. permission denied)")
            self.skipped_label.show()
            self.skipped_label.setToolTip("\n".join(skipped[:10]) + ("\n..." if len(skipped) > 10 else ""))
        else:
            self.skipped_label.hide()

    def open_file(self, file_path: str):
        if os.path.exists(file_path):
            os.startfile(file_path)
            self.hide()
            
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        elif event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
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
        if self.search_thread is not None:
            self.search_thread.quit()
            self.search_thread.wait()
        super().closeEvent(event)


def launch_search_window(config: HaydarConfig):
    from haydar.logging_setup import setup_logging
    import logging

    # Windowed GUI has no console; persist logs to file only.
    setup_logging(console=False)
    logger = logging.getLogger(__name__)

    try:
        app = QApplication(sys.argv)

        font = QFont("Inter", 10)
        app.setFont(font)

        window = SearchWindow(config)

        hotkey_listener = HotkeyListener(config.hotkey, window.toggle_requested.emit)
        hotkey_listener.start()

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
            hotkey_listener.stop()

        if got_sigint:
            raise KeyboardInterrupt()
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.exception("Fatal error in search window")
        raise

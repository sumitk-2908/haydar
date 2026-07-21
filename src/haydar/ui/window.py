import os
import sys
from PySide6.QtWidgets import (
    QApplication, QWidget, QFrame, QVBoxLayout, QLineEdit, 
    QScrollArea, QLabel, QGraphicsDropShadowEffect
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject, QPoint
from PySide6.QtGui import QPainter, QBrush, QColor, QFont, QPen

from haydar.config import HaydarConfig
from haydar.search.hybrid import HybridSearch, SearchResult
from haydar.ui.results import ResultsList
from haydar.ui.hotkey import HotkeyListener


class SearchWorker(QObject):
    finished = Signal(list)
    
    def __init__(self, search_engine: HybridSearch):
        super().__init__()
        self.search_engine = search_engine
        
    def do_search(self, query: str):
        if not query.strip():
            self.finished.emit([])
            return
        try:
            results = self.search_engine.search(query, limit=10)
            self.finished.emit(results)
        except Exception:
            self.finished.emit([])

class SearchWindow(QWidget):
    search_requested = Signal(str)
    
    def __init__(self, config: HaydarConfig):
        super().__init__()
        self.config = config
        
        # Setup UI properties
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(700, 80)
        
        # Search engine & threading
        self.search_engine = HybridSearch(self.config)
        self.search_thread = QThread()
        self.search_worker = SearchWorker(self.search_engine)
        self.search_worker.moveToThread(self.search_thread)
        self.search_requested.connect(self.search_worker.do_search)
        self.search_worker.finished.connect(self.on_search_results)
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
        
        # Search Input
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
        container_layout.addWidget(self.search_input)
        
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
        
        # Status
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: rgba(255, 255, 255, 0.5); font-size: 11px;")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.hide()
        container_layout.addWidget(self.status_label)
        
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
        
    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.search_input.clear()
            self.results_list.set_results([], "")
            self.scroll_area.hide()
            self.status_label.hide()
            self.setFixedSize(700, 80 + 30) # extra space for margins
            
            screen = QApplication.primaryScreen().geometry()
            self.move((screen.width() - self.width()) // 2, (screen.height() - self.height()) // 2 - 200)
            
            self.show()
            self.activateWindow()
            self.search_input.setFocus()
            
    def on_text_changed(self, text: str):
        if not text.strip():
            self.search_timer.stop()
            self.results_list.set_results([], "")
            self.scroll_area.hide()
            self.status_label.hide()
            self.setFixedSize(700, 80 + 30)
        else:
            self.search_timer.start()
            
    def _trigger_search(self):
        query = self.search_input.text().strip()
        if query:
            self.search_requested.emit(query)
            
    def on_search_results(self, results: list[SearchResult]):
        query = self.search_input.text().strip()
        self.results_list.set_results(results, query)
        
        if results:
            self.scroll_area.show()
            self.status_label.setText(f"{len(results)} results found")
            self.status_label.show()
            self.setFixedSize(700, 500 + 30)
        else:
            self.scroll_area.hide()
            self.status_label.setText("No results found")
            self.status_label.show()
            self.setFixedSize(700, 110 + 30)

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
        self.search_thread.quit()
        self.search_thread.wait()
        super().closeEvent(event)


def launch_search_window(config: HaydarConfig):
    app = QApplication(sys.argv)
    
    font = QFont("Inter", 10)
    app.setFont(font)
    
    window = SearchWindow(config)
    
    hotkey_listener = HotkeyListener(config.hotkey, window.toggle)
    hotkey_listener.start()
    
    window.toggle()
    
    app.exec()
    hotkey_listener.stop()

import html
from PySide6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor

from haydar.search.hybrid import SearchResult

def _get_file_icon(file_type: str) -> str:
    """Map file extension to emoji icon."""
    file_type = file_type.lower()
    icon_map = {
        ".pdf": "📄",
        ".docx": "📝",
        ".txt": "📃", ".md": "📃", ".csv": "📃", ".log": "📃",
        ".rst": "📃", ".ini": "📃", ".cfg": "📃", ".toml": "📃",
        ".yaml": "📃", ".yml": "📃",
        ".png": "🖼️", ".jpg": "🖼️", ".jpeg": "🖼️", ".tiff": "🖼️",
        ".py": "💻", ".js": "💻", ".ts": "💻", ".jsx": "💻",
        ".tsx": "💻", ".java": "💻", ".c": "💻", ".cpp": "💻",
        ".h": "💻", ".rs": "💻", ".go": "💻", ".rb": "💻",
        ".html": "💻", ".css": "💻", ".json": "💻", ".xml": "💻",
        ".sql": "💻", ".sh": "💻", ".ps1": "💻", ".bat": "💻",
    }
    return icon_map.get(file_type, "📃")

def _highlight_snippet(snippet: str, query: str) -> str:
    """Highlight matching query words in the snippet with cyan color."""
    if not query:
        return html.escape(snippet)
    
    import re
    escaped_snippet = html.escape(snippet)
    
    # Highlight each individual query word
    words = query.split()
    for word in words:
        if len(word) < 2:
            continue
        escaped_word = html.escape(word)
        pattern = re.compile(re.escape(escaped_word), re.IGNORECASE)
        escaped_snippet = pattern.sub(
            lambda m: f'<span style="color: #00d4ff;">{m.group(0)}</span>',
            escaped_snippet,
        )
    return escaped_snippet

class ResultItem(QFrame):
    def __init__(self, result: SearchResult, query: str, parent=None):
        super().__init__(parent)
        self.result = result
        self.setFixedHeight(72)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        
        # Style
        self.setStyleSheet("""
            ResultItem {
                background-color: transparent;
                border-radius: 8px;
                border-left: 3px solid transparent;
            }
            ResultItem:hover {
                background-color: rgba(255, 255, 255, 0.05);
            }
            ResultItem[selected="true"] {
                background-color: rgba(255, 255, 255, 0.08);
                border-left: 3px solid #00d4ff;
            }
        """)
        self.setProperty("selected", False)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        
        # Icon
        icon_label = QLabel(_get_file_icon(result.file_type))
        icon_label.setStyleSheet("font-size: 24px; background: transparent;")
        icon_label.setFixedWidth(32)
        icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_label)
        
        # Center info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        
        filename_label = QLabel(result.filename)
        filename_label.setStyleSheet("font-weight: bold; color: white; font-size: 14px; background: transparent;")
        
        path_label = QLabel(result.folder)
        path_label.setStyleSheet("color: #aaaaaa; font-size: 11px; background: transparent;")
        
        snippet = result.snippet
        if len(snippet) > 100:
            snippet = snippet[:97] + "..."
        highlighted_snippet = _highlight_snippet(snippet, query)
        
        snippet_label = QLabel(highlighted_snippet)
        snippet_label.setStyleSheet("color: #cccccc; font-size: 12px; background: transparent;")
        snippet_label.setWordWrap(True)
        
        info_layout.addWidget(filename_label)
        info_layout.addWidget(path_label)
        info_layout.addWidget(snippet_label)
        layout.addLayout(info_layout, stretch=1)
        
        # Score
        score_label = QLabel(f"{int(result.score * 100)}%")
        score_label.setStyleSheet("color: #888888; font-size: 11px; background: transparent;")
        score_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        layout.addWidget(score_label)

    def set_selected(self, selected: bool):
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)


class ResultsList(QWidget):
    result_activated = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(4)
        self.layout.setAlignment(Qt.AlignTop)
        
        self.results: list[SearchResult] = []
        self.items: list[ResultItem] = []
        self.selected_index = -1
        
    def set_results(self, results: list[SearchResult], query: str):
        # Clear existing
        for i in reversed(range(self.layout.count())):
            widget = self.layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()
        
        self.results = results
        self.items = []
        self.selected_index = -1
        
        for result in results:
            item = ResultItem(result, query)
            self.items.append(item)
            self.layout.addWidget(item)
            
        if self.items:
            self.selected_index = 0
            self.items[0].set_selected(True)
            
    def get_selected_result(self) -> SearchResult | None:
        if 0 <= self.selected_index < len(self.results):
            return self.results[self.selected_index]
        return None
        
    def select_next(self):
        if not self.items:
            return
        self._set_selected_index((self.selected_index + 1) % len(self.items))
        
    def select_previous(self):
        if not self.items:
            return
        self._set_selected_index((self.selected_index - 1) % len(self.items))
        
    def _set_selected_index(self, index: int):
        if 0 <= self.selected_index < len(self.items):
            self.items[self.selected_index].set_selected(False)
        self.selected_index = index
        if 0 <= self.selected_index < len(self.items):
            self.items[self.selected_index].set_selected(True)
            
    def mouseDoubleClickEvent(self, event):
        child = self.childAt(event.pos())
        while child and not isinstance(child, ResultItem):
            child = child.parentWidget()
        if isinstance(child, ResultItem):
            self.result_activated.emit(child.result.file_path)

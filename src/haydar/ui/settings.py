from __future__ import annotations

import copy
import re
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from haydar.changelog import SECTION_NAMES, ChangelogEntry
from haydar.config import HaydarConfig
from haydar.ocr import (
    TesseractInfo,
    TesseractStatus,
    detect_tesseract,
    get_install_instructions,
)
from haydar.ui.theme import ThemeColors


class _OcrDetectionWorker(QObject):
    result = Signal(object)
    finished = Signal()

    @Slot()
    def run(self) -> None:
        try:
            self.result.emit(detect_tesseract())
        finally:
            self.finished.emit()


class SettingsWindow(QWidget):
    config_changed = Signal(HaydarConfig)
    _active_ocr_jobs: set[tuple[QThread, _OcrDetectionWorker]] = set()

    def __init__(self, config: HaydarConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._pending_folders = list(config.folders)

        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setMinimumSize(360, 320)
        screen = QApplication.screenAt(self.pos()) or QApplication.primaryScreen()
        if screen is None:
            self.resize(600, 520)
        else:
            available = screen.availableGeometry()
            self.resize(min(600, available.width()), min(520, available.height()))
        self.setWindowTitle("Haydar Settings")

        # Transparent background for custom paintEvent
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)

        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet("QWidget { background: transparent; }")
        self.tab_widget.setAccessibleName("Settings categories")

        self.folders_tab = QWidget()
        self.search_tab = QWidget()
        self.appearance_tab = QWidget()
        self.advanced_tab = QWidget()
        self.whats_new_tab = QWidget()

        self.tab_widget.addTab(self.folders_tab, "Folders")
        self.tab_widget.addTab(self.search_tab, "Search")
        self.tab_widget.addTab(self.appearance_tab, "Appearance")
        self.tab_widget.addTab(self.advanced_tab, "Advanced")
        self.tab_widget.addTab(self.whats_new_tab, "What's New")

        main_layout.addWidget(self.tab_widget)

        self._setup_folders_tab()
        self._setup_search_tab()
        self._setup_appearance_tab()
        self._setup_advanced_tab()
        self._setup_whats_new_tab()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_style = f"""
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
        """

        self.save_btn = QPushButton("Save")
        self.save_btn.setStyleSheet(btn_style)
        self.save_btn.setAccessibleName("Save settings")
        self.save_btn.setAccessibleDescription("Save configuration and close")
        self.save_btn.clicked.connect(self._apply)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet(btn_style)
        self.cancel_btn.setAccessibleName("Cancel")
        self.cancel_btn.setAccessibleDescription("Discard changes and close")
        self.cancel_btn.clicked.connect(self._on_cancel)

        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.cancel_btn)
        main_layout.addLayout(btn_layout)

        self._set_tab_order()
        self._start_ocr_check()

    def _set_tab_order(self):
        """Build one deterministic, reversible chain without overwritten links."""
        chain = [
            self.tab_widget,
            self.folders_list,
            self.add_folder_btn,
            self.remove_folder_btn,
            self.hotkey_edit,
            self.debounce_spin,
            self.limit_spin,
            self.opacity_slider,
            self.always_on_top_check,
            self.model_edit,
            self.chunk_size_spin,
            self.chunk_overlap_spin,
            self.excluded_edit,
            self.ocr_install_btn,
        ]
        if self._whats_new_show_all_btn is not None:
            chain.append(self._whats_new_show_all_btn)
        chain.extend([
            self.save_btn,
            self.cancel_btn,
        ])
        for current, following in zip(chain, chain[1:], strict=False):
            QWidget.setTabOrder(current, following)

    def _setup_folders_tab(self):
        layout = QVBoxLayout(self.folders_tab)

        self.folders_list = QListWidget()
        self.folders_list.setSelectionMode(QListWidget.SingleSelection)
        self.folders_list.setAccessibleName("Indexed folders")
        self.folders_list.setAccessibleDescription("List of directories to search")
        self._populate_folders_list()
        layout.addWidget(self.folders_list)

        btn_row = QHBoxLayout()
        self.add_folder_btn = QPushButton("+ Add folder")
        self.add_folder_btn.setAccessibleName("Add folder")
        self.add_folder_btn.setAccessibleDescription("Browse for a new directory to index")
        self.add_folder_btn.clicked.connect(self._on_add_folder)

        self.remove_folder_btn = QPushButton("− Remove selected")
        self.remove_folder_btn.setAccessibleName("Remove selected folder")
        self.remove_folder_btn.setAccessibleDescription("Remove the currently selected directory from the index")
        self.remove_folder_btn.clicked.connect(self._on_remove_folder)

        btn_row.addWidget(self.add_folder_btn)
        btn_row.addWidget(self.remove_folder_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        info_label = QLabel("Changes take effect on the next index run. Run haydar reindex after saving.")
        info_label.setTextFormat(Qt.PlainText)
        info_label.setWordWrap(True)
        info_label.setStyleSheet(f"color: {ThemeColors.TEXT_INFO_ALPHA}; font-size: 11px;")
        layout.addWidget(info_label)

    def _populate_folders_list(self):
        self.folders_list.clear()
        for folder in self._pending_folders:
            self.folders_list.addItem(QListWidgetItem(folder))

    def _on_add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select folder", str(Path.home()))
        if path:
            self._pending_folders.append(path)
            self.folders_list.addItem(QListWidgetItem(path))

    def _on_remove_folder(self):
        row = self.folders_list.currentRow()
        if row >= 0:
            self.folders_list.takeItem(row)
            self._pending_folders.pop(row)

    def _setup_search_tab(self):
        layout = QFormLayout(self.search_tab)

        self.hotkey_edit = QLineEdit(self.config.hotkey)
        self.hotkey_edit.setAccessibleName("Global hotkey")
        self.hotkey_edit.setAccessibleDescription("Keyboard shortcut to show or hide the search window")
        hotkey_info = QLabel("pynput format. Examples: <ctrl>+<space>, <ctrl>+<shift>+f")
        hotkey_info.setStyleSheet(f"color: {ThemeColors.TEXT_INFO_ALPHA}; font-size: 11px;")

        hotkey_layout = QVBoxLayout()
        hotkey_layout.addWidget(self.hotkey_edit)
        hotkey_layout.addWidget(hotkey_info)

        self.hotkey_error_label = QLabel("Unrecognised hotkey format.")
        self.hotkey_error_label.setStyleSheet(f"color: {ThemeColors.ERROR}; font-size: 11px;")
        self.hotkey_error_label.hide()
        hotkey_layout.addWidget(self.hotkey_error_label)

        layout.addRow("Global hotkey", hotkey_layout)

        self.debounce_spin = QDoubleSpinBox()
        self.debounce_spin.setRange(0.05, 2.0)
        self.debounce_spin.setSingleStep(0.05)
        self.debounce_spin.setDecimals(2)
        self.debounce_spin.setSuffix(" s")
        self.debounce_spin.setValue(self.config.watcher_debounce_seconds)
        self.debounce_spin.setAccessibleName("Search debounce")
        self.debounce_spin.setAccessibleDescription("Delay in seconds before triggering a search after typing")
        layout.addRow("Search debounce", self.debounce_spin)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 50)
        self.limit_spin.setSingleStep(1)
        self.limit_spin.setValue(self.config.results_limit)
        self.limit_spin.setAccessibleName("Results limit")
        self.limit_spin.setAccessibleDescription("Maximum number of results to display")
        layout.addRow("Results limit", self.limit_spin)

    def _setup_appearance_tab(self):
        layout = QFormLayout(self.appearance_tab)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(50, 100)
        self.opacity_slider.setTickInterval(10)
        self.opacity_slider.setTickPosition(QSlider.TicksBothSides)
        self.opacity_slider.setValue(self.config.window_opacity)
        self.opacity_slider.setAccessibleName("Window opacity")
        self.opacity_slider.setAccessibleDescription("Adjust the transparency of the search window")
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        layout.addRow("Window opacity", self.opacity_slider)

        self.always_on_top_check = QCheckBox("Always on top")
        self.always_on_top_check.setChecked(self.config.always_on_top)
        self.always_on_top_check.setAccessibleName("Always on top")
        self.always_on_top_check.setAccessibleDescription("Keep the search window above other applications")
        self.always_on_top_check.stateChanged.connect(self._on_always_on_top_changed)
        layout.addRow("", self.always_on_top_check)

    def _preview_config(self) -> HaydarConfig:
        """Return an in-memory preview without synchronous disk I/O."""
        temp_config = copy.deepcopy(self.config)
        temp_config.window_opacity = self.opacity_slider.value()
        temp_config.always_on_top = self.always_on_top_check.isChecked()
        return temp_config

    def _on_opacity_changed(self, value):
        self.config_changed.emit(self._preview_config())

    def _on_always_on_top_changed(self, state):
        self.config_changed.emit(self._preview_config())

    def _setup_advanced_tab(self):
        layout = QFormLayout(self.advanced_tab)

        self.model_edit = QLineEdit(self.config.embedding_model)
        self.model_edit.setAccessibleName("Embedding model")
        self.model_edit.setAccessibleDescription("The sentence-transformers model used for semantic search")
        model_warning = QLabel("Warning: Changing this requires a full reindex. The model will be downloaded on next init.")
        model_warning.setTextFormat(Qt.PlainText)
        model_warning.setWordWrap(True)
        model_warning.setStyleSheet(f"color: {ThemeColors.WARNING}; font-size: 11px;")

        model_layout = QVBoxLayout()
        model_layout.addWidget(self.model_edit)
        model_layout.addWidget(model_warning)
        layout.addRow("Embedding model", model_layout)

        self.chunk_size_spin = QSpinBox()
        self.chunk_size_spin.setRange(50, 2000)
        self.chunk_size_spin.setSingleStep(50)
        self.chunk_size_spin.setSuffix(" words")
        self.chunk_size_spin.setValue(self.config.chunk_size)
        self.chunk_size_spin.setAccessibleName("Chunk size")
        self.chunk_size_spin.setAccessibleDescription("Number of words per indexed chunk")

        chunk_size_warning = QLabel("Warning: Changing this requires a full reindex. The model will be downloaded on next init.")
        chunk_size_warning.setTextFormat(Qt.PlainText)
        chunk_size_warning.setWordWrap(True)
        chunk_size_warning.setStyleSheet(f"color: {ThemeColors.WARNING}; font-size: 11px;")

        chunk_size_layout = QVBoxLayout()
        chunk_size_layout.addWidget(self.chunk_size_spin)
        chunk_size_layout.addWidget(chunk_size_warning)
        layout.addRow("Chunk size", chunk_size_layout)

        self.chunk_overlap_spin = QSpinBox()
        self.chunk_overlap_spin.setRange(0, 200)
        self.chunk_overlap_spin.setSingleStep(10)
        self.chunk_overlap_spin.setSuffix(" words")
        self.chunk_overlap_spin.setValue(self.config.chunk_overlap)
        self.chunk_overlap_spin.setAccessibleName("Chunk overlap")
        self.chunk_overlap_spin.setAccessibleDescription("Number of overlapping words between consecutive chunks")

        self.overlap_error_label = QLabel("Error: Overlap must be less than chunk size.")
        self.overlap_error_label.setStyleSheet(f"color: {ThemeColors.ERROR}; font-size: 11px;")
        self.overlap_error_label.hide()

        chunk_overlap_layout = QVBoxLayout()
        chunk_overlap_layout.addWidget(self.chunk_overlap_spin)
        chunk_overlap_layout.addWidget(self.overlap_error_label)
        layout.addRow("Chunk overlap", chunk_overlap_layout)

        self.excluded_edit = QPlainTextEdit("\n".join(self.config.excluded_patterns))
        self.excluded_edit.setAccessibleName("Excluded patterns")
        self.excluded_edit.setAccessibleDescription("Patterns for files and directories to ignore during indexing")
        excluded_info = QLabel("One pattern per line. Supports glob suffix (*) and exact directory name matching.")
        excluded_info.setTextFormat(Qt.PlainText)
        excluded_info.setWordWrap(True)
        excluded_info.setStyleSheet(f"color: {ThemeColors.TEXT_INFO_ALPHA}; font-size: 11px;")

        excluded_layout = QVBoxLayout()
        excluded_layout.addWidget(self.excluded_edit)
        excluded_layout.addWidget(excluded_info)
        layout.addRow("Excluded patterns", excluded_layout)

        self.ocr_status_label = QLabel("Checking...")
        self.ocr_status_label.setAccessibleName("OCR status")
        self.ocr_status_label.setAccessibleDescription("Image OCR readiness")
        self.ocr_status_label.setTextFormat(Qt.PlainText)
        self.ocr_status_label.setStyleSheet(f"color: {ThemeColors.TEXT_INFO_ALPHA};")

        self.ocr_install_btn = QPushButton("Install instructions")
        self.ocr_install_btn.setAccessibleName("Install OCR instructions")
        self.ocr_install_btn.setAccessibleDescription("Show image OCR installation instructions")
        self.ocr_install_btn.clicked.connect(self._show_ocr_install_instructions)

        ocr_layout = QVBoxLayout()
        ocr_layout.addWidget(self.ocr_status_label)
        ocr_layout.addWidget(self.ocr_install_btn)
        layout.addRow("OCR (Tesseract)", ocr_layout)

    def _setup_whats_new_tab(self) -> None:
        layout = QVBoxLayout(self.whats_new_tab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; } "
            "QWidget#whatsNewContent { background: transparent; }"
        )

        self._whats_new_content = QWidget()
        self._whats_new_content.setObjectName("whatsNewContent")
        self._whats_new_layout = QVBoxLayout(self._whats_new_content)
        self._whats_new_layout.setContentsMargins(0, 0, 0, 0)
        self._hidden_version_entries: list[ChangelogEntry] = []
        self._whats_new_show_all_btn: QPushButton | None = None

        from haydar.changelog import find_changelog, parse_changelog

        path = find_changelog()
        versions = parse_changelog(path) if path is not None else []
        if not versions:
            self._add_changelog_unavailable()
        else:
            for entry in versions[:3]:
                self._whats_new_layout.addWidget(self._create_version_widget(entry))
            self._hidden_version_entries = versions[3:]
            if self._hidden_version_entries:
                self._whats_new_show_all_btn = QPushButton("Show all versions")
                self._whats_new_show_all_btn.setAccessibleName("Show all changelog versions")
                self._whats_new_show_all_btn.clicked.connect(self._show_all_versions)
                self._whats_new_layout.addWidget(self._whats_new_show_all_btn)
            self._whats_new_layout.addStretch()

        scroll.setWidget(self._whats_new_content)
        layout.addWidget(scroll)

    def _add_changelog_unavailable(self) -> None:
        label = QLabel("Changelog not available.")
        label.setStyleSheet("color: rgba(255,255,255,0.4);")
        label.setTextFormat(Qt.PlainText)
        self._whats_new_layout.addWidget(label)
        self._whats_new_layout.addStretch()

    def _create_version_widget(self, entry: ChangelogEntry) -> QWidget:
        section_colors = {
            "Added": "#6ee7b7",
            "Changed": "#93c5fd",
            "Fixed": "#fca5a5",
            "Removed": "#d1d5db",
        }
        container = QWidget()
        version_layout = QVBoxLayout(container)
        version_layout.setContentsMargins(0, 0, 0, 0)
        header = QLabel(f"v{entry['version']}  —  {entry['date'] or 'Unreleased'}")
        header.setStyleSheet(
            "font-weight: bold; color: white; font-size: 13px; margin-top: 12px;"
        )
        header.setTextFormat(Qt.PlainText)
        version_layout.addWidget(header)

        for section_name in SECTION_NAMES:
            items = entry["sections"][section_name]
            if not items:
                continue
            section_header = QLabel(section_name)
            section_header.setStyleSheet(f"color: {section_colors[section_name]};")
            section_header.setTextFormat(Qt.PlainText)
            version_layout.addWidget(section_header)
            for item in items:
                item_label = QLabel(f"• {item}")
                item_label.setWordWrap(True)
                item_label.setTextFormat(Qt.PlainText)
                item_label.setStyleSheet("color: rgba(255,255,255,0.8);")
                version_layout.addWidget(item_label)
        return container

    def _show_all_versions(self) -> None:
        if self._whats_new_show_all_btn is None:
            return
        for entry in self._hidden_version_entries:
            self._whats_new_layout.insertWidget(
                self._whats_new_layout.indexOf(self._whats_new_show_all_btn),
                self._create_version_widget(entry),
            )
        self._hidden_version_entries = []
        self._whats_new_show_all_btn.hide()

    def _start_ocr_check(self) -> None:
        thread = QThread()
        worker = _OcrDetectionWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(self._set_ocr_status)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        job = (thread, worker)
        thread.finished.connect(lambda job=job: self._active_ocr_jobs.discard(job))
        self._ocr_thread = thread
        self._ocr_worker = worker
        self._active_ocr_jobs.add(job)
        thread.start()

    @Slot(object)
    def _set_ocr_status(self, info: TesseractInfo) -> None:
        if info.status is TesseractStatus.FOUND:
            self.ocr_status_label.setText(f"\u2713 Tesseract v{info.version} found; image OCR enabled")
            self.ocr_status_label.setStyleSheet("color: #6ee7b7;")
        elif info.status is TesseractStatus.PYTHON_PACKAGE_MISSING:
            self.ocr_status_label.setText("\u2717 Python OCR adapter not installed")
            self.ocr_status_label.setStyleSheet("color: #f59e0b;")
        elif info.status is TesseractStatus.NOT_FOUND:
            self.ocr_status_label.setText("\u2717 Tesseract executable not found")
            self.ocr_status_label.setStyleSheet("color: #f59e0b;")
        elif info.status is TesseractStatus.WRONG_VERSION:
            self.ocr_status_label.setText(f"\u26a0 Version {info.version} found (v4+ required)")
            self.ocr_status_label.setStyleSheet("color: #f59e0b;")
        else:
            self.ocr_status_label.setText("\u26a0 Tesseract could not be verified; check logs")
            self.ocr_status_label.setStyleSheet(f"color: {ThemeColors.ERROR};")

    @Slot()
    def _show_ocr_install_instructions(self) -> None:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Install OCR")
        dialog.setIcon(QMessageBox.Information)
        dialog.setTextFormat(Qt.PlainText)
        dialog.setText(get_install_instructions())
        dialog.exec()

    def show_settings(self, tab_index: int = 0) -> None:
        self.tab_widget.setCurrentIndex(tab_index)
        self.show()
        self.raise_()

    def show_whats_new(self) -> None:
        self.show_settings(4)

    def _apply(self) -> None:
        if not self._pending_folders:
            QMessageBox.warning(self, "Haydar Settings", "At least one folder is required.")
            return

        hotkey = self.hotkey_edit.text().strip()
        if not re.fullmatch(r"(<\w+>|\w+)(\+(<\w+>|\w+))*", hotkey):
            self.hotkey_error_label.show()
        else:
            self.hotkey_error_label.hide()

        if self.chunk_overlap_spin.value() >= self.chunk_size_spin.value():
            self.overlap_error_label.show()
            return
        self.overlap_error_label.hide()

        _needs_reindex = False
        if self.config.embedding_model != self.model_edit.text().strip() or self.config.chunk_size != self.chunk_size_spin.value():
            _needs_reindex = True

        self.config.folders = list(self._pending_folders)
        self.config.hotkey = hotkey
        self.config.watcher_debounce_seconds = self.debounce_spin.value()
        self.config.results_limit = self.limit_spin.value()
        self.config.window_opacity = self.opacity_slider.value()
        self.config.always_on_top = self.always_on_top_check.isChecked()
        self.config.embedding_model = self.model_edit.text().strip()
        self.config.chunk_size = self.chunk_size_spin.value()
        self.config.chunk_overlap = self.chunk_overlap_spin.value()
        self.config.excluded_patterns = [
            line.strip()
            for line in self.excluded_edit.toPlainText().splitlines()
            if line.strip()
        ]

        self.config.save()
        self.config_changed.emit(self.config)

        if _needs_reindex:
            QMessageBox.information(self, "Haydar Settings", "These changes require a full reindex. Run haydar reindex to apply them.")

        self.hide()

    def _revert(self) -> None:
        self.config = HaydarConfig.load()
        self._pending_folders = list(self.config.folders)
        self._populate_folders_list()

        self.hotkey_edit.setText(self.config.hotkey)
        self.hotkey_error_label.hide()
        self.debounce_spin.setValue(self.config.watcher_debounce_seconds)
        self.limit_spin.setValue(self.config.results_limit)

        self.opacity_slider.setValue(self.config.window_opacity)
        self.always_on_top_check.setChecked(self.config.always_on_top)

        self.model_edit.setText(self.config.embedding_model)
        self.chunk_size_spin.setValue(self.config.chunk_size)
        self.chunk_overlap_spin.setValue(self.config.chunk_overlap)
        self.overlap_error_label.hide()
        self.excluded_edit.setPlainText("\n".join(self.config.excluded_patterns))

    def _on_cancel(self) -> None:
        self._revert()
        self.hide()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), Qt.transparent)
        bg_color = QColor(20, 20, 30, 235)
        painter.setBrush(bg_color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 16, 16)

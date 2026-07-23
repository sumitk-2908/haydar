"""
File watcher module for Haydar.
Monitors configured directories and updates the index on file changes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from haydar.config import ALL_INDEXABLE_EXTENSIONS, HaydarConfig, is_excluded
from haydar.indexer.engine import IndexingEngine

logger = logging.getLogger(__name__)


class _DebouncedHandler(FileSystemEventHandler):
    """
    Watchdog event handler with debouncing and retry logic for indexing.
    """

    def __init__(self, config: HaydarConfig, engine: IndexingEngine):
        super().__init__()
        self.config = config
        self.engine = engine
        self.debounce_seconds = config.watcher_debounce_seconds
        self.last_events: dict[str, float] = {}

    def _should_process(self, filepath: Path) -> bool:
        """Check if file should be indexed based on extension and exclusion rules."""
        if filepath.suffix.lower() not in ALL_INDEXABLE_EXTENSIONS:
            return False
        return not is_excluded(filepath, self.config.excluded_patterns)

    def _debounce(self, path_str: str) -> bool:
        """Return True if the event should be processed (not debounced)."""
        current_time = time.time()
        last_time = self.last_events.get(path_str, 0.0)

        if current_time - last_time < self.debounce_seconds:
            return False

        self.last_events[path_str] = current_time
        return True

    def _index_with_retry(self, filepath: Path) -> None:
        """Attempt to index a file, with retries for locks (common on Windows)."""
        retries = [0.1, 0.5, 1.0]

        for attempt, delay in enumerate(retries, start=1):
            try:
                success = self.engine.index_file(filepath)
                if success:
                    logger.info(f"Indexed: {filepath.name}")
                return
            except (PermissionError, OSError) as e:
                if attempt <= len(retries):
                    logger.debug(f"File locked, retrying {filepath.name} in {delay}s...")
                    time.sleep(delay)
                else:
                    logger.error(f"Failed to index {filepath.name} after {len(retries)} attempts: {e}")

    def _async_index(self, filepath: Path) -> None:
        """Run indexing in a background thread so the watcher isn't blocked."""
        threading.Thread(target=self._index_with_retry, args=(filepath,), daemon=True).start()

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        path = Path(os.fsdecode(event.src_path))
        path_str = str(path)

        if self._should_process(path) and self._debounce(path_str):
            self._async_index(path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        path = Path(os.fsdecode(event.src_path))
        path_str = str(path)

        if self._should_process(path) and self._debounce(path_str):
            self._async_index(path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        path = Path(os.fsdecode(event.src_path))
        self.engine.remove_file(path)
        logger.info(f"Removed: {path.name}")

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        src_path = Path(os.fsdecode(event.src_path))
        dest_path = Path(os.fsdecode(getattr(event, 'dest_path', '')))

        self.engine.remove_file(src_path)

        if self._should_process(dest_path):
            self._async_index(dest_path)

        logger.info(f"Moved: {src_path.name} -> {dest_path.name}")


class FileWatcher:
    """Manages the filesystem observer and handler."""

    def __init__(self, config: HaydarConfig):
        self.config = config
        self.engine = IndexingEngine(config)
        self._observer: Any = None

    def start(self, blocking: bool = True) -> None:
        """Start the file watcher."""
        if not self.config.folders:
            logger.warning("No folders configured to watch.")
            return

        observer = Observer()
        handler = _DebouncedHandler(self.config, self.engine)

        for folder in self.config.folders:
            folder_path = Path(folder)
            if folder_path.exists() and folder_path.is_dir():
                observer.schedule(handler, str(folder_path), recursive=True)
                logger.info(f"Watching folder: {folder}")
            else:
                logger.warning(f"Configured folder does not exist or is not a directory: {folder}")

        observer.start()
        logger.info("File watcher started.")

        if not blocking:
            self._observer = observer
            return

        logger.info("Press Ctrl+C to stop.")
        try:
            while observer.is_alive():
                observer.join(timeout=1)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received. Stopping watcher...")
        finally:
            observer.stop()
            observer.join()
            logger.info("Watcher stopped.")
            self.close()

    def stop(self) -> None:
        """Stop a non-blocking observer started via start(blocking=False)."""
        observer = getattr(self, "_observer", None)
        if observer is not None:
            observer.stop()
            observer.join()
            self._observer = None
            logger.info("Watcher stopped.")
        self.close()

    def close(self) -> None:
        """Release resources."""
        if hasattr(self, 'engine') and hasattr(self.engine, 'close'):
            self.engine.close()


def install_autostart() -> None:
    """Create a .bat file in the Windows Startup folder to run the watcher on login."""
    startup_dir = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_dir.mkdir(parents=True, exist_ok=True)

    bat_path = startup_dir / "haydar_watcher.bat"

    import sys
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable)
        if exe_path.name.lower() == "haydar.exe":
            cli_path = exe_path.with_name("haydar-cli.exe")
            command = f'"{cli_path if cli_path.exists() else exe_path}" watch'
        else:
            command = f'"{exe_path}" watch'
    else:
        command = "pythonw -m haydar watch"

    content = f"@echo off\n{command}\n"
    bat_path.write_text(content, encoding="utf-8")

    logger.info(f"Autostart script installed successfully at: {bat_path}")

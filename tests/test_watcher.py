import pytest
from unittest.mock import Mock, patch
from pathlib import Path
from watchdog.events import FileModifiedEvent, FileDeletedEvent, FileMovedEvent

from haydar.config import HaydarConfig
from haydar.indexer.watcher import _DebouncedHandler, FileWatcher

@pytest.fixture
def mock_engine():
    return Mock()

@pytest.fixture
def config(tmp_haydar):
    cfg = HaydarConfig()
    cfg.watcher_debounce_seconds = 0.1
    return cfg

@patch.object(_DebouncedHandler, '_async_index')
def test_watcher_extension_filtering(mock_async_index, config, mock_engine):
    handler = _DebouncedHandler(config, mock_engine)
    
    # Python file should be processed
    py_event = FileModifiedEvent(src_path="test.py")
    handler.on_modified(py_event)
    mock_async_index.assert_called_once_with(Path("test.py"))
    
    mock_async_index.reset_mock()
    
    # Exe file should be ignored
    exe_event = FileModifiedEvent(src_path="test.exe")
    handler.on_modified(exe_event)
    mock_async_index.assert_not_called()

@patch.object(_DebouncedHandler, '_async_index')
def test_watcher_debounce(mock_async_index, config, mock_engine):
    handler = _DebouncedHandler(config, mock_engine)
    
    # First event should trigger
    handler.on_modified(FileModifiedEvent(src_path="test.md"))
    assert mock_async_index.call_count == 1
    
    # Second event immediately after should be debounced
    handler.on_modified(FileModifiedEvent(src_path="test.md"))
    assert mock_async_index.call_count == 1
    
    # Third event on a different file should trigger
    handler.on_modified(FileModifiedEvent(src_path="test2.md"))
    assert mock_async_index.call_count == 2

def test_watcher_on_deleted(config, mock_engine):
    handler = _DebouncedHandler(config, mock_engine)
    handler.on_deleted(FileDeletedEvent(src_path="test.md"))
    mock_engine.remove_file.assert_called_with(Path("test.md"))

@patch.object(_DebouncedHandler, '_async_index')
def test_watcher_on_moved(mock_async_index, config, mock_engine):
    handler = _DebouncedHandler(config, mock_engine)
    handler.on_moved(FileMovedEvent(src_path="old.md", dest_path="new.md"))
    
    mock_engine.remove_file.assert_called_with(Path("old.md"))
    mock_async_index.assert_called_with(Path("new.md"))

def test_watcher_close_releases_engine(config, mock_engine):
    watcher = FileWatcher(config)
    watcher.engine = mock_engine
    
    watcher.close()
    mock_engine.close.assert_called_once()
    
    # Check idempotent
    watcher.close()
    assert mock_engine.close.call_count == 2

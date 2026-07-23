import pytest
import logging
import sys
from unittest.mock import patch, MagicMock

from haydar.ui.hotkey import HotkeyListener

def test_hotkey_registration_failure(caplog):
    listener = HotkeyListener("<ctrl>+<space>", lambda: None)

    # Inject a fake pynput so the import inside start() succeeds regardless of
    # whether pynput is installed, then force thread creation to fail. This
    # exercises the "registration failed but the GUI keeps running" path.
    fake_pynput = MagicMock()
    with patch.dict(sys.modules, {"pynput": fake_pynput, "pynput.keyboard": fake_pynput}):
        with patch("haydar.ui.hotkey.threading.Thread", side_effect=Exception("mocked permission error")):
            with caplog.at_level(logging.ERROR):
                listener.start()

    assert "Failed to start hotkey listener: mocked permission error" in caplog.text

def test_hotkey_import_error(caplog):
    import sys
    listener = HotkeyListener("<ctrl>+<space>", lambda: None)
    
    # If pynput is missing
    with patch.dict(sys.modules, {'pynput': None, 'pynput.keyboard': None}):
        with caplog.at_level(logging.WARNING):
            listener.start()
            
    assert "pynput is not installed. Global hotkeys will not work." in caplog.text

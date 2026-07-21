import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

class HotkeyListener:
    def __init__(self, hotkey: str, callback: Callable):
        self.hotkey = hotkey
        self.callback = callback
        self._thread = None
        self._listener = None

    def start(self):
        try:
            from pynput.keyboard import GlobalHotKeys
            
            def _run():
                self._listener = GlobalHotKeys({self.hotkey: self.callback})
                with self._listener:
                    self._listener.join()

            self._thread = threading.Thread(target=_run, daemon=True)
            self._thread.start()
            logger.info(f"Started hotkey listener for {self.hotkey}")
        except ImportError:
            logger.warning("pynput is not installed. Global hotkeys will not work.")
        except Exception as e:
            logger.error(f"Failed to start hotkey listener: {e}")

    def stop(self):
        if self._listener:
            self._listener.stop()
        if self._thread:
            self._thread.join(timeout=1.0)

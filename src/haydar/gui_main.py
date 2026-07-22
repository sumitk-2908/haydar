"""
Windowed GUI entry point for Haydar.

This is the entry script for the windowed (no-console) EXE and the
``haydar-ui`` gui-script. It loads config and launches the floating search
window directly, without going through the Typer CLI. All errors are logged to
``~/.haydar/logs/haydar.log`` since there is no console to print to.
"""

from __future__ import annotations

import logging
import sys


def main() -> None:
    from haydar.config import HaydarConfig
    from haydar.logging_setup import setup_logging

    setup_logging(console=False)
    logger = logging.getLogger(__name__)

    try:
        config = HaydarConfig.load()
        if not config.initialized:
            logger.error("Haydar is not initialized. Run `haydar init` first.")
            sys.exit(1)

        from haydar.ui.window import launch_search_window

        launch_search_window(config)
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Fatal error launching Haydar GUI")
        sys.exit(1)


if __name__ == "__main__":
    main()

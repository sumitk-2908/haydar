"""
Central logging configuration for Haydar.

Provides `setup_logging()`, which installs a rotating file handler at
``~/.haydar/logs/haydar.log`` plus (optionally) a console handler. This is the
single place logging is configured so that both the CLI, the windowed GUI EXE,
and the background watcher all persist logs to the same file -- essential for
diagnosing failures in a packaged build where stdout/stderr may be discarded.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from haydar.config import LOG_DIR

_LOG_FILE = LOG_DIR / "haydar.log"
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s [%(run_id)s] %(levelname)-8s %(name)s: %(message)s"

_configured = False


class _RunIdFilter(logging.Filter):
    """Ensure every formatted record has a correlation identifier."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = "--------"
        return True


def setup_logging(level: int = logging.INFO, console: bool = True) -> None:
    """Configure root logging with a rotating file handler.

    Idempotent: safe to call from multiple entry points; only the first call
    installs handlers. A missing/unwritable log directory degrades gracefully
    to console-only logging rather than raising.

    Args:
        level: Root log level.
        console: If True, also log to stderr (useful for the CLI). Set False
            for the windowed GUI where there is no console.
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT)
    run_id_filter = _RunIdFilter()
    root.addFilter(run_id_filter)

    # Filters on handlers also process records propagated from child loggers.
    # File handler (rotating). Failure to create it must not crash the app.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        file_handler.addFilter(run_id_filter)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning(
            "Could not open log file %s: %s", _LOG_FILE, exc
        )

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        # Keep the console quieter than the file to avoid noise in the terminal.
        console_handler.setLevel(logging.WARNING)
        console_handler.addFilter(run_id_filter)
        root.addHandler(console_handler)

    _configured = True

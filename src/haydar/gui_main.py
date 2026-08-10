"""
Windowed GUI entry point for Haydar.

This is the entry script for the windowed (no-console) EXE and the
``haydar-ui`` gui-script. It loads config and launches the floating search
window directly, without going through the Typer CLI. All errors are logged to
``~/.haydar/logs/haydar.log`` since there is no console to print to.
"""

from __future__ import annotations

import contextlib
import logging
import sys


def _enable_windows_dpi_awareness() -> None:
    """Request per-monitor DPI awareness before Qt creates any objects.

    This configures the native process only. Qt widget coordinates remain
    logical pixels and must not be multiplied by a device pixel ratio.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        # Unsupported Windows versions and an already-established DPI context
        # are both safe: Qt can still start with its own defaults.
        pass


def _show_error_dialog(title: str, message: str) -> None:
    """Safely show a native Windows error dialog, ignoring failures."""
    if _report_probe_failure(f"{title}: {message}"):
        # A probe run is unattended. A modal dialog would block until someone
        # dismissed it, turning a startup failure into a hung CI step, so the
        # failure is written to the report instead.
        return
    if sys.platform != "win32":
        return
    try:
        import ctypes
        MB_ICONERROR = 0x10
        # 0 is the desktop window handle (HWND_DESKTOP)
        ctypes.windll.user32.MessageBoxW(0, message, title, MB_ICONERROR)
    except Exception:
        pass


def _report_probe_failure(detail: str) -> bool:
    """Record a fatal startup failure for the packaged-startup probe.

    Returns whether this process is a probe run, so callers know the failure has
    been reported and no dialog is wanted. Import failures are swallowed: if the
    probe machinery itself cannot load, the launcher still sees a missing report
    and a nonzero exit, which is the same verdict.
    """
    try:
        from haydar.startup_probe import StartupReport, is_probing, write_report

        if not is_probing():
            return False
        write_report(StartupReport(errors=[detail]))
        return True
    except Exception:
        return False


def main() -> None:
    _enable_windows_dpi_awareness()
    logger = logging.getLogger(__name__)

    try:
        from haydar.config import (
            CURRENT_SCHEMA_VERSION,
            ConfigFormatError,
            HaydarConfig,
            get_log_path,
        )
        from haydar.logging_setup import setup_logging

        # Logging initialization is inside the fatal boundary: even a handler or
        # import failure receives the native no-console fallback below.
        setup_logging(console=False)

        try:
            config = HaydarConfig.load()
        except ConfigFormatError as exc:
            # Fail closed: rewriting a newer config would drop fields this build
            # cannot maintain. Neither config nor index is touched.
            logger.error("Unsupported config format: %s", exc)
            _show_error_dialog(
                "Haydar Version Required",
                f"{exc}\n\n{exc.hint or ''}\n\nFull log: {get_log_path()}",
            )
            sys.exit(1)

        schema_version = getattr(config, "schema_version", 0)
        if schema_version > CURRENT_SCHEMA_VERSION:
            logger.error(
                "Database schema is newer (config version: %s, current: %s).",
                schema_version,
                CURRENT_SCHEMA_VERSION,
            )
            msg = (
                "This search index was created by a newer Haydar version.\n\n"
                "Install the newer Haydar version that created it. Do not reindex "
                "with this older version. Your files are unaffected.\n\n"
                f"Full log: {get_log_path()}"
            )
            _show_error_dialog("Haydar Version Required", msg)
            sys.exit(1)
        if schema_version < CURRENT_SCHEMA_VERSION:
            logger.error(
                "Database schema is stale (config version: %s, current: %s).",
                schema_version,
                CURRENT_SCHEMA_VERSION,
            )
            msg = (
                "The search index format has changed and needs to be rebuilt.\n\n"
                "Open Haydar settings and choose Rebuild index. Your files will "
                "not be affected.\n\n"
                f"Full log: {get_log_path()}"
            )
            _show_error_dialog("Haydar Update Required", msg)
            sys.exit(1)

        from haydar.ui.application import run_gui_application

        run_gui_application(config)
    except KeyboardInterrupt:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            logger.exception("Fatal error launching Haydar GUI")

        try:
            from haydar.config import get_log_path
            log_path = get_log_path()
        except Exception:
            log_path = "Unknown"

        msg = f"A fatal error occurred while launching Haydar.\n\nFull log: {log_path}"
        _show_error_dialog("Haydar Fatal Error", msg)

        sys.exit(1)


if __name__ == "__main__":
    main()

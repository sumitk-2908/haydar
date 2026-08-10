"""Packaged-startup self-report for ``haydar.exe``.

A windowed build has no console, so a missing hidden import or a crash before
the first window appears is invisible: the process simply exits and nothing is
printed anywhere a CI step can see. This module gives the packaged GUI a way to
say what it actually reached.

When :data:`haydar.config.STARTUP_PROBE_ENV_VAR` names a writable path, the Qt
controller runs its normal startup and then writes one JSON report describing
what happened — which view opened, whether setup began, whether the runtime
imports resolved — and quits. Nothing here is a substitute for that startup: the
report is written *after* the real windows and workers were constructed, so a
report that exists at all is evidence the packaged app started.

The probe deliberately does not wait for setup to finish. Completing setup would
download the embedding model, which is a network operation and not what §15 asks
to be proven; reaching setup is.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from haydar.config import HAYDAR_DIR, STARTUP_PROBE_ENV_VAR

logger = logging.getLogger(__name__)

# Bumped if the report's shape changes in a way a CI step must notice.
REPORT_SCHEMA_VERSION = 1

# Runtime imports the packaged GUI must resolve. PySide6 submodules and the
# watchdog/pynput Windows backends are the ones PyInstaller most often misses,
# and the OCR adapter is bundled (§15) even though the native engine is not.
REQUIRED_IMPORTS: tuple[str, ...] = (
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "haydar.ocr",
    "pytesseract",
    "PIL.Image",
    "watchdog.observers",
    "pynput.keyboard",
)


def report_path() -> Path | None:
    """Return the report destination, or ``None`` when probing is not requested."""
    raw = os.environ.get(STARTUP_PROBE_ENV_VAR, "").strip()
    return Path(raw) if raw else None


def is_probing() -> bool:
    """Whether this process was launched as a packaged-startup probe."""
    return report_path() is not None


def has_console() -> bool:
    """Whether a console window is attached to this process.

    ``haydar.exe`` is built with ``console=False``, so a true result from a
    packaged run means the windowed subsystem flag was lost.
    """
    if sys.platform != "win32":
        return sys.stdout is not None and sys.stdout.isatty()
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        # An unavailable API is not evidence of a console.
        return False


def _import_status() -> dict[str, str]:
    """Import each required module, recording ``ok`` or the failure reason.

    Imports are attempted rather than inferred: a frozen build can carry a
    module's metadata and still fail to load its extension.
    """
    import importlib

    status: dict[str, str] = {}
    for name in REQUIRED_IMPORTS:
        try:
            importlib.import_module(name)
            status[name] = "ok"
        except Exception as exc:
            status[name] = f"{type(exc).__name__}: {exc}"
    return status


def _keyword_search_status() -> str:
    """Whether the bundled ripgrep binary resolved, as a short status string."""
    try:
        from haydar.config import HaydarConfigError, get_rg_path
    except Exception as exc:
        return f"unavailable: {type(exc).__name__}: {exc}"
    try:
        return f"ok: {get_rg_path()}"
    except HaydarConfigError as exc:
        return f"missing: {exc}"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def _ocr_manifest_status() -> str:
    """Whether the pinned OCR manifest is present and readable in the bundle.

    An unreviewed entry is the expected shipped state (§12.1), so this reports
    what the manifest says rather than judging it.
    """
    try:
        from haydar.ocr import OCR_ASSETS
    except Exception as exc:
        return f"unavailable: {type(exc).__name__}: {exc}"
    if not OCR_ASSETS:
        return "empty"
    reviewed = sum(1 for asset in OCR_ASSETS if asset.is_reviewed)
    return f"ok: {len(OCR_ASSETS)} asset(s), {reviewed} reviewed"


@dataclass
class StartupReport:
    """What the packaged GUI reached before the probe stopped it."""

    view: str = "none"
    setup_started: bool = False
    setup_phase: str = ""
    setup_detail: str = ""
    frozen: bool = False
    console: bool = False
    data_root: str = ""
    imports: dict[str, str] = field(default_factory=dict)
    keyword_search: str = ""
    ocr_manifest: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def import_failures(self) -> list[str]:
        return [
            f"{name}: {result}"
            for name, result in self.imports.items()
            if result != "ok"
        ]

    @property
    def ok(self) -> bool:
        """Whether startup satisfied every §15 packaged-startup requirement.

        A console on a windowed build is a failure in its own right: it means
        the GUI was linked against the console subsystem, and every
        ``CREATE_NO_WINDOW`` assumption downstream is then untested.
        """
        return (
            self.view in ("onboarding", "search")
            and not self.import_failures
            and not self.errors
            and not self.console
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA_VERSION,
            "ok": self.ok,
            "view": self.view,
            "setup_started": self.setup_started,
            "setup_phase": self.setup_phase,
            "setup_detail": self.setup_detail,
            "frozen": self.frozen,
            "console": self.console,
            "data_root": self.data_root,
            "imports": dict(self.imports),
            "import_failures": self.import_failures,
            "keyword_search": self.keyword_search,
            "ocr_manifest": self.ocr_manifest,
            "errors": list(self.errors),
        }


def build_report(
    *,
    view: str,
    setup_started: bool,
    setup_phase: str = "",
    setup_detail: str = "",
) -> StartupReport:
    """Collect the report for a startup that reached ``view``."""
    return StartupReport(
        view=view,
        setup_started=setup_started,
        setup_phase=setup_phase,
        setup_detail=setup_detail,
        frozen=bool(getattr(sys, "frozen", False)),
        console=has_console(),
        data_root=str(HAYDAR_DIR),
        imports=_import_status(),
        keyword_search=_keyword_search_status(),
        ocr_manifest=_ocr_manifest_status(),
    )


def write_report(report: StartupReport, destination: Path | None = None) -> Path | None:
    """Write ``report`` as JSON atomically; return the path, or ``None`` on failure.

    Atomic because a CI step may poll for the file: a reader must never see a
    half-written report and conclude the app produced malformed output.
    """
    target = destination if destination is not None else report_path()
    if target is None:
        return None

    payload = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    temp_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        temp_path = None
        return target
    except OSError:
        logger.exception("Could not write the startup probe report")
        return None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

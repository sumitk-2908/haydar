"""Launch the packaged ``haydar.exe`` against a throwaway profile and check it.

The release workflow builds a windowed executable and, until now, never ran it.
A missing hidden import or a console-less startup fault therefore shipped
silently: the process exits, nothing is printed, and only the CLI was smoke
tested. This script closes that gap.

It runs the *packaged* binary — not the source tree — in an isolated profile
(``HAYDAR_HOME``) with the offscreen Qt platform, and asserts from the report
the app writes that it reached onboarding, started setup, resolved every runtime
import, and had no console attached. It also asserts the run never touched the
real ``~/.haydar``.

Usage::

    python scripts/packaged_startup_probe.py --exe dist/haydar.exe

Exit code 0 means the packaged GUI starts. Any other code is a failure with the
reason on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Mirrored from haydar.config so this script stays importable without PySide6
# and without the package installed. tests/test_packaging.py fails if they drift.
HOME_ENV_VAR = "HAYDAR_HOME"
STARTUP_PROBE_ENV_VAR = "HAYDAR_STARTUP_PROBE"

DEFAULT_TIMEOUT_SECONDS = 240


def validate_report(report: object) -> list[str]:
    """Return the reasons ``report`` fails the packaged-startup contract.

    Kept pure so the pass/fail rules are unit-tested rather than only exercised
    by a release build.
    """
    problems: list[str] = []
    if not isinstance(report, dict):
        return [f"report is not a JSON object (got {type(report).__name__})"]

    if report.get("view") not in ("onboarding", "search"):
        problems.append(
            f"no top-level view was reached (view={report.get('view')!r})"
        )
    if report.get("view") == "onboarding" and not report.get("setup_started"):
        problems.append("onboarding opened but setup never started")
    for failure in report.get("import_failures") or []:
        problems.append(f"runtime import failed: {failure}")
    for error in report.get("errors") or []:
        problems.append(f"startup error: {error}")
    if report.get("console"):
        problems.append(
            "a console is attached; the windowed build was linked as a console app"
        )
    if not report.get("frozen"):
        problems.append(
            "the reporting process was not frozen; a packaged EXE must be probed"
        )
    keyword_search = str(report.get("keyword_search") or "")
    if not keyword_search.startswith("ok:"):
        problems.append(f"bundled ripgrep did not resolve ({keyword_search})")
    ocr_manifest = str(report.get("ocr_manifest") or "")
    if not ocr_manifest.startswith("ok:"):
        problems.append(f"OCR manifest is not readable from the bundle ({ocr_manifest})")
    return problems


def _profile_fingerprint(root: Path) -> object:
    """Describe a profile directory well enough to notice any write to it."""
    if not root.exists():
        return None
    entries = []
    for path in sorted(root.rglob("*")):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((str(path.relative_to(root)), stat.st_size, int(stat.st_mtime)))
    return entries


def _tail(path: Path, limit: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(lines[-limit:]) or "(empty log)"


def run_probe(exe: Path, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> int:
    if not exe.is_file():
        print(f"FAIL: no such executable: {exe}")
        return 2

    real_home = Path.home() / ".haydar"
    before = _profile_fingerprint(real_home)

    workspace = Path(tempfile.mkdtemp(prefix="haydar-startup-probe-"))
    profile = workspace / "profile"
    report_path = workspace / "startup-report.json"
    env = dict(os.environ)
    env[HOME_ENV_VAR] = str(profile)
    env[STARTUP_PROBE_ENV_VAR] = str(report_path)
    # Offscreen keeps the probe headless; a real platform plugin would need a
    # session and would leave a window on screen for the length of the run.
    env["QT_QPA_PLATFORM"] = "offscreen"

    print(f"Launching {exe} with {HOME_ENV_VAR}={profile}")
    try:
        completed = subprocess.run(
            [str(exe)],
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
        exit_code = completed.returncode
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired:
        print(f"FAIL: the packaged GUI did not exit within {timeout}s")
        print(_tail(profile / "logs" / "haydar.log"))
        shutil.rmtree(workspace, ignore_errors=True)
        return 1

    problems: list[str] = []
    if exit_code != 0:
        problems.append(f"the packaged GUI exited with code {exit_code}")
    if stderr.strip():
        print(f"stderr:\n{stderr.strip()}")

    if not report_path.is_file():
        problems.append(
            "no startup report was written; the app died before it could report"
        )
        report: object = None
    else:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"the startup report is unreadable: {exc}")
            report = None
        else:
            print(json.dumps(report, indent=2, sort_keys=True))
            problems.extend(validate_report(report))

    after = _profile_fingerprint(real_home)
    if before != after:
        problems.append(
            f"the probe modified the real profile at {real_home}; "
            f"{HOME_ENV_VAR} was not honoured"
        )

    if problems:
        print("FAIL: packaged GUI startup probe")
        for problem in problems:
            print(f"  - {problem}")
        print("\nIsolated profile log:")
        print(_tail(profile / "logs" / "haydar.log"))
        shutil.rmtree(workspace, ignore_errors=True)
        return 1

    print("OK: the packaged GUI started, reached onboarding, and began setup.")
    shutil.rmtree(workspace, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exe",
        default=str(Path("dist") / "haydar.exe"),
        help="Path to the packaged windowed executable (default: dist/haydar.exe)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Seconds to wait for the packaged GUI to report and exit",
    )
    args = parser.parse_args(argv)
    return run_probe(Path(args.exe), timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())

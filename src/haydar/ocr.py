from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum

from packaging.version import InvalidVersion, Version

_VERSION_PATTERN = re.compile(r"^\s*tesseract\s+v?(?P<version>[0-9][0-9A-Za-z.+-]*)\b", re.IGNORECASE)


class TesseractStatus(Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    WRONG_VERSION = "wrong_version"
    PYTHON_PACKAGE_MISSING = "python_package_missing"
    ERROR = "error"


@dataclass(frozen=True)
class TesseractInfo:
    status: TesseractStatus
    version: str | None
    path: str | None
    detail: str | None = None


def _extract_version(stdout: str, stderr: str) -> str | None:
    """Return a Tesseract version token from trusted version-command output."""
    for output in (stdout, stderr):
        for line in output.splitlines():
            match = _VERSION_PATTERN.match(line)
            if match:
                return match.group("version")
    return None


def _major_version(version: str) -> int | None:
    """Parse known Tesseract version forms while retaining their display token."""
    try:
        return Version(version).major
    except InvalidVersion:
        match = re.match(r"^(\d+)", version)
        return int(match.group(1)) if match else None


def detect_tesseract() -> TesseractInfo:
    """Report whether Haydar can operationally perform image OCR without raising."""
    try:
        package_available = importlib.util.find_spec("pytesseract") is not None
    except Exception:
        return TesseractInfo(TesseractStatus.ERROR, None, None, "Python adapter lookup failed")
    if not package_available:
        return TesseractInfo(TesseractStatus.PYTHON_PACKAGE_MISSING, None, None)

    try:
        path = shutil.which("tesseract")
    except Exception:
        return TesseractInfo(TesseractStatus.ERROR, None, None, "executable lookup failed")
    if path is None:
        return TesseractInfo(TesseractStatus.NOT_FOUND, None, None)

    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TesseractInfo(TesseractStatus.ERROR, None, path, "version probe timed out")
    except PermissionError:
        return TesseractInfo(TesseractStatus.ERROR, None, path, "version probe permission denied")
    except OSError:
        return TesseractInfo(TesseractStatus.ERROR, None, path, "version probe failed")
    except Exception:
        return TesseractInfo(TesseractStatus.ERROR, None, path, "version probe failed")

    if result.returncode != 0:
        return TesseractInfo(TesseractStatus.ERROR, None, path, "version probe exited nonzero")

    version = _extract_version(result.stdout or "", result.stderr or "")
    if version is None:
        return TesseractInfo(TesseractStatus.ERROR, None, path, "malformed version output")
    major_version = _major_version(version)
    if major_version is None:
        return TesseractInfo(TesseractStatus.ERROR, version, path, "malformed version output")
    if major_version < 4:
        return TesseractInfo(TesseractStatus.WRONG_VERSION, version, path)
    return TesseractInfo(TesseractStatus.FOUND, version, path)


def get_tesseract_path() -> str | None:
    """Return the Tesseract executable path when it is available."""
    return shutil.which("tesseract")


def get_install_instructions() -> str:
    """Return installation instructions for Haydar's Python adapter and engine."""
    return """To enable image search, install both OCR components:

  1. Install Haydar's Python OCR adapter:
     pip install "haydar[ocr]"
  2. Install Tesseract from:
     https://github.com/UB-Mannheim/tesseract/wiki
  3. Add Tesseract to your PATH and restart Haydar.

Or install Tesseract via winget:
  winget install UB-Mannheim.TesseractOCR"""

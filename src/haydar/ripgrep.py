"""
ripgrep binary provisioning for Haydar.

Keyword search shells out to ripgrep (`rg`). This module downloads a pinned
ripgrep release, verifies it against a hardcoded SHA-256 checksum, and extracts
the single `rg`/`rg.exe` binary into a destination directory.

Two consumers:
  * ``scripts/pull-rg.py`` -- build-time provisioning (bundled into the EXE).
  * ``haydar init`` -- first-run provisioning for pip installs, via
    :func:`ensure_ripgrep`.

The checksums below MUST be updated in lockstep with ``VERSION``. Never skip
verification: the binary is executed, so an unverified download is a code
execution risk.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "14.1.0"

# SHA-256 checksums from the ripgrep 14.1.0 release. Keep in sync with VERSION.
CHECKSUMS: dict[str, str] = {
    "ripgrep-14.1.0-x86_64-pc-windows-msvc.zip": "fe4f75edfaa50f0d4fecbf47696b7629f3449c9c2c5a4da828753139e5a2e203",
    "ripgrep-14.1.0-x86_64-unknown-linux-musl.tar.gz": "7d44ecba4e88ce6f5e3d7cb834f3c7e7b8c8d810c9c395bcfe83c0722cc7bcfd",
    "ripgrep-14.1.0-aarch64-unknown-linux-musl.tar.gz": "d26e4e37ce74a2ff7bb6cba5f54388e2c0b497b7193630f952f4cda1bcda2c47",
    "ripgrep-14.1.0-x86_64-apple-darwin.tar.gz": "e2f18390bbf8159d28bb5a6435bdcd01ee22513ba9ff18a0026e6ec1d604e0e5",
    "ripgrep-14.1.0-aarch64-apple-darwin.tar.gz": "1de54bd9cf1ef45cf1f31f9ffdc9e95cb50438b46e30eb8a61ce587ec62fdb0b",
}

_BASE_URL = "https://github.com/BurntSushi/ripgrep/releases/download"


class RipgrepError(Exception):
    """Raised when ripgrep cannot be downloaded, verified, or extracted."""


def _executable_name(system: str) -> str:
    return "rg.exe" if system == "windows" else "rg"


def get_release_asset() -> str:
    """Return the ripgrep release asset filename for the current platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        return f"ripgrep-{VERSION}-x86_64-pc-windows-msvc.zip"
    if system == "linux":
        if machine in ("aarch64", "arm64"):
            return f"ripgrep-{VERSION}-aarch64-unknown-linux-musl.tar.gz"
        return f"ripgrep-{VERSION}-x86_64-unknown-linux-musl.tar.gz"
    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return f"ripgrep-{VERSION}-aarch64-apple-darwin.tar.gz"
        return f"ripgrep-{VERSION}-x86_64-apple-darwin.tar.gz"
    raise RipgrepError(f"Unsupported OS: {system} {machine}")


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "haydar"})
    with urllib.request.urlopen(req) as response, open(dest, "wb") as out_file:
        shutil.copyfileobj(response, out_file)


def _sha256(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            hasher.update(block)
    return hasher.hexdigest()


def verify_checksum(filepath: Path, filename: str) -> None:
    """Verify ``filepath`` against the pinned checksum for ``filename``.

    Raises :class:`RipgrepError` on mismatch or when no checksum is known
    (fail closed -- an unknown checksum is treated as an error, not a warning).
    """
    expected = CHECKSUMS.get(filename)
    if not expected:
        raise RipgrepError(f"No pinned checksum for {filename}; refusing to trust it.")
    actual = _sha256(filepath)
    if actual != expected:
        raise RipgrepError(
            f"Checksum mismatch for {filename}!\n"
            f"  expected: {expected}\n  actual:   {actual}"
        )


def _extract(archive_path: Path, dest_dir: Path, system: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    exe = _executable_name(system)
    extracted: Path | None = None

    if archive_path.name.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as z:
            for info in z.infolist():
                if info.filename.endswith(exe):
                    info.filename = exe
                    z.extract(info, dest_dir)
                    extracted = dest_dir / exe
                    break
    elif archive_path.name.endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith(exe) and not member.isdir():
                    member.name = exe
                    # `filter="data"` (3.12+) rejects unsafe tar members as
                    # defense-in-depth; the archive is already hash-pinned.
                    if sys.version_info >= (3, 12):
                        tar.extract(member, dest_dir, filter="data")
                    else:
                        tar.extract(member, dest_dir)
                    extracted = dest_dir / exe
                    if system != "windows":
                        extracted.chmod(0o755)
                    break

    if not extracted or not extracted.exists():
        raise RipgrepError(f"Could not find {exe} inside {archive_path.name}.")
    return extracted


def ensure_ripgrep(dest_dir: Path, cache_dir: Path | None = None) -> Path:
    """Ensure a verified ripgrep binary exists in ``dest_dir``; return its path.

    If the binary already exists it is returned immediately. Otherwise the
    pinned release is downloaded to ``cache_dir`` (default: ``dest_dir/.cache``),
    verified against SHA-256, and the ``rg`` binary extracted into ``dest_dir``.
    """
    system = platform.system().lower()
    dest_dir = Path(dest_dir)
    target = dest_dir / _executable_name(system)
    if target.exists():
        return target

    filename = get_release_asset()
    cache_dir = Path(cache_dir) if cache_dir else dest_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / filename

    url = f"{_BASE_URL}/{VERSION}/{filename}"
    try:
        if not archive_path.exists():
            _download(url, archive_path)
        verify_checksum(archive_path, filename)
        return _extract(archive_path, dest_dir, system)
    except RipgrepError:
        raise
    except Exception as exc:  # network, IO, archive errors
        raise RipgrepError(f"Failed to provision ripgrep: {exc}") from exc

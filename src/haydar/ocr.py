"""OCR detection and private, verified provisioning of the OCR engine.

Two responsibilities, deliberately in one module because they share the same
notion of "which Tesseract are we using":

* **Detection** answers whether Haydar can OCR an image right now, preferring a
  privately provisioned engine over any system installation.
* **Provisioning** installs one pinned engine into ``~/.haydar/ocr/versions/``
  and activates it by atomically replacing ``current.json``.

The provisioning path executes downloaded code, so it is written to fail closed.
Nothing is activated before its archive hash matches the pinned value and the
extracted binary answers ``--version``; archives are never handed to
``extractall``; and any failure or cancellation leaves the previously working
version exactly as it was.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packaging.version import InvalidVersion, Version

from haydar.config import (
    OCR_CURRENT_POINTER,
    OCR_STAGING_DIR,
    OCR_VERSIONS_DIR,
)

logger = logging.getLogger(__name__)

_VERSION_PATTERN = re.compile(r"^\s*tesseract\s+v?(?P<version>[0-9][0-9A-Za-z.+-]*)\b", re.IGNORECASE)

# Bounds that make a hostile or corrupt archive a bounded failure rather than a
# disk-filling one. All are checked before any byte is written.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5_000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

DOWNLOAD_CHUNK_BYTES = 64 * 1024
NETWORK_TIMEOUT_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 20.0

# A manifest entry whose archive hash has not been reviewed yet. Provisioning
# refuses to run against it: an unpinned download would be arbitrary code
# execution, so "not reviewed" must fail rather than fetch.
PENDING_REVIEW = "PENDING_REVIEW"


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


def _windows_tesseract_candidates() -> tuple[Path, ...]:
    """Return conventional Windows locations used by Tesseract installers."""
    if os.name != "nt":
        return ()

    roots = (
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    )
    candidates: list[Path] = []
    for root in roots:
        if not root:
            continue
        base = Path(root)
        if root == os.environ.get("LOCALAPPDATA"):
            base /= "Programs"
        candidate = base / "Tesseract-OCR" / "tesseract.exe"
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _find_system_tesseract() -> str | None:
    """Locate Tesseract through PATH or conventional Windows install directories."""
    path = shutil.which("tesseract")
    if path is not None:
        return path
    for candidate in _windows_tesseract_candidates():
        if candidate.is_file():
            return str(candidate)
    return None


def _find_tesseract_executable() -> str | None:
    """Resolve the engine to use: the private activation wins over the system one.

    Preferring the private install means a user who clicked **Install OCR** gets
    the version Haydar verified, not whatever happens to be on PATH.
    """
    private = active_private_executable()
    if private is not None:
        return str(private)
    return _find_system_tesseract()


def _probe_kwargs() -> dict[str, Any]:
    """Subprocess options shared by every ``tesseract --version`` probe.

    ``stdin=DEVNULL`` matters beyond tidiness: windowed ``haydar.exe`` has no
    console, so an inherited standard handle is invalid and the child fails to
    start with "the handle is invalid" rather than answering. ``CREATE_NO_WINDOW``
    keeps a console from flashing over the GUI.
    """
    return {
        "capture_output": True,
        "text": True,
        "stdin": subprocess.DEVNULL,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def detect_tesseract() -> TesseractInfo:
    """Report whether Haydar can operationally perform image OCR without raising."""
    try:
        package_available = importlib.util.find_spec("pytesseract") is not None
    except Exception:
        return TesseractInfo(TesseractStatus.ERROR, None, None, "Python adapter lookup failed")
    if not package_available:
        return TesseractInfo(TesseractStatus.PYTHON_PACKAGE_MISSING, None, None)

    try:
        path = _find_tesseract_executable()
    except Exception:
        return TesseractInfo(TesseractStatus.ERROR, None, None, "executable lookup failed")
    if path is None:
        return TesseractInfo(TesseractStatus.NOT_FOUND, None, None)

    try:
        result = subprocess.run(
            [path, "--version"],
            timeout=5,
            check=False,
            **_probe_kwargs(),
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
    """Return the Tesseract executable path when it is available.

    The private activation is preferred over any system installation, so the
    engine Haydar verified is the engine Haydar runs.
    """
    return _find_tesseract_executable()


# ── Pinned asset manifest ──────────────────────────────────────────────────────


# A URL that names a moving target rather than one immutable artifact. Pinning a
# hash against one of these is a contradiction: the bytes are free to change
# underneath the pin, so the next download simply fails verification.
_MUTABLE_URL_MARKERS = (
    "/latest/",
    "/main/",
    "/master/",
    "/head/",
    "/current/",
    "/nightly/",
    "/edge/",
)


def _is_loopback(host: str) -> bool:
    """Whether a host is this machine, used only by the local test fixture."""
    return host.lower() in ("127.0.0.1", "localhost", "::1", "[::1]")


@dataclass(frozen=True)
class OcrAsset:
    """Immutable metadata for one reviewed, redistributable OCR distribution.

    Every field is part of the trust decision: the URL says where the bytes come
    from, ``sha256`` says which bytes are acceptable, and the relative paths say
    what must be present before the install is allowed to activate.
    """

    version: str
    platform: str
    architecture: str
    url: str
    archive_filename: str
    sha256: str
    executable_relative_path: str
    data_relative_paths: tuple[str, ...] = ()
    upstream_license_files: tuple[str, ...] = ()
    upstream_project: str = "tesseract-ocr/tesseract"
    source_url: str = ""

    @property
    def is_reviewed(self) -> bool:
        """Whether this entry carries a real pinned hash.

        A placeholder hash means the asset has not been through licensing and
        integrity review, and provisioning must refuse it rather than download
        something it cannot verify.
        """
        return bool(self.sha256) and self.sha256 != PENDING_REVIEW

    @property
    def url_problem(self) -> str | None:
        """Why this URL is unfit to pin against, or ``None`` when it is fine.

        Checked before any download so a future pin cannot quietly ship a
        "latest" URL or plain HTTP. The hash is what makes the bytes
        trustworthy, but a moving URL means the pin can only ever start
        failing, and cleartext lets a network attacker choose which failure the
        user sees.
        """
        if not self.url:
            return "no download URL is recorded"

        parsed = urlparse(self.url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        if scheme not in ("https", "http"):
            return f"unsupported scheme {scheme!r}"
        # http is tolerated only for the loopback fixture the tests serve.
        if scheme == "http" and not _is_loopback(host):
            return "downloads must use https"

        lowered = self.url.lower()
        if any(marker in lowered for marker in _MUTABLE_URL_MARKERS):
            return "the URL names a moving target rather than a fixed release"
        return None

    @property
    def has_immutable_url(self) -> bool:
        return self.url_problem is None

    @property
    def version_token(self) -> str:
        """The cache marker recorded against images this engine processed."""
        return f"tesseract-{self.version}"


# ── Why this manifest still ships unreviewed ───────────────────────────────────
#
# Reviewed 2026-08-10 against the live upstreams. No candidate currently clears
# §15 ("do not bundle an unlicensed/unreviewed native OCR archive"), so the
# entry stays PENDING_REVIEW and one-click install fails closed. What was found:
#
# * Upstream `tesseract-ocr/tesseract` publishes exactly one Windows artifact
#   per release — an NSIS `.exe` installer (5.5.3 → 26,573,224 bytes, GitHub
#   digest sha256:bee9e343…70ae4). There is no portable zip from any
#   authoritative publisher; the "portable zip" repos on GitHub are third-party
#   repacks of old versions.
# * That installer bundles pango, which MSYS2 records as LGPL-2.1 (and pulls
#   glib2/cairo with it), while shipping only Tesseract's own LICENSE, AUTHORS,
#   and README — no per-dependency licence texts. Redistributing it verbatim
#   would therefore convey LGPL components without their required notices.
#   Whether Haydar's distribution satisfies the LGPL relinking terms is a
#   licensing decision an owner has to make, not one this code can assume.
# * The installer also fetches language data from
#   `raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/<lang>.traineddata`
#   — a branch URL whose contents can change, so it cannot be pinned at all.
# * conda-forge's `tesseract-5.5.3-he87eeb8_0.conda` is a genuine zip with a
#   published sha256 and an all-permissive dependency set, but it ships only
#   `tesseract.exe` plus its own DLL: leptonica, libarchive, libcurl, libtiff
#   and the runtime are separate packages. Supporting it means a multi-artifact
#   manifest and a zstd reader, which is a design change rather than a pin.
# * `eng.traineddata` on its own is clean and pinnable today: Apache-2.0, and
#   the commit-pinned URL below was downloaded and hashed during this review.
#   It is recorded here so a future pin does not have to re-derive it.
#
# To finish: choose a distribution, settle the licensing question, record the
# outcome in THIRD_PARTY_NOTICES.md (§15), then set `url`/`sha256` and update
# `test_the_shipped_manifest_entry_is_marked_unreviewed`.

# Verified 2026-08-10: 4,113,088 bytes, Apache-2.0, commit-pinned and therefore
# immutable. Kept as reviewed evidence for whoever pins the engine itself.
ENG_TRAINEDDATA_URL = (
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/"
    "87416418657359cb625c412a48b6e1d6d41c29bd/eng.traineddata"
)
ENG_TRAINEDDATA_SHA256 = (
    "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
)

OCR_ASSETS: tuple[OcrAsset, ...] = (
    OcrAsset(
        version="5.5.3",
        platform="windows",
        architecture="x86_64",
        url="",
        archive_filename="tesseract-5.5.3-win64.zip",
        sha256=PENDING_REVIEW,
        executable_relative_path="tesseract.exe",
        data_relative_paths=("tessdata/eng.traineddata",),
        upstream_license_files=("LICENSE",),
        source_url="https://github.com/tesseract-ocr/tesseract",
    ),
)


class OcrPhase(Enum):
    """Typed provisioning phases, reported as they are entered."""

    CHECKING = "checking"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    EXTRACTING = "extracting"
    PROBING = "probing"
    ACTIVATING = "activating"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class OcrEvent:
    """One provisioning progress report."""

    phase: OcrPhase
    message: str
    completed: int | None = None
    total: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class OcrInstallResult:
    """The outcome of a provisioning attempt."""

    phase: OcrPhase
    version: str = ""
    executable_path: str = ""
    message: str = ""
    error_code: str | None = None

    @property
    def ready(self) -> bool:
        return self.phase is OcrPhase.COMPLETE

    @property
    def version_token(self) -> str:
        """Marker recorded on images this engine processed, for later refresh."""
        return f"tesseract-{self.version}" if self.version else ""


class OcrProvisionError(Exception):
    """Provisioning failed. ``retryable`` distinguishes offline from rejected."""

    def __init__(
        self, message: str, *, error_code: str, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class OcrCancelled(Exception):
    """The caller cancelled provisioning between two bounded steps."""


# ── Activation pointer ─────────────────────────────────────────────────────────


def read_active_pointer() -> dict[str, Any] | None:
    """Return the parsed activation pointer, or ``None`` when absent/unusable."""
    try:
        raw = json.loads(OCR_CURRENT_POINTER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def active_private_executable() -> Path | None:
    """Return the activated private engine, or ``None`` if there isn't one.

    The pointer is only believed as far as the filesystem confirms it: a version
    directory that was removed underneath us reads as "no private engine"
    instead of a path that would fail on exec.
    """
    pointer = read_active_pointer()
    if not pointer:
        return None
    version = pointer.get("version")
    relative = pointer.get("executable_relative_path")
    if not isinstance(version, str) or not isinstance(relative, str):
        return None
    candidate = OCR_VERSIONS_DIR / version / relative
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _write_active_pointer(asset: OcrAsset, version_dir: Path) -> None:
    """Atomically publish the new active version.

    ``os.replace`` is what makes activation a single observable step: a reader
    sees either the old version or the new one, never a half-written pointer.
    """
    payload = json.dumps(
        {
            "version": asset.version,
            "executable_relative_path": asset.executable_relative_path,
            "installed_at": int(time.time()),
            "upstream_project": asset.upstream_project,
            "source_url": asset.source_url,
            "sha256": asset.sha256,
        },
        indent=2,
    )
    OCR_CURRENT_POINTER.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(OCR_CURRENT_POINTER.parent),
            prefix=".current.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, OCR_CURRENT_POINTER)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    _ = version_dir  # activation is by pointer, not by moving the active dir


# ── Safe extraction ────────────────────────────────────────────────────────────


def _reject(reason: str, *, code: str = "unsafe_archive") -> OcrProvisionError:
    return OcrProvisionError(f"Refusing the OCR archive: {reason}", error_code=code)


def _safe_member_target(name: str, destination: Path) -> Path:
    """Resolve an archive member to a path inside ``destination``, or reject it.

    Everything here is a rejection rule rather than a sanitisation rule. Quietly
    rewriting a hostile path would extract *something* from an archive that
    already proved it should not be trusted.
    """
    if not name or name in (".", ".."):
        raise _reject(f"invalid member name {name!r}")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise _reject(f"absolute path member {name!r}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise _reject(f"drive-qualified member {name!r}")
    if normalized.startswith("//") or normalized.startswith("\\\\"):
        raise _reject(f"UNC member {name!r}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise _reject(f"path traversal member {name!r}")
    if not parts:
        raise _reject(f"empty member path {name!r}")

    base = destination.resolve()
    target = base.joinpath(*parts)
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise _reject(f"unresolvable member {name!r}") from exc
    # The decisive check: whatever the name looked like, the result must land
    # under the staging directory.
    if resolved != base and base not in resolved.parents:
        raise _reject(f"member escapes the staging directory: {name!r}")
    return target


def _is_symlink_member(info: zipfile.ZipInfo) -> bool:
    """Whether a zip entry claims to be a link or other non-regular file.

    The external attribute's upper 16 bits carry a Unix mode. Writers disagree
    about whether the file-type bits are present: a bare permission mask like
    ``0o600`` means "regular file" under the Info-ZIP convention, while an
    explicit ``S_IFLNK`` means the entry is a link.
    """
    mode = info.external_attr >> 16
    if not mode:
        return False
    file_type = stat.S_IFMT(mode)
    if file_type == 0:
        # No file-type bits: conventional regular-file marker, not a link.
        return False
    # A symlink, fifo, device, or socket is never an acceptable member.
    return file_type not in (stat.S_IFREG, stat.S_IFDIR)


def safe_extract_zip(archive_path: Path, destination: Path) -> int:
    """Extract a zip archive member by member, rejecting anything unsafe.

    Never uses ``extractall``: that would honour whatever paths the archive
    contains. Returns the number of files written.

    Rejections: traversal, absolute and drive-qualified paths, UNC paths,
    symlinks and other non-regular entries, too many members, and declared or
    actual expansion beyond the configured ceiling.
    """
    destination.mkdir(parents=True, exist_ok=True)
    written = 0

    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise _reject(
                f"{len(infos)} members exceeds the {MAX_ARCHIVE_MEMBERS} limit",
                code="archive_too_many_files",
            )

        declared_total = sum(info.file_size for info in infos)
        if declared_total > MAX_EXPANDED_BYTES:
            raise _reject(
                f"declared expansion of {declared_total} bytes exceeds the limit",
                code="archive_too_large",
            )
        compressed_total = sum(info.compress_size for info in infos)
        if (
            compressed_total > 0
            and declared_total / compressed_total > MAX_COMPRESSION_RATIO
        ):
            raise _reject(
                f"compression ratio {declared_total // max(1, compressed_total)}:1 "
                "looks like a decompression bomb",
                code="archive_bomb",
            )

        extracted_bytes = 0
        for info in infos:
            target = _safe_member_target(info.filename, destination)
            if _is_symlink_member(info):
                raise _reject(f"link or special member {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            # Copied in bounded chunks and counted as it lands, so a member that
            # lied about ``file_size`` is still caught mid-write.
            with archive.open(info) as source, open(target, "wb") as sink:
                while True:
                    chunk = source.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    extracted_bytes += len(chunk)
                    if extracted_bytes > MAX_EXPANDED_BYTES:
                        raise _reject(
                            "expanded size exceeds the limit",
                            code="archive_too_large",
                        )
                    sink.write(chunk)
            written += 1

    return written


# ── The provisioner ────────────────────────────────────────────────────────────


class OcrProvisioner:
    """Download, verify, extract, probe, and atomically activate one OCR engine.

    The whole class is one transaction with a single commit point: replacing
    ``current.json``. Everything before it happens in staging, so a failure at
    any step leaves the previously active version untouched and usable.
    """

    def __init__(
        self,
        asset: OcrAsset | None = None,
        *,
        progress_callback: Callable[[OcrEvent], None] | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.asset = asset if asset is not None else select_asset()
        self._progress = progress_callback
        # Injected so tests drive a local HTTP fixture; production uses urllib.
        self._opener = opener or urllib.request.urlopen

    # -- reporting ---------------------------------------------------------

    def _emit(
        self,
        phase: OcrPhase,
        message: str,
        *,
        completed: int | None = None,
        total: int | None = None,
        error_code: str | None = None,
    ) -> None:
        if self._progress is None:
            return
        try:
            self._progress(
                OcrEvent(
                    phase=phase,
                    message=message,
                    completed=completed,
                    total=total,
                    error_code=error_code,
                )
            )
        except Exception:
            logger.exception("OCR progress callback failed")

    @staticmethod
    def _check_cancelled(cancel_event) -> None:
        """Cancellation is checked before and after every blocking step."""
        if cancel_event is not None and cancel_event.is_set():
            raise OcrCancelled()

    # -- steps -------------------------------------------------------------

    def _download(self, destination: Path, cancel_event) -> None:
        """Stream the archive to staging, cancellable between chunks."""
        self._check_cancelled(cancel_event)
        url = self.asset.url
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("https", "http"):
            # http is permitted only because the local test fixture uses it; the
            # pinned hash, not the transport, is what makes the bytes trusted.
            raise OcrProvisionError(
                f"Unsupported download scheme {scheme!r}.", error_code="bad_url"
            )

        request = urllib.request.Request(url, headers={"User-Agent": "haydar"})
        try:
            response = self._opener(request, timeout=NETWORK_TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Offline is retryable: the user can click again with a connection.
            raise OcrProvisionError(
                f"Could not reach the download server: {exc}",
                error_code="offline",
                retryable=True,
            ) from exc

        total: int | None = None
        with contextlib.closing(response):
            with contextlib.suppress(Exception):
                length = response.headers.get("Content-Length")
                total = int(length) if length else None
            if total is not None and total > MAX_ARCHIVE_BYTES:
                raise _reject(
                    f"declared download size {total} exceeds the limit",
                    code="archive_too_large",
                )

            received = 0
            with open(destination, "wb") as sink:
                while True:
                    self._check_cancelled(cancel_event)
                    try:
                        chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    except (urllib.error.URLError, OSError) as exc:
                        raise OcrProvisionError(
                            f"The download was interrupted: {exc}",
                            error_code="offline",
                            retryable=True,
                        ) from exc
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_ARCHIVE_BYTES:
                        raise _reject(
                            "download exceeded the size limit",
                            code="archive_too_large",
                        )
                    sink.write(chunk)
                    self._emit(
                        OcrPhase.DOWNLOADING,
                        "Downloading text recognition…",
                        completed=received,
                        total=total,
                    )
        self._check_cancelled(cancel_event)

    def _verify(self, archive_path: Path, cancel_event) -> None:
        """Hash the staged archive and compare it with the pinned value."""
        self._check_cancelled(cancel_event)
        self._emit(OcrPhase.VERIFYING, "Verifying the download…")
        digest = hashlib.sha256()
        with open(archive_path, "rb") as handle:
            while True:
                self._check_cancelled(cancel_event)
                chunk = handle.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        actual = digest.hexdigest()
        # Constant-time: the comparison is against a secret-shaped value, and a
        # timing-independent check costs nothing here.
        if not hmac.compare_digest(actual, self.asset.sha256.lower()):
            raise OcrProvisionError(
                "The downloaded file did not match its expected contents.",
                error_code="checksum_mismatch",
            )

    def _verify_contents(self, staging_dir: Path) -> Path:
        """Confirm the required executable, language data, and licences exist."""
        executable = staging_dir / self.asset.executable_relative_path
        if not executable.is_file():
            raise OcrProvisionError(
                "The archive did not contain the expected program.",
                error_code="missing_executable",
            )
        for relative in self.asset.data_relative_paths:
            if not (staging_dir / relative).is_file():
                raise OcrProvisionError(
                    f"The archive is missing required data: {relative}",
                    error_code="missing_data",
                )
        for relative in self.asset.upstream_license_files:
            if not (staging_dir / relative).is_file():
                # Licence files must ship with the activated version, so a build
                # missing them is not activatable.
                raise OcrProvisionError(
                    f"The archive is missing its licence file: {relative}",
                    error_code="missing_license",
                )
        return executable

    def _probe(self, executable: Path, cancel_event) -> str:
        """Run ``tesseract --version`` with no shell and a bounded timeout."""
        self._check_cancelled(cancel_event)
        self._emit(OcrPhase.PROBING, "Checking the installation…")
        try:
            result = subprocess.run(
                [str(executable), "--version"],
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
                shell=False,
                **_probe_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrProvisionError(
                "The installed program did not respond.", error_code="probe_timeout"
            ) from exc
        except OSError as exc:
            raise OcrProvisionError(
                f"The installed program could not be started: {exc}",
                error_code="probe_failed",
            ) from exc

        if result.returncode != 0:
            raise OcrProvisionError(
                "The installed program reported an error.", error_code="probe_failed"
            )
        version = _extract_version(result.stdout or "", result.stderr or "")
        if version is None:
            raise OcrProvisionError(
                "The installed program did not identify itself.",
                error_code="probe_failed",
            )
        return version

    # -- transaction -------------------------------------------------------

    def install(self, *, cancel_event=None, force: bool = False) -> OcrInstallResult:
        """Provision OCR, returning a typed result. Never partially activates."""
        self._emit(OcrPhase.CHECKING, "Checking for text recognition…")

        if not force:
            existing = active_private_executable()
            if existing is not None:
                pointer = read_active_pointer() or {}
                return OcrInstallResult(
                    phase=OcrPhase.COMPLETE,
                    version=str(pointer.get("version", "")),
                    executable_path=str(existing),
                    message="Text recognition is already installed.",
                )

        if not self.asset.is_reviewed:
            # Fail closed: downloading against an unreviewed hash would mean
            # executing bytes nobody verified.
            raise OcrProvisionError(
                "Automatic text recognition setup is not available in this build.",
                error_code="asset_not_reviewed",
            )
        problem = self.asset.url_problem
        if problem is not None:
            # A reviewed hash against a moving or cleartext URL is not a pin.
            # Refusing here keeps the failure at setup time rather than turning
            # into a confusing checksum mismatch after a large download.
            logger.error("Refusing the OCR asset URL: %s", problem)
            raise OcrProvisionError(
                "Automatic text recognition setup is not available in this build.",
                error_code="asset_not_reviewed",
            )

        OCR_STAGING_DIR.mkdir(parents=True, exist_ok=True)
        OCR_VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
        # Uniquely named so two concurrent attempts cannot share staging state.
        staging_root = Path(
            tempfile.mkdtemp(prefix=f"install-{self.asset.version}-", dir=str(OCR_STAGING_DIR))
        )
        archive_path = staging_root / self.asset.archive_filename
        extract_dir = staging_root / "unpacked"

        try:
            self._emit(OcrPhase.DOWNLOADING, "Downloading text recognition…")
            self._download(archive_path, cancel_event)
            self._verify(archive_path, cancel_event)

            self._check_cancelled(cancel_event)
            self._emit(OcrPhase.EXTRACTING, "Unpacking text recognition…")
            safe_extract_zip(archive_path, extract_dir)
            self._check_cancelled(cancel_event)

            executable = self._verify_contents(extract_dir)
            probed_version = self._probe(executable, cancel_event)

            self._check_cancelled(cancel_event)
            self._emit(OcrPhase.ACTIVATING, "Finishing setup…")
            version_dir = self._promote(extract_dir)
            _write_active_pointer(self.asset, version_dir)

            active = version_dir / self.asset.executable_relative_path
            self._emit(OcrPhase.COMPLETE, "Text recognition is ready.")
            logger.info("Activated private OCR engine %s", self.asset.version)
            return OcrInstallResult(
                phase=OcrPhase.COMPLETE,
                version=probed_version or self.asset.version,
                executable_path=str(active),
                message="Text recognition is ready.",
            )
        except OcrCancelled:
            self._emit(OcrPhase.CANCELLED, "Setup cancelled.")
            return OcrInstallResult(
                phase=OcrPhase.CANCELLED, message="Setup cancelled."
            )
        except OcrProvisionError as exc:
            self._emit(OcrPhase.FAILED, str(exc), error_code=exc.error_code)
            raise
        finally:
            # Best-effort: leaving staging behind is untidy, but failing here
            # would turn a successful install into a reported failure.
            shutil.rmtree(staging_root, ignore_errors=True)

    def _promote(self, extract_dir: Path) -> Path:
        """Move the verified staging tree into its version directory.

        A pre-existing directory for this version is replaced only after the new
        tree is verified, and the old one is kept under a temporary name until
        the move succeeds so a failed rename cannot leave nothing behind.
        """
        version_dir = OCR_VERSIONS_DIR / self.asset.version
        backup: Path | None = None
        if version_dir.exists():
            backup = version_dir.with_name(f"{self.asset.version}.replaced-{int(time.time())}")
            os.replace(version_dir, backup)
        try:
            os.replace(extract_dir, version_dir)
        except OSError as exc:
            if backup is not None:
                with contextlib.suppress(OSError):
                    os.replace(backup, version_dir)
            raise OcrProvisionError(
                f"Could not finish the installation: {exc}", error_code="activate_failed"
            ) from exc
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        return version_dir


def select_asset(
    *, platform_name: str | None = None, architecture: str | None = None
) -> OcrAsset:
    """Return the pinned asset for this platform.

    Raises rather than guessing: an unknown platform has no reviewed asset, and
    substituting a different one would defeat the point of pinning.
    """
    import platform as platform_module

    system = (platform_name or platform_module.system()).lower()
    machine = (architecture or platform_module.machine()).lower()
    normalized_arch = "x86_64" if machine in ("amd64", "x86_64") else machine

    for asset in OCR_ASSETS:
        if asset.platform == system and asset.architecture == normalized_arch:
            return asset
    raise OcrProvisionError(
        f"No reviewed text recognition download exists for {system} {machine}.",
        error_code="unsupported_platform",
    )


def ocr_status() -> TesseractInfo:
    """Report current OCR readiness, private installation preferred."""
    return detect_tesseract()


def install_ocr(
    *,
    cancel_event=None,
    progress_callback: Callable[[OcrEvent], None] | None = None,
    asset: OcrAsset | None = None,
    force: bool = False,
) -> OcrInstallResult:
    """Provision OCR in one call. This is the GUI's and CLI's entry point.

    Offline-safe: if a verified private engine or a supported system engine is
    already usable, it returns ready without touching the network.
    """
    if not force:
        existing = active_private_executable()
        if existing is None:
            info = detect_tesseract()
            if info.status is TesseractStatus.FOUND and info.path:
                # A supported system installation is used as-is and never
                # modified; provisioning would add nothing.
                return OcrInstallResult(
                    phase=OcrPhase.COMPLETE,
                    version=info.version or "",
                    executable_path=info.path,
                    message="Text recognition is already available.",
                )

    provisioner = OcrProvisioner(asset, progress_callback=progress_callback)
    return provisioner.install(cancel_event=cancel_event, force=force)


def _any_asset_is_reviewed() -> bool:
    """Report whether provisioning has an asset it is actually allowed to fetch."""
    return any(asset.is_reviewed for asset in OCR_ASSETS)


def get_install_instructions() -> str:
    """Return the user-facing explanation of how image text search is enabled.

    The wording follows the manifest. With a reviewed asset it describes the
    one-click install. With none — the shipped state — it describes the manual
    engine install, because saying only "unavailable" would withhold a fix that
    works: a self-installed engine is detected and used (§19, amended 2026-08-11
    to permit documenting it once one-click provisioning proved undeliverable).

    Deliberately free of package managers and PATH edits: the engine is found at
    its default install location, so the copy stays at "install it, restart"
    rather than turning into shell instructions a normal user should not need.
    """
    privacy = (
        "Your files are never uploaded — recognition runs entirely on this "
        "computer."
    )
    if not _any_asset_is_reviewed():
        return (
            "Image text search needs a text recognition engine, and Haydar "
            "cannot install one for you in this build.\n\n"
            "Install Tesseract OCR for Windows (version 4 or newer, including "
            "English language data) using the installer linked from the "
            "Tesseract project's own documentation, keeping the default "
            "location. That is the only thing you need; Haydar includes "
            "everything else.\n\n"
            "Restart Haydar afterwards and it finds the engine on its own. "
            "Images found before then are remembered and are added to search "
            "without a reindex.\n\n" + privacy
        )
    return (
        "Image text search needs Haydar's text recognition engine.\n\n"
        "Choose Install OCR and Haydar downloads and verifies it privately, "
        "then adds text from your images to search. Images found before then "
        "are remembered and indexed automatically once it is ready.\n\n"
        "The one-time download needs an internet connection. " + privacy
    )

"""GUI-neutral first-run provisioning and bounded folder inspection."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from haydar.config import (
    ALL_INDEXABLE_EXTENSIONS,
    RIPGREP_DIR,
    HaydarConfig,
    is_excluded,
)
from haydar.lifecycle import IndexLifecycle
from haydar.ripgrep import ensure_ripgrep
from haydar.search.store import VectorStore

logger = logging.getLogger(__name__)

# Bounds for the pre-inclusion folder scan. These exist so a user who points
# Haydar at a network share or a whole drive gets a warning in about a second
# rather than a frozen dialog.
SUPPORTED_FILE_WARNING_THRESHOLD = 10_000
VISITED_ENTRY_HARD_CAP = 50_000
SOFT_WARNING_SECONDS = 1.5
HARD_STOP_SECONDS = 3.0

# Bounded probes: a hung binary must not stall first run indefinitely.
RIPGREP_PROBE_TIMEOUT_SECONDS = 10.0
_PROBE_TEXT = "haydar search readiness probe"


class SetupPhase(Enum):
    """Stable phases emitted by :class:`SetupCoordinator`."""

    PREPARING_DIRECTORIES = "preparing_directories"
    # Kept under the historical name so existing callers and tests keep working;
    # the phase now also verifies the binary it found.
    PREPARING_KEYWORD_SEARCH = "verifying_keyword_search"
    PREPARING_MODEL = "preparing_model"
    PREPARING_COLLECTION = "preparing_collection"
    SEARCH_READY = "search_ready"


SetupStatus = Literal["started", "progress", "completed", "failed", "cancelled"]


@dataclass(frozen=True)
class SetupEvent:
    """A single observable step of first-run provisioning."""

    phase: SetupPhase
    status: SetupStatus
    message: str
    completed: int | None = None
    total: int | None = None
    error_code: str | None = None


# ``SetupProgress`` is the historical name for this event. It remains an alias so
# existing GUI/CLI callers and tests continue to work unchanged.
SetupProgress = SetupEvent


@dataclass(frozen=True)
class FolderScanResult:
    """A bounded estimate used before an optional folder is accepted."""

    supported_files: int
    visited_files: int
    capped: bool
    timed_out: bool
    inaccessible_directories: int
    elapsed_seconds: float = 0.0
    soft_timed_out: bool = False
    is_network_root: bool = False
    is_drive_root: bool = False

    @property
    def incomplete(self) -> bool:
        return self.capped or self.timed_out

    @property
    def needs_confirmation(self) -> bool:
        """Whether the user must confirm before this folder is persisted.

        Any bound being reached means the count is an estimate rather than a
        measurement, so the user is warned instead of being shown a number that
        looks authoritative.
        """
        return (
            self.capped
            or self.timed_out
            or self.soft_timed_out
            or self.is_network_root
            or self.is_drive_root
            or self.supported_files >= SUPPORTED_FILE_WARNING_THRESHOLD
        )


class SetupCancelled(Exception):
    """Raised when first-run provisioning is cancelled cooperatively."""


class _Store(Protocol):
    def get_stats(self) -> dict: ...


ProgressCallback = Callable[[SetupProgress], None]
StoreFactory = Callable[[HaydarConfig, bool], _Store]
RipgrepProvisioner = Callable[[Path], Path]
RipgrepLocator = Callable[[], Path | None]


def locate_ripgrep() -> Path | None:
    """Return an already-present ripgrep binary, or ``None`` if there is none.

    Resolution goes through :func:`haydar.config.get_rg_path`, which is the same
    lookup keyword search itself performs — bundle first, then the private
    ``~/.haydar/bin`` copy, then the development tree. Using it here is what
    makes setup verify *the binary that will actually run*: probing a freshly
    downloaded copy while search executes the bundled one would leave the
    executed file unverified, and would make a packaged first run download
    something it already ships.
    """
    from haydar.config import HaydarConfigError, get_rg_path

    try:
        return Path(get_rg_path())
    except HaydarConfigError:
        return None



def is_network_root(folder: Path) -> bool:
    """Whether a path is a UNC/network share root such as ``\\\\server\\share``."""
    text = str(folder)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if os.name == "nt":
        try:
            import ctypes

            drive = os.path.splitdrive(os.path.abspath(text))[0]
            if drive:
                # DRIVE_REMOTE == 4
                return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == 4
        except Exception:
            logger.debug("Could not classify drive type for %s", folder, exc_info=True)
    return False


def is_drive_root(folder: Path) -> bool:
    """Whether a path is the root of a whole drive (``C:\\``, ``/``)."""
    resolved = Path(os.path.abspath(str(folder)))
    return resolved.parent == resolved


def scan_folder(
    folder: Path,
    config: HaydarConfig,
    *,
    file_cap: int = SUPPORTED_FILE_WARNING_THRESHOLD,
    visited_cap: int = VISITED_ENTRY_HARD_CAP,
    deadline_seconds: float = SOFT_WARNING_SECONDS,
    hard_deadline_seconds: float | None = None,
    cancel_event: threading.Event | None = None,
) -> FolderScanResult:
    """Estimate a folder's indexable size without letting a huge tree stall the UI.

    The result is deliberately an estimate whenever a bound is reached; the
    caller warns rather than presenting the number as a measurement. Paths are
    counted, never collected, so scanning a million-entry share costs constant
    memory. Errors on individual children are counted and skipped, so one
    unreadable subdirectory does not make an otherwise valid folder unusable.

    ``deadline_seconds`` is the soft threshold at which the result is flagged as
    slow; ``hard_deadline_seconds`` stops the walk outright. Raising the soft
    deadline alone also raises the hard stop, so a caller asking for a longer
    scan gets one rather than an argument error.
    """
    if file_cap <= 0:
        raise ValueError("file_cap must be positive")
    if visited_cap <= 0:
        raise ValueError("visited_cap must be positive")
    if deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    if hard_deadline_seconds is None:
        hard_deadline_seconds = max(HARD_STOP_SECONDS, deadline_seconds * 2)
    if hard_deadline_seconds < deadline_seconds:
        raise ValueError("hard_deadline_seconds must be >= deadline_seconds")

    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist or is not a directory: {folder}")

    started = time.monotonic()
    supported = 0
    visited = 0
    inaccessible = 0
    capped = False
    timed_out = False
    soft_timed_out = False

    def on_error(_exc: OSError) -> None:
        nonlocal inaccessible
        inaccessible += 1

    for root, dirs, files in os.walk(folder, onerror=on_error):
        if cancel_event is not None and cancel_event.is_set():
            raise SetupCancelled("Folder scan cancelled")

        elapsed = time.monotonic() - started
        if elapsed >= hard_deadline_seconds:
            timed_out = True
            break
        if elapsed >= deadline_seconds:
            soft_timed_out = True

        root_path = Path(root)
        relative_root = root_path.relative_to(folder)
        dirs[:] = [
            name
            for name in dirs
            if not is_excluded(relative_root / name, config.excluded_patterns)
        ]
        for name in files:
            visited += 1
            if visited >= visited_cap:
                capped = True
                break

            relative_path = relative_root / name
            if (
                Path(name).suffix.lower() in ALL_INDEXABLE_EXTENSIONS
                and not is_excluded(relative_path, config.excluded_patterns)
            ):
                supported += 1
                if supported >= file_cap:
                    capped = True
                    break

            # Checking the clock every entry would dominate the scan on fast
            # local disks, so sample it instead.
            if visited % 512 == 0:
                elapsed = time.monotonic() - started
                if elapsed >= hard_deadline_seconds:
                    timed_out = True
                    break
                if elapsed >= deadline_seconds:
                    soft_timed_out = True
        if capped or timed_out:
            break

    return FolderScanResult(
        supported_files=supported,
        visited_files=visited,
        capped=capped,
        timed_out=timed_out,
        inaccessible_directories=inaccessible,
        elapsed_seconds=time.monotonic() - started,
        soft_timed_out=soft_timed_out,
        is_network_root=is_network_root(folder),
        is_drive_root=is_drive_root(folder),
    )


class SetupError(Exception):
    """A setup failure carrying a stable code so callers can offer the right retry."""

    def __init__(self, message: str, *, code: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        # ``retryable`` distinguishes "try again when you have a network" from
        # "this will fail identically next time".
        self.retryable = code in {"offline", "keyword_search_unavailable", "model_unavailable"}


class SetupCoordinator:
    """Prepare Haydar for search without waiting for initial indexing.

    This class contains no Qt dependencies. GUI workers and expert CLI commands
    can call it while supplying their own progress and cancellation primitives.
    """

    def __init__(
        self,
        config: HaydarConfig,
        *,
        store_factory: StoreFactory | None = None,
        ripgrep_provisioner: RipgrepProvisioner = ensure_ripgrep,
        ripgrep_verifier: Callable[[Path], None] | None = None,
        ripgrep_locator: RipgrepLocator = locate_ripgrep,
    ) -> None:
        self.config = config
        self._store_factory = store_factory or (
            lambda current_config, allow_download: VectorStore(
                current_config,
                allow_download=allow_download,
            )
        )
        self._ripgrep_provisioner = ripgrep_provisioner
        self._ripgrep_verifier = ripgrep_verifier or _probe_ripgrep
        self._ripgrep_locator = ripgrep_locator
        self.lifecycle = IndexLifecycle(config)

    def prepare_search(
        self,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> HaydarConfig:
        """Provision required search components and persist readiness atomically.

        Initial indexing is intentionally not started here. Callers show the
        search UI as soon as this returns, then launch the resumable index job in
        a separate worker.

        A failure or cancellation leaves ``search_ready`` false but preserves the
        user's folders and every asset already verified and activated, so
        retrying never redoes verified work.
        """
        self._check_cancelled(cancel_event)
        self._started(
            progress_callback,
            SetupPhase.PREPARING_DIRECTORIES,
            "Preparing Haydar's private data folders…",
        )
        self.config.ensure_dirs()
        self.config.folders_configured = bool(self.config.folders)
        self.config.search_ready = False
        self.config.initial_index_error = ""
        self.config.save()
        self._completed(
            progress_callback, SetupPhase.PREPARING_DIRECTORIES, "Data folders ready."
        )

        self._check_cancelled(cancel_event)
        self._started(
            progress_callback,
            SetupPhase.PREPARING_KEYWORD_SEARCH,
            "Preparing instant keyword search…",
        )
        rg_path = self._prepare_keyword_search(progress_callback)
        self._check_cancelled(cancel_event)
        self._completed(
            progress_callback,
            SetupPhase.PREPARING_KEYWORD_SEARCH,
            f"Keyword search ready ({Path(rg_path).name}).",
        )

        self._check_cancelled(cancel_event)
        self._started(
            progress_callback,
            SetupPhase.PREPARING_MODEL,
            "Downloading or verifying the search model…",
        )
        store = self._prepare_model(progress_callback)
        self._check_cancelled(cancel_event)
        self._completed(
            progress_callback, SetupPhase.PREPARING_MODEL, "Search model verified."
        )

        self._check_cancelled(cancel_event)
        self._started(
            progress_callback,
            SetupPhase.PREPARING_COLLECTION,
            "Opening the search index…",
        )
        self._prepare_collection(store, progress_callback)
        self._check_cancelled(cancel_event)
        self._completed(
            progress_callback, SetupPhase.PREPARING_COLLECTION, "Search index ready."
        )

        self._check_cancelled(cancel_event)
        # Readiness is persisted before it is announced: a search window opened
        # against readiness that did not survive a crash would be a lie.
        self.lifecycle.mark_search_ready()
        self._completed(
            progress_callback,
            SetupPhase.SEARCH_READY,
            "Search is ready. Indexing will continue in the background.",
        )
        return self.config

    # -- phases ------------------------------------------------------------

    def _prepare_keyword_search(self, progress_callback: ProgressCallback | None) -> Path:
        """Locate or provision ripgrep, then verify the binary actually runs.

        An already-present binary — bundled in the packaged app, or the private
        copy under ``~/.haydar/bin`` — is used rather than re-downloaded, so a
        packaged first run needs no network for keyword search. Whichever binary
        is resolved is then re-verified rather than trusted: a truncated or
        replaced file on disk is exactly the case a first-run check should catch,
        and the file is executed later.
        """
        rg_path = self._ripgrep_locator()
        if rg_path is None:
            try:
                rg_path = Path(self._ripgrep_provisioner(RIPGREP_DIR))
            except Exception as exc:
                raise SetupError(
                    "Keyword search could not be prepared because ripgrep is unavailable.",
                    code="keyword_search_unavailable",
                    hint=(
                        "Check your internet connection and try again. Semantic search "
                        "will still work once setup completes."
                    ),
                ) from exc

        try:
            self._ripgrep_verifier(rg_path)
        except SetupError:
            raise
        except Exception as exc:
            raise SetupError(
                "The keyword search program could not be verified.",
                code="keyword_search_unavailable",
                hint="Try setup again; Haydar will download a fresh verified copy.",
            ) from exc
        return rg_path

    def _prepare_model(self, progress_callback: ProgressCallback | None) -> _Store:
        """Resolve the configured embedding model and confirm it can embed text.

        Download is permitted only for this call. Constructing the store is not
        sufficient evidence: the model is only proven usable once it produces an
        embedding.
        """
        try:
            store = self._store_factory(self.config, True)
        except Exception as exc:
            raise SetupError(
                f"The search model '{self.config.embedding_model}' could not be prepared.",
                code=_classify_provisioning_error(exc),
                hint=(
                    "Connect to the internet and try again. The model is downloaded "
                    "once and then works entirely offline."
                ),
            ) from exc

        probe = getattr(store, "embed_probe", None)
        if callable(probe):
            try:
                probe(_PROBE_TEXT)
            except Exception as exc:
                raise SetupError(
                    f"The search model '{self.config.embedding_model}' could not "
                    "process text.",
                    code=_classify_provisioning_error(exc),
                    hint="Try setup again to re-download the model.",
                ) from exc
        return store

    def _prepare_collection(
        self, store: _Store, progress_callback: ProgressCallback | None
    ) -> None:
        """Confirm the vector collection can be opened, counted, and queried.

        This never clears or recreates the collection: an existing user's index
        must survive setup untouched.
        """
        try:
            store.get_stats()
        except Exception as exc:
            raise SetupError(
                "The search index could not be opened.",
                code="collection_unavailable",
                hint=(
                    "Close any other running copy of Haydar and try again. Your "
                    "files are unaffected."
                ),
            ) from exc

        capability_check = getattr(store, "verify_readable", None)
        if callable(capability_check):
            try:
                capability_check()
            except Exception as exc:
                raise SetupError(
                    "The search index could not answer a test query.",
                    code="collection_unavailable",
                    hint=(
                        "Close any other running copy of Haydar and try again. If "
                        "this persists, rebuild the index from Settings."
                    ),
                ) from exc

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _check_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise SetupCancelled("Setup cancelled")

    @classmethod
    def _started(
        cls,
        callback: ProgressCallback | None,
        phase: SetupPhase,
        message: str,
    ) -> None:
        cls._report(callback, phase, message, "started")

    @classmethod
    def _completed(
        cls,
        callback: ProgressCallback | None,
        phase: SetupPhase,
        message: str,
    ) -> None:
        cls._report(callback, phase, message, "completed")

    @staticmethod
    def _report(
        callback: ProgressCallback | None,
        phase: SetupPhase,
        message: str,
        status: SetupStatus = "progress",
        *,
        error_code: str | None = None,
    ) -> None:
        if callback is not None:
            callback(
                SetupEvent(
                    phase=phase,
                    status=status,
                    message=message,
                    error_code=error_code,
                )
            )


def _classify_provisioning_error(exc: Exception) -> str:
    """Map a provisioning failure to a stable code, favouring offline detection."""
    detail = str(exc).lower()
    offline_markers = (
        "connection",
        "network",
        "timed out",
        "timeout",
        "unreachable",
        "temporary failure in name resolution",
        "getaddrinfo",
        "offline",
        "max retries",
    )
    if any(marker in detail for marker in offline_markers):
        return "offline"
    return "model_unavailable"


def _probe_ripgrep(rg_path: Path) -> None:
    """Run a bounded ``rg --version`` so a broken binary fails setup, not search."""
    if not Path(rg_path).is_file():
        raise SetupError(
            "The keyword search program is missing.",
            code="keyword_search_unavailable",
            hint="Try setup again to download a fresh verified copy.",
        )

    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            [str(rg_path), "--version"],
            capture_output=True,
            text=True,
            timeout=RIPGREP_PROBE_TIMEOUT_SECONDS,
            check=False,
            # The probe is non-interactive, and inheriting an invalid or
            # redirected stdin handle is its own failure mode on Windows.
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise SetupError(
            "The keyword search program did not respond.",
            code="keyword_search_unavailable",
            hint="Try setup again to download a fresh verified copy.",
        ) from exc
    except OSError as exc:
        raise SetupError(
            "The keyword search program could not be started.",
            code="keyword_search_unavailable",
            hint="Try setup again to download a fresh verified copy.",
        ) from exc

    if result.returncode != 0 or "ripgrep" not in (result.stdout or "").lower():
        raise SetupError(
            "The keyword search program failed its self-check.",
            code="keyword_search_unavailable",
            hint="Try setup again to download a fresh verified copy.",
        )

"""
Haydar configuration management.

Config is stored as JSON at ~/.haydar/config.json.
Database (ChromaDB) lives at ~/.haydar/db/.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_CONFIG_WRITE_LOCK = threading.RLock()

# ── Paths ──────────────────────────────────────────────────────────────────────

# Every Haydar path below derives from one root, so overriding the root is the
# only supported way to run against an isolated profile. The packaged startup
# probe depends on this: it launches the real ``haydar.exe`` against a throwaway
# profile and must not be able to reach the user's data. Like every constant in
# this module the root is bound at import time, so the variable has to be set
# before ``haydar.config`` is imported — in-process tests monkeypatch the bound
# copies instead (see ``tests/conftest.py``).
HOME_ENV_VAR = "HAYDAR_HOME"

# Set to a report path by the packaged startup probe. Read here so every layer
# agrees on the name; the windowed entry point also consults it to keep a modal
# error dialog from hanging an unattended run.
STARTUP_PROBE_ENV_VAR = "HAYDAR_STARTUP_PROBE"


def _resolve_haydar_dir() -> Path:
    """Return the Haydar data root, honouring :data:`HOME_ENV_VAR`."""
    override = os.environ.get(HOME_ENV_VAR, "").strip()
    if not override:
        return Path.home() / ".haydar"
    return Path(override).expanduser()


HAYDAR_DIR = _resolve_haydar_dir()
CONFIG_PATH = HAYDAR_DIR / "config.json"
DB_DIR = HAYDAR_DIR / "db"
LOG_DIR = HAYDAR_DIR / "logs"

def get_log_path() -> Path:
    """Return the path to the main Haydar log file."""
    return LOG_DIR / "haydar.log"

MODELS_DIR = HAYDAR_DIR / "models"
CACHE_DIR = HAYDAR_DIR / "cache"
RIPGREP_DIR = HAYDAR_DIR / "bin"
INDEX_LOCK = HAYDAR_DIR / ".indexing.lock"

# Privately provisioned OCR engine. Versions are installed side by side and
# activated by atomically replacing ``current.json``, so a failed or cancelled
# install always leaves the previously working version in place.
OCR_DIR = HAYDAR_DIR / "ocr"
OCR_VERSIONS_DIR = OCR_DIR / "versions"
OCR_STAGING_DIR = OCR_DIR / "staging"
OCR_CURRENT_POINTER = OCR_DIR / "current.json"

# DB Schema Version (increment when changing schema/chunk sizes that require reindex)
CURRENT_SCHEMA_VERSION = 1

# Config file format version, independent of the vector schema version. Bump it
# only when the *shape* of config.json changes in a way that needs migration; a
# chunking or embedding change bumps CURRENT_SCHEMA_VERSION instead.
CONFIG_FORMAT_VERSION = 2

# Terminal states after which the file watcher may safely start. `complete` is
# the normal path; `cancelled`/`failed` mean the crawl stopped but committed
# work is durable and no writer is active.
SAFE_TERMINAL_INDEX_STATES: frozenset[str] = frozenset({
    "cancelled",
    "failed",
    "complete",
})

INITIAL_INDEX_STATES: frozenset[str] = frozenset({
    "not_started",
    "running",
    "paused",
    "cancelled",
    "failed",
    "complete",
})

# Why a run is in `paused`. A user pause must not be silently auto-resumed at
# the next launch, while a process that died mid-crawl should resume itself.
PAUSE_REASONS: frozenset[str] = frozenset({
    "",            # no pause recorded
    "user",        # explicit user request; requires explicit Resume
    "interrupted", # prior process ended while running; auto-resumes
})


class ConfigFormatError(Exception):
    """A config file this build cannot safely read or rewrite.

    Raised for a future ``config_format_version``. Callers must fail closed:
    show an upgrade message and leave both config and index untouched.
    """

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint



def get_documents_folder() -> Path | None:
    """Resolve the user's Documents known folder, or ``None`` if unavailable.

    On Windows the localized known folder is resolved through ``SHGetKnownFolderPath``
    so a non-English install (Documenti, Dokumente, 文档) is found correctly. The
    English ``~/Documents`` name is only a fallback, and a redirected Documents
    folder on another drive is honoured by the known-folder path.
    """
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            folder_id = ctypes.create_string_buffer(
                bytes.fromhex("d09ad3fd8f23af46adb46c85480369c7")
            )
            buffer = ctypes.c_wchar_p()
            result = ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id), 0, None, ctypes.byref(buffer)
            )
            try:
                if result == 0 and buffer.value:
                    candidate = Path(buffer.value)
                    if candidate.is_dir():
                        return candidate
            finally:
                ctypes.windll.ole32.CoTaskMemFree(buffer)
        except Exception:
            # A missing API or a COM failure is not fatal; fall through to the
            # conventional name below so setup can still offer something.
            logger.debug("Could not resolve the Documents known folder", exc_info=True)

    fallback = Path.home() / "Documents"
    return fallback if fallback.is_dir() else None


def _default_folders() -> list[str]:
    """Return the safe default folder for a genuinely fresh installation.

    Only Documents is selected automatically. Potentially very large locations
    such as Desktop, Downloads, whole drives, and network shares require an
    explicit bounded pre-scan and user confirmation in the first-run UI.

    This is called *only* when no ``folders`` key was persisted. An existing
    configuration always keeps its own list, including an intentionally empty one.
    """
    documents = get_documents_folder()
    return [str(documents)] if documents is not None else []


# ── File type size limits (bytes) ──────────────────────────────────────────────

DEFAULT_SIZE_LIMITS: dict[str, int] = {
    # Plain text / code: 10 MB
    "text": 10 * 1024 * 1024,
    # PDF / DOCX: 100 MB
    "document": 100 * 1024 * 1024,
    # Images (OCR): 20 MB
    "image": 20 * 1024 * 1024,
}

# ── Supported extensions ───────────────────────────────────────────────────────

TEXT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".csv", ".log", ".rst", ".ini", ".cfg", ".conf",
    ".env", ".toml", ".yaml", ".yml",
}

CODE_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h",
    ".hpp", ".cs", ".rs", ".go", ".rb", ".php", ".swift", ".kt",
    ".scala", ".r", ".m", ".sql", ".sh", ".bash", ".ps1", ".bat",
    ".html", ".css", ".scss", ".less", ".json", ".xml", ".svg",
    ".vue", ".svelte", ".dart", ".lua", ".pl", ".ex", ".exs",
    ".zig", ".nim", ".v", ".gradle", ".cmake", ".makefile",
}

DOCUMENT_EXTENSIONS: set[str] = {
    ".pdf", ".docx",
}

IMAGE_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".tiff",
}

ALL_INDEXABLE_EXTENSIONS: set[str] = (
    TEXT_EXTENSIONS | CODE_EXTENSIONS | DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS
)

# ── Excluded patterns ─────────────────────────────────────────────────────────

ROOT_ANCHORED_EXCLUSIONS: set[str] = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "system volume information",
    "recovery",
    "perflogs",
}

DEFAULT_EXCLUDED_PATTERNS: list[str] = [
    "node_modules",
    ".git",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "*.egg-info",
    ".haydar",
    ".svelte-kit",
    "target",
    "out",
    ".next",
    "vendor",
    ".idea",
    ".vscode",
    "appdata",
    "temp",
    "tmp",
    "hiberfil.sys",
    "pagefile.sys",
    "swapfile.sys",
    ".cache",
    ".ruff_cache",
    "coverage",
    "htmlcov",
    ".npm",
    ".DS_Store",
    "Thumbs.db",
]


# ── Config dataclass ──────────────────────────────────────────────────────────

@dataclass
class HaydarConfig:
    """Main configuration for Haydar."""

    # Folders to index. Fresh installs select Documents only; existing JSON
    # configurations retain their explicitly persisted folder list.
    folders: list[str] = field(default_factory=_default_folders)

    # Patterns to exclude (directory/file name fragments)
    excluded_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_PATTERNS)
    )

    # File size limits per type (bytes)
    size_limits: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_SIZE_LIMITS)
    )

    # Embedding model
    embedding_model: str = "all-MiniLM-L6-v2"

    # Chunk settings
    chunk_size: int = 500  # tokens (approximate by words)
    chunk_overlap: int = 50

    # Batching settings for embedding
    embedding_batch_size: int = 1000

    # Hotkey
    hotkey: str = "<ctrl>+<space>"  # pynput format

    # Watcher
    watcher_debounce_seconds: float = 0.5

    results_limit: int = 10
    window_opacity: int = 92        # percent, 50–100
    always_on_top: bool = True
    last_update_check: float = 0.0
    update_check_interval_hours: float = 24.0
    update_check_snoozed_until: float = 0.0

    # First launch what's new
    last_seen_version: str = ""

    # Config file format version, independent of ``schema_version``.
    config_format_version: int = CONFIG_FORMAT_VERSION

    # Setup lifecycle. These are the only inputs to GUI launch decisions.
    folders_configured: bool = False
    search_ready: bool = False
    initial_index_state: str = "not_started"
    initial_index_error: str = ""
    initial_index_pause_reason: str = ""

    # Legacy compatibility mirror, kept serialized for one release window so an
    # older build can still read this file. Writers derive it from
    # ``search_ready`` in :meth:`save`; never assign it independently and never
    # read it to make a decision.
    initialized: bool = False

    # DB Schema Version
    schema_version: int = CURRENT_SCHEMA_VERSION

    # Keys present in config.json that this build does not know about. Retained
    # verbatim so a downgrade/upgrade cycle cannot erase a newer build's fields.
    unknown_keys: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.initial_index_state not in INITIAL_INDEX_STATES:
            logger.warning(
                "Unknown initial index state %r; treating it as not_started",
                self.initial_index_state,
            )
            self.initial_index_state = "not_started"
        if self.initial_index_pause_reason not in PAUSE_REASONS:
            self.initial_index_pause_reason = ""
        # A persisted ``running`` value is deliberately preserved here. Recovery
        # to ``paused`` is an auditable transition performed by
        # ``haydar.lifecycle.IndexLifecycle.recover_interrupted_run()``, not a
        # silent side effect of constructing a dataclass, so callers and tests
        # can observe that it happened.

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this config, including unknown keys."""
        known = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name != "unknown_keys"
        }
        # Known fields win: an unknown key can never shadow a field this build
        # actively manages.
        return {**self.unknown_keys, **known}

    def save(self) -> None:
        """Persist config atomically, serializing writers within this process."""
        # ``initialized`` is a derived mirror, never an independent input.
        self.initialized = self.search_ready
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        with _CONFIG_WRITE_LOCK:
            HAYDAR_DIR.mkdir(parents=True, exist_ok=True)
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=CONFIG_PATH.parent,
                    prefix=f".{CONFIG_PATH.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                    temp_file.write(payload)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(temp_path, CONFIG_PATH)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
        logger.debug("Config saved to %s", CONFIG_PATH)

    @classmethod
    def load(cls) -> HaydarConfig:
        """Load config, migrating older formats without losing user intent.

        Raises :class:`ConfigFormatError` for a config written by a newer build:
        rewriting it would silently drop fields this version does not know how to
        maintain. Corrupt JSON falls back to defaults, because an unreadable file
        carries no intent to preserve.
        """
        if not CONFIG_PATH.exists():
            return cls()
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("configuration root must be an object")
        except (json.JSONDecodeError, TypeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("Corrupt config file, using defaults: %s", exc)
            _quarantine_corrupt_config()
            return cls()

        return cls.from_raw(raw)

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> HaydarConfig:
        """Build a config from parsed JSON, applying the pure migration rules."""
        migrated = migrate_raw_config(raw)
        known_names = {f.name for f in fields(cls) if f.name != "unknown_keys"}
        known = {k: v for k, v in migrated.items() if k in known_names}
        unknown = {k: v for k, v in migrated.items() if k not in known_names}
        return cls(**known, unknown_keys=unknown)

    def ensure_dirs(self) -> None:
        """Create all required directories."""
        HAYDAR_DIR.mkdir(parents=True, exist_ok=True)
        DB_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        RIPGREP_DIR.mkdir(parents=True, exist_ok=True)
        OCR_DIR.mkdir(parents=True, exist_ok=True)
        OCR_VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
        OCR_STAGING_DIR.mkdir(parents=True, exist_ok=True)


_LIFECYCLE_KEYS = ("folders_configured", "search_ready", "initial_index_state")


def _quarantine_corrupt_config() -> None:
    """Keep a copy of an unreadable config next to it before defaults replace it.

    Falling back to defaults discards whatever the user had configured. The file
    cannot be parsed, so it cannot be migrated, but it can still be preserved for
    manual recovery instead of being overwritten by the next save.
    """
    try:
        if not CONFIG_PATH.exists():
            return
        backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.corrupt-{int(time.time())}")
        os.replace(CONFIG_PATH, backup)
        logger.warning("Unreadable config preserved at %s", backup)
    except OSError:
        logger.debug("Could not preserve the unreadable config", exc_info=True)


def migrate_raw_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate a parsed config payload to the current format.

    Pure, idempotent, and driven by raw *key presence* rather than by values, so
    a legacy file and an already-migrated file both converge without ever
    inventing intent the user did not express.

    The rules, in order of precedence:

    * A future ``config_format_version`` fails closed.
    * A persisted ``folders`` key always wins, including an empty list. Defaults
      are only ever applied when the key is absent entirely.
    * Explicit lifecycle keys are preserved exactly; missing ones are filled
      conservatively.
    * Legacy ``initialized=true`` with no lifecycle keys means a previously
      working install: ready and complete.
    * ``complete`` is never inferred from ``initialized`` when the caller has
      already written any explicit lifecycle key.
    """
    data = dict(raw)

    format_version = data.get("config_format_version", 1)
    if not isinstance(format_version, int) or isinstance(format_version, bool):
        format_version = 1
    if format_version > CONFIG_FORMAT_VERSION:
        raise ConfigFormatError(
            f"This configuration was written by a newer Haydar "
            f"(config format v{format_version}; this build supports "
            f"v{CONFIG_FORMAT_VERSION}).",
            hint=(
                "Install the newer Haydar version that created it. Your files, "
                "configuration, and index are unchanged."
            ),
        )
    data["config_format_version"] = CONFIG_FORMAT_VERSION

    # Existing data wins: only a genuinely absent key gets defaults.
    if "folders" not in data:
        data["folders"] = _default_folders()
    folders = data.get("folders")
    if not isinstance(folders, list):
        data["folders"] = _default_folders()

    has_any_lifecycle_key = any(key in data for key in _LIFECYCLE_KEYS)
    legacy_initialized = data.get("initialized") is True

    if not has_any_lifecycle_key:
        if legacy_initialized:
            # A working legacy install: its folders were configured, its model and
            # collection were provisioned, and its crawl had finished.
            data["folders_configured"] = True
            data["search_ready"] = True
            data["initial_index_state"] = "complete"
        else:
            data["folders_configured"] = bool(data["folders"])
            data["search_ready"] = False
            data["initial_index_state"] = "not_started"
    else:
        # Partially written lifecycle state: fill only what is missing, and never
        # infer completion from the legacy flag.
        data.setdefault("folders_configured", bool(data["folders"]))
        data.setdefault("search_ready", False)
        data.setdefault("initial_index_state", "not_started")

    data.setdefault("initial_index_error", "")
    data.setdefault("initial_index_pause_reason", "")
    return data



def get_size_category(extension: str) -> str:
    """Map a file extension to its size-limit category."""
    if extension in DOCUMENT_EXTENSIONS:
        return "document"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    return "text"


def is_excluded(path: Path, excluded_patterns: list[str]) -> bool:
    """Check if a path should be excluded from indexing."""
    parts = path.parts

    # Check root-anchored exclusions (parts[1] is the folder immediately inside the drive root)
    if len(parts) > 1 and parts[1].lower() in ROOT_ANCHORED_EXCLUSIONS:
        return True

    # Check name-based exclusions
    for pattern in excluded_patterns:
        for part in parts:
            if pattern.startswith("*"):
                # Glob-style suffix match (e.g., *.egg-info)
                if part.lower().endswith(pattern[1:].lower()):
                    return True
            else:
                # Exact match
                if part.lower() == pattern.lower():
                    return True
    return False

class HaydarConfigError(Exception):
    """Exception raised for configuration or missing binary errors."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

def get_rg_path() -> Path:
    """
    Locate the ripgrep binary.
    Checks (1) the PyInstaller bundle, (2) the user data dir (~/.haydar/bin,
    where `haydar init` fetches it for pip installs), then (3) the local dev
    path next to this file. Raises HaydarConfigError if not found.
    """
    import platform
    import sys

    executable_name = "rg.exe" if platform.system().lower() == "windows" else "rg"

    # 1. PyInstaller bundled path
    if hasattr(sys, '_MEIPASS'):
        bundle_path = Path(sys._MEIPASS) / "haydar" / "bin" / executable_name
        if bundle_path.exists():
            return bundle_path

    # 2. User data dir (populated by `haydar init` on pip installs)
    user_path = RIPGREP_DIR / executable_name
    if user_path.exists():
        return user_path

    # 3. Local development path (assuming this file is at src/haydar/config.py)
    dev_path = Path(__file__).resolve().parent / "bin" / executable_name
    if dev_path.exists():
        return dev_path

    raise HaydarConfigError(
        f"The ripgrep binary '{executable_name}' could not be found.",
        hint="Run `haydar-cli.exe init` to download it."
    )

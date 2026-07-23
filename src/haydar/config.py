"""
Haydar configuration management.

Config is stored as JSON at ~/.haydar/config.json.
Database (ChromaDB) lives at ~/.haydar/db/.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

HAYDAR_DIR = Path.home() / ".haydar"
CONFIG_PATH = HAYDAR_DIR / "config.json"
DB_DIR = HAYDAR_DIR / "db"
LOG_DIR = HAYDAR_DIR / "logs"
MODELS_DIR = HAYDAR_DIR / "models"
CACHE_DIR = HAYDAR_DIR / "cache"
RIPGREP_DIR = HAYDAR_DIR / "bin"
INDEX_LOCK = HAYDAR_DIR / ".indexing.lock"

# DB Schema Version (increment when changing schema/chunk sizes that require reindex)
CURRENT_SCHEMA_VERSION = 1


def _default_folders() -> list[str]:
    """Return the user's common document folders that exist.

    Defaults to Documents, Desktop, and Downloads. Returns an empty list if none
    of those exist -- callers (``haydar init``) then prompt for folders rather
    than silently indexing an arbitrary directory (e.g. an EXE's launch cwd).
    """
    home = Path.home()
    candidates = [home / "Documents", home / "Desktop", home / "Downloads"]
    return [str(p) for p in candidates if p.is_dir()]


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

    # Folders to index
    folders: list[str] = field(default_factory=list)

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

    # Initialized flag
    initialized: bool = False

    # DB Schema Version
    schema_version: int = CURRENT_SCHEMA_VERSION

    def save(self) -> None:
        """Persist config to disk."""
        HAYDAR_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug("Config saved to %s", CONFIG_PATH)

    @classmethod
    def load(cls) -> HaydarConfig:
        """Load config from disk, or return defaults if no config file exists."""
        if not CONFIG_PATH.exists():
            return cls()
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return cls(**{
                k: v for k, v in data.items()
                if k in cls.__dataclass_fields__
            })
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Corrupt config file, using defaults: %s", exc)
            return cls()

    def ensure_dirs(self) -> None:
        """Create all required directories."""
        HAYDAR_DIR.mkdir(parents=True, exist_ok=True)
        DB_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        RIPGREP_DIR.mkdir(parents=True, exist_ok=True)


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
    pass

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
        f"Could not find ripgrep binary '{executable_name}'.\n"
        "Run 'haydar init' to download it, or execute 'python scripts/pull-rg.py'."
    )

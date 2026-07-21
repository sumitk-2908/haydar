"""
Haydar configuration management.

Config is stored as JSON at ~/.haydar/config.json.
Database (ChromaDB) lives at ~/.haydar/db/.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

HAYDAR_DIR = Path.home() / ".haydar"
CONFIG_PATH = HAYDAR_DIR / "config.json"
DB_DIR = HAYDAR_DIR / "db"
LOG_DIR = HAYDAR_DIR / "logs"
INDEX_LOCK = HAYDAR_DIR / ".indexing.lock"


def _default_folders() -> list[str]:
    """Return default folders to index (user home subdirectories that exist)."""
    home = Path.home()
    candidates = [
        home / "Documents",
        home / "Desktop",
        home / "Downloads",
    ]
    return [str(p) for p in candidates if p.exists()]


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
    "$RECYCLE.BIN",
    "System Volume Information",
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

    # Hotkey
    hotkey: str = "<ctrl>+<space>"  # pynput format

    # Watcher
    watcher_debounce_seconds: float = 0.5

    # Initialized flag
    initialized: bool = False

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
    for pattern in excluded_patterns:
        for part in parts:
            if pattern.startswith("*"):
                # Glob-style suffix match (e.g., *.egg-info)
                if part.endswith(pattern[1:]):
                    return True
            elif part == pattern:
                return True
    return False

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Literal, TypedDict, cast

logger = logging.getLogger(__name__)

SectionName = Literal["Added", "Changed", "Fixed", "Removed"]
SECTION_NAMES: tuple[SectionName, ...] = ("Added", "Changed", "Fixed", "Removed")
_VERSION_HEADER = re.compile(r"^## \[([^\]\s][^\]]*?)\] - (\d{4}-\d{2}-\d{2})$")
_UNRELEASED_HEADER = re.compile(r"^## \[Unreleased\]$")
_CHANGELOG_HEADER = re.compile(r"^## \[")
_SECTION_HEADER = re.compile(r"^### (.+?)$")


class ChangelogSections(TypedDict):
    Added: list[str]
    Changed: list[str]
    Fixed: list[str]
    Removed: list[str]


class ChangelogEntry(TypedDict):
    version: str
    date: str | None
    sections: ChangelogSections


def _empty_sections() -> ChangelogSections:
    return {"Added": [], "Changed": [], "Fixed": [], "Removed": []}


def find_changelog() -> Path | None:
    """Find the changelog in a bundle, installed package, or source checkout."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root is not None:
            candidates.append(Path(bundle_root) / "CHANGELOG.md")

    # Wheels force-include the project changelog alongside this module.
    candidates.append(Path(__file__).resolve().parent / "CHANGELOG.md")
    # Source checkout fallback: src/haydar/changelog.py -> repository root.
    candidates.append(Path(__file__).resolve().parent.parent.parent / "CHANGELOG.md")

    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            logger.warning("Could not inspect changelog candidate: %s", path)
    return None


def parse_changelog(path: Path) -> list[ChangelogEntry]:
    """Parse supported Keep-a-Changelog entries, returning an empty list on I/O failure."""
    versions: list[ChangelogEntry] = []
    current_version: ChangelogEntry | None = None
    current_section: SectionName | None = None

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not read changelog %s: %s", path, exc)
        return []

    for raw_line in lines:
        line = raw_line.strip()

        if _UNRELEASED_HEADER.fullmatch(line):
            current_version = {
                "version": "unreleased",
                "date": None,
                "sections": _empty_sections(),
            }
            versions.append(current_version)
            current_section = None
            continue

        version_match = _VERSION_HEADER.fullmatch(line)
        if version_match is not None:
            current_version = {
                "version": version_match.group(1).strip(),
                "date": version_match.group(2),
                "sections": _empty_sections(),
            }
            versions.append(current_version)
            current_section = None
            continue

        # A changelog-looking version header that fails the exact contracts
        # deactivates the previous entry. Otherwise its following items could be
        # incorrectly appended to the preceding valid version and section.
        if _CHANGELOG_HEADER.match(line):
            current_version = None
            current_section = None
            continue

        section_match = _SECTION_HEADER.fullmatch(line)
        if section_match is not None:
            section_name = section_match.group(1).strip()
            current_section = (
                cast(SectionName, section_name) if section_name in SECTION_NAMES else None
            )
            continue

        if line.startswith("- ") and current_version is not None and current_section:
            item = line[2:].strip()
            if item:
                current_version["sections"][current_section].append(item)

    return versions


def get_entry_for_version(version: str) -> ChangelogEntry | None:
    """Return the parsed entry for an exact version string, or ``None``."""
    path = find_changelog()
    if path is None:
        return None

    for entry in parse_changelog(path):
        if entry["version"] == version:
            return entry
    return None

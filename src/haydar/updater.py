from __future__ import annotations

import json
import logging
import urllib.request

from packaging.version import InvalidVersion, Version

from haydar import __version__

RELEASES_API = "https://api.github.com/repos/haydar-search/haydar/releases/latest"
RELEASES_BASE = "https://github.com/haydar-search/haydar/releases/tag"
_MAX_RESPONSE_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)


def get_latest_version(timeout: float = 5.0) -> str | None:
    """Hit GitHub Releases API; return a valid version or ``None`` on any error."""
    try:
        request = urllib.request.Request(
            RELEASES_API,
            headers={"User-Agent": f"haydar/{__version__}"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("GitHub release response exceeded the size limit")

        data = json.loads(raw.decode("utf-8"))
        tag = data.get("tag_name")
        if not isinstance(tag, str):
            raise ValueError("GitHub release response has no string tag_name")
        version = tag[1:] if tag.startswith("v") else tag
        Version(version)
        return version
    except Exception as exc:
        # Keep the public fail-soft contract while retaining bounded diagnostics.
        logger.debug("Update check failed: %s", exc)
        return None


def is_newer(latest: str, current: str) -> bool:
    """Return True iff latest > current using PEP 440 comparison.

    Never raises. A malformed remote tag (not PEP 440, e.g. ``nightly-build``)
    yields ``False`` rather than propagating ``InvalidVersion`` to the update
    banner thread or the CLI.
    """
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return False


def get_release_url(version: str) -> str:
    """Return the fixed-origin GitHub page for a valid PEP 440 release."""
    normalized = version[1:] if version.startswith("v") else version
    try:
        validated = Version(normalized)
    except InvalidVersion as exc:
        raise ValueError(f"Invalid release version: {version!r}") from exc
    return f"{RELEASES_BASE}/v{validated}"

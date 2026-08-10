"""Verify Haydar's pinned third-party assets against their real upstreams.

This is the opt-in pre-release check §16 asks for. No test in the normal suite
may depend on a live upstream — those use local HTTP fixtures — so the question
"is the pin we ship still the bytes upstream serves?" is answered here instead,
deliberately, before a release.

It downloads each pinned asset and compares its SHA-256 with the value recorded
in the source tree. A mismatch is a real finding either way round: either the
upstream artifact was replaced, or our pin is wrong. Both must block a release.

Run it manually, or through the ``verify_upstream_pins`` input on the release
workflow::

    python scripts/verify_upstream_pins.py
"""

from __future__ import annotations

import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from haydar.ocr import (  # noqa: E402
    ENG_TRAINEDDATA_SHA256,
    ENG_TRAINEDDATA_URL,
    OCR_ASSETS,
)
from haydar.ripgrep import _BASE_URL, CHECKSUMS, VERSION  # noqa: E402

CHUNK_BYTES = 64 * 1024
TIMEOUT_SECONDS = 120


def _download_sha256(url: str) -> tuple[str, int]:
    """Stream ``url`` and return its digest and length without buffering it all."""
    request = urllib.request.Request(url, headers={"User-Agent": "haydar-pin-check"})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        while True:
            chunk = response.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _check(label: str, url: str, expected: str) -> bool:
    print(f"\n{label}\n  {url}")
    try:
        actual, size = _download_sha256(url)
    except (urllib.error.URLError, OSError) as exc:
        print(f"  FAIL: could not download: {exc}")
        return False
    if actual != expected:
        print(f"  FAIL: sha256 mismatch ({size} bytes)")
        print(f"    pinned: {expected}")
        print(f"    actual: {actual}")
        return False
    print(f"  OK: {size} bytes, sha256 matches the pin")
    return True


def main() -> int:
    results: list[bool] = []

    for filename, expected in CHECKSUMS.items():
        results.append(
            _check(f"ripgrep {VERSION}: {filename}", f"{_BASE_URL}/{VERSION}/{filename}", expected)
        )

    results.append(
        _check("tessdata eng.traineddata", ENG_TRAINEDDATA_URL, ENG_TRAINEDDATA_SHA256)
    )

    for asset in OCR_ASSETS:
        label = f"OCR engine {asset.version} ({asset.platform}/{asset.architecture})"
        if not asset.is_reviewed:
            # The shipped manifest is deliberately unpinned: the 2026-08-10
            # licensing review found no qualifying Windows distribution, so
            # provisioning fails closed. Nothing to verify, and it is not a
            # failure — see THIRD_PARTY_NOTICES.md.
            print(f"\n{label}\n  SKIP: not reviewed; one-click OCR fails closed by design")
            continue
        problem = asset.url_problem
        if problem is not None:
            print(f"\n{label}\n  FAIL: unfit URL to pin against: {problem}")
            results.append(False)
            continue
        results.append(_check(label, asset.url, asset.sha256))

    failures = results.count(False)
    print(f"\n{len(results) - failures}/{len(results)} pinned assets verified.")
    if failures:
        print("FAIL: at least one pin no longer matches its upstream.")
        return 1
    print("OK: every pinned asset matches its upstream.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import hashlib

import pytest

from haydar.ripgrep import (
    CHECKSUMS,
    RipgrepError,
    ensure_ripgrep,
    get_release_asset,
    verify_checksum,
)


def test_get_release_asset_known_platform():
    asset = get_release_asset()
    assert asset in CHECKSUMS  # every returned asset has a pinned checksum


def test_verify_checksum_rejects_unknown_file(tmp_path):
    f = tmp_path / "unknown.zip"
    f.write_bytes(b"data")
    with pytest.raises(RipgrepError, match="No pinned checksum"):
        verify_checksum(f, "unknown.zip")


def test_verify_checksum_detects_mismatch(tmp_path):
    name = next(iter(CHECKSUMS))
    f = tmp_path / name
    f.write_bytes(b"tampered content")
    with pytest.raises(RipgrepError, match="Checksum mismatch"):
        verify_checksum(f, name)


def test_verify_checksum_passes_for_matching_content(tmp_path, monkeypatch):
    name = "fake-asset.zip"
    content = b"the real bytes"
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setitem(CHECKSUMS, name, digest)

    f = tmp_path / name
    f.write_bytes(content)
    verify_checksum(f, name)  # should not raise


def test_ensure_ripgrep_returns_existing(tmp_path):
    import platform

    exe = "rg.exe" if platform.system().lower() == "windows" else "rg"
    (tmp_path / exe).write_bytes(b"already here")
    # Should short-circuit without any network access.
    assert ensure_ripgrep(tmp_path) == tmp_path / exe

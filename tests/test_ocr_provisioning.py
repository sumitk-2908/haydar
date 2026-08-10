"""OCR provisioning contract: verified, private, atomic, and fail-closed.

Provisioning downloads and then *executes* code, so these tests are mostly about
refusal: an unpinned asset, a mismatched hash, a hostile archive, or a binary
that will not answer ``--version`` must all leave the previous installation
exactly as it was.

No test touches a live upstream. A loopback HTTP server serves archives built
here, so what is exercised is Haydar's verification, not the network.
"""

import hashlib
import http.server
import json
import os
import re
import socket
import stat
import threading
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

import haydar.ocr as ocr_module
from haydar.ocr import (
    MAX_ARCHIVE_MEMBERS,
    OcrAsset,
    OcrPhase,
    OcrProvisioner,
    OcrProvisionError,
    active_private_executable,
    install_ocr,
    read_active_pointer,
    safe_extract_zip,
    select_asset,
)

pytestmark = pytest.mark.usefixtures("tmp_haydar")


def _pointer() -> Path:
    """Read the pointer path through the module.

    ``tmp_haydar`` rebinds the module's copy of the constant, so importing it
    into this file at collection time would capture the real ``~/.haydar`` one.
    """
    return ocr_module.OCR_CURRENT_POINTER


def _versions_dir() -> Path:
    return ocr_module.OCR_VERSIONS_DIR


# ── archive and server fixtures ────────────────────────────────────────────────

# A batch file stands in for tesseract.exe: it is executable on Windows and
# answers --version the way the probe expects.
FAKE_EXE_BODY = "@echo off\r\necho tesseract 5.4.0\r\n"


def _build_archive(path: Path, members: dict[str, str] | None = None) -> bytes:
    """Write a tiny valid archive and return its bytes."""
    contents = members if members is not None else {
        "tesseract.bat": FAKE_EXE_BODY,
        "tessdata/eng.traineddata": "fake language data",
        "LICENSE": "Apache License 2.0",
        "COPYING": "Apache License 2.0",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in contents.items():
            archive.writestr(name, body)
    return path.read_bytes()


@pytest.fixture
def archive_server(tmp_path):
    """Serve files from a temp directory over loopback HTTP."""
    root = tmp_path / "served"
    root.mkdir()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _asset(url: str, digest: str, **overrides) -> OcrAsset:
    base = OcrAsset(
        version="5.4.0-test",
        platform="windows",
        architecture="x86_64",
        url=url,
        archive_filename="ocr.zip",
        sha256=digest,
        executable_relative_path="tesseract.bat",
        data_relative_paths=("tessdata/eng.traineddata",),
        upstream_license_files=("LICENSE", "COPYING"),
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture
def published(archive_server, tmp_path):
    """Publish a good archive and return the asset that points at it."""
    root, base_url = archive_server
    payload = _build_archive(root / "ocr.zip")
    digest = hashlib.sha256(payload).hexdigest()
    return _asset(f"{base_url}/ocr.zip", digest), root, base_url


# ── happy path ─────────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_a_verified_archive_is_extracted_probed_and_activated(published):
    asset, _root, _url = published
    events = []

    result = OcrProvisioner(asset, progress_callback=events.append).install()

    assert result.ready
    assert result.phase is OcrPhase.COMPLETE
    assert Path(result.executable_path).is_file()
    # Phases are reported in order, so the band can narrate the install.
    phases = [event.phase for event in events]
    for phase in (
        OcrPhase.DOWNLOADING,
        OcrPhase.VERIFYING,
        OcrPhase.EXTRACTING,
        OcrPhase.PROBING,
        OcrPhase.ACTIVATING,
        OcrPhase.COMPLETE,
    ):
        assert phase in phases


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_activation_is_a_single_atomic_pointer_write(published):
    asset, _root, _url = published

    OcrProvisioner(asset).install()

    pointer = read_active_pointer()
    assert pointer is not None
    assert pointer["version"] == asset.version
    assert pointer["sha256"] == asset.sha256
    # No leftover temp pointers beside the real one.
    siblings = list(_pointer().parent.glob(".current.*"))
    assert siblings == []


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_the_private_engine_is_preferred_over_a_system_installation(published):
    asset, _root, _url = published

    OcrProvisioner(asset).install()

    with patch("haydar.ocr.shutil.which", return_value=r"C:\System\tesseract.exe"):
        from haydar.ocr import get_tesseract_path

        resolved = get_tesseract_path()

    assert Path(resolved) == active_private_executable()


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_staging_is_cleaned_up_after_a_successful_install(published, tmp_haydar):
    from haydar.config import OCR_STAGING_DIR

    asset, _root, _url = published
    OcrProvisioner(asset).install()

    assert list(OCR_STAGING_DIR.glob("install-*")) == []


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_installing_twice_is_idempotent_and_does_not_redownload(published):
    asset, root, _url = published
    OcrProvisioner(asset).install()

    # Remove the source so a second download would fail outright.
    (root / "ocr.zip").unlink()
    second = OcrProvisioner(asset).install()

    assert second.ready
    assert "already installed" in second.message


# ── verification failures leave the old version alone ─────────────────────────


def _pointer_snapshot():
    return _pointer().read_bytes() if _pointer().exists() else None


def test_a_hash_mismatch_is_rejected_before_anything_is_activated(
    archive_server, tmp_path
):
    root, base_url = archive_server
    _build_archive(root / "ocr.zip")
    asset = _asset(f"{base_url}/ocr.zip", "0" * 64)

    with pytest.raises(OcrProvisionError) as excinfo:
        OcrProvisioner(asset).install()

    assert excinfo.value.error_code == "checksum_mismatch"
    assert active_private_executable() is None
    assert not _pointer().exists()


def test_a_truncated_download_is_rejected(archive_server, tmp_path):
    """A partial file hashes differently, which is the whole point of pinning."""
    root, base_url = archive_server
    payload = _build_archive(root / "ocr.zip")
    digest = hashlib.sha256(payload).hexdigest()
    (root / "ocr.zip").write_bytes(payload[: len(payload) // 2])

    with pytest.raises(OcrProvisionError) as excinfo:
        OcrProvisioner(_asset(f"{base_url}/ocr.zip", digest)).install()

    assert excinfo.value.error_code == "checksum_mismatch"


def test_being_offline_is_reported_as_retryable(archive_server):
    root, base_url = archive_server
    asset = _asset(f"{base_url}/missing.zip", "a" * 64)

    with pytest.raises(OcrProvisionError) as excinfo:
        OcrProvisioner(asset).install()

    assert excinfo.value.error_code == "offline"
    assert excinfo.value.retryable is True


def test_an_unreviewed_asset_refuses_to_download_at_all(archive_server):
    """Fail closed: no pinned hash means no download, not an unverified one."""
    root, base_url = archive_server
    _build_archive(root / "ocr.zip")
    opened = []

    def opener(*args, **kwargs):
        opened.append(args)
        raise AssertionError("must not download an unreviewed asset")

    asset = _asset(f"{base_url}/ocr.zip", "PENDING_REVIEW")
    provisioner = OcrProvisioner(asset, opener=opener)

    with pytest.raises(OcrProvisionError) as excinfo:
        provisioner.install()

    assert excinfo.value.error_code == "asset_not_reviewed"
    assert opened == []


def test_the_shipped_manifest_entry_is_marked_unreviewed():
    """Guards against shipping a guessed hash: review must be explicit.

    Reviewed 2026-08-10 and deliberately still unpinned — see the review note in
    ``haydar/ocr.py``. No authoritative portable zip of Windows Tesseract
    exists; the only upstream artifact is an NSIS installer that bundles
    LGPL-2.1 components without their notices. §15 forbids shipping that
    unreviewed, so one-click install fails closed instead.

    When a distribution is chosen and its licensing recorded, flip this to
    assert ``is_reviewed is True`` alongside the URL tests below.
    """
    asset = select_asset(platform_name="Windows", architecture="AMD64")

    assert asset.is_reviewed is False
    assert asset.upstream_license_files


def test_a_pinned_url_must_be_https_and_immutable():
    """The rule a future pin has to satisfy, enforced now rather than later.

    A hash pins bytes, not a location. A "latest" URL is free to change what it
    serves, so pinning against one can only ever start failing; and cleartext
    lets someone else choose which failure the user sees.
    """
    good = _asset(
        "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/x.zip",
        "a" * 64,
    )
    assert good.url_problem is None
    assert good.has_immutable_url is True

    for url, reason in (
        ("http://example.com/tesseract.zip", "https"),
        ("https://example.com/tesseract/latest/x.zip", "moving target"),
        ("https://example.com/tessdata_fast/main/eng.traineddata", "moving target"),
        ("https://example.com/builds/master/x.zip", "moving target"),
        ("ftp://example.com/x.zip", "scheme"),
        ("", "no download URL"),
    ):
        problem = _asset(url, "a" * 64).url_problem
        assert problem is not None, url
        assert reason in problem, (url, problem)


def test_the_recorded_language_data_pin_is_immutable_and_hash_shaped():
    """The one artifact this review did clear, kept for a future pin."""
    from haydar.ocr import ENG_TRAINEDDATA_SHA256, ENG_TRAINEDDATA_URL

    assert ENG_TRAINEDDATA_URL.startswith("https://")
    # Commit-pinned, not branch-pinned: a branch URL is not a pin at all.
    assert "/main/" not in ENG_TRAINEDDATA_URL
    assert re.search(r"/[0-9a-f]{40}/", ENG_TRAINEDDATA_URL)
    assert re.fullmatch(r"[0-9a-f]{64}", ENG_TRAINEDDATA_SHA256)


def test_a_reviewed_hash_against_a_moving_url_still_refuses_to_download():
    """Belt and braces: a real hash does not excuse an unpinnable location."""
    opened = []

    def opener(*args, **kwargs):
        opened.append(args)
        raise AssertionError("must not download from a moving URL")

    asset = _asset("https://example.com/tesseract/latest/ocr.zip", "a" * 64)

    with pytest.raises(OcrProvisionError) as excinfo:
        OcrProvisioner(asset, opener=opener).install()

    assert excinfo.value.error_code == "asset_not_reviewed"
    assert opened == []


def test_the_local_test_fixture_url_is_still_allowed():
    """Loopback http is the one exception, because the tests serve it."""
    assert _asset("http://127.0.0.1:8080/ocr.zip", "a" * 64).url_problem is None
    assert _asset("http://localhost:8080/ocr.zip", "a" * 64).url_problem is None


def test_an_unsupported_platform_has_no_asset_to_substitute():
    with pytest.raises(OcrProvisionError) as excinfo:
        select_asset(platform_name="Linux", architecture="aarch64")

    assert excinfo.value.error_code == "unsupported_platform"


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_a_missing_executable_in_the_archive_is_rejected(archive_server, tmp_path):
    root, base_url = archive_server
    payload = _build_archive(
        root / "ocr.zip",
        members={"README.txt": "no program here", "LICENSE": "x", "COPYING": "x"},
    )
    digest = hashlib.sha256(payload).hexdigest()

    with pytest.raises(OcrProvisionError) as excinfo:
        OcrProvisioner(_asset(f"{base_url}/ocr.zip", digest)).install()

    assert excinfo.value.error_code == "missing_executable"
    assert not _pointer().exists()


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_licence_files_survive_activation_and_stay_readable(published):
    """§15: downloaded OCR licence files remain in the activated version dir.

    ``_verify_contents`` only proves the licences were in *staging*. Activation
    then moves that tree, so this asserts what a user actually ends up with:
    the licences sit beside the executable they cover, with their upstream text
    intact, for as long as the version is installed.
    """
    asset, _root, _url = published

    result = OcrProvisioner(asset).install()

    version_dir = _versions_dir() / asset.version
    executable = Path(result.executable_path)
    assert executable.parent == version_dir
    for relative in asset.upstream_license_files:
        licence = version_dir / relative
        assert licence.is_file(), f"{relative} did not survive activation"
        assert licence.read_text(encoding="utf-8") == "Apache License 2.0"


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_licence_files_survive_an_upgrade_that_replaces_a_version(
    published, archive_server, tmp_path
):
    """Replacing an installed version must not leave it without its notices.

    ``_promote`` renames the old version directory aside before moving the new
    one in. A licence file that was only present in the *replaced* tree would
    silently disappear on upgrade.
    """
    asset, root, base_url = published
    OcrProvisioner(asset).install()

    payload = _build_archive(
        root / "ocr2.zip",
        members={
            "tesseract.bat": FAKE_EXE_BODY,
            "tessdata/eng.traineddata": "newer language data",
            "LICENSE": "Apache License 2.0",
            "COPYING": "Apache License 2.0",
        },
    )
    digest = hashlib.sha256(payload).hexdigest()
    upgraded = _asset(f"{base_url}/ocr2.zip", digest)

    OcrProvisioner(upgraded).install(force=True)

    version_dir = _versions_dir() / upgraded.version
    for relative in upgraded.upstream_license_files:
        assert (version_dir / relative).is_file()
    # No replaced-version leftovers keeping a stale copy of the notices around.
    assert list(_versions_dir().glob("*.replaced-*")) == []


def test_an_archive_without_its_licence_files_is_not_activatable(
    archive_server, tmp_path
):
    root, base_url = archive_server
    payload = _build_archive(
        root / "ocr.zip",
        members={
            "tesseract.bat": FAKE_EXE_BODY,
            "tessdata/eng.traineddata": "data",
        },
    )
    digest = hashlib.sha256(payload).hexdigest()

    with pytest.raises(OcrProvisionError) as excinfo:
        OcrProvisioner(_asset(f"{base_url}/ocr.zip", digest)).install()

    assert excinfo.value.error_code == "missing_license"


def test_missing_language_data_is_rejected(archive_server, tmp_path):
    root, base_url = archive_server
    payload = _build_archive(
        root / "ocr.zip",
        members={"tesseract.bat": FAKE_EXE_BODY, "LICENSE": "x", "COPYING": "x"},
    )
    digest = hashlib.sha256(payload).hexdigest()

    with pytest.raises(OcrProvisionError) as excinfo:
        OcrProvisioner(_asset(f"{base_url}/ocr.zip", digest)).install()

    assert excinfo.value.error_code == "missing_data"


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_a_binary_that_fails_its_version_probe_is_not_activated(
    archive_server, tmp_path
):
    root, base_url = archive_server
    payload = _build_archive(
        root / "ocr.zip",
        members={
            "tesseract.bat": "@echo off\r\nexit /b 1\r\n",
            "tessdata/eng.traineddata": "data",
            "LICENSE": "x",
            "COPYING": "x",
        },
    )
    digest = hashlib.sha256(payload).hexdigest()

    with pytest.raises(OcrProvisionError) as excinfo:
        OcrProvisioner(_asset(f"{base_url}/ocr.zip", digest)).install()

    assert excinfo.value.error_code == "probe_failed"
    assert active_private_executable() is None


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_a_failed_upgrade_keeps_the_previously_working_version(published, tmp_path):
    """The decisive rollback test: a bad second install must not break OCR."""
    good_asset, root, base_url = published
    OcrProvisioner(good_asset).install()
    working = active_private_executable()
    pointer_before = _pointer_snapshot()

    bad_payload = _build_archive(
        root / "bad.zip", members={"README.txt": "junk", "LICENSE": "x", "COPYING": "x"}
    )
    bad = _asset(
        f"{base_url}/bad.zip",
        hashlib.sha256(bad_payload).hexdigest(),
        version="9.9.9-bad",
        archive_filename="bad.zip",
    )

    with pytest.raises(OcrProvisionError):
        OcrProvisioner(bad).install(force=True)

    assert active_private_executable() == working
    assert working.is_file()
    assert _pointer_snapshot() == pointer_before


# ── cancellation ───────────────────────────────────────────────────────────────


def test_cancelling_before_the_download_activates_nothing(published):
    asset, _root, _url = published
    cancel = threading.Event()
    cancel.set()

    result = OcrProvisioner(asset).install(cancel_event=cancel)

    assert result.phase is OcrPhase.CANCELLED
    assert not _pointer().exists()


def test_cancelling_mid_download_leaves_no_activation(published):
    asset, _root, _url = published
    cancel = threading.Event()
    events = []

    def watch(event):
        events.append(event)
        if event.phase is OcrPhase.DOWNLOADING and event.completed:
            cancel.set()

    result = OcrProvisioner(asset, progress_callback=watch).install(cancel_event=cancel)

    assert result.phase is OcrPhase.CANCELLED
    assert active_private_executable() is None
    assert not _pointer().exists()


@pytest.mark.skipif(os.name != "nt", reason="probe runs a .bat")
def test_a_cancelled_upgrade_keeps_the_working_version(published):
    asset, _root, _url = published
    OcrProvisioner(asset).install()
    working = active_private_executable()

    cancel = threading.Event()
    cancel.set()
    result = OcrProvisioner(asset).install(cancel_event=cancel, force=True)

    assert result.phase is OcrPhase.CANCELLED
    assert active_private_executable() == working


# ── extraction safety ──────────────────────────────────────────────────────────


def _write_raw_zip(path: Path, entries) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for info, body in entries:
            archive.writestr(info, body)
    return path


@pytest.mark.parametrize(
    "member",
    [
        "../escaped.txt",
        "../../escaped.txt",
        "nested/../../escaped.txt",
        "..\\escaped.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "C:\\drive.txt",
        "//server/share/unc.txt",
    ],
)
def test_unsafe_member_paths_are_rejected(tmp_path, member):
    archive = _write_raw_zip(tmp_path / "evil.zip", [(member, "payload")])
    destination = tmp_path / "out"

    with pytest.raises(OcrProvisionError) as excinfo:
        safe_extract_zip(archive, destination)

    assert excinfo.value.error_code == "unsafe_archive"
    # Nothing landed outside the destination.
    assert not (tmp_path / "escaped.txt").exists()
    assert not (destination.parent / "escaped.txt").exists()


def test_a_symlink_member_is_rejected(tmp_path):
    """A link could redirect a later write outside the staging tree."""
    info = zipfile.ZipInfo("link.txt")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = _write_raw_zip(tmp_path / "link.zip", [(info, "C:/Windows/System32")])

    with pytest.raises(OcrProvisionError) as excinfo:
        safe_extract_zip(archive, tmp_path / "out")

    assert excinfo.value.error_code == "unsafe_archive"


def test_too_many_members_is_rejected(tmp_path):
    entries = [(f"file-{i}.txt", "") for i in range(MAX_ARCHIVE_MEMBERS + 5)]
    archive = _write_raw_zip(tmp_path / "many.zip", entries)

    with pytest.raises(OcrProvisionError) as excinfo:
        safe_extract_zip(archive, tmp_path / "out")

    assert excinfo.value.error_code == "archive_too_many_files"


def test_a_decompression_bomb_is_rejected_before_it_is_written(tmp_path):
    archive_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.bin", b"\0" * (40 * 1024 * 1024))
    destination = tmp_path / "out"

    with pytest.raises(OcrProvisionError) as excinfo:
        safe_extract_zip(archive_path, destination)

    assert excinfo.value.error_code in ("archive_bomb", "archive_too_large")
    assert not (destination / "bomb.bin").exists()


def test_a_normal_archive_extracts_with_its_tree_intact(tmp_path):
    archive = tmp_path / "good.zip"
    _build_archive(archive)
    destination = tmp_path / "out"

    written = safe_extract_zip(archive, destination)

    assert written == 4
    assert (destination / "tesseract.bat").is_file()
    assert (destination / "tessdata" / "eng.traineddata").is_file()


# ── pointer robustness ─────────────────────────────────────────────────────────


def test_a_corrupt_pointer_reads_as_no_private_engine():
    _pointer().parent.mkdir(parents=True, exist_ok=True)
    _pointer().write_text("{not json", encoding="utf-8")

    assert read_active_pointer() is None
    assert active_private_executable() is None


def test_a_pointer_to_a_removed_version_reads_as_no_private_engine():
    _pointer().parent.mkdir(parents=True, exist_ok=True)
    _pointer().write_text(
        json.dumps({"version": "5.4.0", "executable_relative_path": "tesseract.exe"}),
        encoding="utf-8",
    )

    assert read_active_pointer() is not None
    assert active_private_executable() is None


def test_no_versions_directory_means_no_private_engine():
    assert not (_versions_dir() / "5.4.0").exists()
    assert active_private_executable() is None


# ── install_ocr entry point ────────────────────────────────────────────────────


def test_install_ocr_uses_a_supported_system_engine_without_downloading():
    """A working system install is left untouched rather than duplicated."""
    from haydar.ocr import TesseractInfo, TesseractStatus

    info = TesseractInfo(TesseractStatus.FOUND, "5.3.0", r"C:\Tess\tesseract.exe")
    with (
        patch("haydar.ocr.detect_tesseract", return_value=info),
        patch("haydar.ocr.OcrProvisioner") as provisioner,
    ):
        result = install_ocr()

    assert result.ready
    assert result.executable_path == r"C:\Tess\tesseract.exe"
    provisioner.assert_not_called()


def test_install_ocr_provisions_when_no_engine_is_present():
    from haydar.ocr import TesseractInfo, TesseractStatus

    info = TesseractInfo(TesseractStatus.NOT_FOUND, None, None)
    with (
        patch("haydar.ocr.detect_tesseract", return_value=info),
        pytest.raises(OcrProvisionError) as excinfo,
    ):
        install_ocr()

    # The shipped asset is unreviewed, so it fails closed rather than fetching.
    assert excinfo.value.error_code == "asset_not_reviewed"


def test_install_ocr_reports_a_version_token_for_cache_refresh(published):
    """The token is what marks which engine an image's empty result came from."""
    from haydar.ocr import OcrInstallResult

    result = OcrInstallResult(phase=OcrPhase.COMPLETE, version="5.4.0")

    assert result.version_token == "tesseract-5.4.0"
    assert published[0].version_token == "tesseract-5.4.0-test"


# ── user-facing copy ───────────────────────────────────────────────────────────


def test_install_instructions_never_direct_a_user_to_pip_winget_or_path():
    """§19, in both manifest states.

    "Unavailable" is the state most likely to tempt someone into adding a
    helpful "meanwhile, download Tesseract from…" line, which is exactly what
    the contract forbids — so both branches are checked, not just the happy one.
    """
    from haydar.ocr import get_install_instructions

    reviewed = _asset("https://example.invalid/ocr.zip", "a" * 64)
    for assets in ((), (reviewed,)):
        with patch("haydar.ocr.OCR_ASSETS", assets):
            text = get_install_instructions().lower()
        for forbidden in (
            "pip install",
            "winget",
            "path",
            "haydar-cli",
            "github.com",
        ):
            assert forbidden not in text, f"{forbidden!r} leaked into OCR copy"

    # Naming the engine is permitted only in the unreviewed state, where it is
    # the user's route to a working feature (§19 amended 2026-08-11). With a
    # reviewed asset Haydar installs it, so naming it would push work back onto
    # the user that the product does itself.
    with patch("haydar.ocr.OCR_ASSETS", (reviewed,)):
        assert "tesseract" not in get_install_instructions().lower()


def test_install_instructions_explain_the_one_click_path_and_stay_local():
    """With a reviewed asset, the copy describes the one-click install (§12.4)."""
    from haydar.ocr import get_install_instructions

    reviewed = _asset("https://example.invalid/ocr.zip", "a" * 64)
    with patch("haydar.ocr.OCR_ASSETS", (reviewed,)):
        text = get_install_instructions().lower()

    assert "install ocr" in text
    assert "never uploaded" in text


def test_install_instructions_do_not_promise_a_download_that_cannot_happen():
    """With no reviewed asset, the copy must not advertise a one-click install.

    Provisioning fails closed in this state, so telling the user to choose
    Install OCR would be promising a button press that always fails. What the
    copy offers instead is the manual engine install, which does work.
    """
    from haydar.ocr import get_install_instructions

    with patch("haydar.ocr.OCR_ASSETS", ()):
        text = get_install_instructions().lower()

    assert "choose install ocr" not in text
    # Names the engine and the single follow-up action, so "cannot install one
    # for you" is never the end of the message.
    assert "tesseract" in text
    assert "restart haydar" in text
    # The promises that still hold: nothing is lost, and nothing is uploaded.
    assert "reindex" in text
    assert "never uploaded" in text


def test_the_shipped_build_explains_how_to_enable_image_search():
    """The manifest as shipped is unreviewed, so this is the copy users see."""
    from haydar.ocr import get_install_instructions

    text = get_install_instructions()
    assert "cannot install one for you" in text
    assert "Tesseract" in text

"""Contract tests for the `haydar-cli.exe ocr` command group (§14).

Two things are being pinned. First, the commands exist and drive the *same*
services the GUI does — the CLI is an adapter, not a second implementation.
Second, none of the copy sends a normal user to pip, PATH, or a manual download,
which §19 rejects outright.
"""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from haydar.cli import app
from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexSnapshot, JobOutcome, JobPhase
from haydar.ocr import (
    OcrInstallResult,
    OcrPhase,
    OcrProvisionError,
    TesseractInfo,
    TesseractStatus,
)

runner = CliRunner()

FOUND = TesseractInfo(TesseractStatus.FOUND, "5.5.3", r"C:\Tess\tesseract.exe")
MISSING = TesseractInfo(TesseractStatus.NOT_FOUND, None, None)

# Copy that §19 forbids in any user-facing path.
FORBIDDEN_PHRASES = ("pip install", "winget", "PATH", "chocolatey", "apt-get")


def _ready_config(**kwargs):
    return HaydarConfig(
        folders=[r"C:\Docs"], search_ready=True, folders_configured=True, **kwargs
    )


@pytest.fixture
def coordinator():
    """Stand in for the shared job coordinator and report a finished backfill."""
    instance = MagicMock()
    instance.snapshot.return_value = IndexSnapshot(
        phase=JobPhase.COMPLETE,
        outcome=JobOutcome.COMPLETE,
        committed_files=4,
        ocr_deferred=0,
    )
    with patch("haydar.indexer.jobs.IndexJobCoordinator", return_value=instance) as cls:
        yield cls, instance


# -- ocr status ---------------------------------------------------------------


def test_ocr_status_reports_a_ready_engine():
    with (
        patch("haydar.cli.detect_tesseract", return_value=FOUND),
        patch("haydar.cli._deferred_image_count", return_value=0),
    ):
        result = runner.invoke(app, ["ocr", "status"])

    assert result.exit_code == 0
    assert "Ready" in result.stdout
    assert "5.5.3" in result.stdout


def test_ocr_status_counts_images_still_waiting():
    """Pending images with no engine get the count and the way to fix it.

    The advice is the engine install, not `ocr install`: provisioning fails
    closed with `asset_not_reviewed` in the shipped build, so pointing at it
    would be pointing at a command that always fails (§19 amended 2026-08-11).
    """
    with (
        patch("haydar.cli.detect_tesseract", return_value=MISSING),
        patch("haydar.cli._deferred_image_count", return_value=12),
    ):
        result = runner.invoke(app, ["ocr", "status"])

    assert result.exit_code == 0
    assert "12 image(s) are waiting" in result.stdout
    assert "Tesseract" in result.stdout
    assert "ocr install" not in result.stdout


def test_ocr_status_offers_a_backfill_when_the_engine_is_already_present():
    """Installed but images pending is a backfill, not another install."""
    with (
        patch("haydar.cli.detect_tesseract", return_value=FOUND),
        patch("haydar.cli._deferred_image_count", return_value=3),
    ):
        result = runner.invoke(app, ["ocr", "status"])

    assert "ocr backfill" in result.stdout


def test_ocr_status_names_the_private_install_when_one_is_active():
    with (
        patch("haydar.cli.detect_tesseract", return_value=FOUND),
        patch("haydar.cli._deferred_image_count", return_value=0),
        patch("haydar.ocr.read_active_pointer", return_value={"version": "5.5.3"}),
    ):
        result = runner.invoke(app, ["ocr", "status"])

    assert "Private install" in result.stdout


# -- ocr install --------------------------------------------------------------


def test_ocr_install_drives_the_shared_provisioner(coordinator):
    _cls, _instance = coordinator
    ready = OcrInstallResult(
        phase=OcrPhase.COMPLETE,
        version="5.5.3",
        executable_path=r"C:\Users\me\.haydar\ocr\versions\5.5.3\tesseract.exe",
        message="Text recognition is ready.",
    )
    with (
        patch("haydar.ocr.install_ocr", return_value=ready) as install,
        patch("haydar.cli.HaydarConfig.load", return_value=_ready_config()),
    ):
        result = runner.invoke(app, ["ocr", "install"])

    assert result.exit_code == 0
    install.assert_called_once()
    assert install.call_args.kwargs["force"] is False
    assert "Text recognition is ready" in result.stdout


def test_ocr_install_starts_the_image_backfill_with_the_new_version(coordinator):
    _cls, instance = coordinator
    ready = OcrInstallResult(
        phase=OcrPhase.COMPLETE, version="5.5.3", message="Text recognition is ready."
    )
    with (
        patch("haydar.ocr.install_ocr", return_value=ready),
        patch("haydar.cli.HaydarConfig.load", return_value=_ready_config()),
    ):
        result = runner.invoke(app, ["ocr", "install"])

    assert result.exit_code == 0
    # The same coordinator entry point the GUI uses, tagged with the engine
    # version so stale-engine images are picked up too.
    instance.start_ocr_backfill.assert_called_once_with("tesseract-5.5.3")
    assert "4 image(s) indexed" in result.stdout


def test_ocr_install_can_skip_the_backfill(coordinator):
    _cls, instance = coordinator
    ready = OcrInstallResult(phase=OcrPhase.COMPLETE, version="5.5.3", message="ok")
    with (
        patch("haydar.ocr.install_ocr", return_value=ready),
        patch("haydar.cli.HaydarConfig.load", return_value=_ready_config()),
    ):
        result = runner.invoke(app, ["ocr", "install", "--no-backfill"])

    assert result.exit_code == 0
    instance.start_ocr_backfill.assert_not_called()


def test_ocr_install_forwards_force():
    ready = OcrInstallResult(phase=OcrPhase.COMPLETE, version="5.5.3", message="ok")
    with (
        patch("haydar.ocr.install_ocr", return_value=ready) as install,
        patch("haydar.cli.HaydarConfig.load", return_value=_ready_config()),
    ):
        runner.invoke(app, ["ocr", "install", "--force", "--no-backfill"])

    assert install.call_args.kwargs["force"] is True


def test_an_unreviewed_asset_fails_closed_with_a_plain_reason():
    """The shipped manifest state: refuse, and say so without jargon."""
    error = OcrProvisionError(
        "Automatic text recognition setup is not available in this build.",
        error_code="asset_not_reviewed",
    )
    with (
        patch("haydar.ocr.install_ocr", side_effect=error),
        patch("haydar.cli.HaydarConfig.load", return_value=_ready_config()),
    ):
        result = runner.invoke(app, ["ocr", "install"])

    assert result.exit_code == 1
    assert "not available in this build" in result.stdout
    assert not any(phrase in result.stdout for phrase in FORBIDDEN_PHRASES)


def test_an_offline_install_is_reported_as_retryable():
    error = OcrProvisionError(
        "Could not reach the download server.", error_code="offline", retryable=True
    )
    with (
        patch("haydar.ocr.install_ocr", side_effect=error),
        patch("haydar.cli.HaydarConfig.load", return_value=_ready_config()),
    ):
        result = runner.invoke(app, ["ocr", "install"])

    assert result.exit_code == 1
    assert "Could not reach the download server" in result.stdout


# -- ocr backfill -------------------------------------------------------------


def test_ocr_backfill_requires_a_working_engine(coordinator):
    _cls, instance = coordinator
    with patch("haydar.cli.detect_tesseract", return_value=MISSING):
        result = runner.invoke(app, ["ocr", "backfill"])

    assert result.exit_code == 1
    assert "not available yet" in result.stdout
    instance.start_ocr_backfill.assert_not_called()


def test_ocr_backfill_runs_over_the_shared_coordinator(coordinator):
    _cls, instance = coordinator
    with (
        patch("haydar.cli.detect_tesseract", return_value=FOUND),
        patch("haydar.cli.HaydarConfig.load", return_value=_ready_config()),
    ):
        result = runner.invoke(app, ["ocr", "backfill"])

    assert result.exit_code == 0
    instance.start_ocr_backfill.assert_called_once_with("tesseract-5.5.3")


def test_a_failed_backfill_exits_nonzero(coordinator):
    _cls, instance = coordinator
    instance.snapshot.return_value = IndexSnapshot(
        phase=JobPhase.FAILED, outcome=JobOutcome.FAILED, error_message="disk full"
    )
    with (
        patch("haydar.cli.detect_tesseract", return_value=FOUND),
        patch("haydar.cli.HaydarConfig.load", return_value=_ready_config()),
    ):
        result = runner.invoke(app, ["ocr", "backfill"])

    assert result.exit_code == 1
    assert "disk full" in result.stdout


def test_the_backfill_gates_on_search_readiness(coordinator):
    """An unprepared profile cannot index anything, image or otherwise."""
    _cls, instance = coordinator
    not_ready = HaydarConfig(folders=[r"C:\Docs"], search_ready=False)
    with (
        patch("haydar.cli.detect_tesseract", return_value=FOUND),
        patch("haydar.cli.HaydarConfig.load", return_value=not_ready),
    ):
        result = runner.invoke(app, ["ocr", "backfill"])

    assert result.exit_code == 1
    instance.start_ocr_backfill.assert_not_called()


# -- the advertised commands exist --------------------------------------------


@pytest.mark.parametrize("command", ["install", "status", "backfill"])
def test_every_advertised_ocr_subcommand_exists(command):
    """The `init` and `status` copy points at these; they must resolve."""
    result = runner.invoke(app, ["ocr", command, "--help"])

    assert result.exit_code == 0


def test_ocr_group_is_listed_in_top_level_help():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "ocr" in result.stdout


# -- §19 copy -----------------------------------------------------------------


def test_init_never_tells_a_user_to_pip_install_the_adapter():
    """§19: a normal user is never sent to a package manager.

    A missing adapter is a packaging fault the user cannot fix with pip, so the
    message must point at reinstalling Haydar instead.
    """
    from haydar.cli import _print_init_ocr_status

    with patch("haydar.cli.rprint") as printed:
        _print_init_ocr_status(
            TesseractInfo(TesseractStatus.PYTHON_PACKAGE_MISSING, None, None)
        )

    rendered = " ".join(str(call.args[0]) for call in printed.call_args_list)
    assert not any(phrase in rendered for phrase in FORBIDDEN_PHRASES)
    assert "Reinstall Haydar" in rendered


@pytest.mark.parametrize(
    "info",
    [
        TesseractInfo(TesseractStatus.PYTHON_PACKAGE_MISSING, None, None),
        TesseractInfo(TesseractStatus.NOT_FOUND, None, None),
        TesseractInfo(TesseractStatus.WRONG_VERSION, "3.05", r"C:\Tess\tesseract.exe"),
        TesseractInfo(TesseractStatus.ERROR, None, r"C:\Tess\tesseract.exe", "timeout"),
    ],
)
def test_no_init_ocr_message_mentions_a_package_manager_or_path(info):
    from haydar.cli import _print_init_ocr_status

    with patch("haydar.cli.rprint") as printed:
        _print_init_ocr_status(info)

    rendered = " ".join(str(call.args[0]) for call in printed.call_args_list)
    assert not any(phrase in rendered for phrase in FORBIDDEN_PHRASES)


def test_the_ocr_status_alias_points_at_a_command_that_exists():
    """The old command must not advertise a name that no longer resolves."""
    with patch(
        "haydar.cli.detect_tesseract",
        return_value=TesseractInfo(
            TesseractStatus.ERROR, None, r"C:\Tess\tesseract.exe", "timed out"
        ),
    ):
        result = runner.invoke(app, ["ocr-status"])

    assert result.exit_code == 0
    assert "ocr status" in result.stdout
    # It names the CLI binary rather than a bare `haydar` that does not exist.
    assert "haydar-cli.exe ocr status" in result.stdout.replace("\n", "")

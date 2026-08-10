import time
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from haydar.cli import app
from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexSnapshot, JobOutcome, JobPhase
from haydar.ocr import TesseractInfo, TesseractStatus

runner = CliRunner()


def _init_patches(tmp_path, info, config=None):
    """Patch the seams `init` actually uses: setup, the engine, and OCR detection.

    ``init`` now runs setup and the index job as separate steps, so the fakes
    stand in for the coordinator and engine rather than a single ``index_all``.
    """
    config = config or HaydarConfig(folders=[str(tmp_path)])
    engine = MagicMock()
    engine.__enter__.return_value.run_job.return_value = IndexSnapshot(
        run_id="test",
        phase=JobPhase.COMPLETE,
        outcome=JobOutcome.COMPLETE,
        committed_files=1,
    )
    return config, engine, (
        patch("haydar.cli.HaydarConfig.load", return_value=config),
        patch("haydar.cli.HaydarConfig.save"),
        patch("haydar.setup.SetupCoordinator.prepare_search", return_value=config),
        patch("haydar.cli.detect_tesseract", return_value=info),
        patch("haydar.indexer.engine.IndexingEngine", return_value=engine),
    )


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Haydar" in result.stdout


def test_cli_init_yes(tmp_path):
    _config, _engine, patches = _init_patches(
        tmp_path,
        TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0


def test_init_directs_normal_users_to_the_gui(tmp_path):
    """The CLI is an expert interface; it must not present itself as the path."""
    _config, _engine, patches = _init_patches(
        tmp_path,
        TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = runner.invoke(app, ["init", "--yes"])

    assert "haydar.exe" in result.stdout


def test_init_persists_the_lifecycle_through_to_complete(tmp_path):
    config, _engine, patches = _init_patches(
        tmp_path,
        TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        runner.invoke(app, ["init", "--yes"])

    assert config.initial_index_state == "complete"


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"), "image OCR enabled"),
        (TesseractInfo(TesseractStatus.PYTHON_PACKAGE_MISSING, None, None), "Python OCR adapter is not installed"),
        (TesseractInfo(TesseractStatus.NOT_FOUND, None, None), "executable not found"),
        (TesseractInfo(TesseractStatus.WRONG_VERSION, "3.05", "C:/Tesseract/tesseract.exe"), "v4+ is required"),
        (TesseractInfo(TesseractStatus.ERROR, None, "C:/Tesseract/tesseract.exe", "timed out"), "could not be verified"),
    ],
)
def test_ocr_status_reports_each_readiness_state(info, expected):
    with patch("haydar.cli.detect_tesseract", return_value=info):
        result = runner.invoke(app, ["ocr-status"])

    assert result.exit_code == 0
    assert expected in result.stdout


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"), "image OCR enabled"),
        (TesseractInfo(TesseractStatus.PYTHON_PACKAGE_MISSING, None, None), "adapter is missing"),
        (TesseractInfo(TesseractStatus.NOT_FOUND, None, None), "executable not found"),
        (TesseractInfo(TesseractStatus.WRONG_VERSION, "3.05", "C:/Tesseract/tesseract.exe"), "v4+ required"),
        (TesseractInfo(TesseractStatus.ERROR, None, "C:/Tesseract/tesseract.exe", "timed out"), "could not be verified"),
    ],
)
def test_init_reports_each_ocr_readiness_state(tmp_path, info, expected):
    _config, _engine, patches = _init_patches(tmp_path, info)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0
    assert expected in result.stdout


def test_cli_status():
    with (
        patch("haydar.search.store.VectorStore") as mock_store,
        patch("haydar.cli.HaydarConfig.load"),
    ):
        mock_store.return_value.get_stats.return_value = {
            "files_indexed": 1,
            "chunks_stored": 2,
            "db_size_bytes": 100,
        }
        result = runner.invoke(app, ["status"])
        assert result.exit_code in (0, 1)


def test_cli_config():
    with patch("haydar.cli.HaydarConfig.load"):
        result = runner.invoke(app, ["config"])
        assert result.exit_code in (0, 1)


def test_cli_reindex():
    with (
        patch("haydar.cli.HaydarConfig.load") as mock_load,
        patch("haydar.cli._ensure_ripgrep"),
        patch("haydar.indexer.engine.IndexingEngine"),
    ):
        mock_config = HaydarConfig()
        mock_config.folders = ["/fake/dir"]
        mock_config.initialized = True
        mock_load.return_value = mock_config

        result = runner.invoke(app, ["reindex"])
        assert result.exit_code in (0, 1)


def _invoke_update(config: HaydarConfig, latest: str | None, *args: str):
    config.save = MagicMock()
    with (
        patch("haydar.cli.HaydarConfig.load", return_value=config),
        patch("haydar.updater.get_latest_version", return_value=latest) as get_latest,
    ):
        result = runner.invoke(app, ["update-check", *args])
    return result, get_latest


def test_update_check_suppresses_recent_check():
    config = HaydarConfig(last_update_check=time.time())
    result, get_latest = _invoke_update(config, "99.0")
    assert result.exit_code == 0
    assert "last checked recently" in result.stdout
    get_latest.assert_not_called()
    config.save.assert_not_called()


def test_update_check_force_reports_available_and_saves():
    config = HaydarConfig(last_update_check=time.time())
    result, get_latest = _invoke_update(config, "99.0", "--force")
    assert result.exit_code == 0
    assert "Update available: 99.0" in result.stdout
    assert "/v99.0" in result.stdout
    get_latest.assert_called_once()
    config.save.assert_called_once()


def test_update_check_reports_current_and_saves():
    config = HaydarConfig(last_update_check=0)
    result, _ = _invoke_update(config, "0.0.1")
    assert result.exit_code == 0
    assert "Up to date (current:" in result.stdout
    config.save.assert_called_once()


def test_update_check_network_failure_has_nonzero_exit_without_store_import():
    config = HaydarConfig(last_update_check=0)
    with patch.dict("sys.modules", {"haydar.search.store": None}):
        result, _ = _invoke_update(config, None)
    assert result.exit_code == 1
    assert "Could not reach GitHub" in result.stdout
    config.save.assert_not_called()


def test_update_check_future_clock_value_does_not_suppress_forever():
    config = HaydarConfig(last_update_check=time.time() + 3600)
    result, get_latest = _invoke_update(config, "0.0.1")
    assert result.exit_code == 0
    get_latest.assert_called_once()


@pytest.mark.parametrize("force", [False, True])
def test_update_check_snooze_only_suppresses_without_force(force):
    config = HaydarConfig(update_check_snoozed_until=time.time() + 3600)
    args = ("--force",) if force else ()
    result, get_latest = _invoke_update(config, "0.0.1", *args)
    assert result.exit_code == 0
    if force:
        get_latest.assert_called_once()
    else:
        get_latest.assert_not_called()
        assert "dismissed temporarily" in result.stdout


# -- readiness gating -------------------------------------------------------


@pytest.mark.parametrize("command", [["search", "q"], ["reindex", "--yes"], ["watch"]])
def test_commands_refuse_before_search_is_ready(command):
    """Gating is on search readiness, never on the legacy initialized flag."""
    config = HaydarConfig(folders=[r"C:\Docs"], search_ready=False)
    with patch("haydar.cli.HaydarConfig.load", return_value=config):
        result = runner.invoke(app, command)

    assert result.exit_code == 1
    assert "haydar.exe" in result.stdout


@pytest.mark.parametrize("state", ["not_started", "running", "paused"])
def test_search_works_while_the_initial_index_is_incomplete(state):
    """A partial index is a valid state to search."""
    config = HaydarConfig(
        folders=[r"C:\Docs"], search_ready=True, initial_index_state=state
    )
    search = MagicMock()
    search.return_value.search.return_value = []
    with (
        patch("haydar.cli.HaydarConfig.load", return_value=config),
        patch("haydar.search.hybrid.HybridSearch", search),
    ):
        result = runner.invoke(app, ["search", "budget"])

    assert result.exit_code == 0
    assert state in result.stdout


@pytest.mark.parametrize("state", ["not_started", "running", "paused"])
def test_watch_refuses_while_the_initial_index_is_unsafe(state):
    """The watcher gate is never bypassed: it would race the crawl for the lock."""
    config = HaydarConfig(
        folders=[r"C:\Docs"], search_ready=True, initial_index_state=state
    )
    watcher = MagicMock()
    with (
        patch("haydar.cli.HaydarConfig.load", return_value=config),
        patch("haydar.indexer.watcher.FileWatcher", watcher),
    ):
        result = runner.invoke(app, ["watch"])

    assert result.exit_code == 1
    assert watcher.call_count == 0
    assert "cannot start yet" in result.stdout


@pytest.mark.parametrize("state", ["complete", "cancelled", "failed"])
def test_watch_starts_after_a_safe_terminal_state(state):
    config = HaydarConfig(
        folders=[r"C:\Docs"], search_ready=True, initial_index_state=state
    )
    watcher = MagicMock()
    with (
        patch("haydar.cli.HaydarConfig.load", return_value=config),
        patch("haydar.indexer.watcher.FileWatcher", watcher),
    ):
        result = runner.invoke(app, ["watch"])

    assert result.exit_code == 0
    watcher.return_value.start.assert_called_once_with(blocking=True)


def test_status_reports_readiness_without_requiring_a_ready_index():
    config = HaydarConfig(
        folders=[r"C:\Docs"],
        folders_configured=True,
        search_ready=False,
        initial_index_state="paused",
    )
    with patch("haydar.cli.HaydarConfig.load", return_value=config):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "paused" in result.stdout
    assert "Watcher eligible" in result.stdout

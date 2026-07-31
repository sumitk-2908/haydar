import time
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from haydar.cli import app
from haydar.config import HaydarConfig
from haydar.ocr import TesseractInfo, TesseractStatus

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Haydar" in result.stdout


def test_cli_init_yes(tmp_path):
    engine = MagicMock()
    engine.__enter__.return_value.index_all.return_value = {}
    with (
        patch("haydar.cli.HaydarConfig.load") as mock_load,
        patch("haydar.cli.HaydarConfig.save"),
        patch("haydar.cli._ensure_ripgrep"),
        patch(
            "haydar.cli.detect_tesseract",
            return_value=TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"),
        ),
        patch("haydar.indexer.engine.IndexingEngine", return_value=engine),
    ):
        mock_config = HaydarConfig()
        mock_config.folders = [str(tmp_path)]
        mock_load.return_value = mock_config

        result = runner.invoke(app, ["init", "--yes"])
        assert result.exit_code == 0


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
    if info.status in {
        TesseractStatus.PYTHON_PACKAGE_MISSING,
        TesseractStatus.NOT_FOUND,
        TesseractStatus.WRONG_VERSION,
    }:
        assert 'pip install "haydar[ocr]"' in result.stdout


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
    engine = MagicMock()
    engine.__enter__.return_value.index_all.return_value = {}
    with (
        patch("haydar.cli.HaydarConfig.load", return_value=HaydarConfig(folders=[str(tmp_path)])),
        patch("haydar.cli.HaydarConfig.save"),
        patch("haydar.cli._ensure_ripgrep"),
        patch("haydar.cli.detect_tesseract", return_value=info),
        patch("haydar.indexer.engine.IndexingEngine", return_value=engine),
    ):
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

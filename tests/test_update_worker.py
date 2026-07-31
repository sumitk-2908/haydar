import time
from unittest.mock import MagicMock, patch

from haydar.config import HaydarConfig
from haydar.ui.window import UpdateCheckWorker


def _worker(config: HaydarConfig):
    config.save = MagicMock()
    return UpdateCheckWorker(config)


def test_update_worker_interval_opt_out(monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    worker = _worker(HaydarConfig(update_check_interval_hours=0))
    with patch("haydar.ui.window.get_latest_version") as fetch:
        worker.check()
    fetch.assert_not_called()


def test_update_worker_skips_development_mode(monkeypatch):
    monkeypatch.delattr("sys.frozen", raising=False)
    worker = _worker(HaydarConfig(last_update_check=0))
    with patch("haydar.ui.window.get_latest_version") as fetch:
        worker.check()
    fetch.assert_not_called()


def test_update_worker_suppresses_recent_check(monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    worker = _worker(HaydarConfig(last_update_check=time.time()))
    with patch("haydar.ui.window.get_latest_version") as fetch:
        worker.check()
    fetch.assert_not_called()


def test_update_worker_emits_result_and_timestamp_without_saving(monkeypatch, qtbot):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    config = HaydarConfig(last_update_check=0, update_check_interval_hours=0.01)
    worker = _worker(config)
    versions = []
    checked = []
    worker.update_available.connect(versions.append)
    worker.checked.connect(checked.append)
    with (
        patch("haydar.ui.window.get_latest_version", return_value="99.0"),
        patch("haydar.ui.window.time.time", side_effect=[1000.0, 1001.0]),
    ):
        worker.check()
    qtbot.wait(10)
    assert versions == ["99.0"]
    assert checked == [1001.0]
    assert config.last_update_check == 0
    config.save.assert_not_called()


def test_update_worker_network_failure_does_not_persist(monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    config = HaydarConfig(last_update_check=0, update_check_interval_hours=0.01)
    worker = _worker(config)
    with patch("haydar.ui.window.get_latest_version", return_value=None):
        worker.check()
    config.save.assert_not_called()


def test_update_worker_does_not_save_in_background(monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    worker = _worker(HaydarConfig(last_update_check=0))
    with patch("haydar.ui.window.get_latest_version", return_value="0.0.1"):
        worker.check()
    worker.config.save.assert_not_called()


def test_update_worker_future_last_check_does_not_suppress(monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    worker = _worker(HaydarConfig(last_update_check=time.time() + 3600))
    with patch("haydar.ui.window.get_latest_version", return_value="0.0.1") as fetch:
        worker.check()
    fetch.assert_called_once()


def test_update_worker_finishes_after_check(monkeypatch, qtbot):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    worker = _worker(HaydarConfig(last_update_check=0))
    finished = []
    worker.finished.connect(lambda: finished.append(True))
    with patch("haydar.ui.window.get_latest_version", return_value=None):
        worker.check()
    assert finished == [True]


def test_update_worker_finished_on_fetch_error(monkeypatch, qtbot):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    worker = _worker(HaydarConfig(last_update_check=0))
    finished = []
    worker.finished.connect(lambda: finished.append(True))
    with patch("haydar.ui.window.get_latest_version", side_effect=OSError("offline")):
        worker.check()
    assert finished == [True]

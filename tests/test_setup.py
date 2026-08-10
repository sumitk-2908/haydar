import json
import threading

import pytest

from haydar.config import HaydarConfig
from haydar.setup import (
    SetupCancelled,
    SetupCoordinator,
    SetupError,
    SetupPhase,
    scan_folder,
)


class _FakeStore:
    """A store that satisfies every readiness probe the coordinator runs."""

    def __init__(self, calls):
        self.calls = calls

    def get_stats(self):
        self.calls.append("stats")
        return {"total_chunks": 0}

    def embed_probe(self, text):
        self.calls.append("embed")
        return 384

    def verify_readable(self):
        self.calls.append("query")


def _coordinator(config, calls, **overrides):
    """Build a coordinator with every external dependency stubbed out."""
    options = {
        # No binary is already present by default, so these tests exercise the
        # provisioning path. The "already bundled" path has its own test below.
        "ripgrep_locator": lambda: None,
        "ripgrep_provisioner": lambda destination: calls.append(("rg", destination))
        or destination / "rg.exe",
        "ripgrep_verifier": lambda path: calls.append(("verify_rg", path)),
        "store_factory": lambda current, allow_download: calls.append(
            ("store", current, allow_download)
        )
        or _FakeStore(calls),
    }
    options.update(overrides)
    return SetupCoordinator(config, **options)


def _patch_config_path(monkeypatch, tmp_haydar):
    import haydar.config as config_module

    config_path = tmp_haydar / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    return config_path


def test_prepare_search_persists_readiness_before_indexing(
    tmp_haydar, monkeypatch, tmp_path
):
    config_path = _patch_config_path(monkeypatch, tmp_haydar)
    documents = tmp_path / "Documents"
    documents.mkdir()
    config = HaydarConfig(
        folders=[str(documents)],
        initial_index_state="not_started",
    )
    calls = []
    events = []

    coordinator = _coordinator(config, calls)
    result = coordinator.prepare_search(progress_callback=events.append)

    assert result.search_ready is True
    assert result.initialized is True
    assert result.folders_configured is True
    # Setup provisions readiness only; it never starts or advances the crawl.
    assert result.initial_index_state == "not_started"
    assert events[-1].phase is SetupPhase.SEARCH_READY
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["search_ready"] is True
    assert persisted["initial_index_state"] == "not_started"


def test_prepare_search_runs_phases_in_the_documented_order(
    tmp_haydar, monkeypatch, tmp_path
):
    _patch_config_path(monkeypatch, tmp_haydar)
    documents = tmp_path / "Documents"
    documents.mkdir()
    calls = []
    events = []

    _coordinator(HaydarConfig(folders=[str(documents)]), calls).prepare_search(
        progress_callback=events.append
    )

    started = [event.phase for event in events if event.status == "started"]
    assert started == [
        SetupPhase.PREPARING_DIRECTORIES,
        SetupPhase.PREPARING_KEYWORD_SEARCH,
        SetupPhase.PREPARING_MODEL,
        SetupPhase.PREPARING_COLLECTION,
    ]
    # Keyword search is verified, the exact model is proven to embed, and the
    # collection answers both a count and a query before readiness is claimed.
    assert [call[0] if isinstance(call, tuple) else call for call in calls] == [
        "rg",
        "verify_rg",
        "store",
        "embed",
        "stats",
        "query",
    ]


def test_search_ready_event_is_emitted_only_after_the_save_succeeds(
    tmp_haydar, monkeypatch, tmp_path
):
    config_path = _patch_config_path(monkeypatch, tmp_haydar)
    documents = tmp_path / "Documents"
    documents.mkdir()
    observed: list[bool] = []

    def record_persisted_state(event):
        if event.phase is SetupPhase.SEARCH_READY:
            on_disk = json.loads(config_path.read_text(encoding="utf-8"))
            observed.append(on_disk["search_ready"])

    _coordinator(HaydarConfig(folders=[str(documents)]), []).prepare_search(
        progress_callback=record_persisted_state
    )

    # Readiness the user cannot rely on after a crash is not readiness.
    assert observed == [True]


def test_prepare_search_does_not_mark_ready_when_model_fails(
    tmp_haydar, monkeypatch, tmp_path
):
    _patch_config_path(monkeypatch, tmp_haydar)
    documents = tmp_path / "Documents"
    documents.mkdir()
    config = HaydarConfig(folders=[str(documents)])

    def fail_store(_config, _allow_download):
        raise RuntimeError("offline")

    coordinator = _coordinator(config, [], store_factory=fail_store)

    with pytest.raises(SetupError):
        coordinator.prepare_search()

    assert config.search_ready is False
    assert HaydarConfig.load().search_ready is False
    # The user's folder selection survives a failed setup.
    assert HaydarConfig.load().folders == [str(documents)]


def test_offline_failure_is_reported_as_retryable(tmp_haydar, monkeypatch, tmp_path):
    _patch_config_path(monkeypatch, tmp_haydar)
    documents = tmp_path / "Documents"
    documents.mkdir()

    def offline_store(_config, _allow_download):
        raise OSError("Max retries exceeded: connection refused")

    coordinator = _coordinator(
        HaydarConfig(folders=[str(documents)]), [], store_factory=offline_store
    )

    with pytest.raises(SetupError) as exc_info:
        coordinator.prepare_search()

    assert exc_info.value.code == "offline"
    assert exc_info.value.retryable is True


def test_an_already_present_ripgrep_is_verified_without_downloading(
    tmp_haydar, monkeypatch, tmp_path
):
    """A bundled or private binary is used as-is; a packaged first run is offline.

    The located path is also the one that gets verified, so setup probes the
    binary keyword search will actually execute rather than a second copy.
    """
    _patch_config_path(monkeypatch, tmp_haydar)
    documents = tmp_path / "Documents"
    documents.mkdir()
    bundled = tmp_path / "bundle" / "rg.exe"
    bundled.parent.mkdir()
    bundled.write_text("binary", encoding="utf-8")
    calls = []

    coordinator = _coordinator(
        HaydarConfig(folders=[str(documents)]),
        calls,
        ripgrep_locator=lambda: bundled,
        ripgrep_provisioner=lambda _destination: pytest.fail(
            "a present binary must not be re-downloaded"
        ),
    )
    coordinator.prepare_search()

    assert ("verify_rg", bundled) in calls


def test_locate_ripgrep_reports_absence_instead_of_raising(monkeypatch):
    """A missing binary is a ``None`` the coordinator can act on, not an error."""
    import haydar.setup as setup_module
    from haydar.config import HaydarConfigError

    def missing():
        raise HaydarConfigError("no rg here", hint="")

    monkeypatch.setattr("haydar.config.get_rg_path", missing)

    assert setup_module.locate_ripgrep() is None


def test_unverifiable_keyword_search_binary_blocks_readiness(
    tmp_haydar, monkeypatch, tmp_path
):
    _patch_config_path(monkeypatch, tmp_haydar)
    documents = tmp_path / "Documents"
    documents.mkdir()
    config = HaydarConfig(folders=[str(documents)])

    def reject(_path):
        raise SetupError(
            "bad binary", code="keyword_search_unavailable", hint="retry"
        )

    coordinator = _coordinator(config, [], ripgrep_verifier=reject)

    with pytest.raises(SetupError):
        coordinator.prepare_search()

    assert config.search_ready is False


def test_prepare_search_honours_cancellation_before_provisioning(
    tmp_haydar, monkeypatch
):
    _patch_config_path(monkeypatch, tmp_haydar)
    cancelled = threading.Event()
    cancelled.set()
    coordinator = SetupCoordinator(
        HaydarConfig(),
        ripgrep_provisioner=lambda _destination: pytest.fail("must not provision"),
    )

    with pytest.raises(SetupCancelled):
        coordinator.prepare_search(cancel_event=cancelled)


def test_cancellation_after_provisioning_preserves_verified_assets(
    tmp_haydar, monkeypatch, tmp_path
):
    _patch_config_path(monkeypatch, tmp_haydar)
    documents = tmp_path / "Documents"
    documents.mkdir()
    config = HaydarConfig(folders=[str(documents)])
    cancel_event = threading.Event()
    calls = []

    def cancel_once_verified(path):
        calls.append(("verify_rg", path))
        cancel_event.set()

    coordinator = _coordinator(config, calls, ripgrep_verifier=cancel_once_verified)

    with pytest.raises(SetupCancelled):
        coordinator.prepare_search(cancel_event=cancel_event)

    assert config.search_ready is False
    assert config.folders == [str(documents)]
    # Work already completed is not undone; a retry resumes from here.
    assert ("verify_rg", tmp_haydar / "bin" / "rg.exe") in calls


def test_setup_never_clears_an_existing_collection(tmp_haydar, monkeypatch, tmp_path):
    _patch_config_path(monkeypatch, tmp_haydar)
    documents = tmp_path / "Documents"
    documents.mkdir()

    class _GuardedStore(_FakeStore):
        def clear(self):
            raise AssertionError("setup must never clear the user's index")

    coordinator = _coordinator(
        HaydarConfig(folders=[str(documents)]),
        [],
        store_factory=lambda _config, _allow: _GuardedStore([]),
    )

    coordinator.prepare_search()


def test_scan_folder_stops_at_supported_file_cap(tmp_path):
    folder = tmp_path / "large"
    folder.mkdir()
    for index in range(25):
        (folder / f"file-{index}.txt").write_text("content", encoding="utf-8")

    result = scan_folder(
        folder,
        HaydarConfig(),
        file_cap=10,
        deadline_seconds=5,
    )

    assert result.supported_files == 10
    assert result.capped is True
    assert result.incomplete is True
    assert result.needs_confirmation is True


def test_scan_folder_ignores_unsupported_and_excluded_files(tmp_path):
    folder = tmp_path / "folder"
    excluded = folder / "node_modules"
    excluded.mkdir(parents=True)
    (folder / "included.pdf").write_bytes(b"pdf")
    (folder / "ignored.bin").write_bytes(b"binary")
    (excluded / "hidden.txt").write_text("hidden", encoding="utf-8")

    result = scan_folder(folder, HaydarConfig(), deadline_seconds=5)

    assert result.supported_files == 1


def test_small_local_folder_needs_no_confirmation(tmp_path):
    folder = tmp_path / "small"
    folder.mkdir()
    (folder / "notes.txt").write_text("hello", encoding="utf-8")

    result = scan_folder(folder, HaydarConfig(), deadline_seconds=5)

    assert result.needs_confirmation is False
    assert result.incomplete is False


def test_scan_folder_stops_at_visited_entry_cap(tmp_path):
    folder = tmp_path / "many"
    folder.mkdir()
    for index in range(40):
        (folder / f"file-{index}.bin").write_bytes(b"x")

    result = scan_folder(
        folder, HaydarConfig(), visited_cap=10, deadline_seconds=5
    )

    # Unsupported files still cost a visit, so the hard cap protects the scan
    # even when nothing indexable is found.
    assert result.visited_files == 10
    assert result.capped is True
    assert result.needs_confirmation is True


def test_scan_folder_counts_inaccessible_directories_without_failing(
    tmp_path, monkeypatch
):
    import haydar.setup as setup_module

    folder = tmp_path / "partly-readable"
    folder.mkdir()
    (folder / "ok.txt").write_text("fine", encoding="utf-8")

    def walk_with_error(_path, onerror=None, **_kwargs):
        if onerror is not None:
            onerror(OSError("permission denied"))
        yield str(folder), [], ["ok.txt"]

    monkeypatch.setattr(setup_module.os, "walk", walk_with_error)

    result = scan_folder(folder, HaydarConfig(), deadline_seconds=5)

    assert result.inaccessible_directories == 1
    assert result.supported_files == 1


def test_scan_folder_is_cancellable(tmp_path):
    folder = tmp_path / "cancelled"
    folder.mkdir()
    (folder / "a.txt").write_text("a", encoding="utf-8")
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(SetupCancelled):
        scan_folder(folder, HaydarConfig(), cancel_event=cancel_event)


def test_drive_root_always_requires_confirmation(tmp_path, monkeypatch):
    import haydar.setup as setup_module

    folder = tmp_path / "root-like"
    folder.mkdir()
    (folder / "a.txt").write_text("a", encoding="utf-8")
    monkeypatch.setattr(setup_module, "is_drive_root", lambda _folder: True)

    result = scan_folder(folder, HaydarConfig(), deadline_seconds=5)

    assert result.is_drive_root is True
    assert result.needs_confirmation is True


def test_network_root_always_requires_confirmation(tmp_path, monkeypatch):
    import haydar.setup as setup_module

    folder = tmp_path / "share-like"
    folder.mkdir()
    (folder / "a.txt").write_text("a", encoding="utf-8")
    monkeypatch.setattr(setup_module, "is_network_root", lambda _folder: True)

    result = scan_folder(folder, HaydarConfig(), deadline_seconds=5)

    assert result.is_network_root is True
    assert result.needs_confirmation is True

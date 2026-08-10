"""Contract tests for job coordination and application-level sequencing."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from haydar.application import ApplicationService
from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexSnapshot, JobKind, JobOutcome, JobPhase
from haydar.indexer.jobs import IndexJobCoordinator


class _FakeEngine:
    """An engine whose run outcome and timing the test controls."""

    def __init__(self, outcome=JobOutcome.COMPLETE, block: threading.Event | None = None):
        self.outcome = outcome
        self.block = block
        self.closed = False
        self.calls = []

    def run_job(self, **kwargs):
        self.calls.append(kwargs)
        control = kwargs.get("control")
        if self.block is not None:
            # Simulate a long crawl that stops when asked.
            while not self.block.is_set():
                if control is not None and control.should_stop:
                    break
                time.sleep(0.005)

        outcome = self.outcome
        if control is not None and control.cancel_requested:
            outcome = JobOutcome.CANCELLED
        elif control is not None and control.pause_requested:
            outcome = JobOutcome.PAUSED

        snapshot = IndexSnapshot(
            run_id=kwargs.get("run_id", ""),
            kind=kwargs.get("kind", JobKind.INITIAL),
            phase=JobPhase.COMPLETE,
            outcome=outcome,
            committed_files=3,
            error_message="boom" if outcome is JobOutcome.FAILED else "",
        )
        on_snapshot = kwargs.get("on_snapshot")
        if on_snapshot is not None:
            on_snapshot(snapshot)
        return snapshot

    def close(self):
        self.closed = True


def _coordinator(config=None, engine=None, **config_kwargs):
    config = config or HaydarConfig(folders=[r"C:\Docs"], **config_kwargs)
    engine = engine or _FakeEngine()
    coordinator = IndexJobCoordinator(config, engine_factory=lambda _cfg: engine)
    return config, coordinator, engine


# -- lifecycle integration --------------------------------------------------


def test_a_completed_run_persists_complete():
    config, coordinator, _ = _coordinator()

    coordinator.start_initial()
    assert coordinator.wait_for_terminal(timeout=5)

    assert config.initial_index_state == "complete"


def test_a_cancelled_run_persists_cancelled_and_does_not_auto_restart():
    block = threading.Event()
    config, coordinator, _ = _coordinator(engine=_FakeEngine(block=block))

    coordinator.start_initial()
    coordinator.cancel()
    block.set()
    assert coordinator.wait_for_terminal(timeout=5)

    assert config.initial_index_state == "cancelled"
    assert coordinator.autostart_if_due() is None


def test_a_paused_run_records_why_it_paused():
    block = threading.Event()
    config, coordinator, _ = _coordinator(engine=_FakeEngine(block=block))

    coordinator.start_initial()
    coordinator.pause()
    block.set()
    assert coordinator.wait_for_terminal(timeout=5)

    assert config.initial_index_state == "paused"
    assert config.initial_index_pause_reason == "user"


def test_a_failed_run_records_a_bounded_reason():
    config, coordinator, _ = _coordinator(engine=_FakeEngine(outcome=JobOutcome.FAILED))

    coordinator.start_initial()
    assert coordinator.wait_for_terminal(timeout=5)

    assert config.initial_index_state == "failed"
    assert config.initial_index_error == "boom"


def test_duplicate_starts_are_idempotent_while_a_run_is_active():
    block = threading.Event()
    _, coordinator, engine = _coordinator(engine=_FakeEngine(block=block))

    first = coordinator.start_initial()
    second = coordinator.start_initial()
    block.set()
    coordinator.wait_for_terminal(timeout=5)

    assert first == second
    assert len(engine.calls) == 1


# -- launch policy ----------------------------------------------------------


def test_autostart_runs_for_a_fresh_install():
    _, coordinator, _ = _coordinator(initial_index_state="not_started")

    assert coordinator.autostart_if_due() is not None
    coordinator.wait_for_terminal(timeout=5)


def test_autostart_recovers_and_resumes_an_interrupted_run():
    config, coordinator, _ = _coordinator(initial_index_state="running")

    run_id = coordinator.autostart_if_due()
    assert coordinator.wait_for_terminal(timeout=5)

    # Recovery happened, and the crawl resumed on its own.
    assert run_id is not None
    assert config.initial_index_state == "complete"


def test_autostart_respects_an_explicit_user_pause():
    _, coordinator, engine = _coordinator(
        initial_index_state="paused", initial_index_pause_reason="user"
    )

    assert coordinator.autostart_if_due() is None
    assert engine.calls == []


def test_autostart_does_not_rerun_a_complete_crawl():
    _, coordinator, engine = _coordinator(initial_index_state="complete")

    assert coordinator.autostart_if_due() is None
    assert engine.calls == []


def test_a_failure_is_retried_once_per_launch():
    _, coordinator, _ = _coordinator(
        engine=_FakeEngine(outcome=JobOutcome.FAILED),
        initial_index_state="failed",
    )

    assert coordinator.autostart_if_due() is not None
    coordinator.wait_for_terminal(timeout=5)
    # The second attempt within the same launch waits for an explicit Retry.
    assert coordinator.autostart_if_due() is None


# -- job kinds --------------------------------------------------------------


def test_incremental_and_ocr_jobs_never_regress_a_complete_crawl():
    config, coordinator, _ = _coordinator(initial_index_state="complete")

    coordinator.start_incremental()
    assert coordinator.wait_for_terminal(timeout=5)
    assert config.initial_index_state == "complete"

    coordinator.start_ocr_backfill("tesseract-5.3.1")
    assert coordinator.wait_for_terminal(timeout=5)
    assert config.initial_index_state == "complete"


def test_ocr_backfill_targets_images_only():
    _, coordinator, engine = _coordinator(initial_index_state="complete")

    coordinator.start_ocr_backfill("tesseract-5.3.1")
    coordinator.wait_for_terminal(timeout=5)

    call = engine.calls[0]
    assert call["kind"] is JobKind.OCR_BACKFILL
    assert call["only_extensions"] == frozenset({".png", ".jpg", ".jpeg", ".tiff"})
    assert call["ocr_version"] == "tesseract-5.3.1"


# -- snapshots --------------------------------------------------------------


def test_subscribers_receive_snapshots_and_can_unsubscribe():
    _, coordinator, _ = _coordinator()
    received = []
    subscription = coordinator.subscribe(received.append)

    coordinator.start_initial()
    coordinator.wait_for_terminal(timeout=5)
    count_while_subscribed = len(received)
    subscription.cancel()
    coordinator.start_incremental()
    coordinator.wait_for_terminal(timeout=5)

    assert count_while_subscribed > 0
    assert len(received) == count_while_subscribed


def test_a_failing_subscriber_does_not_break_the_run():
    config, coordinator, _ = _coordinator()
    coordinator.subscribe(lambda _s: (_ for _ in ()).throw(RuntimeError("bad UI")))

    coordinator.start_initial()
    assert coordinator.wait_for_terminal(timeout=5)

    assert config.initial_index_state == "complete"


def test_the_engine_is_always_closed():
    _, coordinator, engine = _coordinator()

    coordinator.start_initial()
    coordinator.wait_for_terminal(timeout=5)

    assert engine.closed is True


# -- shutdown ---------------------------------------------------------------


def test_shutdown_requests_a_cooperative_pause():
    block = threading.Event()
    config, coordinator, _ = _coordinator(engine=_FakeEngine(block=block))
    coordinator.start_initial()

    acknowledged = coordinator.shutdown(timeout=5)
    block.set()

    assert acknowledged is True
    assert config.initial_index_state == "paused"


def test_shutdown_leaves_running_persisted_when_the_worker_will_not_stop():
    """A worker that ignores the pause leaves recovery to the next launch."""
    never = threading.Event()

    class _StubbornEngine(_FakeEngine):
        def run_job(self, **kwargs):
            never.wait(timeout=30)
            return IndexSnapshot(outcome=JobOutcome.COMPLETE)

    config, coordinator, _ = _coordinator(engine=_StubbornEngine())
    coordinator.start_initial()

    acknowledged = coordinator.shutdown(timeout=0.2)
    never.set()

    assert acknowledged is False
    assert config.initial_index_state == "running"


# -- watcher timing ---------------------------------------------------------


@pytest.mark.parametrize("state", ["not_started", "running", "paused"])
def test_watcher_does_not_start_before_a_safe_terminal_state(state):
    config = HaydarConfig(folders=[r"C:\Docs"], initial_index_state=state)
    watcher = MagicMock()
    service = ApplicationService(
        config,
        job_coordinator=IndexJobCoordinator(
            config, engine_factory=lambda _c: _FakeEngine()
        ),
        watcher_factory=lambda _c: watcher,
    )

    assert service.start_watcher_if_eligible() is False
    assert watcher.start.call_count == 0


@pytest.mark.parametrize("state", ["complete", "cancelled", "failed"])
def test_watcher_starts_after_a_safe_terminal_state(state):
    config = HaydarConfig(folders=[r"C:\Docs"], initial_index_state=state)
    watcher = MagicMock()
    service = ApplicationService(
        config,
        job_coordinator=IndexJobCoordinator(
            config, engine_factory=lambda _c: _FakeEngine()
        ),
        watcher_factory=lambda _c: watcher,
    )

    assert service.start_watcher_if_eligible() is True
    watcher.start.assert_called_once_with(blocking=False)


def test_watcher_waits_for_the_worker_to_release_the_writer_lock():
    """A terminal persisted state is not enough while a worker is still running."""
    block = threading.Event()
    config = HaydarConfig(folders=[r"C:\Docs"], initial_index_state="complete")
    coordinator = IndexJobCoordinator(
        config, engine_factory=lambda _c: _FakeEngine(block=block)
    )
    watcher = MagicMock()
    service = ApplicationService(
        config, job_coordinator=coordinator, watcher_factory=lambda _c: watcher
    )

    coordinator.start_initial(force=True)
    assert service.start_watcher_if_eligible() is False

    block.set()
    coordinator.wait_for_terminal(timeout=5)
    assert service.start_watcher_if_eligible() is True


def test_watcher_is_started_only_once():
    config = HaydarConfig(folders=[r"C:\Docs"], initial_index_state="complete")
    watcher = MagicMock()
    service = ApplicationService(
        config,
        job_coordinator=IndexJobCoordinator(
            config, engine_factory=lambda _c: _FakeEngine()
        ),
        watcher_factory=lambda _c: watcher,
    )

    service.start_watcher_if_eligible()
    service.start_watcher_if_eligible()

    assert watcher.start.call_count == 1


def test_folder_change_stops_the_watcher_and_schedules_a_fresh_crawl():
    config = HaydarConfig(folders=[r"C:\Old"], initial_index_state="complete")
    watcher = MagicMock()
    service = ApplicationService(
        config,
        job_coordinator=IndexJobCoordinator(
            config, engine_factory=lambda _c: _FakeEngine()
        ),
        watcher_factory=lambda _c: watcher,
    )
    service.start_watcher_if_eligible()

    service.apply_folder_change([r"C:\New"])
    service.jobs.wait_for_terminal(timeout=5)

    watcher.stop.assert_called_once()
    assert config.folders == [r"C:\New"]
    assert config.folders_configured is True


# -- onboarding -------------------------------------------------------------


def test_a_migrated_legacy_install_skips_onboarding():
    config = HaydarConfig(
        folders=[r"C:\Docs"],
        folders_configured=True,
        search_ready=True,
        initial_index_state="complete",
    )
    service = ApplicationService(config, watcher_factory=None)

    assert service.needs_onboarding is False


def test_a_fresh_install_requires_onboarding():
    service = ApplicationService(HaydarConfig(folders=[]), watcher_factory=None)

    assert service.needs_onboarding is True


def test_no_configured_folders_means_no_initial_crawl():
    config = HaydarConfig(folders=[], search_ready=True)
    service = ApplicationService(
        config,
        job_coordinator=IndexJobCoordinator(
            config, engine_factory=lambda _c: _FakeEngine()
        ),
    )

    assert service.start_initial_index_if_due() is None

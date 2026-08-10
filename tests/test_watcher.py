"""Watcher contract: bounded queue, one serialized writer, safe handoff.

The properties under test are the ones §10 of the first-run contract names:
events are coalesced per canonical path into a *bounded* queue, exactly one
consumer applies them through the shared writer lock, move/delete ordering
survives coalescing, and a folder-set change stops, reconciles, and restarts
rather than mutating a live observer.
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from haydar.application import ApplicationService
from haydar.config import HaydarConfig
from haydar.indexer.jobs import IndexJobCoordinator
from haydar.indexer.watcher import (
    FileWatcher,
    SerializedWriter,
    WatchEventQueue,
    WriteKind,
    _WatchEventHandler,
)


class _Clock:
    """A manual clock, so debounce behaviour is tested without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def config(tmp_haydar):
    cfg = HaydarConfig()
    cfg.watcher_debounce_seconds = 0.1
    return cfg


def _queue(clock, **kwargs):
    kwargs.setdefault("debounce_seconds", 0.0)
    return WatchEventQueue(time_source=clock, **kwargs)


def _drain(queue) -> list[tuple[WriteKind, str]]:
    items = []
    while True:
        item = queue.pop_due(timeout=0)
        if item is None:
            return items
        items.append((item.kind, str(item.path)))


# -- queue: bounding --------------------------------------------------------


def test_queue_never_grows_past_its_capacity(clock):
    queue = _queue(clock, capacity=4)

    for index in range(50):
        queue.submit(WriteKind.INDEX, rf"C:\Docs\file-{index}.txt")

    assert len(queue) == 4
    assert queue.dropped == 46


def test_overflow_is_reported_once_per_burst_so_a_catch_up_can_run(clock):
    overflows = []
    queue = _queue(clock, capacity=2, on_overflow=lambda: overflows.append(1))

    for index in range(10):
        queue.submit(WriteKind.INDEX, rf"C:\Docs\file-{index}.txt")

    # One report for the burst, not one per dropped event.
    assert overflows == [1]
    assert queue.dropped == 8


def test_overflow_can_be_reported_again_after_the_queue_drains(clock):
    overflows = []
    queue = _queue(clock, capacity=2, on_overflow=lambda: overflows.append(1))

    for index in range(6):
        queue.submit(WriteKind.INDEX, rf"C:\Docs\a-{index}.txt")
    _drain(queue)
    for index in range(6):
        queue.submit(WriteKind.INDEX, rf"C:\Docs\b-{index}.txt")

    assert overflows == [1, 1]


def test_the_oldest_pending_write_is_the_one_dropped(clock):
    queue = _queue(clock, capacity=2)

    queue.submit(WriteKind.INDEX, r"C:\Docs\old.txt")
    queue.submit(WriteKind.INDEX, r"C:\Docs\mid.txt")
    queue.submit(WriteKind.INDEX, r"C:\Docs\new.txt")

    remaining = [path for _kind, path in queue.snapshot()]
    assert [Path(p).name for p in remaining] == ["mid.txt", "new.txt"]


# -- queue: coalescing ------------------------------------------------------


def test_repeated_events_for_one_path_collapse_into_a_single_write(clock):
    queue = _queue(clock)

    for _ in range(100):
        queue.submit(WriteKind.INDEX, r"C:\Docs\busy.txt")

    assert len(queue) == 1
    assert len(_drain(queue)) == 1


def test_coalescing_is_case_insensitive_on_windows_paths(clock):
    queue = _queue(clock)

    queue.submit(WriteKind.INDEX, r"C:\Docs\Report.txt")
    queue.submit(WriteKind.INDEX, r"c:\docs\report.txt")

    assert len(queue) == 1


def test_distinct_paths_are_not_coalesced(clock):
    queue = _queue(clock)

    queue.submit(WriteKind.INDEX, r"C:\Docs\a.txt")
    queue.submit(WriteKind.INDEX, r"C:\Docs\b.txt")

    assert len(queue) == 2


def test_the_newest_event_for_a_path_wins(clock):
    """A file created then deleted is a removal, not an index of a missing file."""
    queue = _queue(clock)

    queue.submit(WriteKind.INDEX, r"C:\Docs\gone.txt")
    queue.submit(WriteKind.REMOVE, r"C:\Docs\gone.txt")

    assert [kind for kind, _path in _drain(queue)] == [WriteKind.REMOVE]


def test_a_recreated_file_is_indexed_rather_than_removed(clock):
    queue = _queue(clock)

    queue.submit(WriteKind.REMOVE, r"C:\Docs\back.txt")
    queue.submit(WriteKind.INDEX, r"C:\Docs\back.txt")

    assert [kind for kind, _path in _drain(queue)] == [WriteKind.INDEX]


def test_debounce_delays_a_write_without_dropping_it(clock):
    queue = _queue(clock, debounce_seconds=0.5)
    queue.submit(WriteKind.INDEX, r"C:\Docs\slow.txt")

    assert queue.pop_due(timeout=0) is None

    clock.advance(0.5)
    item = queue.pop_due(timeout=0)

    assert item is not None and item.path.name == "slow.txt"


def test_a_continuously_rewritten_file_does_not_block_the_queue(clock):
    """Its due time moves back, and so does its position — others still drain."""
    queue = _queue(clock, debounce_seconds=0.5)

    queue.submit(WriteKind.INDEX, r"C:\Docs\churning.txt")
    clock.advance(0.4)
    queue.submit(WriteKind.INDEX, r"C:\Docs\churning.txt")
    queue.submit(WriteKind.INDEX, r"C:\Docs\settled.txt")
    clock.advance(0.5)

    drained = [Path(path).name for _kind, path in _drain(queue)]
    assert drained == ["churning.txt", "settled.txt"]


# -- move and delete ordering ----------------------------------------------


def test_a_move_removes_the_source_before_indexing_the_destination(config, clock):
    queue = _queue(clock)
    handler = _WatchEventHandler(config, queue)

    handler.on_moved(FileMovedEvent(src_path=r"C:\Docs\old.md", dest_path=r"C:\Docs\new.md"))

    kinds = [(kind, Path(path).name) for kind, path in _drain(queue)]
    assert kinds == [(WriteKind.REMOVE, "old.md"), (WriteKind.INDEX, "new.md")]


def test_a_delete_after_a_move_still_removes_the_destination(config, clock):
    queue = _queue(clock)
    handler = _WatchEventHandler(config, queue)

    handler.on_moved(FileMovedEvent(src_path=r"C:\Docs\a.md", dest_path=r"C:\Docs\b.md"))
    handler.on_deleted(FileDeletedEvent(src_path=r"C:\Docs\b.md"))

    kinds = [(kind, Path(path).name) for kind, path in _drain(queue)]
    assert kinds == [(WriteKind.REMOVE, "a.md"), (WriteKind.REMOVE, "b.md")]


def test_a_deletion_is_queued_even_for_a_now_excluded_path(config, clock):
    """A path that became excluded after indexing still needs its records gone."""
    config.excluded_patterns = ["Docs"]
    queue = _queue(clock)
    handler = _WatchEventHandler(config, queue)

    handler.on_deleted(FileDeletedEvent(src_path=r"C:\Docs\stale.md"))
    handler.on_created(FileCreatedEvent(src_path=r"C:\Docs\fresh.md"))

    kinds = [(kind, Path(path).name) for kind, path in _drain(queue)]
    assert kinds == [(WriteKind.REMOVE, "stale.md")]


# -- handler filtering ------------------------------------------------------


def test_unsupported_extensions_are_never_queued(config, clock):
    queue = _queue(clock)
    handler = _WatchEventHandler(config, queue)

    handler.on_modified(FileModifiedEvent(src_path=r"C:\Docs\test.py"))
    handler.on_modified(FileModifiedEvent(src_path=r"C:\Docs\test.exe"))

    assert [Path(path).name for _kind, path in _drain(queue)] == ["test.py"]


def test_directory_events_are_ignored(config, clock):
    queue = _queue(clock)
    handler = _WatchEventHandler(config, queue)

    event = FileModifiedEvent(src_path=r"C:\Docs\subdir")
    event.is_directory = True
    handler.on_modified(event)

    assert len(queue) == 0


def test_the_handler_does_no_indexing_itself(config, clock):
    """It is a producer only: one consumer owns every write."""
    queue = _queue(clock)
    engine = MagicMock()
    handler = _WatchEventHandler(config, queue)

    handler.on_created(FileCreatedEvent(src_path=r"C:\Docs\new.md"))

    engine.index_file.assert_not_called()
    engine.remove_file.assert_not_called()


# -- the serialized writer --------------------------------------------------


def test_one_consumer_applies_queued_writes_in_order(clock):
    queue = _queue(clock)
    engine = MagicMock()
    engine.index_file.return_value = True
    writer = SerializedWriter(engine, queue)

    queue.submit(WriteKind.REMOVE, r"C:\Docs\old.md")
    queue.submit(WriteKind.INDEX, r"C:\Docs\new.md")
    while writer.process_once(timeout=0):
        pass

    assert [Path(c.args[0]).name for c in engine.remove_file.call_args_list] == ["old.md"]
    assert [Path(c.args[0]).name for c in engine.index_file.call_args_list] == ["new.md"]


def test_a_burst_of_events_never_spawns_a_thread_per_event(config, clock):
    """The old handler started one thread per event; this is what replaced it."""
    queue = _queue(clock)
    engine = MagicMock()
    engine.index_file.return_value = True
    handler = _WatchEventHandler(config, queue)
    writer = SerializedWriter(engine, queue)

    before = threading.active_count()
    for index in range(200):
        handler.on_modified(FileModifiedEvent(src_path=rf"C:\Docs\file-{index}.md"))
    after_events = threading.active_count()

    writer.start()
    try:
        deadline = threading.Event()
        deadline.wait(0.5)
        peak = threading.active_count()
    finally:
        writer.stop(timeout=5)

    # Queueing spawns nothing, and draining adds exactly one consumer.
    assert after_events == before
    assert peak <= before + 1
    assert not writer.running


def test_a_failed_write_is_retried_and_then_given_up_on(clock):
    queue = _queue(clock)
    engine = MagicMock()
    engine.index_file.side_effect = PermissionError("file is locked")
    writer = SerializedWriter(engine, queue)

    queue.submit(WriteKind.INDEX, r"C:\Docs\locked.md")
    for _ in range(6):
        clock.advance(2.0)
        writer.process_once(timeout=0)

    # Bounded retries, then the path is dropped rather than retried forever.
    assert engine.index_file.call_count == 3
    assert len(queue) == 0


def test_a_newer_event_supersedes_a_pending_retry(clock):
    queue = _queue(clock)
    engine = MagicMock()
    engine.index_file.side_effect = OSError("busy")
    writer = SerializedWriter(engine, queue)

    queue.submit(WriteKind.INDEX, r"C:\Docs\churn.md")
    writer.process_once(timeout=0)
    queue.submit(WriteKind.REMOVE, r"C:\Docs\churn.md")

    assert [kind for kind, _path in queue.snapshot()] == [WriteKind.REMOVE]


@pytest.mark.skipif(
    sys.platform != "win32", reason="INDEX_LOCK uses msvcrt (Windows only)"
)
def test_every_watcher_write_takes_the_shared_writer_lock(tmp_haydar, tmp_path, clock):
    """Watcher writes must serialize with indexing, not bypass it."""
    from contextlib import contextmanager

    from haydar.indexer.engine import IndexingEngine

    folder = tmp_path / "corpus"
    folder.mkdir()
    target = folder / "note.txt"
    target.write_text("hello world", encoding="utf-8")
    doomed = folder / "gone.txt"
    doomed.write_text("bye", encoding="utf-8")

    cfg = HaydarConfig(folders=[str(folder)])
    cfg.excluded_patterns = []
    modes = []

    with patch("haydar.indexer.engine.VectorStore") as mock_class:
        mock_class.return_value = MagicMock()
        engine = IndexingEngine(cfg, allow_download=False)
        real_open = IndexingEngine._acquire_index_lock

        @contextmanager
        def recording_lock(self, *, blocking):
            modes.append(blocking)
            with real_open(self, blocking=blocking):
                yield

        queue = _queue(clock)
        writer = SerializedWriter(engine, queue)
        try:
            with patch.object(IndexingEngine, "_acquire_index_lock", recording_lock):
                queue.submit(WriteKind.INDEX, target)
                queue.submit(WriteKind.REMOVE, doomed)
                while writer.process_once(timeout=0):
                    pass
        finally:
            engine.close()

    # Both writes took the lock, and blocking so they queue rather than vanish.
    assert modes == [True, True]


# -- watcher lifecycle ------------------------------------------------------


def test_stopping_the_watcher_joins_the_observer_and_the_writer(config, tmp_path):
    folder = tmp_path / "watched"
    folder.mkdir()
    config.folders = [str(folder)]
    engine = MagicMock()
    watcher = FileWatcher(config, engine=engine)

    watcher.start(blocking=False)
    assert watcher.writer.running
    assert watcher.folders == (str(folder),)

    watcher.stop()

    assert not watcher.writer.running
    assert watcher.folders == ()
    engine.close.assert_called_once()


def test_the_watcher_records_the_folder_snapshot_it_started_against(config, tmp_path):
    present = tmp_path / "present"
    present.mkdir()
    config.folders = [str(present), str(tmp_path / "missing")]
    watcher = FileWatcher(config, engine=MagicMock())

    try:
        watcher.start(blocking=False)
        # A configured-but-absent folder is not silently claimed as watched.
        assert watcher.folders == (str(present),)
    finally:
        watcher.stop()


def test_watcher_close_releases_engine(config):
    engine = MagicMock()
    watcher = FileWatcher(config, engine=engine)

    watcher.close()
    engine.close.assert_called_once()

    watcher.close()
    assert engine.close.call_count == 2


# -- application sequencing -------------------------------------------------


class _StubEngine:
    """An engine whose runs complete immediately."""

    def __init__(self):
        self.calls = []

    def run_job(self, **kwargs):
        from haydar.indexer.engine import IndexSnapshot, JobOutcome

        self.calls.append(kwargs)
        return IndexSnapshot(
            run_id=kwargs.get("run_id", ""),
            kind=kwargs.get("kind"),
            outcome=JobOutcome.COMPLETE,
        )

    def close(self):
        pass


def _service(config, watcher, engine=None):
    engine = engine or _StubEngine()
    return ApplicationService(
        config,
        job_coordinator=IndexJobCoordinator(config, engine_factory=lambda _c: engine),
        watcher_factory=lambda _c: watcher,
    ), engine


def _ready(**kwargs):
    options = {
        "folders": [r"C:\Docs"],
        "folders_configured": True,
        "search_ready": True,
        "initial_index_state": "complete",
    }
    options.update(kwargs)
    return HaydarConfig(**options)


def test_starting_the_watcher_schedules_a_catch_up_scan(tmp_haydar):
    """Changes during the crawl-to-watch gap are found by an incremental pass."""
    from haydar.indexer.engine import JobKind

    watcher = MagicMock()
    service, engine = _service(_ready(), watcher)

    assert service.start_watcher_if_eligible()
    service.jobs.wait_for_terminal(timeout=5)

    watcher.start.assert_called_once_with(blocking=False)
    assert [call["kind"] for call in engine.calls] == [JobKind.INCREMENTAL]


def test_the_catch_up_never_regresses_a_completed_initial_crawl(tmp_haydar):
    config = _ready()
    watcher = MagicMock()
    service, _engine = _service(config, watcher)

    service.start_watcher_if_eligible()
    service.jobs.wait_for_terminal(timeout=5)

    assert config.initial_index_state == "complete"


def test_the_observer_starts_before_the_catch_up_so_no_new_gap_opens(tmp_haydar):
    """A scan that ran first would miss anything changed while it was running."""
    order = []
    watcher = MagicMock()
    watcher.start.side_effect = lambda **_kw: order.append("observer")

    class _OrderedEngine(_StubEngine):
        def run_job(self, **kwargs):
            order.append("catch_up")
            return super().run_job(**kwargs)

    service, _engine = _service(_ready(), watcher, engine=_OrderedEngine())
    service.start_watcher_if_eligible()
    service.jobs.wait_for_terminal(timeout=5)

    assert order == ["observer", "catch_up"]


def test_queue_overflow_is_wired_to_the_same_catch_up(tmp_haydar):
    watcher = MagicMock()
    service, engine = _service(_ready(), watcher)

    service.start_watcher_if_eligible()
    service.jobs.wait_for_terminal(timeout=5)

    watcher.set_overflow_callback.assert_called_once()
    handler = watcher.set_overflow_callback.call_args.args[0]
    handler()
    service.jobs.wait_for_terminal(timeout=5)

    from haydar.indexer.engine import JobKind

    assert [call["kind"] for call in engine.calls] == [
        JobKind.INCREMENTAL,
        JobKind.INCREMENTAL,
    ]


@pytest.mark.parametrize("state", ["not_started", "running", "paused"])
def test_the_watcher_does_not_start_before_a_safe_terminal_state(tmp_haydar, state):
    watcher = MagicMock()
    service, engine = _service(_ready(initial_index_state=state), watcher)

    assert service.start_watcher_if_eligible() is False
    watcher.start.assert_not_called()
    # And no catch-up either: it would race the crawl for the writer lock.
    assert engine.calls == []


def test_a_folder_change_stops_joins_reconciles_and_restarts(tmp_haydar):
    """Order matters: the old observer must not write against the old snapshot."""
    from haydar.indexer.engine import JobKind

    config = _ready()
    watcher = MagicMock()
    order = []
    watcher.stop.side_effect = lambda: order.append("stop_watcher")

    class _OrderedEngine(_StubEngine):
        def run_job(self, **kwargs):
            order.append(f"run:{kwargs.get('kind').value}")
            return super().run_job(**kwargs)

    service, engine = _service(config, watcher, engine=_OrderedEngine())
    service.start_watcher_if_eligible()
    service.jobs.wait_for_terminal(timeout=5)
    order.clear()

    service.apply_folder_change([r"C:\Other"])
    service.jobs.wait_for_terminal(timeout=5)

    assert order[0] == "stop_watcher"
    assert order[-1] == f"run:{JobKind.INITIAL.value}"
    assert config.folders == [r"C:\Other"]
    # Completion was invalidated, so the new folder set gets a real crawl.
    assert engine.calls[-1]["kind"] is JobKind.INITIAL
    assert service.watcher_running is False


def test_the_watcher_restarts_against_the_new_folder_snapshot(tmp_haydar):
    config = _ready()
    watchers = []

    def factory(cfg):
        made = MagicMock()
        made.folders = tuple(cfg.folders)
        watchers.append(made)
        return made

    service = ApplicationService(
        config,
        job_coordinator=IndexJobCoordinator(
            config, engine_factory=lambda _c: _StubEngine()
        ),
        watcher_factory=factory,
    )

    service.start_watcher_if_eligible()
    service.jobs.wait_for_terminal(timeout=5)
    service.apply_folder_change([r"C:\Other"])
    service.jobs.wait_for_terminal(timeout=5)
    assert service.start_watcher_if_eligible()
    service.jobs.wait_for_terminal(timeout=5)

    assert len(watchers) == 2
    assert watchers[0].folders == (r"C:\Docs",)
    assert watchers[1].folders == (r"C:\Other",)

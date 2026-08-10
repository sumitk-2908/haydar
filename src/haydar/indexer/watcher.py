"""Filesystem watching with a bounded event queue and one serialized writer.

Three properties this module exists to guarantee:

* **One writer, never one thread per event.** A folder copy can produce
  thousands of events in a second. Spawning a thread each time would put an
  unbounded number of writers in contention for the same process-wide lock, so
  events land in a bounded queue that exactly one consumer thread drains.
* **Coalescing is per canonical path; ordering is causal.** Repeated writes to
  one file collapse into a single pending write, while distinct paths keep the
  order their last event arrived in — which is what preserves the
  remove-source-then-index-destination ordering of a move.
* **Every write goes through the shared writer lock.** The consumer calls the
  same engine methods the crawl uses, so watcher writes serialize behind an
  in-flight batch instead of racing it.

Losing events is a correctness problem, not a performance one, so an overflow is
reported rather than swallowed: the application layer answers it with an
incremental catch-up scan.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from haydar.config import ALL_INDEXABLE_EXTENSIONS, HaydarConfig, is_excluded
from haydar.indexer.engine import IndexingEngine

logger = logging.getLogger(__name__)

# Upper bound on pending distinct paths. Bursts beyond this are reported as an
# overflow and answered with a catch-up scan rather than growing without limit.
DEFAULT_QUEUE_CAPACITY = 2048

# A write that raises (a file still locked by the writing application is the
# common Windows case) is retried on the queue rather than by sleeping inside
# the consumer, so one stubborn file cannot stall every other pending write.
MAX_WRITE_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (0.25, 1.0)

# How long the consumer blocks waiting for work before re-checking its stop flag.
CONSUMER_POLL_SECONDS = 0.25


class WriteKind(Enum):
    """What the writer should do with a path."""

    INDEX = "index"
    REMOVE = "remove"


@dataclass
class PendingWrite:
    """One coalesced pending write for a single canonical path."""

    kind: WriteKind
    path: Path
    due: float
    attempts: int = 0


class WatchEventQueue:
    """Bounded, debounced, path-coalescing queue of pending writes.

    Debouncing is a delay rather than a drop: an event pushes its path's due
    time forward, so a file being written in many small appends is indexed once
    after it settles instead of once per chunk *and* never simply discarded.
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_QUEUE_CAPACITY,
        debounce_seconds: float = 0.5,
        time_source: Callable[[], float] = time.monotonic,
        on_overflow: Callable[[], None] | None = None,
    ) -> None:
        self.capacity = max(1, capacity)
        self.debounce_seconds = max(0.0, debounce_seconds)
        self._now = time_source
        self._on_overflow = on_overflow
        self._pending: OrderedDict[str, PendingWrite] = OrderedDict()
        self._condition = threading.Condition()
        self._closed = False
        self.dropped = 0
        self._overflow_reported = False

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def canonical(path: Path | str) -> str:
        """Return the case-folded absolute key two events must share to coalesce."""
        return os.path.normcase(os.path.abspath(str(path)))

    def set_overflow_callback(self, callback: Callable[[], None] | None) -> None:
        with self._condition:
            self._on_overflow = callback

    def __len__(self) -> int:
        with self._condition:
            return len(self._pending)

    def snapshot(self) -> list[tuple[WriteKind, str]]:
        """Return pending writes in order. For tests and diagnostics only."""
        with self._condition:
            return [(item.kind, str(item.path)) for item in self._pending.values()]

    # -- producer ----------------------------------------------------------

    def submit(
        self, kind: WriteKind, path: Path | str, *, delay: float | None = None
    ) -> None:
        """Queue a write, coalescing with any pending write for the same path.

        The newest event for a path wins outright: a file created and then
        deleted is a removal, and a file deleted and then recreated is an index.
        The entry also moves to the back of the queue, so a continuously
        rewritten file cannot hold up everything queued behind it.
        """
        key = self.canonical(path)
        absolute = Path(os.path.abspath(str(path)))
        wait = self.debounce_seconds if delay is None else max(0.0, delay)
        overflowed = False
        callback: Callable[[], None] | None = None

        with self._condition:
            if self._closed:
                return
            due = self._now() + wait
            if key in self._pending:
                self._pending.pop(key)
            elif len(self._pending) >= self.capacity:
                dropped_key, _dropped = self._pending.popitem(last=False)
                self.dropped += 1
                logger.warning(
                    "Watcher queue is full (%d); dropped a pending write for %s.",
                    self.capacity,
                    dropped_key,
                )
                if not self._overflow_reported:
                    self._overflow_reported = True
                    overflowed = True
                    callback = self._on_overflow
            self._pending[key] = PendingWrite(kind=kind, path=absolute, due=due)
            self._condition.notify()

        if overflowed and callback is not None:
            # Outside the lock: the callback schedules a catch-up scan and must
            # never be able to deadlock against a producer.
            try:
                callback()
            except Exception:
                logger.exception("Watcher overflow handler failed")

    # -- consumer ----------------------------------------------------------

    def pop_due(self, timeout: float | None = None) -> PendingWrite | None:
        """Return the next write whose debounce has elapsed, or ``None``.

        Blocks up to ``timeout`` seconds waiting for one to become due. A
        ``timeout`` of ``0`` polls without blocking, which is what lets tests
        drive the queue against a fake clock.
        """
        with self._condition:
            deadline = None if timeout is None else self._now() + timeout
            while True:
                if self._closed and not self._pending:
                    return None
                item = self._pop_due_locked()
                if item is not None:
                    if not self._pending:
                        # Drained: the next burst may report overflow again.
                        self._overflow_reported = False
                    return item
                wait = self._wait_seconds_locked(deadline)
                if wait is not None and wait <= 0:
                    return None
                self._condition.wait(wait)

    def _pop_due_locked(self) -> PendingWrite | None:
        if not self._pending:
            return None
        key = next(iter(self._pending))
        item = self._pending[key]
        if item.due > self._now():
            return None
        del self._pending[key]
        return item

    def _wait_seconds_locked(self, deadline: float | None) -> float | None:
        now = self._now()
        candidates: list[float] = []
        if deadline is not None:
            candidates.append(deadline - now)
        if self._pending:
            head = next(iter(self._pending.values()))
            candidates.append(head.due - now)
        if not candidates:
            # Nothing pending and no deadline: sleep until a producer or close()
            # wakes us.
            return None
        return max(0.0, min(candidates))

    def requeue(self, item: PendingWrite) -> None:
        """Re-queue a failed write with backoff, unless it is out of attempts."""
        if item.attempts >= MAX_WRITE_ATTEMPTS:
            logger.error(
                "Giving up on %s after %d attempts.", item.path, item.attempts
            )
            return
        index = min(item.attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)
        delay = RETRY_BACKOFF_SECONDS[max(0, index)]
        key = self.canonical(item.path)
        with self._condition:
            if self._closed or key in self._pending:
                # A newer event for this path supersedes the retry.
                return
            item.due = self._now() + delay
            self._pending[key] = item
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed


class _WatchEventHandler(FileSystemEventHandler):
    """Translate watchdog events into queued writes. Does no indexing itself."""

    def __init__(self, config: HaydarConfig, queue: WatchEventQueue) -> None:
        super().__init__()
        self.config = config
        self.queue = queue

    # -- filtering ---------------------------------------------------------

    @staticmethod
    def _is_indexable(filepath: Path) -> bool:
        return filepath.suffix.lower() in ALL_INDEXABLE_EXTENSIONS

    def _should_index(self, filepath: Path) -> bool:
        if not self._is_indexable(filepath):
            return False
        return not is_excluded(filepath, self.config.excluded_patterns)

    # -- events ------------------------------------------------------------

    def _submit_index(self, event: FileSystemEvent, attr: str = "src_path") -> None:
        path = Path(os.fsdecode(getattr(event, attr, "") or ""))
        if self._should_index(path):
            self.queue.submit(WriteKind.INDEX, path)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._submit_index(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._submit_index(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(os.fsdecode(event.src_path))
        # Exclusions are not applied to removals: a path that became excluded
        # after it was indexed still needs its records taken out.
        if self._is_indexable(path):
            self.queue.submit(WriteKind.REMOVE, path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        source = Path(os.fsdecode(event.src_path))
        # Submitted first so the queue's insertion order preserves the causal
        # ordering of the move: the old path is removed before the new one is
        # indexed.
        if self._is_indexable(source):
            self.queue.submit(WriteKind.REMOVE, source)
        self._submit_index(event, "dest_path")


class SerializedWriter:
    """The single consumer thread that applies queued writes.

    It calls the same engine methods the crawl uses, so each write takes the
    process-wide writer lock for its own critical section and no watcher write
    can overlap an indexing batch.
    """

    def __init__(
        self,
        engine: Any,
        queue: WatchEventQueue,
        *,
        poll_seconds: float = CONSUMER_POLL_SECONDS,
    ) -> None:
        self.engine = engine
        self.queue = queue
        self.poll_seconds = poll_seconds
        self.processed = 0
        self.failed = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="haydar-watch-writer", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the consumer and join it, so no write outlives the watcher."""
        self._stop.set()
        self.queue.close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("Watcher writer did not stop within %.1fs.", timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- work --------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self.process_once(timeout=self.poll_seconds)

    def process_once(self, timeout: float | None = 0.0) -> bool:
        """Apply at most one due write. Returns whether any work was done."""
        item = self.queue.pop_due(timeout=timeout)
        if item is None:
            return False
        item.attempts += 1
        try:
            self._apply(item)
        except Exception as exc:
            self.failed += 1
            logger.debug(
                "Watcher write failed for %s (attempt %d): %s",
                item.path,
                item.attempts,
                exc,
            )
            self.queue.requeue(item)
            return True
        self.processed += 1
        return True

    def _apply(self, item: PendingWrite) -> None:
        if item.kind is WriteKind.REMOVE:
            self.engine.remove_file(item.path)
            logger.info("Removed: %s", item.path.name)
            return
        if self.engine.index_file(item.path):
            logger.info("Indexed: %s", item.path.name)


class FileWatcher:
    """Own one observer, its event queue, and its single writer thread."""

    def __init__(
        self,
        config: HaydarConfig,
        *,
        engine: Any = None,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        on_overflow: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.engine = engine if engine is not None else IndexingEngine(config)
        self.queue = WatchEventQueue(
            capacity=queue_capacity,
            debounce_seconds=config.watcher_debounce_seconds,
            on_overflow=on_overflow,
        )
        self.writer = SerializedWriter(self.engine, self.queue)
        # The folder set this observer was started against. A change to it
        # requires a stop/reconcile/restart rather than an in-place edit.
        self.folders: tuple[str, ...] = ()
        self._observer: Any = None

    def set_overflow_callback(self, callback: Callable[[], None] | None) -> None:
        """Register what to do when a burst outruns the queue.

        The application answers this with an incremental catch-up scan, which is
        the only way a dropped event is not a permanently missed file.
        """
        self.queue.set_overflow_callback(callback)

    def start(self, blocking: bool = True) -> None:
        """Start the observer and its writer against the current folder snapshot."""
        if not self.config.folders:
            logger.warning("No folders configured to watch.")
            return

        observer = Observer()
        handler = _WatchEventHandler(self.config, self.queue)

        watched: list[str] = []
        for folder in self.config.folders:
            folder_path = Path(folder)
            if folder_path.exists() and folder_path.is_dir():
                observer.schedule(handler, str(folder_path), recursive=True)
                watched.append(folder)
                logger.info(f"Watching folder: {folder}")
            else:
                logger.warning(f"Configured folder does not exist or is not a directory: {folder}")

        self.folders = tuple(watched)
        self.writer.start()
        observer.start()
        self._observer = observer
        logger.info("File watcher started.")

        if not blocking:
            return

        logger.info("Press Ctrl+C to stop.")
        try:
            while observer.is_alive():
                observer.join(timeout=1)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received. Stopping watcher...")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop and join the observer, drain-stop the writer, release the engine."""
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join()
        # Ordered after the observer so no event can be queued once the writer
        # has been told to stop.
        self.writer.stop()
        self.folders = ()
        logger.info("Watcher stopped.")
        self.close()

    def close(self) -> None:
        """Release resources."""
        if hasattr(self, 'engine') and hasattr(self.engine, 'close'):
            self.engine.close()


def install_autostart() -> None:
    """Create a .bat file in the Windows Startup folder to run the watcher on login."""
    startup_dir = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_dir.mkdir(parents=True, exist_ok=True)

    bat_path = startup_dir / "haydar_watcher.bat"

    import sys
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable)
        if exe_path.name.lower() == "haydar.exe":
            cli_path = exe_path.with_name("haydar-cli.exe")
            command = f'"{cli_path if cli_path.exists() else exe_path}" watch'
        else:
            command = f'"{exe_path}" watch'
    else:
        command = "pythonw -m haydar watch"

    content = f"@echo off\n{command}\n"
    bat_path.write_text(content, encoding="utf-8")

    logger.info(f"Autostart script installed successfully at: {bat_path}")

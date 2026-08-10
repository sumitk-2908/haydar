"""GUI-neutral job coordination for indexing runs.

This is the layer the Qt controller and the CLI both drive. It owns the worker
thread, translates run outcomes into persisted lifecycle transitions, and hands
subscribers immutable snapshots. Nothing here imports Qt, so the same
orchestration is exercised by tests and by ``haydar-cli.exe``.

The UI is deliberately kept out of the business of deciding what a run *meant*:
it renders whatever snapshot it is given, and this class decides whether a stop
was a pause, a cancellation, or a failure.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import replace

from haydar.config import IMAGE_EXTENSIONS, HaydarConfig
from haydar.indexer.engine import (
    IndexingEngine,
    IndexSnapshot,
    JobControl,
    JobKind,
    JobOutcome,
    JobPhase,
)
from haydar.lifecycle import IndexLifecycle

logger = logging.getLogger(__name__)

# How long shutdown waits for a cooperative pause before giving up and letting
# next-launch recovery handle it.
SHUTDOWN_GRACE_SECONDS = 10.0

IndexEventCallback = Callable[[IndexSnapshot], None]
EngineFactory = Callable[[HaydarConfig], IndexingEngine]


class Subscription:
    """A cancellable registration for job snapshots."""

    def __init__(self, unsubscribe: Callable[[], None]) -> None:
        self._unsubscribe = unsubscribe
        self._active = True

    def cancel(self) -> None:
        if self._active:
            self._active = False
            self._unsubscribe()


class IndexJobCoordinator:
    """Own the lifecycle of indexing runs for one configuration.

    Duplicate starts are idempotent while a run is active. Pause and cancel are
    *requests*: the state only changes once the worker acknowledges by reaching
    a terminal snapshot, so the UI never shows a state the data has not reached.
    """

    def __init__(
        self,
        config: HaydarConfig,
        *,
        engine_factory: EngineFactory | None = None,
        lifecycle: IndexLifecycle | None = None,
    ) -> None:
        self.config = config
        self.lifecycle = lifecycle or IndexLifecycle(config)
        self._engine_factory = engine_factory or (
            lambda cfg: IndexingEngine(cfg, allow_download=False)
        )
        self._subscribers: list[IndexEventCallback] = []
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._control = JobControl()
        self._snapshot = IndexSnapshot(
            phase=JobPhase.STARTING,
            outcome=None,
        )
        self._run_id: str | None = None
        self._terminal = threading.Event()
        self._terminal.set()
        self._retried_this_launch = False

    # -- subscription ------------------------------------------------------

    def subscribe(self, callback: IndexEventCallback) -> Subscription:
        with self._lock:
            self._subscribers.append(callback)

        def remove() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return Subscription(remove)

    def snapshot(self) -> IndexSnapshot:
        """Return a copy of the latest snapshot, safe to read from any thread."""
        with self._lock:
            return replace(self._snapshot)

    def _publish(self, snapshot: IndexSnapshot) -> None:
        with self._lock:
            self._snapshot = replace(snapshot)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(self.snapshot())
            except Exception:
                # A misbehaving subscriber must not take down indexing.
                logger.exception("Index snapshot subscriber failed")

    # -- state -------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_watcher_eligible(self) -> bool:
        """Whether the watcher may start: safe terminal state *and* no live writer.

        Both halves matter. A persisted terminal state with a worker still
        finishing its final flush would mean the watcher races the crawl for the
        writer lock.
        """
        return self.lifecycle.is_watcher_eligible and not self.is_running

    # -- commands ----------------------------------------------------------

    def start_initial(self, *, force: bool = False) -> str:
        """Start or resume the initial crawl; returns a stable run id."""
        return self._start(
            kind=JobKind.REBUILD if force else JobKind.INITIAL, force=force
        )

    def resume(self) -> str:
        """Explicitly resume a paused or cancelled crawl."""
        return self._start(kind=JobKind.INITIAL, force=False)

    def start_incremental(self) -> str:
        """Run a catch-up pass without touching initial-crawl completion."""
        return self._start(kind=JobKind.INCREMENTAL, force=False)

    def start_ocr_backfill(self, ocr_version: str) -> str:
        """Index images that were deferred or processed by an older OCR engine.

        This is a separate job kind on purpose: it must never regress an
        already-complete initial crawl, and it touches only image records.
        """
        return self._start(
            kind=JobKind.OCR_BACKFILL,
            force=False,
            only_extensions=frozenset(IMAGE_EXTENSIONS),
            ocr_version=ocr_version,
        )

    def _start(
        self,
        *,
        kind: JobKind,
        force: bool,
        only_extensions: frozenset[str] | None = None,
        ocr_version: str = "",
    ) -> str:
        with self._lock:
            if self.is_running:
                # Idempotent: a second click returns the run already in flight.
                return self._run_id or ""

            run_id = secrets.token_hex(4)
            self._run_id = run_id
            self._control = JobControl()
            self._terminal.clear()

            # Only a full crawl owns the persisted initial-index lifecycle.
            # Incremental and OCR jobs must not regress a completed crawl.
            if kind in (JobKind.INITIAL, JobKind.REBUILD):
                if self.lifecycle.state == "running":
                    self.lifecycle.recover_interrupted_run()
                if force and self.lifecycle.state == "complete":
                    self.lifecycle.transition("running")
                elif self.lifecycle.state != "running":
                    if self.lifecycle.state == "complete":
                        self.lifecycle.invalidate_initial_completion()
                    self.lifecycle.transition("running")

            thread = threading.Thread(
                target=self._run,
                args=(run_id, kind, force, only_extensions, ocr_version),
                name=f"haydar-index-{run_id}",
                daemon=True,
            )
            self._thread = thread
        thread.start()
        return run_id

    def pause(self) -> None:
        """Request a pause. The state changes only once the worker acknowledges."""
        self._control.request_pause()

    def cancel(self) -> None:
        """Request cancellation, forfeiting deletion reconciliation for this run."""
        self._control.request_cancel()

    def wait_for_terminal(self, timeout: float | None = None) -> bool:
        """Block until the current run reaches a terminal state."""
        return self._terminal.wait(timeout)

    def shutdown(self, timeout: float = SHUTDOWN_GRACE_SECONDS) -> bool:
        """Cooperatively pause and wait for the writer lock to be released.

        On timeout the persisted state stays ``running``; the next launch
        recovers it to ``paused`` and resumes safely, which is why leaving it is
        correct rather than forcing a state the data may not have reached.
        """
        if not self.is_running:
            return True
        self.pause()
        acknowledged = self._terminal.wait(timeout)
        if not acknowledged:
            logger.warning(
                "Index worker did not acknowledge pause within %.1fs; "
                "next launch will recover it.",
                timeout,
            )
        return acknowledged

    # -- worker ------------------------------------------------------------

    def _run(
        self,
        run_id: str,
        kind: JobKind,
        force: bool,
        only_extensions: frozenset[str] | None,
        ocr_version: str,
    ) -> None:
        engine = None
        try:
            engine = self._engine_factory(self.config)
            snapshot = engine.run_job(
                kind=kind,
                force=force,
                control=self._control,
                on_snapshot=self._publish,
                on_committed=self._publish,
                only_extensions=only_extensions,
                ocr_version=ocr_version,
                run_id=run_id,
            )
            self._apply_outcome(kind, snapshot)
        except Exception as exc:
            logger.exception("Index job %s crashed", run_id)
            failed = replace(
                self.snapshot(),
                run_id=run_id,
                kind=kind,
                phase=JobPhase.FAILED,
                outcome=JobOutcome.FAILED,
                error_message=str(exc),
            )
            self._apply_outcome(kind, failed)
        finally:
            if engine is not None:
                engine.close()
            with self._lock:
                self._thread = None
            # Set only after the engine is closed and its lock released, so a
            # watcher waiting on this signal cannot race the final flush.
            self._terminal.set()

    def _apply_outcome(self, kind: JobKind, snapshot: IndexSnapshot) -> None:
        """Translate a run outcome into a persisted lifecycle transition."""
        if kind in (JobKind.INITIAL, JobKind.REBUILD):
            try:
                if snapshot.outcome is JobOutcome.COMPLETE:
                    self.lifecycle.transition("complete")
                elif snapshot.outcome is JobOutcome.PAUSED:
                    self.lifecycle.transition(
                        "paused",
                        pause_reason="user" if self._control.pause_requested else "interrupted",
                    )
                elif snapshot.outcome is JobOutcome.CANCELLED:
                    self.lifecycle.transition("cancelled")
                elif snapshot.outcome is JobOutcome.FAILED:
                    self.lifecycle.transition("failed", error=snapshot.error_message)
            except Exception:
                logger.exception("Could not persist the index lifecycle transition")
        self._publish(snapshot)

    # -- launch policy -----------------------------------------------------

    def autostart_if_due(self) -> str | None:
        """Start the initial crawl at launch when the persisted state calls for it.

        Encodes the launch policy in one place: ``not_started`` and an
        interrupted pause start immediately, a failure retries once per launch,
        and an explicit cancellation or user pause waits to be asked.
        """
        if self.is_running:
            return None

        if self.lifecycle.state == "running":
            self.lifecycle.recover_interrupted_run()

        state = self.lifecycle.state
        if state == "failed":
            if self._retried_this_launch:
                return None
            self._retried_this_launch = True
            logger.info("Retrying the previously failed initial index once.")
            return self.start_initial()

        if self.lifecycle.should_autostart_initial_index:
            return self.start_initial()
        return None

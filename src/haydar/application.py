"""GUI-neutral product orchestration.

This module answers the sequencing questions the product contract cares about —
when search may open, when the initial crawl starts, when it is safe to start
the watcher — without any Qt involvement. The Qt controller and the CLI are both
thin adapters over it, which is what keeps the frontend replaceable and lets the
sequencing be tested without a display.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexSnapshot
from haydar.indexer.jobs import IndexJobCoordinator
from haydar.lifecycle import IndexLifecycle
from haydar.setup import SetupCoordinator, SetupEvent

logger = logging.getLogger(__name__)

SetupEventCallback = Callable[[SetupEvent], None]
SnapshotCallback = Callable[[IndexSnapshot], None]
ReadyCallback = Callable[[HaydarConfig], None]


@dataclass
class WatcherHandle:
    """A started watcher plus the folder snapshot it was started against."""

    watcher: object
    folders: tuple[str, ...]


def default_watcher_factory(config: HaydarConfig):
    """Build the real file watcher.

    Lives here rather than in the Qt layer so ``ui/`` never imports the indexer
    package; the import is local so this module stays dependency-light.
    """
    from haydar.indexer.watcher import FileWatcher

    return FileWatcher(config)


class ApplicationService:
    """Coordinate setup, indexing, and watcher timing for one configuration."""

    def __init__(
        self,
        config: HaydarConfig,
        *,
        setup_coordinator: SetupCoordinator | None = None,
        job_coordinator: IndexJobCoordinator | None = None,
        watcher_factory: Callable[[HaydarConfig], object] | None = None,
    ) -> None:
        self.config = config
        self.lifecycle = IndexLifecycle(config)
        self.setup = setup_coordinator or SetupCoordinator(config)
        self.jobs = job_coordinator or IndexJobCoordinator(
            config, lifecycle=self.lifecycle
        )
        # ``None`` disables watching outright (headless callers and tests);
        # real callers pass ``default_watcher_factory``.
        self._watcher_factory = watcher_factory
        self._watcher: WatcherHandle | None = None
        self._watcher_lock = threading.RLock()

    # -- readiness ---------------------------------------------------------

    @property
    def needs_onboarding(self) -> bool:
        """Whether the user must be shown setup before search can open.

        A migrated legacy install answers ``False`` here, so an existing user is
        never sent back through onboarding on upgrade.
        """
        return not self.config.search_ready

    def prepare_search(
        self,
        *,
        progress_callback: SetupEventCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> HaydarConfig:
        """Provision search prerequisites. Returns once readiness is persisted."""
        return self.setup.prepare_search(
            progress_callback=progress_callback, cancel_event=cancel_event
        )

    # -- indexing ----------------------------------------------------------

    def start_initial_index_if_due(self) -> str | None:
        """Apply the launch policy for the initial crawl."""
        if not self.config.folders:
            logger.info("No folders configured; skipping the initial index.")
            return None
        return self.jobs.autostart_if_due()

    def pause_index(self) -> None:
        self.jobs.pause()

    def cancel_index(self) -> None:
        self.jobs.cancel()

    def resume_index(self) -> str:
        return self.jobs.resume()

    def retry_index(self) -> str:
        return self.jobs.start_initial()

    def rebuild_index(self) -> str:
        """Start an explicit full refresh, which is allowed even from complete."""
        return self.jobs.start_initial(force=True)

    def snapshot(self) -> IndexSnapshot:
        return self.jobs.snapshot()

    def subscribe(self, callback: SnapshotCallback):
        return self.jobs.subscribe(callback)

    # -- watcher -----------------------------------------------------------

    @property
    def watcher_running(self) -> bool:
        return self._watcher is not None

    def start_watcher_if_eligible(self) -> bool:
        """Start the watcher only after a safe terminal crawl state.

        Two conditions must both hold: the persisted lifecycle is in a safe
        terminal state, and no index worker is still holding the writer lock.
        Starting earlier would let watcher writes race the crawl.

        A short incremental catch-up runs after the observer is live so changes
        made during the crawl-to-watch gap are not lost. It is started *after*
        scheduling the observer on purpose: a scan first would reopen the same
        gap between the scan finishing and the observer starting.
        """
        if self._watcher_factory is None:
            # Explicitly disabled (tests, headless callers).
            return False
        with self._watcher_lock:
            if self._watcher is not None:
                return False
            if not self.jobs.is_watcher_eligible:
                logger.debug(
                    "Watcher not started: state=%s running=%s",
                    self.lifecycle.state,
                    self.jobs.is_running,
                )
                return False
            if not self.config.folders:
                return False

            watcher = self._watcher_factory(self.config)
            # A burst that outruns the bounded queue drops writes, so the
            # watcher answers overflow with the same catch-up scan.
            register = getattr(watcher, "set_overflow_callback", None)
            if callable(register):
                register(self.request_catch_up)
            start = getattr(watcher, "start", None)
            if callable(start):
                start(blocking=False)
            self._watcher = WatcherHandle(
                watcher=watcher, folders=tuple(self.config.folders)
            )
            logger.info("Watcher started for %d folder(s).", len(self.config.folders))

        self.request_catch_up()
        return True

    def request_catch_up(self) -> str | None:
        """Run an incremental pass to close a gap the watcher could not observe.

        Used both for the crawl-to-watch handoff and for queue overflow. It is
        an *incremental* job by design: it must not regress a completed initial
        crawl, and a cancelled or paused crawl must not gain deletion
        reconciliation it never earned.
        """
        if not self.config.folders:
            return None
        try:
            return self.jobs.start_incremental()
        except Exception:
            logger.exception("Could not start the watcher catch-up scan")
            return None

    def stop_watcher(self) -> None:
        with self._watcher_lock:
            handle, self._watcher = self._watcher, None
        if handle is None:
            return
        stop = getattr(handle.watcher, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                logger.exception("Could not stop the file watcher cleanly")

    def apply_folder_change(self, folders: list[str]) -> None:
        """Persist a new folder set and schedule the work it implies.

        Order matters: the old observer is stopped and joined first so it cannot
        write against the previous folder snapshot, then completion is
        invalidated, then a fresh crawl starts. The watcher restarts on its own
        once that crawl reaches a safe terminal state.
        """
        self.stop_watcher()
        self.jobs.cancel()
        self.jobs.wait_for_terminal(timeout=30)

        self.lifecycle.mark_folders_configured(folders)
        self.lifecycle.invalidate_initial_completion()
        self.jobs.start_initial()

    # -- shutdown ----------------------------------------------------------

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop the watcher, cooperatively pause indexing, and release stores."""
        self.stop_watcher()
        self.jobs.shutdown(timeout=timeout)

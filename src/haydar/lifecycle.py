"""Explicit, auditable transitions for the persisted first-run lifecycle.

Every change to ``search_ready`` and ``initial_index_state`` goes through this
module. The point is that a transition is a recorded fact, not a value some
caller happened to assign: an invalid transition raises instead of quietly
corrupting the persisted state, and recovery from an interrupted run is an
observable operation rather than a side effect of loading a dataclass.

This module deliberately has no Qt and no indexer dependency, so the GUI, the
CLI, and tests all drive the same state machine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from haydar.config import (
    INITIAL_INDEX_STATES,
    SAFE_TERMINAL_INDEX_STATES,
    HaydarConfig,
)

logger = logging.getLogger(__name__)

# Allowed initial-index transitions. A pair absent from this map is a bug in the
# caller, not a state to be tolerated.
_ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("not_started", "running"),
    ("running", "paused"),
    ("paused", "running"),
    ("running", "cancelled"),
    ("cancelled", "running"),
    ("running", "failed"),
    ("failed", "running"),
    ("running", "complete"),
    # A configured-corpus change invalidates completion, then restarts normally.
    ("complete", "not_started"),
    # An explicit rebuild/full refresh may start immediately from complete.
    ("complete", "running"),
    # A folder change while stopped invalidates completion from any state.
    ("paused", "not_started"),
    ("cancelled", "not_started"),
    ("failed", "not_started"),
})


class LifecycleTransitionError(Exception):
    """An attempt to move the lifecycle through a transition that is not defined."""


class IndexLifecycle:
    """Apply and persist first-run lifecycle transitions for one config object.

    ``save`` is injected so callers can batch or intercept persistence; by
    default each transition is atomically written through ``HaydarConfig.save``.
    """

    def __init__(
        self,
        config: HaydarConfig,
        *,
        save: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self._save = save if save is not None else config.save

    # -- readiness ---------------------------------------------------------

    def mark_search_ready(self) -> None:
        """Record that every search prerequisite was verified, then persist.

        The event that unblocks the search window must only be emitted after this
        returns: readiness the user cannot rely on across a restart is not
        readiness.
        """
        self.config.search_ready = True
        self.config.initial_index_error = ""
        self._save()
        logger.info("Search readiness persisted.")

    def clear_search_ready(self, reason: str) -> None:
        """Record that a verified prerequisite or schema check has failed."""
        self.config.search_ready = False
        self.config.initial_index_error = reason[:500]
        self._save()
        logger.warning("Search readiness cleared: %s", reason)

    def mark_folders_configured(self, folders: Iterable[str]) -> None:
        """Persist the user's folder selection as explicit intent.

        Assigning the list here (rather than mutating ``config.folders`` in place
        at the call site) keeps "folders were chosen" and "which folders" a single
        atomic fact.
        """
        self.config.folders = list(folders)
        self.config.folders_configured = bool(self.config.folders)
        self._save()

    # -- initial index -----------------------------------------------------

    @property
    def state(self) -> str:
        return self.config.initial_index_state

    def can_transition(self, target: str) -> bool:
        return (self.state, target) in _ALLOWED_TRANSITIONS

    def transition(
        self,
        target: str,
        *,
        error: str | None = None,
        pause_reason: str | None = None,
    ) -> None:
        """Move the initial-index lifecycle to ``target`` and persist it."""
        if target not in INITIAL_INDEX_STATES:
            raise LifecycleTransitionError(f"Unknown initial index state: {target!r}")

        current = self.state
        if current == target:
            # Idempotent: a duplicate acknowledgement is not an error, and must
            # not be treated as a second transition.
            return
        if (current, target) not in _ALLOWED_TRANSITIONS:
            raise LifecycleTransitionError(
                f"Invalid initial-index transition {current!r} -> {target!r}"
            )

        self.config.initial_index_state = target
        if target == "failed":
            self.config.initial_index_error = (error or "")[:500]
        elif target in ("running", "complete", "not_started"):
            self.config.initial_index_error = ""

        if target == "paused":
            self.config.initial_index_pause_reason = pause_reason or "user"
        else:
            self.config.initial_index_pause_reason = ""

        self._save()
        logger.info("Initial index state: %s -> %s", current, target)

    def recover_interrupted_run(self) -> bool:
        """Convert a persisted ``running`` state into an auto-resumable pause.

        A process cannot still own a crawl once a new process has loaded its
        config, so persisted ``running`` means the previous run was interrupted.
        Recording that as ``paused`` with reason ``interrupted`` distinguishes it
        from a pause the user asked for, which must not silently auto-resume.

        Returns ``True`` when a recovery actually happened.
        """
        if self.state != "running":
            return False
        logger.info("Recovering an interrupted initial index run.")
        self.transition("paused", pause_reason="interrupted")
        return True

    def invalidate_initial_completion(self) -> bool:
        """Require a fresh crawl after the configured corpus changed.

        Returns ``True`` when the state actually moved to ``not_started``.
        """
        if self.state in ("not_started", "running"):
            return False
        self.transition("not_started")
        return True

    # -- derived questions -------------------------------------------------

    @property
    def is_watcher_eligible(self) -> bool:
        """Whether the initial crawl has reached a safe terminal state.

        This answers only the persisted-state half of watcher eligibility. The
        caller must additionally confirm the index worker has stopped and
        released the writer lock before starting an observer.
        """
        return self.state in SAFE_TERMINAL_INDEX_STATES

    @property
    def should_autostart_initial_index(self) -> bool:
        """Whether launch should start or resume the initial crawl by itself.

        ``cancelled`` is excluded on purpose: the user stopped it, so restarting
        without being asked would override an explicit decision. A user-requested
        pause is likewise preserved, while an interrupted run resumes.
        """
        if self.state == "not_started":
            return True
        if self.state == "paused":
            return self.config.initial_index_pause_reason != "user"
        return False

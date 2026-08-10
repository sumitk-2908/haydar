"""Contract tests for the persisted first-run lifecycle state machine."""

import pytest

from haydar.config import HaydarConfig
from haydar.lifecycle import IndexLifecycle, LifecycleTransitionError


def _lifecycle(**kwargs):
    """Build a lifecycle over an in-memory config, recording every persisted state."""
    config = HaydarConfig(folders=[r"C:\Docs"], **kwargs)
    saved: list[tuple[str, bool]] = []
    lifecycle = IndexLifecycle(
        config,
        save=lambda: saved.append((config.initial_index_state, config.search_ready)),
    )
    return config, lifecycle, saved


def test_recovery_from_running_is_observable_and_persisted():
    config, lifecycle, saved = _lifecycle(initial_index_state="running")

    assert lifecycle.recover_interrupted_run() is True
    assert config.initial_index_state == "paused"
    assert config.initial_index_pause_reason == "interrupted"
    assert saved == [("paused", False)]


def test_recovery_is_a_no_op_outside_running():
    config, lifecycle, saved = _lifecycle(initial_index_state="complete")

    assert lifecycle.recover_interrupted_run() is False
    assert config.initial_index_state == "complete"
    assert saved == []


def test_interrupted_pause_auto_resumes_but_user_pause_does_not():
    _, interrupted, _ = _lifecycle(initial_index_state="running")
    interrupted.recover_interrupted_run()
    assert interrupted.should_autostart_initial_index is True

    _, user_paused, _ = _lifecycle(initial_index_state="running")
    user_paused.transition("paused", pause_reason="user")
    assert user_paused.should_autostart_initial_index is False


def test_cancelled_requires_explicit_resume():
    _, lifecycle, _ = _lifecycle(initial_index_state="running")
    lifecycle.transition("cancelled")

    assert lifecycle.should_autostart_initial_index is False
    # Resume remains available as an explicit user command.
    assert lifecycle.can_transition("running") is True


def test_not_started_and_fresh_launch_start_immediately():
    _, lifecycle, _ = _lifecycle(initial_index_state="not_started")

    assert lifecycle.should_autostart_initial_index is True


def test_complete_does_not_rerun_the_initial_crawl():
    _, lifecycle, _ = _lifecycle(initial_index_state="complete")

    assert lifecycle.should_autostart_initial_index is False


@pytest.mark.parametrize(
    "state, eligible",
    [
        ("not_started", False),
        ("running", False),
        ("paused", False),
        ("cancelled", True),
        ("failed", True),
        ("complete", True),
    ],
)
def test_watcher_eligibility_only_after_a_safe_terminal_state(state, eligible):
    _, lifecycle, _ = _lifecycle(initial_index_state=state)

    assert lifecycle.is_watcher_eligible is eligible


@pytest.mark.parametrize(
    "start, target",
    [
        ("not_started", "running"),
        ("running", "paused"),
        ("paused", "running"),
        ("running", "cancelled"),
        ("cancelled", "running"),
        ("running", "failed"),
        ("failed", "running"),
        ("running", "complete"),
        ("complete", "not_started"),
        ("complete", "running"),
    ],
)
def test_documented_transitions_are_allowed(start, target):
    config, lifecycle, _ = _lifecycle(initial_index_state=start)

    lifecycle.transition(target)

    assert config.initial_index_state == target


@pytest.mark.parametrize(
    "start, target",
    [
        ("not_started", "complete"),
        ("not_started", "paused"),
        ("paused", "complete"),
        ("cancelled", "complete"),
        ("complete", "paused"),
    ],
)
def test_undocumented_transitions_are_rejected(start, target):
    config, lifecycle, _ = _lifecycle(initial_index_state=start)

    with pytest.raises(LifecycleTransitionError):
        lifecycle.transition(target)

    assert config.initial_index_state == start


def test_repeating_the_current_state_is_idempotent():
    config, lifecycle, saved = _lifecycle(initial_index_state="running")

    lifecycle.transition("running")

    assert config.initial_index_state == "running"
    assert saved == []


def test_failure_records_a_bounded_reason_and_running_clears_it():
    config, lifecycle, _ = _lifecycle(initial_index_state="running")

    lifecycle.transition("failed", error="x" * 900)
    assert len(config.initial_index_error) == 500

    lifecycle.transition("running")
    assert config.initial_index_error == ""


def test_search_ready_is_persisted_before_it_is_announced():
    config, lifecycle, saved = _lifecycle()

    lifecycle.mark_search_ready()

    assert config.search_ready is True
    assert saved == [("not_started", True)]


def test_clearing_readiness_requires_a_recorded_reason():
    config, lifecycle, _ = _lifecycle(search_ready=True)

    lifecycle.clear_search_ready("schema mismatch")

    assert config.search_ready is False
    assert config.initial_index_error == "schema mismatch"


def test_folder_selection_is_persisted_as_explicit_intent():
    config, lifecycle, saved = _lifecycle()

    lifecycle.mark_folders_configured([r"C:\A", r"D:\B"])

    assert config.folders == [r"C:\A", r"D:\B"]
    assert config.folders_configured is True
    assert len(saved) == 1


def test_empty_folder_selection_is_not_treated_as_configured():
    config, lifecycle, _ = _lifecycle()

    lifecycle.mark_folders_configured([])

    assert config.folders == []
    assert config.folders_configured is False


def test_corpus_change_invalidates_completion_before_a_new_crawl():
    config, lifecycle, _ = _lifecycle(initial_index_state="complete")

    assert lifecycle.invalidate_initial_completion() is True
    assert config.initial_index_state == "not_started"
    # And a fresh crawl then starts through the normal transition.
    lifecycle.transition("running")
    assert config.initial_index_state == "running"


def test_invalidation_does_not_disturb_an_active_run():
    config, lifecycle, _ = _lifecycle(initial_index_state="running")

    assert lifecycle.invalidate_initial_completion() is False
    assert config.initial_index_state == "running"

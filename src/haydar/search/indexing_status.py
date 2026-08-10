"""Interface layer for indexing status consumed by the UI.

The architecture rule in ``.agents/AGENTS.md`` is that ``src/haydar/ui/`` may
import only from ``search/`` or ``config.py``, never from ``indexer/`` internals.
The UI still needs to *render* index state, so the read-only view types are
re-exported here and the widgets depend on this module instead.

Only value types and enums cross this boundary. The engine, coordinator, and
cache stay behind it, so the backend remains replaceable without touching Qt.
"""

from __future__ import annotations

from haydar.indexer.engine import (
    IndexSnapshot,
    JobKind,
    JobOutcome,
    JobPhase,
)

__all__ = ["IndexSnapshot", "JobKind", "JobOutcome", "JobPhase", "describe"]


# User-facing copy for each state. Kept here rather than in the widget so the
# CLI and the GUI describe the same state identically.
_MESSAGES = {
    "running": "Indexing in background. Search includes committed files.",
    "pausing": "Pausing after current batch…",
    "paused": "Indexing paused. Indexed files remain searchable.",
    "cancelled": "Initial indexing cancelled. Search covers indexed files only.",
    "failed": "Indexing stopped. Search covers indexed files only.",
    "resumed": "Resuming indexing. Already indexed files were kept.",
    "complete": "All configured folders indexed.",
    "ocr_backfill": "Adding image text to search…",
}


def describe(snapshot: IndexSnapshot, *, resumed: bool = False) -> str:
    """Return the status message for a snapshot, including honest counts.

    During discovery the message carries a discovered count rather than a
    percentage, because the denominator is still moving.
    """
    if snapshot.outcome is JobOutcome.COMPLETE:
        return _MESSAGES["complete"]
    if snapshot.outcome is JobOutcome.CANCELLED:
        return _MESSAGES["cancelled"]
    if snapshot.outcome is JobOutcome.FAILED:
        reason = snapshot.error_message.strip()
        base = _MESSAGES["failed"]
        return f"{base} {reason[:120]}" if reason else base
    if snapshot.outcome is JobOutcome.PAUSED:
        return _MESSAGES["paused"]

    if snapshot.phase is JobPhase.PAUSING:
        return _MESSAGES["pausing"]
    if snapshot.phase is JobPhase.CANCELLING:
        return "Cancelling after current batch…"

    if snapshot.kind is JobKind.OCR_BACKFILL:
        return f"{_MESSAGES['ocr_backfill']} {snapshot.committed_files} image(s) added."

    base = _MESSAGES["resumed"] if resumed else _MESSAGES["running"]
    if not snapshot.discovery_complete:
        return f"{base} Found {snapshot.discovered} file(s) so far."
    done = snapshot.examined + snapshot.skipped_unchanged
    total = snapshot.discovered + snapshot.skipped_unchanged
    return f"{base} {done} of {total} file(s)."


def deferred_message(count: int) -> str:
    """Return the OCR-deferred notice for ``count`` images."""
    return f"{count} image{'s' if count != 1 else ''} are waiting for OCR."

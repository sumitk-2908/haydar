"""Launch-time index freshness estimate, exposed through the search layer.

The UI needs this number but may not import ``indexer`` internals, so the
estimate is surfaced here. The work itself still belongs to the indexer, which
this module calls into on the UI's behalf.
"""

from __future__ import annotations

from haydar.config import HaydarConfig


def estimate_unindexed_count(
    folders: list[str], config: HaydarConfig, cap: int = 10_000
) -> int:
    """Return a bounded count of files newer than the newest indexed file.

    Deliberately an estimate: the crawl is capped so this stays cheap at launch
    on large folders. Returns 0 when nothing is indexed yet, since there is no
    baseline to be stale against.
    """
    from haydar.indexer.engine import IndexingEngine

    return IndexingEngine.estimate_unindexed_count(folders, config, cap=cap)

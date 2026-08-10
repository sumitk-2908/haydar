"""Tests for structured logging correlation identifiers."""

import logging
import re
from unittest.mock import MagicMock

from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexingEngine
from haydar.logging_setup import _RunIdFilter


def test_run_id_filter_adds_placeholder() -> None:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="message", args=(), exc_info=None,
    )

    assert _RunIdFilter().filter(record) is True
    assert record.run_id == "--------"


def test_run_id_present_in_index_log(tmp_haydar, caplog) -> None:
    config = HaydarConfig(folders=[], initialized=True)
    engine = IndexingEngine(config, allow_download=False)
    engine._store = MagicMock()

    with caplog.at_level(logging.INFO, logger="haydar.indexer.engine"):
        engine.index_all()

    records = [
        record for record in caplog.records
        if record.name == "haydar.indexer.engine"
    ]
    assert records
    assert all(
        re.fullmatch(r"[0-9a-f]{8}", record.run_id)
        for record in records
    )


def test_run_id_is_unique_per_search(monkeypatch) -> None:
    from haydar.search.hybrid import HybridSearch

    config = HaydarConfig(folders=[], initialized=True)
    search = HybridSearch(config)
    observed = []

    def capture(query, limit, cancel_event, worker, log_extra: dict[str, str] | None = None):
        if log_extra is None:
            raise AssertionError("run_id metadata was not provided")
        observed.append(log_extra["run_id"])
        return
        yield  # Keep this a generator function.

    monkeypatch.setattr(search, "_stream_ripgrep", capture)
    list(search.search_stream("one", mode="keyword"))
    list(search.search_stream("two", mode="keyword"))

    assert len(observed) == 2
    assert observed[0] != observed[1]
    assert all(re.fullmatch(r"[0-9a-f]{8}", value) for value in observed)

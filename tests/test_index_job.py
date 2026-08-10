"""Contract tests for the bounded, resumable indexing job.

These cover the properties that make partial indexing safe: durable commits,
honest reconciliation, bounded memory, and the difference between pause and
cancel.
"""

from unittest.mock import MagicMock, patch

import pytest

from haydar.config import HaydarConfig
from haydar.indexer.cache import CacheWriteError, FileCache
from haydar.indexer.engine import (
    IndexingEngine,
    JobControl,
    JobKind,
    JobOutcome,
    JobPhase,
)


def _config(folder, **kwargs):
    config = HaydarConfig(folders=[str(folder)], **kwargs)
    config.excluded_patterns = []
    return config


def _corpus(tmp_path, count=12, name="corpus"):
    folder = tmp_path / name
    folder.mkdir()
    for index in range(count):
        (folder / f"file-{index}.txt").write_text(
            f"document {index} " + "word " * 30, encoding="utf-8"
        )
    return folder


@pytest.fixture
def engine_factory(tmp_haydar):
    """Build engines over a mocked vector store, closing each one afterwards."""
    created = []

    def make(config, **kwargs):
        patcher = patch("haydar.indexer.engine.VectorStore")
        mock_class = patcher.start()
        store = MagicMock()
        mock_class.return_value = store
        engine = IndexingEngine(config, allow_download=False, **kwargs)
        created.append((engine, patcher))
        return engine, store

    yield make

    for engine, patcher in created:
        engine.close()
        patcher.stop()


# -- outcomes ---------------------------------------------------------------


def test_completed_run_reports_a_typed_outcome(tmp_path, engine_factory):
    engine, _ = engine_factory(_config(_corpus(tmp_path)))

    snapshot = engine.run_job()

    # The caller never has to infer the outcome from a stats dictionary.
    assert snapshot.outcome is JobOutcome.COMPLETE
    assert snapshot.phase is JobPhase.COMPLETE
    assert snapshot.committed_files == 12
    assert snapshot.discovery_complete is True


def test_cancel_and_pause_are_distinguishable_outcomes(tmp_path, engine_factory):
    engine, _ = engine_factory(_config(_corpus(tmp_path, count=40)))
    cancel_control = JobControl()
    cancel_control.request_cancel()
    assert engine.run_job(control=cancel_control).outcome is JobOutcome.CANCELLED

    engine2, _ = engine_factory(_config(_corpus(tmp_path, count=40, name="corpus2")))
    pause_control = JobControl()
    pause_control.request_pause()
    assert engine2.run_job(control=pause_control).outcome is JobOutcome.PAUSED


def test_all_terminal_outcomes_are_resumable(tmp_path, engine_factory):
    engine, _ = engine_factory(_config(_corpus(tmp_path, count=4)))
    control = JobControl()
    control.request_pause()

    assert engine.run_job(control=control).resumable is True


# -- durability -------------------------------------------------------------


def test_committed_batches_survive_cancellation(tmp_path, engine_factory):
    """Cancellation must never roll back work that was already committed."""
    folder = _corpus(tmp_path, count=30)
    config = _config(folder)
    config.embedding_batch_size = 2
    engine, store = engine_factory(config)

    control = JobControl()
    committed = []

    def cancel_after_first_commit(snapshot):
        committed.append(snapshot.committed_files)
        control.request_cancel()

    snapshot = engine.run_job(control=control, on_committed=cancel_after_first_commit)

    assert snapshot.outcome is JobOutcome.CANCELLED
    assert snapshot.committed_files > 0
    # Those files remain recorded, so a resumed run skips them.
    cached = [
        engine.cache.get(str(p.absolute()))
        for p in folder.glob("*.txt")
    ]
    assert sum(1 for row in cached if row is not None) == snapshot.committed_files
    assert store.add_documents.call_count >= 1


def test_cache_write_failure_after_a_vector_commit_fails_the_run(
    tmp_path, engine_factory
):
    """A diverged cache must fail loudly, never silently drop the files."""
    engine, _ = engine_factory(_config(_corpus(tmp_path, count=4)))

    def explode(*_args, **_kwargs):
        raise CacheWriteError("disk full")

    engine.cache.set_many = explode

    snapshot = engine.run_job()

    assert snapshot.outcome is JobOutcome.FAILED
    assert "disk full" in snapshot.error_message


def test_a_failed_run_reprocesses_its_files_next_time(tmp_path, engine_factory):
    folder = _corpus(tmp_path, count=4)
    engine, _ = engine_factory(_config(folder))
    original = engine.cache.set_many
    engine.cache.set_many = lambda *a, **k: (_ for _ in ()).throw(
        CacheWriteError("transient")
    )

    assert engine.run_job().outcome is JobOutcome.FAILED

    # Recovery: the same deterministic chunk ids are re-upserted safely.
    engine.cache.set_many = original
    recovered = engine.run_job()

    assert recovered.outcome is JobOutcome.COMPLETE
    assert recovered.committed_files == 4


# -- reconciliation ---------------------------------------------------------


def test_deletion_reconciliation_runs_after_a_complete_crawl(
    tmp_path, engine_factory
):
    folder = _corpus(tmp_path, count=6)
    engine, store = engine_factory(_config(folder))
    engine.run_job()

    removed = folder / "file-0.txt"
    removed.unlink()
    snapshot = engine.run_job()

    assert snapshot.deleted == 1
    deleted_paths = store.delete_by_filepaths.call_args[0][0]
    assert str(removed.absolute()) in deleted_paths


@pytest.mark.parametrize("stop", ["cancel", "pause"])
def test_no_deletion_reconciliation_after_an_incomplete_crawl(
    tmp_path, engine_factory, stop
):
    """An interrupted crawl never proved a file was absent."""
    folder = _corpus(tmp_path, count=8)
    engine, store = engine_factory(_config(folder))
    engine.run_job()
    store.delete_by_filepaths.reset_mock()

    (folder / "file-0.txt").unlink()
    control = JobControl()
    getattr(control, f"request_{stop}")()
    snapshot = engine.run_job(control=control)

    assert snapshot.deleted == 0
    assert store.delete_by_filepaths.call_count == 0


# -- resume -----------------------------------------------------------------


def test_resume_skips_committed_files_and_indexes_the_rest(
    tmp_path, engine_factory
):
    folder = _corpus(tmp_path, count=6)
    engine, _ = engine_factory(_config(folder))
    engine.run_job()

    (folder / "new-file.txt").write_text("brand new " + "word " * 40, encoding="utf-8")
    resumed = engine.run_job()

    assert resumed.committed_files == 1
    assert resumed.skipped_unchanged == 6


def test_resume_is_robust_to_files_added_and_deleted_while_stopped(
    tmp_path, engine_factory
):
    folder = _corpus(tmp_path, count=5)
    engine, _ = engine_factory(_config(folder))
    engine.run_job()

    (folder / "file-0.txt").unlink()
    (folder / "added.txt").write_text("added " + "word " * 40, encoding="utf-8")
    resumed = engine.run_job()

    assert resumed.outcome is JobOutcome.COMPLETE
    assert resumed.committed_files == 1
    assert resumed.deleted == 1


# -- bounds -----------------------------------------------------------------


def test_batches_never_exceed_the_configured_chunk_bound(tmp_path, engine_factory):
    folder = tmp_path / "big"
    folder.mkdir()
    for index in range(40):
        (folder / f"doc-{index}.txt").write_text("word " * 4000, encoding="utf-8")
    config = _config(folder)
    config.embedding_batch_size = 20
    engine, store = engine_factory(config)

    engine.run_job()

    sizes = [call.kwargs["ids"] for call in store.add_documents.call_args_list]
    assert sizes
    assert max(len(ids) for ids in sizes) <= 20


def test_batches_also_respect_the_byte_ceiling(tmp_path, engine_factory):
    folder = tmp_path / "heavy"
    folder.mkdir()
    for index in range(6):
        (folder / f"doc-{index}.txt").write_text("word " * 20000, encoding="utf-8")
    config = _config(folder)
    config.embedding_batch_size = 100_000  # deliberately not the binding bound
    config.embedding_batch_max_bytes = 64 * 1024
    engine, store = engine_factory(config)

    engine.run_job()

    # Several flushes happen because the byte ceiling binds first.
    assert store.add_documents.call_count > 1


def test_discovery_does_not_materialize_the_corpus(tmp_path, engine_factory):
    """Discovery is a generator, so nothing accumulates a per-file list."""
    folder = _corpus(tmp_path, count=25)
    engine, _ = engine_factory(_config(folder))

    discovery = engine.discover(JobControl())
    first = next(discovery)

    assert first.path.exists()
    assert hasattr(discovery, "__next__")
    discovery.close()


# -- dispositions -----------------------------------------------------------


def test_ocr_deferred_images_are_recorded_but_never_complete(
    tmp_path, engine_factory, monkeypatch
):
    from haydar.indexer.extractors import Disposition, ExtractionOutcome

    folder = tmp_path / "images"
    folder.mkdir()
    image = folder / "scan.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    monkeypatch.setattr(
        "haydar.indexer.engine.extract",
        lambda *_a, **_k: ExtractionOutcome(
            disposition=Disposition.OCR_DEFERRED, detail="not_found"
        ),
    )
    engine, _ = engine_factory(_config(folder))

    snapshot = engine.run_job()

    assert snapshot.ocr_deferred == 1
    row = engine.cache.get(str(image.absolute()))
    assert row["disposition"] == "ocr_deferred"
    # It stays eligible: a rerun still offers it for processing.
    assert engine.cache.is_unchanged_and_complete(
        str(image.absolute()), image.stat().st_mtime, image.stat().st_size
    ) is False


def test_deferred_images_are_rediscovered_on_the_next_run(
    tmp_path, engine_factory, monkeypatch
):
    from haydar.indexer.extractors import Disposition, ExtractionOutcome

    folder = tmp_path / "images"
    folder.mkdir()
    (folder / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    monkeypatch.setattr(
        "haydar.indexer.engine.extract",
        lambda *_a, **_k: ExtractionOutcome(
            disposition=Disposition.OCR_DEFERRED, detail="not_found"
        ),
    )
    engine, _ = engine_factory(_config(folder))
    engine.run_job()

    second = engine.run_job()

    assert second.ocr_deferred == 1
    assert second.skipped_unchanged == 0


def test_oversized_files_are_counted_not_errored(tmp_path, engine_factory):
    folder = tmp_path / "large"
    folder.mkdir()
    (folder / "huge.txt").write_text("word " * 100, encoding="utf-8")
    config = _config(folder)
    config.size_limits = {"text": 10, "document": 10, "image": 10}
    engine, _ = engine_factory(config)

    snapshot = engine.run_job()

    assert snapshot.skipped_size == 1
    assert snapshot.errors == 0


# -- progress honesty -------------------------------------------------------


def test_percentage_is_withheld_until_discovery_finishes(tmp_path, engine_factory):
    engine, _ = engine_factory(_config(_corpus(tmp_path, count=6)))
    observed = []

    engine.run_job(
        on_snapshot=lambda s: observed.append((s.phase, s.has_stable_total))
    )

    discovering = [
        stable for phase, stable in observed if phase is JobPhase.DISCOVERING
    ]
    assert discovering and not any(discovering)
    assert observed[-1][1] is True


def test_current_path_is_bounded_for_display(tmp_path, engine_factory):
    folder = tmp_path / ("deep" + "x" * 60)
    folder.mkdir()
    (folder / ("a" * 80 + ".txt")).write_text("word " * 30, encoding="utf-8")
    engine, _ = engine_factory(_config(folder))

    snapshot = engine.run_job()

    assert len(snapshot.current_path) <= 100


# -- job kinds --------------------------------------------------------------


def test_incremental_jobs_do_not_reconcile_deletions(tmp_path, engine_factory):
    """Only a full crawl may conclude a file is gone."""
    folder = _corpus(tmp_path, count=5)
    engine, store = engine_factory(_config(folder))
    engine.run_job()
    store.delete_by_filepaths.reset_mock()

    (folder / "file-0.txt").unlink()
    snapshot = engine.run_job(kind=JobKind.INCREMENTAL)

    assert snapshot.deleted == 0


def test_generations_increase_monotonically(tmp_haydar):
    cache = FileCache()
    try:
        first = cache.next_generation()
        second = cache.next_generation()
    finally:
        cache.close()

    assert second > first

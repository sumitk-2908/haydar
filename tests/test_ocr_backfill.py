"""Contract tests for the image-only OCR backfill (§12.4).

The properties that matter here are all about *restraint*. Installing OCR must
revisit exactly the images that are waiting for it — deferred ones, and ones
read by an older engine — and touch nothing else. A backfill that re-crawled the
corpus, re-embedded a text file, or reconciled a deletion would be a full
reindex wearing a smaller name.
"""

from unittest.mock import MagicMock, patch

import pytest

from haydar.config import HaydarConfig
from haydar.indexer.cache import FileCache
from haydar.indexer.engine import (
    IndexingEngine,
    JobControl,
    JobKind,
    JobOutcome,
)
from haydar.indexer.extractors import Disposition, ExtractedContent, ExtractionOutcome

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _config(folder, **kwargs):
    config = HaydarConfig(folders=[str(folder)], **kwargs)
    config.excluded_patterns = []
    return config


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


def _mixed_corpus(tmp_path):
    """A folder with two images and two non-image files."""
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / "notes.txt").write_text("plain text " + "word " * 30, encoding="utf-8")
    (folder / "readme.md").write_text("markdown " + "word " * 30, encoding="utf-8")
    (folder / "scan.png").write_bytes(PNG_HEADER + b"0" * 64)
    (folder / "receipt.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 64)
    return folder


def _deferring_extract(*_args, **_kwargs):
    return ExtractionOutcome(
        disposition=Disposition.OCR_DEFERRED, detail="not_found"
    )


def _ocr_extract(version="tesseract-5.5.3", text="invoice total 42"):
    """Stand in for a working OCR engine, text files still extracted normally."""

    def extract(filepath, file_hash=None, refresh_ocr=False):
        if filepath.suffix.lower() in (".png", ".jpg", ".jpeg", ".tiff"):
            return ExtractionOutcome(
                disposition=Disposition.CONTENT,
                content=ExtractedContent(text=text, metadata={}),
                ocr_engine_version=version,
            )
        return ExtractionOutcome(
            disposition=Disposition.CONTENT,
            content=ExtractedContent(
                text=filepath.read_text(encoding="utf-8"), metadata={}
            ),
        )

    return extract


# -- the work list comes from the cache ---------------------------------------


def test_the_backfill_visits_only_the_images_the_cache_says_are_pending(
    tmp_path, engine_factory, monkeypatch
):
    """Not a re-crawl: the eligible set is read from the cache, not the disk."""
    folder = _mixed_corpus(tmp_path)
    engine, _ = engine_factory(_config(folder))

    monkeypatch.setattr("haydar.indexer.engine.extract", _deferring_extract)
    engine.run_job()

    visited = []
    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract())
    monkeypatch.setattr(
        engine, "discover", _fail_if_called("a backfill must not crawl the corpus")
    )

    def record(item, *args, **kwargs):
        visited.append(item.path.name)
        return original(item, *args, **kwargs)

    original = engine._extract_worker
    monkeypatch.setattr(engine, "_extract_worker", record)

    snapshot = engine.run_job(kind=JobKind.OCR_BACKFILL, ocr_version="tesseract-5.5.3")

    assert snapshot.outcome is JobOutcome.COMPLETE
    assert sorted(visited) == ["receipt.jpg", "scan.png"]


def test_images_read_by_an_older_engine_are_revisited(
    tmp_path, engine_factory, monkeypatch
):
    folder = _mixed_corpus(tmp_path)
    engine, _ = engine_factory(_config(folder))

    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract("tesseract-5.0.0"))
    engine.run_job()
    assert engine.cache.get(str((folder / "scan.png").absolute()))[
        "ocr_engine_version"
    ] == "tesseract-5.0.0"

    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract("tesseract-5.5.3"))
    snapshot = engine.run_job(
        kind=JobKind.OCR_BACKFILL, ocr_version="tesseract-5.5.3"
    )

    assert snapshot.committed_files == 2
    row = engine.cache.get(str((folder / "scan.png").absolute()))
    assert row["ocr_engine_version"] == "tesseract-5.5.3"


def test_images_already_read_by_the_current_engine_are_left_alone(
    tmp_path, engine_factory, monkeypatch
):
    folder = _mixed_corpus(tmp_path)
    engine, _ = engine_factory(_config(folder))

    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract("tesseract-5.5.3"))
    engine.run_job()

    snapshot = engine.run_job(
        kind=JobKind.OCR_BACKFILL, ocr_version="tesseract-5.5.3"
    )

    # Nothing is pending, so the backfill is a no-op rather than a re-embed.
    assert snapshot.committed_files == 0
    assert snapshot.discovered == 0


# -- non-image records are untouched ------------------------------------------


def test_non_image_cache_and_vector_records_are_untouched(
    tmp_path, engine_factory, monkeypatch
):
    """The §12.4 promise: a backfill preserves everything that is not an image."""
    folder = _mixed_corpus(tmp_path)
    engine, store = engine_factory(_config(folder))

    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract("tesseract-5.0.0"))
    engine.run_job()

    text_before = {
        name: engine.cache.get(str((folder / name).absolute()))
        for name in ("notes.txt", "readme.md")
    }
    store.reset_mock()

    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract("tesseract-5.5.3"))
    engine.run_job(kind=JobKind.OCR_BACKFILL, ocr_version="tesseract-5.5.3")

    for name, before in text_before.items():
        assert engine.cache.get(str((folder / name).absolute())) == before

    # No vector write in the backfill mentions a non-image path.
    written = [
        path
        for call in store.delete_by_filepaths.call_args_list
        for path in call.args[0]
    ]
    written += [
        metadata["file_path"]
        for call in store.add_documents.call_args_list
        for metadata in call.kwargs.get("metadatas", [])
    ]
    assert written
    assert not any(path.endswith((".txt", ".md")) for path in written)


def test_a_backfill_never_reconciles_deletions(tmp_path, engine_factory, monkeypatch):
    """It visits a subset by design, so absence was never proven."""
    folder = _mixed_corpus(tmp_path)
    engine, store = engine_factory(_config(folder))

    monkeypatch.setattr("haydar.indexer.engine.extract", _deferring_extract)
    engine.run_job()

    # A file disappears between runs; the backfill must not notice or act.
    (folder / "notes.txt").unlink()
    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract())
    snapshot = engine.run_job(kind=JobKind.OCR_BACKFILL, ocr_version="tesseract-5.5.3")

    assert snapshot.deleted == 0
    assert engine.cache.get(str((folder / "notes.txt").absolute())) is not None


def test_a_deleted_image_is_skipped_rather_than_reconciled(
    tmp_path, engine_factory, monkeypatch
):
    folder = _mixed_corpus(tmp_path)
    engine, _ = engine_factory(_config(folder))

    monkeypatch.setattr("haydar.indexer.engine.extract", _deferring_extract)
    engine.run_job()

    image = folder / "scan.png"
    cached_path = str(image.absolute())
    image.unlink()

    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract())
    snapshot = engine.run_job(kind=JobKind.OCR_BACKFILL, ocr_version="tesseract-5.5.3")

    assert snapshot.outcome is JobOutcome.COMPLETE
    assert snapshot.committed_files == 1  # only the surviving image
    # The stale row is left for a real crawl to reconcile.
    assert engine.cache.get(cached_path) is not None


def test_an_image_outside_the_configured_folders_is_not_visited(
    tmp_path, engine_factory, monkeypatch
):
    """A folder removed from settings must not be silently re-indexed."""
    folder = _mixed_corpus(tmp_path)
    other = tmp_path / "unconfigured"
    other.mkdir()
    stray = other / "stray.png"
    stray.write_bytes(PNG_HEADER + b"0" * 64)

    engine, _ = engine_factory(_config(folder))
    engine.cache.set(
        str(stray.absolute()), stray.stat().st_mtime, stray.stat().st_size,
        "hash", 0, disposition="ocr_deferred",
    )

    monkeypatch.setattr("haydar.indexer.engine.extract", _deferring_extract)
    engine.run_job()

    visited = []
    original = engine._extract_worker
    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract())
    monkeypatch.setattr(
        engine,
        "_extract_worker",
        lambda item, *a, **k: (visited.append(item.path.name), original(item, *a, **k))[1],
    )

    engine.run_job(kind=JobKind.OCR_BACKFILL, ocr_version="tesseract-5.5.3")

    assert "stray.png" not in visited


# -- the content cache must not defeat a re-OCR --------------------------------


def test_a_reocr_is_not_served_from_the_content_keyed_text_cache(
    tmp_path, engine_factory, monkeypatch
):
    """The image bytes are unchanged; the engine reading them is not.

    Without this, a backfill would return the previous engine's text, record no
    engine version, and leave the image eligible on every future run.
    """
    folder = tmp_path / "images"
    folder.mkdir()
    image = folder / "scan.png"
    image.write_bytes(PNG_HEADER + b"0" * 64)
    engine, _ = engine_factory(_config(folder))

    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract("tesseract-5.0.0", "old text"))
    engine.run_job()

    seen = []

    def new_engine(filepath, file_hash=None, refresh_ocr=False):
        seen.append(refresh_ocr)
        return _ocr_extract("tesseract-5.5.3", "new text")(
            filepath, file_hash, refresh_ocr
        )

    monkeypatch.setattr("haydar.indexer.engine.extract", new_engine)
    engine.run_job(kind=JobKind.OCR_BACKFILL, ocr_version="tesseract-5.5.3")

    assert seen == [True]
    assert engine.cache.get(str(image.absolute()))["ocr_engine_version"] == (
        "tesseract-5.5.3"
    )


def test_the_real_extractor_bypasses_its_cache_only_for_image_refresh(tmp_haydar):
    """The bypass is scoped: a text file still benefits from the cache."""
    from haydar.config import CACHE_DIR
    from haydar.indexer.extractors import extract

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "abc123.txt").write_text("cached text", encoding="utf-8")

    document = tmp_haydar / "note.txt"
    document.write_text("fresh text", encoding="utf-8")
    picture = tmp_haydar / "scan.png"
    picture.write_bytes(PNG_HEADER + b"0" * 64)

    # A text file reads from the cache even when a refresh was requested: only
    # images are re-read, because only images depend on the OCR engine.
    assert extract(document, file_hash="abc123", refresh_ocr=True).text == "cached text"
    # An image without a refresh also reads the cache — the normal fast path.
    assert extract(picture, file_hash="abc123").text == "cached text"

    with patch(
        "haydar.indexer.extractors._extract_image",
        return_value=ExtractionOutcome(
            disposition=Disposition.CONTENT,
            content=ExtractedContent(text="fresh ocr", metadata={}),
            ocr_engine_version="tesseract-5.5.3",
        ),
    ) as ocr:
        refreshed = extract(picture, file_hash="abc123", refresh_ocr=True)

    # The refresh actually ran the engine and replaced the stale cached text.
    assert refreshed.text == "fresh ocr"
    ocr.assert_called_once()
    assert (CACHE_DIR / "abc123.txt").read_text(encoding="utf-8") == "fresh ocr"


# -- cache query ---------------------------------------------------------------


def test_pending_images_query_matches_only_eligible_image_rows(tmp_haydar):
    cache = FileCache()
    try:
        cache.set("C:/docs/a.png", 1.0, 10, "h", 0, disposition="ocr_deferred")
        cache.set(
            "C:/docs/b.PNG", 1.0, 10, "h", 3,
            disposition="indexed", ocr_engine_version="tesseract-5.0.0",
        )
        cache.set(
            "C:/docs/c.jpg", 1.0, 10, "h", 3,
            disposition="indexed", ocr_engine_version="tesseract-5.5.3",
        )
        cache.set("C:/docs/notes.txt", 1.0, 10, "h", 3, disposition="indexed")
        # A file whose *name* contains a suffix-like string, not its extension.
        cache.set("C:/docs/png-notes.txt", 1.0, 10, "h", 3, disposition="indexed")

        pending = set(
            cache.iter_pending_images(
                {".png", ".jpg", ".jpeg", ".tiff"}, ocr_version="tesseract-5.5.3"
            )
        )
    finally:
        cache.close()

    # Deferred and stale-engine images only; case-insensitive on the suffix.
    assert pending == {"C:/docs/a.png", "C:/docs/b.PNG"}


def test_without_a_known_engine_version_only_deferral_counts(tmp_haydar):
    cache = FileCache()
    try:
        cache.set("C:/docs/a.png", 1.0, 10, "h", 0, disposition="ocr_deferred")
        cache.set(
            "C:/docs/b.png", 1.0, 10, "h", 3,
            disposition="indexed", ocr_engine_version="tesseract-5.0.0",
        )

        pending = set(cache.iter_pending_images({".png"}))
    finally:
        cache.close()

    assert pending == {"C:/docs/a.png"}


def test_the_query_streams_rather_than_materializing_every_row(tmp_haydar):
    """Memory is bounded by the batch, not by the number of cached files."""
    cache = FileCache()
    try:
        for index in range(500):
            cache.set(
                f"C:/docs/img-{index}.png", 1.0, 10, "h", 0, disposition="ocr_deferred"
            )

        pending = cache.iter_pending_images({".png"})
        first = next(pending)
    finally:
        cache.close()

    assert first.endswith(".png")
    assert hasattr(pending, "__next__")


# -- coordinator wiring --------------------------------------------------------


def test_the_coordinator_starts_a_backfill_that_the_engine_can_route(tmp_haydar):
    """The kind is what selects the cache-driven work list."""
    from haydar.indexer.jobs import IndexJobCoordinator

    calls = []

    class _Engine:
        def run_job(self, **kwargs):
            calls.append(kwargs)
            from haydar.indexer.engine import IndexSnapshot, JobPhase

            return IndexSnapshot(
                kind=kwargs["kind"], phase=JobPhase.COMPLETE,
                outcome=JobOutcome.COMPLETE,
            )

        def close(self):
            pass

    config = HaydarConfig(folders=[r"C:\Docs"], initial_index_state="complete")
    coordinator = IndexJobCoordinator(config, engine_factory=lambda _cfg: _Engine())

    coordinator.start_ocr_backfill("tesseract-5.5.3")
    assert coordinator.wait_for_terminal(timeout=5)

    assert calls[0]["kind"] is JobKind.OCR_BACKFILL
    assert calls[0]["ocr_version"] == "tesseract-5.5.3"
    # An OCR job never regresses a completed initial crawl.
    assert config.initial_index_state == "complete"


def _fail_if_called(message):
    def fail(*_args, **_kwargs):
        raise AssertionError(message)

    return fail


def test_a_backfill_can_be_cancelled_between_images(
    tmp_path, engine_factory, monkeypatch
):
    folder = _mixed_corpus(tmp_path)
    engine, _ = engine_factory(_config(folder))

    monkeypatch.setattr("haydar.indexer.engine.extract", _deferring_extract)
    engine.run_job()

    control = JobControl()
    control.request_cancel()
    monkeypatch.setattr("haydar.indexer.engine.extract", _ocr_extract())

    snapshot = engine.run_job(
        kind=JobKind.OCR_BACKFILL, ocr_version="tesseract-5.5.3", control=control
    )

    assert snapshot.outcome is JobOutcome.CANCELLED
    assert snapshot.deleted == 0

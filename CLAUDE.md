# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Haydar is a Windows-only, fully-local semantic file search tool: a Typer CLI + PySide6 floating GUI over a ChromaDB vector store, with ripgrep for keyword search and a watchdog file watcher. Python 3.10+.

## Architecture boundary (enforced rule from `.agents/AGENTS.md`)

Two layers: `src/haydar/ui/` (PySide6 frontend) and `src/haydar/indexer/` + `src/haydar/search/` (backend). **UI modules may import only from `src/haydar/search/` (the interface layer) or `src/haydar/config.py` — never from `indexer/` internals.** This keeps the backend replaceable without touching the UI. `cli.py` sits above everything and is exempt.

## Layering and load order

Dependency direction is `config ← search ← indexer`, with `config`/`search ← ui`, and `cli ← everything`. `config.py` is the leaf (no internal deps).

- **`cli.py` lazy-imports heavy subsystems inside command bodies** (PySide6, ChromaDB, sentence-transformers). Preserve this — top-level imports would make every command pay full startup cost. The `main` callback calls `setup_logging()`; the windowed GUI path (`gui_main.py` → `launch_search_window`) sets up its own file-only logging since it has no console.
- **`HybridSearch.store` is a lazy `@property`.** Constructing `HybridSearch` does NOT build the `VectorStore`, so empty queries and keyword-only search work without the embedding model present. The model is only required when a semantic query actually runs.
- **`VectorStore.__init__` raises `VectorStoreError` (never `sys.exit`)** on missing model / corrupt DB. Callers handle it at the boundary: CLI via `_fail()`, UI by degrading gracefully (window stays alive, error shown on search). Don't reintroduce `sys.exit` in the interface layer — it would kill the Qt process.

## ripgrep is required and not vendored

Keyword search shells out to `rg`. The binary is **not** in git (gitignored at `src/haydar/bin/`). Provisioning logic lives in `src/haydar/ripgrep.py` (`ensure_ripgrep()`, SHA-256 verified against pinned checksums — never bypass verification, the binary is executed):
- **pip / dev:** fetched on first `haydar init` into `~/.haydar/bin/`, or run `python scripts/pull-rg.py` manually.
- **EXE:** bundled at build time — CI runs `pull-rg.py` before PyInstaller so `haydar.spec` can bundle it.

`get_rg_path()` in `config.py` searches bundle → `~/.haydar/bin` → dev `src/haydar/bin`.

## Runtime data location

All user data lives under `~/.haydar/` (`config.json`, `db/` ChromaDB + `haydar_cache.db` SQLite, `logs/`, `models/`, `cache/`, `bin/`). Path constants are defined in `config.py`. **These constants are bound at import time and other modules do `from haydar.config import DB_DIR`, so tests must monkeypatch every module's copy** — see `tests/conftest.py`'s `tmp_haydar` fixture, which is the required pattern for any test that touches storage.

## Indexing pipeline

`IndexingEngine.index_all()` (in `indexer/engine.py`): `os.walk` crawl (prunes excluded dirs) → threaded producer/consumer extraction → batched upserts to `VectorStore`. `FileCache` (SQLite mtime+size+hash) skips unchanged files; the cache is updated **only after a successful flush**. Deleted-file invalidation runs before indexing. `IndexingEngine` is a context manager — use `with IndexingEngine(...)` so the SQLite connection closes. `FileWatcher` reuses the engine for single-file updates on watchdog events.

## Commands

```bash
pip install -e .[dev,ocr]        # dev install (ocr extra = pytesseract; needs Tesseract on PATH)
python scripts/pull-rg.py        # fetch + verify ripgrep (needed before keyword search / building)

pytest tests/ -q                 # run tests
pytest tests/test_engine.py::test_chunk_text_overlap_and_offsets   # single test
pytest tests/ --cov=haydar --cov-report=term-missing               # with coverage

pyinstaller haydar.spec          # build both EXEs (dist/haydar.exe windowed, dist/haydar-cli.exe console)

haydar init                      # first-time setup (downloads ~80MB model, requires internet)
haydar search "query" --mode keyword   # semantic (default) or keyword
```

Version is single-sourced from `src/haydar/__init__.py` (`[tool.hatch.version]`).

## Gotchas

- **Windows-only surfaces:** `os.startfile` (UI open), `subprocess.CREATE_NO_WINDOW` (rg), `install_autostart()` writes a `.bat` to the Startup folder. Guard any cross-platform work accordingly.
- **`search.embeddings` does not exist** — the store uses Chroma's own `SentenceTransformerEmbeddingFunction`. Don't reintroduce a duplicate embedding wrapper.
- **DB schema versioning:** bumping chunking/schema requires incrementing `CURRENT_SCHEMA_VERSION` in `config.py`; `_check_initialized()` forces a `reindex` on mismatch.
- **EXEs are unsigned** — SmartScreen warns on first launch (documented in README).

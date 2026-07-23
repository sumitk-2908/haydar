# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-23

### Added
- `haydar uninstall` command with backup capability.
- GitHub Actions CI/CD: release workflow (dual EXE build) plus a CI workflow running `ruff` lint, `mypy` type-checking, and tests (with a coverage floor) on every push and PR across Python 3.11–3.13, installing from a pinned lockfile.
- Graceful error handling for missing Tesseract (OCR), database corruption, and file permissions.
- Database schema versioning.
- Persistent rotating file logging at `~/.haydar/logs/haydar.log`.
- Keyword search mode (`haydar search --mode keyword`) backed by ripgrep.
- Automatic, SHA-256-verified ripgrep provisioning on first `haydar init` (pip) and bundling into the standalone EXE at build time.
- Windowed GUI executable (`haydar.exe`) separate from the console CLI (`haydar-cli.exe`), plus a `haydar-ui` gui-script for pip installs.
- OS-level indexing lock so a concurrent `reindex` and watcher batch can't race ChromaDB upserts.
- First-run progress message before the ~80 MB embedding-model download so `haydar init` no longer looks hung.
- Bounded on-disk extraction cache: oldest entries are evicted at index time once the cache exceeds a size cap.
- `verify.ps1` and `install.ps1` helper scripts, and `CONTRIBUTING.md` / `SECURITY.md`.
- Test suites for the watcher, hotkey, hybrid search, and an end-to-end `index_all()` integration + deleted-file invalidation test.

### Changed
- Default index folders are Documents, Desktop, and Downloads; when none exist, `haydar init` now prompts for folders instead of silently indexing the current directory.
- `IndexingEngine` builds its `VectorStore` lazily (mirroring `HybridSearch.store`), so constructing an engine or watcher no longer forces a model load or hard-fails when the model is absent.
- `VectorStore` now raises a typed `VectorStoreError` instead of calling `sys.exit()`, so a missing model or corrupt database no longer kills the GUI process.
- Autostart entry now points at `haydar-cli.exe` (or `sys.executable`) instead of `pythonw`, so it works for EXE users.
- Search result snippets render as plain text (`Qt.PlainText`), so file content can never be interpreted as markup.
- `requires-python` raised to `>=3.11`; Python 3.10 dropped from CI and classifiers.

### Fixed
- Keyword search now works in packaged and pip-installed builds (ripgrep was previously never bundled or fetched).
- File watcher observer and SQLite cache connection are now closed cleanly on shutdown.
- Bare `except:` in hybrid search narrowed to `except OSError:` so `KeyboardInterrupt`/`SystemExit` propagate.
- Trailing short chunk of a multi-chunk file is no longer silently dropped (chunk-tail floor lowered).
- Tar extraction uses `filter="data"` on Python 3.12+ as defense-in-depth.

### Removed
- Unused `search/embeddings.py` module and a dead `import multiprocessing` (dead code).

## [0.1.0] - 2026-07-20

### Added
- Initial release.
- Hybrid semantic and keyword search.
- Text extraction for PDF, DOCX, Code, and Images.
- PySide6 floating UI with global hotkey.
- Background file watcher.
- SQLite-backed ChromaDB vector store.

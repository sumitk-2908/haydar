# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.8.0] - 2026-08-11

### Added
- Guided first run: onboarding selects Documents only on a genuinely fresh install, and optional folders are bounded-scanned with a warning before large, network, or whole-drive locations are added.
- Search now opens as soon as keyword search, the embedding model, and the vector collection are ready — before the first crawl finishes — and results improve as each batch commits.
- An index status band in the search window showing indexing state and counts, with Pause, Resume, Cancel, and Retry controls; search and settings stay usable throughout.
- Resumable initial indexing with a persisted lifecycle (`not_started`, `running`, `paused`, `cancelled`, `failed`, `complete`) that recovers automatically after an interrupted run and keeps committed work.
- One-click OCR provisioning: pinned asset manifest, streamed download, constant-time checksum comparison, member-by-member extraction, executable probe, and atomic activation. No engine distribution passed the licensing review, so installation fails closed — a Tesseract you install yourself is still detected and used. See `KNOWN_GAPS.md`.
- Image-only OCR backfill driven from the file cache, so images deferred while OCR was unavailable are picked up later without a reindex.
- `ocr install`, `ocr status`, and `ocr backfill` CLI commands over the same services the GUI uses.
- A published documentation site, `THIRD_PARTY_NOTICES.md` covering every bundled and provisioned component, and `KNOWN_GAPS.md` recording deliberate omissions.
- A packaged-GUI startup probe that runs the frozen `haydar.exe` in an isolated profile and gates every release on it starting without a console or a missing import.

### Changed
- Indexing now uses generator-based discovery and SQLite crawl generations instead of whole-crawl lists, so memory is bounded by the batch window rather than the size of the corpus.
- The writer lock is taken once per flush rather than for the whole crawl, and searches never acquire it — semantic and keyword search stay responsive while indexing runs.
- The file watcher replaces its thread-per-event handler with a bounded, coalescing queue and a single serialized writer, and starts only after the initial crawl reaches a safe terminal state.
- `haydar uninstall` now preserves `~/.haydar` by default and requires `--remove-data` to delete it, matching `uninstall.ps1`.
- The CLI gates on search readiness rather than the legacy `initialized` flag, so a partial index is a valid state to search.
- Configuration moved to format version 2 with a pure, idempotent migration that never replaces a persisted folder list with defaults and preserves unknown keys.
- Documentation now leads with downloading and launching `haydar.exe`; the CLI is documented as an optional expert tool.

### Fixed
- Update checks queried a repository that does not exist, so no update was ever reported. All release, install, and verify scripts now point at the real repository.
- OCR version probes failed inside the windowed `haydar.exe` with "the handle is invalid", because the console-less process passed an inherited standard handle to the child.
- Deferred Qt callbacks could reach a status band whose underlying object had already been destroyed by a window close, raising inside the event loop.
- An OCR backfill replayed the previous engine's text, because extraction served a content-keyed cache entry before dispatching to OCR — the bytes are unchanged on a re-OCR, but the engine is not.
- A corrupt `config.json` is now preserved alongside the replacement instead of being silently overwritten with defaults.
- Error-state window sizing is stable across Qt versions.
- Documentation links pointed at a `main` branch that does not exist on this repository.

## [0.7.0] - 2026-07-31

### Added
- Automatic update checks in the CLI and GUI with safe version parsing and direct links to GitHub releases.
- OCR readiness detection and status reporting for Tesseract, including actionable CLI and Settings guidance.
- A parsed, packaged changelog with a Settings “What's New” view and post-update notification banner.
- Expanded accessibility, high-DPI, error handling, scale, updater, OCR, changelog, Settings, and GUI regression coverage.
- A standalone uninstaller and safer installer handling for existing installations, checksums, and replacement failures.
- Extraction support and hardening for additional file types, archives, encodings, and malformed inputs.

### Changed
- Settings and search-window behavior now provide safer persistence, plain-text rendering, responsive sizing, and clearer status feedback.
- CI and release workflows now validate supported Python versions, OCR packaging, PowerShell scripts, release assets, and checksums.
- Dependency constraints and package metadata were tightened for reproducible supported builds.

### Fixed
- OCR detection now distinguishes missing adapters, missing executables, unsupported versions, and probe failures.
- Changelog discovery and parsing now work in source, wheel, and frozen application layouts.
- Update checks fail safely on malformed responses and network errors.
- Search, extraction, configuration, startup, and UI edge cases now degrade without corrupting user state.

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

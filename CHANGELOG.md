# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `haydar uninstall` command with backup capability.
- GitHub Actions CI/CD: release workflow (dual EXE build) plus a CI workflow running lint/tests on every push and PR across Python 3.10–3.13.
- Graceful error handling for missing Tesseract (OCR), database corruption, and file permissions.
- Database schema versioning.
- Persistent rotating file logging at `~/.haydar/logs/haydar.log`.
- Keyword search mode (`haydar search --mode keyword`) backed by ripgrep.
- Automatic, SHA-256-verified ripgrep provisioning on first `haydar init` (pip) and bundling into the standalone EXE at build time.
- Windowed GUI executable (`haydar.exe`) separate from the console CLI (`haydar-cli.exe`), plus a `haydar-ui` gui-script for pip installs.
- Basic integration and unit tests across config, extractors, cache, engine chunking, and the vector store.

### Changed
- Default index folders are now Documents, Desktop, and Downloads (falling back to the current directory) instead of only the current directory.
- `VectorStore` now raises a typed `VectorStoreError` instead of calling `sys.exit()`, so a missing model or corrupt database no longer kills the GUI process.

### Fixed
- Keyword search now works in packaged and pip-installed builds (ripgrep was previously never bundled or fetched).
- File watcher observer and SQLite cache connection are now closed cleanly on shutdown.

### Removed
- Unused `search/embeddings.py` module (dead code).

## [0.1.0] - 2026-07-20

### Added
- Initial release.
- Hybrid semantic and keyword search.
- Text extraction for PDF, DOCX, Code, and Images.
- PySide6 floating UI with global hotkey.
- Background file watcher.
- SQLite-backed ChromaDB vector store.

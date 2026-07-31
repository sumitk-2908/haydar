# ⚡ Haydar

**Fast, local semantic file search for Windows.**

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Find any file by *what it contains*, not what it's named. Powered by AI embeddings, entirely on-device — no cloud, no API costs.

---

## Features

- 🔍 **Semantic search** — search by meaning, not just keywords, with an optional exact-match keyword mode
- 📄 **All file types** — PDFs, DOCX, TXT, code files, images (OCR)
- 🏠 **100% local** — everything runs on your device, no data leaves your machine
- ⚡ **Fast** — sub-second search across thousands of files
- 🪟 **Floating UI** — summon with `Ctrl+Space`, dismiss with `Esc`
- 👁 **Live watcher** — new and changed files are indexed automatically
- 💻 **CLI + GUI** — powerful command line and beautiful desktop interface

## Quick Start

Download the standalone EXE from the [latest release](https://github.com/haydar-search/haydar/releases), verify it (see below), then:

```powershell
haydar-cli.exe init
haydar-cli.exe search "quarterly budget report"
```

## Installation

### Requirements

- Python 3.11+ (only needed for a source/dev install)
- Windows 10/11

### Standalone Windows EXE (recommended)

Prebuilt executables are attached to each [GitHub Release](https://github.com/haydar-search/haydar/releases):

- `haydar.exe` — the floating search UI (no console window).
- `haydar-cli.exe` — the full command-line interface.

Each EXE ships with a matching `.sha256` file. Verify the download before running it:

```powershell
# Compare the printed hash to the contents of the .sha256 file
(Get-FileHash haydar-cli.exe -Algorithm SHA256).Hash.ToLower() -eq
    (Get-Content haydar-cli.exe.sha256).Split(' ')[0].ToLower()
```

`True` means the download is intact. A ready-to-run copy of this check is in
[`verify.ps1`](./verify.ps1), and [`install.ps1`](./install.ps1) will download
and verify all release assets before replacing an installation, roll back a
failed commit, install to `%LOCALAPPDATA%\Haydar\`, and add it to your PATH.
Existing installations require explicit confirmation; use `-Yes` only for
intentional automation.

Then run `haydar-cli.exe init` once to set up your index.

> **Note:** The EXEs are not code-signed yet, so Windows SmartScreen may show a
> "Windows protected your PC" warning on first launch. Click **More info →
> Run anyway**. Verifying the SHA-256 checksum confirms the download is intact.

### Uninstall

To remove Haydar, download [`uninstall.ps1`](https://github.com/haydar-search/haydar/releases/latest/download/uninstall.ps1) from the latest release and run it:

```powershell
.\uninstall.ps1
```

By default, this removes the executables, checksum files, autostart script, and
exact Haydar PATH entries, but preserves indexed data and configuration in
`~/.haydar/`. The script prompts before changing anything. Noninteractive use
requires `-Yes`; `-KeepData` explicitly preserves data and takes precedence over
`-RemoveData`. To completely remove all data, use the `-RemoveData` switch:

```powershell
.\uninstall.ps1 -RemoveData
```

If an executable is locked, close Haydar and its watcher, then rerun the script.
Unrelated files in the installation directory and unrelated or empty PATH
entries are preserved.

### Install from source

```powershell
git clone https://github.com/haydar-search/haydar.git
cd haydar
pip install -e .[dev,ocr]
python scripts/pull-rg.py   # fetch + verify ripgrep
haydar init
```

### Optional: Image OCR Support

To search inside images (PNG, JPG, TIFF), install the OCR extras:

```bash
pip install haydar[ocr]
```

You'll also need to install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) for Windows.

## Usage

### Initialize (first time)

```bash
haydar init
```

This will:
1. Select folders to index (defaults: Documents, Desktop, Downloads — override with `--folders`)
2. Download the embedding model (~80MB, first run only, **requires internet**)
3. Download the ripgrep binary for keyword search (verified by SHA-256)
4. Index all supported files in your selected folders

### Search

**From the terminal:**
```bash
haydar search "machine learning research notes"
haydar search "invoice from march" --limit 5
haydar search "TODO" --mode keyword     # exact-match keyword search (ripgrep)
```

**Floating UI:**
```bash
haydar search   # opens the floating search window
```

Or press `Ctrl+Space` anywhere (when the watcher is running).

### File Watcher

```bash
haydar watch                    # start watching for file changes
haydar watch --install-autostart  # auto-start on Windows login
```

### Other Commands

```bash
haydar status                   # show index stats
haydar config                   # view/edit configuration
haydar config --add-folder C:\Projects  # add a folder
haydar config --set-hotkey "<ctrl>+<shift>+f"  # change hotkey
haydar reindex                  # force full re-index
```

## How It Works

1. **Extraction** — Text is extracted from PDFs, DOCX, code files, images (OCR)
2. **Chunking** — Documents are split into ~500-word chunks with overlap
3. **Embedding** — Each chunk is converted to a 384-dimensional vector using `all-MiniLM-L6-v2`
4. **Storage** — Vectors are stored locally in ChromaDB at `~/.haydar/db/`
5. **Search** — Your query is embedded and matched against all chunks by cosine similarity (semantic mode). A separate keyword mode uses ripgrep for fast exact-text matches.

## Supported File Types

| Category | Extensions |
|----------|-----------|
| Documents | `.pdf`, `.docx` |
| Text | `.txt`, `.md`, `.csv`, `.log`, `.rst`, `.ini`, `.cfg`, `.toml`, `.yaml` |
| Code | `.py`, `.js`, `.ts`, `.java`, `.c`, `.cpp`, `.rs`, `.go`, `.rb`, `.html`, `.css`, `.json`, `.xml`, and 30+ more |
| Images (OCR) | `.png`, `.jpg`, `.jpeg`, `.tiff` *(requires `haydar[ocr]`)* |

## Configuration

Config is stored at `~/.haydar/config.json`. You can edit it directly or use `haydar config`.

| Setting | Default | Description |
|---------|---------|-------------|
| `embedding_model` | `all-MiniLM-L6-v2` | Sentence-transformers model |
| `hotkey` | `<ctrl>+<space>` | Global hotkey for search UI |
| `chunk_size` | `500` | Words per chunk |
| `chunk_overlap` | `50` | Overlap between chunks |
| Size limits | 10MB text, 100MB docs | Per-type file size limits |

## Troubleshooting

- **Model download / offline failure:** If you see `Model not found at ~/.haydar/models/`, ensure you are connected to the internet and run `haydar init` to download the embedding model.
- **Empty search results:** Check if your folders are indexed correctly using `haydar status`. Try running `haydar reindex`. Ensure the files are not excluded by `config.json`.
- **Hotkey / System tray failure:** If the `Ctrl+Space` hotkey doesn't work, ensure `haydar watch` is running in the background. If the system tray icon doesn't appear, you may be missing PySide6 plugins. Reinstall using `pip install --force-reinstall PySide6`.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Embeddings | sentence-transformers (`all-MiniLM-L6-v2`) |
| Vector DB | ChromaDB (fully local) |
| Text extraction | pypdf, python-docx, chardet |
| Keyword search | ripgrep |
| OCR | pytesseract (optional) |
| File watching | watchdog |
| Desktop UI | PySide6 |
| Global hotkey | pynput |
| CLI | Typer + Rich |

## Development

```powershell
pip install -e .[dev,ocr]        # dev install
python scripts/pull-rg.py        # fetch + verify ripgrep

ruff check src/ tests/           # lint
mypy src/haydar                  # type-check
pytest tests/ -q --cov=haydar --cov-report=term-missing   # tests + coverage
```

**Architecture rule (enforced):** UI modules under `src/haydar/ui/` may import
only from `src/haydar/search/` or `src/haydar/config.py` — never from
`indexer/` internals. This keeps the backend replaceable without touching the
UI. See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full contributor guide and
[SECURITY.md](./SECURITY.md) for the security model and reporting.

## License

MIT — see [LICENSE](./LICENSE).

# ⚡ Haydar

**Fast, local semantic file search for Windows.**

Find any file by *what it contains*, not what it's named. Powered by AI embeddings, entirely on-device — no cloud, no API costs.

---

## Features

- 🔍 **Semantic search** — search by meaning, not just keywords
- 📄 **All file types** — PDFs, DOCX, TXT, code files, images (OCR)
- 🏠 **100% local** — everything runs on your device, no data leaves your machine
- ⚡ **Fast** — sub-second search across thousands of files
- 🪟 **Floating UI** — summon with `Ctrl+Space`, dismiss with `Esc`
- 👁 **Live watcher** — new and changed files are indexed automatically
- 💻 **CLI + GUI** — powerful command line and beautiful desktop interface

## Quick Start

```bash
pip install haydar
haydar init
haydar search "quarterly budget report"
```

## Installation

### Requirements

- Python 3.10+
- Windows 10/11

### Install from PyPI

```bash
pip install haydar
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
1. Ask you which folders to index (defaults: Documents, Desktop, Downloads)
2. Download the embedding model (~80MB, first run only, requires internet)
3. Index all supported files in your selected folders

### Search

**From the terminal:**
```bash
haydar search "machine learning research notes"
haydar search "invoice from march" --limit 5
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
5. **Search** — Your query is embedded and matched against all chunks using hybrid semantic + keyword search

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

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.10+ |
| Embeddings | sentence-transformers (`all-MiniLM-L6-v2`) |
| Vector DB | ChromaDB (fully local) |
| Text extraction | pypdf, python-docx, chardet |
| OCR | pytesseract (optional) |
| File watching | watchdog |
| Desktop UI | PySide6 |
| Global hotkey | pynput |
| CLI | Typer + Rich |

## License

MIT — see [LICENSE](./LICENSE).

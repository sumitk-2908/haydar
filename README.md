# ⚡ Haydar

**Fast, local semantic file search for Windows.**

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Find any file by *what it contains*, not what it's named. Powered by AI embeddings, entirely on-device no cloud, no API costs.

---

## Features

- 🔍 **Semantic search** — search by meaning, not just keywords, with an optional exact-match keyword mode
- 📄 **All file types** — PDFs, DOCX, TXT, code files, images (OCR)
- 🏠 **100% local** — everything runs on your device, no data leaves your machine
- ⚡ **Fast** — sub-second search across thousands of files
- 🪟 **Floating UI** — summon with `Ctrl+Space`, dismiss with `Esc`
- 👁 **Live watcher** — new and changed files are indexed automatically
- 🧰 **Optional CLI** — `haydar-cli.exe` for automation and scripting

## Quick Start

1. Download **`haydar.exe`** from the [latest release](https://github.com/sumitk-2908/haydar/releases).
2. Verify it (see [Verify your download](#verify-your-download)).
3. Run it.

That's all. Haydar sets itself up on first launch — no command line, no
configuration file, no separate install step.

## What happens on first launch

- **Your Documents folder is selected.** Nothing else is indexed until you say
  so. Desktop, Downloads, whole drives, and network shares are opt-in, and
  Haydar checks their size and warns you before adding one.
- **Search opens as soon as it works**, not when indexing finishes. Preparing
  search means downloading the embedding model (about 80 MB, once) and getting
  keyword search ready — usually well under a minute.
- **Results improve while you use it.** Indexing continues in the background and
  every batch of files becomes searchable the moment it is saved. A partial
  index is a working index. The search window opens **before indexing
  completes**, and covers everything committed so far.
- **You stay in control.** Pause or cancel indexing at any time; search keeps
  working over whatever is already indexed. If it fails, or your PC restarts
  mid-index, Haydar resumes from where it stopped and never re-does finished
  work.

## Installation

### Download and run (recommended)

Prebuilt executables are attached to each [GitHub Release](https://github.com/sumitk-2908/haydar/releases):

- **`haydar.exe`** — the application. This is the one to download.
- `haydar-cli.exe` — optional command-line interface for automation and scripting.

Nothing needs to be installed and neither file needs the other.

> **Note:** The EXEs are not code-signed yet, so Windows SmartScreen may show a
> "Windows protected your PC" warning on first launch. Click **More info →
> Run anyway**. Verifying the SHA-256 checksum confirms the download is intact.

### Verify your download

Each EXE ships with a matching `.sha256` file. A ready-to-run check is in
[`verify.ps1`](./verify.ps1):

```powershell
.\verify.ps1                        # verifies .\haydar.exe
```

`OK` means the download is intact. To check by hand:

```powershell
(Get-FileHash haydar.exe -Algorithm SHA256).Hash.ToLower() -eq
    (Get-Content haydar.exe.sha256).Split(' ')[0].ToLower()
```

### PowerShell installer (optional)

[`install.ps1`](./install.ps1) downloads and verifies all release assets before
replacing an installation, rolls back a failed commit, installs to
`%LOCALAPPDATA%\Haydar\`, and adds it to your PATH. Existing installations
require explicit confirmation; use `-Yes` only for intentional automation. Your
indexed data and settings in `%USERPROFILE%\.haydar` are always preserved across
upgrades.

### Uninstall

Download [`uninstall.ps1`](https://github.com/sumitk-2908/haydar/releases/latest/download/uninstall.ps1) from the latest release and run it:

```powershell
.\uninstall.ps1
```

By default this removes the executables, checksum files, autostart script, and
exact Haydar PATH entries, but **preserves your index and configuration** in
`~/.haydar/`. The script prompts before changing anything. Noninteractive use
requires `-Yes`; `-KeepData` explicitly preserves data and takes precedence over
`-RemoveData`. To also delete the index and settings:

```powershell
.\uninstall.ps1 -RemoveData
```

If an executable is locked, close Haydar and its watcher, then rerun the script.
Unrelated files in the installation directory and unrelated or empty PATH
entries are preserved.

### Install from source

For development, or to run Haydar with Python:

```powershell
git clone https://github.com/sumitk-2908/haydar.git
cd haydar
pip install -e .[dev,ocr]
python scripts/pull-rg.py   # fetch + verify ripgrep
```

## Searching

Press `Ctrl+Space` anywhere to summon the search window, `Esc` to dismiss it.

Type a concept rather than a filename — "quarterly budget projections" finds the
right spreadsheet even if none of those words are in its name. Switch to keyword
mode for exact-text matches. Arrow keys select a result; `Enter` opens it.

Folders, hotkey, and other options live in the Settings window.

### Image text search (OCR)

Searching inside images is optional. Haydar cannot install the text-recognition
engine for you in this build — no engine distribution currently meets its
licensing and verification bar — but **it will use one you install yourself**.
See [KNOWN_GAPS.md](./KNOWN_GAPS.md) for the reasoning and
[THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) for the licence detail.

Until then, images are still remembered as they are found, so nothing is lost and
you never need to reindex.

#### Enabling it

Install **Tesseract OCR for Windows, version 4 or newer, with English language
data**. The installer offered by the Tesseract project's own documentation is the
one to use; accept the default location and leave the English language data
selected. Tesseract is the only thing you need — Haydar bundles everything else.

Then restart Haydar. It finds the engine automatically, checking the standard
install locations under `Program Files` and `%LOCALAPPDATA%\Programs`, so there
is nothing to configure and no path to set.

Images found before then are never lost and never need a reindex: they stay
queued, so the next indexing pass reads them. To pick them up straight away, use
the **Install OCR** action shown on the indexing status band while images are
waiting — with the engine already installed it skips any download and starts that
catch-up immediately. New and changed images are read automatically from then on.

Recognition always runs on your computer; your images are never uploaded.

If image results still do not appear, `haydar-cli.exe ocr status` reports exactly
what is missing.

## Command line (optional)

`haydar-cli.exe` is an expert interface over the same engine. It is not required
for setup, recovery, OCR, or anything else the application does.

```powershell
haydar-cli.exe status                      # index and readiness status
haydar-cli.exe search "invoice from march" # search from a terminal
haydar-cli.exe search "TODO" --mode keyword
haydar-cli.exe config --add-folder C:\Projects
haydar-cli.exe reindex                     # force a full re-index
haydar-cli.exe ocr status                  # OCR readiness
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
| Images (OCR) | `.png`, `.jpg`, `.jpeg`, `.tiff` *(needs the optional text-recognition engine)* |

## Configuration

Settings are edited in the Settings window. They are stored at
`~/.haydar/config.json` if you prefer to edit them directly.

| Setting | Default | Description |
|---------|---------|-------------|
| `embedding_model` | `all-MiniLM-L6-v2` | Sentence-transformers model |
| `hotkey` | `<ctrl>+<space>` | Global hotkey for search UI |
| `chunk_size` | `500` | Words per chunk |
| `chunk_overlap` | `50` | Overlap between chunks |
| Size limits | 10MB text, 100MB docs | Per-type file size limits |

## Troubleshooting

- **Setup could not download the model:** Connect to the internet and launch
  Haydar again; it retries automatically and keeps whatever it already verified.
- **Indexing stopped:** The status band in the search window shows why and
  offers Retry or Resume. Search keeps working over the files already indexed.
- **Empty search results:** Confirm your folders in Settings. If the index looks
  wrong, use **Rebuild index** in Settings.
- **`Ctrl+Space` does nothing:** Another application may have reserved the
  hotkey; change it in Settings.
- Diagnostic logs are at `~/.haydar/logs/haydar.log`.

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

Third-party components, and whether each is bundled or downloaded, are listed in
[THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md).

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

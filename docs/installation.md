# Installation

## System Requirements

- Windows 10 or Windows 11, 64-bit.
- 4 GB RAM minimum; 8 GB or more is recommended for larger indexes.
- Internet access on first launch, to download the embedding model and prepare keyword search.
- At least several hundred megabytes of free disk space for the model and index. Large corpora need additional space under `~/.haydar/`.

## Download and Run

Download the latest release from the [GitHub Releases page](https://github.com/sumitk-2908/haydar/releases/latest):

- **`haydar.exe`** — the Haydar application. This is the download for normal use.
- `haydar-cli.exe` — optional command-line interface for automation and scripting.
- Matching `.sha256` files — SHA-256 checksums for both executables.

Verify the download, then run `haydar.exe`. It performs first-run setup itself: nothing needs to be installed, and the command line is never required.

```powershell
.\verify.ps1                          # verifies .\haydar.exe
.\verify.ps1 -Path .\haydar-cli.exe   # only if you also downloaded the CLI
```

The verifier must print `OK`. You can also compare a hash manually:

```powershell
(Get-FileHash .\haydar.exe -Algorithm SHA256).Hash.ToLower() -eq `
    (Get-Content .\haydar.exe.sha256).Split(' ')[0].ToLower()
```

!!! warning "Unsigned executable"
    Haydar's release EXEs are currently unsigned, so Windows SmartScreen may warn on first launch. Use **More info → Run anyway** only after verifying the published SHA-256 checksum.

### PowerShell Installer

The installer is optional. It exists to place Haydar on your PATH and to make upgrades transactional. Download `install.ps1` from the same release and run it from PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\install.ps1
```

The installer downloads both executables and their checksums, verifies them before installing anything, installs to `%LOCALAPPDATA%\Haydar`, and adds that directory to the user PATH. A failed installation is rolled back. Existing installations require explicit confirmation. For intentional automation, pass `-Yes`:

```powershell
.\install.ps1 -Version v0.9.0 -Yes
```

!!! note "Upgrades keep your data"
    Installing over an existing version replaces program files only. Your configured folders, index, cache, and indexing state in `%USERPROFILE%\.haydar` are preserved.

## Verify an Installation

Launch `haydar.exe`. The search window appearing is confirmation that setup completed.

If you installed the optional CLI and want to check it, open a new PowerShell window so it receives the updated PATH:

```powershell
haydar-cli.exe --version
haydar-cli.exe status
```

## Uninstall

Download `uninstall.ps1` from the latest release and run it:

```powershell
.\uninstall.ps1
```

The default operation removes the installed executables, checksum files, autostart script, and exact Haydar PATH entries **while preserving `~/.haydar/`** — your index and configuration survive. Use `-RemoveData` only when you explicitly want to delete them:

```powershell
.\uninstall.ps1 -RemoveData
```

`-KeepData` is a compatibility alias that takes precedence over `-RemoveData`. Noninteractive runs require `-Yes`.

## Optional: Image Text Search

Searching inside images is optional, and Haydar cannot set it up for you in this build: no text-recognition engine distribution currently meets its licensing and verification bar. The reasoning is recorded in [KNOWN_GAPS.md](https://github.com/sumitk-2908/haydar/blob/master/KNOWN_GAPS.md), and the licence detail in [THIRD_PARTY_NOTICES.md](https://github.com/sumitk-2908/haydar/blob/master/THIRD_PARTY_NOTICES.md).

Everything else works normally. Images are remembered as they are found, so nothing is lost while image search is off.

### Enabling it yourself

Install the engine and Haydar picks it up on its own:

1. Install **Tesseract OCR for Windows**, version 4 or newer, using the installer linked from the [Tesseract project's documentation](https://tesseract-ocr.github.io/tessdoc/Installation.html). Keep the default install location, and keep **English** selected in the language data — Haydar reads images as English.
2. Restart Haydar.

That is the whole list. Tesseract is the only thing you need to install; Haydar ships every other piece, so there is nothing to add to your PATH and no configuration to edit.

Images found before you installed the engine are not lost and do not need a reindex: they stay queued, so the next indexing pass reads them. To pick them up straight away, use the **Install OCR** action the floating window shows while images are waiting — with the engine already present it skips any download and starts that catch-up immediately. Recognition always runs on your computer — your images are never uploaded.

!!! note "Running from source"
    A source install needs the OCR extra as well: `pip install -e ".[ocr]"`. The packaged `haydar.exe` already includes it, so Tesseract alone is enough there.

## Install from Source

A source install is useful for development or when you want to run Haydar with Python:

```powershell
git clone https://github.com/sumitk-2908/haydar.git
cd haydar
python -m pip install -e .[dev,ocr]
python scripts/pull-rg.py
```

The `ocr` extra installs the Python OCR adapter, which the release executables already bundle.

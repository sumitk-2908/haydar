# Third-Party Notices

Haydar is MIT-licensed (see [LICENSE](./LICENSE)). It redistributes and downloads
third-party components, listed here with their upstream source, licence, and —
most importantly — **how they reach your computer**:

| Disposition | Meaning |
|---|---|
| **Bundled** | Redistributed inside the Haydar release. You receive it when you download `haydar.exe`. |
| **Provisioned** | Not redistributed. Haydar downloads it to your own machine, under `%USERPROFILE%\.haydar\`, verifying it before use. |

The split matters legally and practically: bundled components are things Haydar
distributes and must carry notices for, while provisioned components are fetched
by you, from their own publisher, at their own URL.

Haydar never uploads your files. Network access is used only for the one-time
downloads described below and for optional release-version checks.

---

## Summary

| Component | Disposition | Licence |
|---|---|---|
| ripgrep 14.1.0 | Bundled in `haydar.exe`; Provisioned for pip/source installs | MIT OR Unlicense |
| Native Tesseract OCR engine | **Not bundled. Provisioning currently unavailable** (see below) | Apache-2.0 (plus dependencies) |
| tessdata `eng.traineddata` | Provisioned (with the engine, when available) | Apache-2.0 |
| pytesseract | Bundled | Apache-2.0 |
| Pillow | Bundled | MIT-CMU |
| `all-MiniLM-L6-v2` embedding model | Provisioned on first run | Apache-2.0 |
| Python runtime and packaged dependencies | Bundled | See *Packaged dependencies* |

---

## ripgrep

**Bundled in the release executables; Provisioned for pip and source installs.**

Keyword search shells out to ripgrep. The binary is not committed to the Haydar
source tree. For a release build, CI fetches and verifies it before PyInstaller
runs, so `haydar.exe` and `haydar-cli.exe` **redistribute** it. For a pip or
source install, Haydar downloads it to `%USERPROFILE%\.haydar\bin\` on first run.

Both paths use the same pinned version and SHA-256 verification
(`src/haydar/ripgrep.py`); an unknown or mismatched checksum is a hard failure,
because the binary is executed.

- Version: **14.1.0**
- Upstream project: https://github.com/BurntSushi/ripgrep
- Release: https://github.com/BurntSushi/ripgrep/releases/tag/14.1.0
- Licence: MIT OR Unlicense —
  https://github.com/BurntSushi/ripgrep/blob/master/LICENSE-MIT and
  https://github.com/BurntSushi/ripgrep/blob/master/UNLICENSE

Windows x86-64 archive `ripgrep-14.1.0-x86_64-pc-windows-msvc.zip`, SHA-256
`fe4f75edfaa50f0d4fecbf47696b7629f3449c9c2c5a4da828753139e5a2e203`.

---

## Native Tesseract OCR engine

**Not bundled. Automatic provisioning is currently unavailable pending a
licensing decision.**

Image text search needs a native OCR engine. Haydar is designed to install one
privately under `%USERPROFILE%\.haydar\ocr\versions\`, verified by pinned
SHA-256 and activated atomically — it is never bundled into the release, because
§15 of the product contract forbids redistributing an unreviewed native OCR
archive.

A licensing and integrity review completed on **2026-08-10** found no Windows
Tesseract distribution that currently qualifies, so the shipped manifest carries
no pinned URL or hash and one-click installation **fails closed** rather than
downloading something unverified. This is the recorded outcome of that review,
not an outstanding task. The findings:

- Upstream `tesseract-ocr/tesseract` publishes exactly one Windows artifact per
  release — an NSIS `.exe` installer. No portable zip exists from any
  authoritative publisher; the "portable zip" repositories on GitHub are
  third-party repacks of older versions.
- That installer bundles **pango**, which MSYS2 records as **LGPL-2.1** (pulling
  glib2 and cairo with it), while shipping only Tesseract's own `LICENSE`,
  `AUTHORS`, and `README`. Redistributing it verbatim would convey LGPL
  components without their required notices.
- The installer fetches language data from a `tessdata_fast` **branch** URL,
  which cannot be pinned to fixed bytes.
- conda-forge's `tesseract-5.5.3` package is a genuine archive with a published
  SHA-256 and permissive dependencies, but ships only `tesseract.exe` and its own
  DLL; leptonica, libarchive, libcurl, libtiff, and the VC runtime are separate
  packages. Supporting it requires a multi-artifact manifest and a zstd reader.

Consequences for users: image text search is unavailable until a distribution is
chosen. Haydar records images it could not read and indexes them automatically
once OCR becomes available — no manual step and no reindex is required. Nothing
about this asks you to install anything yourself.

- Upstream project: https://github.com/tesseract-ocr/tesseract
- Licence: Apache-2.0 —
  https://github.com/tesseract-ocr/tesseract/blob/main/LICENSE

When an engine is provisioned, its upstream licence files are verified to be
present before activation and are retained inside the activated version
directory, alongside the executable, for as long as that version is installed.

---

## tessdata (`eng.traineddata`)

**Provisioned alongside the OCR engine, when one is available.**

English language data for Tesseract. Reviewed and hash-pinned on 2026-08-10
(recorded in `src/haydar/ocr.py`), and cleanly redistributable on its own — but
it is only useful with an engine, so it is downloaded with one rather than
separately.

- Upstream project: https://github.com/tesseract-ocr/tessdata_fast
- Commit-pinned file:
  `https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/eng.traineddata`
- SHA-256: `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2`
- Licence: Apache-2.0 —
  https://github.com/tesseract-ocr/tessdata_fast/blob/main/LICENSE

---

## pytesseract

**Bundled.**

The Python adapter that drives the native OCR engine. It ships inside
`haydar.exe` so that image support is a matter of provisioning the engine, never
of installing a Python package.

- Upstream project: https://github.com/madmaze/pytesseract
- Licence: Apache-2.0 —
  https://github.com/madmaze/pytesseract/blob/master/LICENSE

---

## Pillow

**Bundled.**

Image decoding for OCR, bundled with the adapter above.

- Upstream project: https://github.com/python-pillow/Pillow
- Licence: MIT-CMU —
  https://github.com/python-pillow/Pillow/blob/main/LICENSE

---

## Embedding model (`all-MiniLM-L6-v2`)

**Provisioned on first run.**

Semantic search embeds text locally with a sentence-transformers model. The model
(about 80 MB) is downloaded once, during first-run setup, into
`%USERPROFILE%\.haydar\models\`. It is not redistributed in the executable. After
that download, embedding and search run entirely offline; queries and file
contents are never sent anywhere.

- Model: `sentence-transformers/all-MiniLM-L6-v2`
- Source: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
- Licence: Apache-2.0

---

## Packaged dependencies

**Bundled.**

The release executables are built with PyInstaller and therefore redistribute the
Python runtime and Haydar's Python dependencies. The authoritative list, with
exact versions, is `requirements-lock.txt` and the `dependencies` table in
`pyproject.toml`; the principal components are:

| Component | Purpose | Licence |
|---|---|---|
| CPython | Language runtime | PSF-2.0 |
| PySide6 / Qt | Desktop user interface | LGPL-3.0 |
| ChromaDB | Local vector database | Apache-2.0 |
| sentence-transformers | Embedding pipeline | Apache-2.0 |
| PyTorch | Model execution backend | BSD-3-Clause |
| Hugging Face `tokenizers`, `huggingface_hub` | Tokenization and model resolution | Apache-2.0 |
| onnxruntime | Model execution backend | MIT |
| pypdf | PDF text extraction | BSD-3-Clause |
| python-docx | DOCX text extraction | MIT |
| chardet | Character-encoding detection | LGPL-2.1 |
| watchdog | File-system watching | Apache-2.0 |
| pynput | Global hotkey | LGPL-3.0 |
| Typer, Click, Rich | Command-line interface | MIT |
| packaging | Version parsing | Apache-2.0 OR BSD-2-Clause |

### LGPL components

PySide6/Qt, pynput, and chardet are LGPL-licensed and are redistributed as
unmodified library code. Their licences are available from their upstream
projects, and their sources can be obtained from PyPI and from the upstream
repositories linked in `pyproject.toml`.

---

## Reporting an omission

If a component is missing from this file or is described incorrectly, please open
an issue at https://github.com/sumitk-2908/haydar/issues.

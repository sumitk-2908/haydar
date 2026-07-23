# Contributing to Haydar

Thanks for your interest in improving Haydar. This guide covers local setup, the
one architectural rule that matters, and what a good PR looks like.

## Development setup

Haydar is Windows-only and targets Python 3.11+.

```powershell
git clone https://github.com/haydar-search/haydar.git
cd haydar
pip install -e .[dev,ocr]        # dev + OCR extras
python scripts/pull-rg.py        # fetch + verify the ripgrep binary
```

The OCR extra pulls in `pytesseract`; searching inside images also needs the
[Tesseract OCR engine](https://github.com/UB-Mannheim/tesseract/wiki) on PATH.

## Architecture boundary (enforced)

Haydar has two layers:

- **UI** — `src/haydar/ui/` (PySide6 frontend)
- **Backend** — `src/haydar/indexer/` + `src/haydar/search/`

**UI modules may import only from `src/haydar/search/` (the interface layer) or
`src/haydar/config.py` — never from `indexer/` internals.** This keeps the
backend replaceable without touching the UI. `cli.py` sits above everything and
is exempt.

This is the most non-obvious constraint in the codebase. Verify it stays clean:

```powershell
# Must return nothing:
Select-String -Path src/haydar/ui/*.py -Pattern "from haydar.indexer"
```

Other invariants worth preserving (see `CLAUDE.md` / `.agents/AGENTS.md`):

- `cli.py` lazy-imports heavy subsystems (PySide6, ChromaDB, sentence-transformers)
  inside command bodies — don't hoist them to module top level.
- `HybridSearch.store` and `IndexingEngine.store` are lazy properties — constructing
  them must not force a model load.
- `VectorStore.__init__` raises `VectorStoreError`, never `sys.exit()`.
- Never bypass the ripgrep SHA-256 verification in `ripgrep.py` — the binary is executed.
- Bumping chunking/schema requires incrementing `CURRENT_SCHEMA_VERSION` in `config.py`.

## Before you open a PR

Run the full local gate — CI runs the same checks:

```powershell
ruff check src/ tests/           # lint
mypy src/haydar                  # type-check (non-strict)
pytest tests/ -q --cov=haydar --cov-report=term-missing   # tests + coverage
```

PR checklist:

- [ ] `ruff`, `mypy`, and `pytest` all pass locally.
- [ ] New behavior has tests; coverage does not drop below the CI floor.
- [ ] The architecture-boundary check above is clean.
- [ ] `CHANGELOG.md` updated under `[Unreleased]`.
- [ ] Windows-only APIs (`os.startfile`, `subprocess.CREATE_NO_WINDOW`,
      `msvcrt.locking`, autostart `.bat`) are guarded if you touch cross-platform code.

## Reporting bugs

Open a GitHub issue with your OS version, Python version, the command you ran,
and the relevant lines from `~/.haydar/logs/haydar.log`. For security issues,
see [SECURITY.md](./SECURITY.md) instead of filing a public issue.

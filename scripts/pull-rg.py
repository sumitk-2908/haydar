"""
Build-time helper: download and verify the ripgrep binary into
``src/haydar/bin/`` so it can be bundled into the PyInstaller EXE.

This is a thin wrapper around ``haydar.ripgrep.ensure_ripgrep`` so the download,
SHA-256 verification, and extraction logic live in one place (and are reusable
at runtime by ``haydar init`` for pip installs).

Usage:
    python scripts/pull-rg.py
"""

import argparse
import sys
from pathlib import Path

# Make the package importable when run from a source checkout without install.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from haydar.ripgrep import ensure_ripgrep, RipgrepError  # noqa: E402


def _update_gitignore(project_root: Path) -> None:
    gitignore = project_root / ".gitignore"
    entry = "src/haydar/bin/"
    if not gitignore.exists():
        return
    content = gitignore.read_text(encoding="utf-8")
    if entry not in content:
        with open(gitignore, "a", encoding="utf-8") as f:
            f.write(f"\n# Downloaded binaries\n{entry}\n")
        print("Updated .gitignore")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download the ripgrep binary.")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="(Always on) Verify SHA-256 checksum. Kept for compatibility.",
    )
    parser.parse_args()

    bin_dir = PROJECT_ROOT / "src" / "haydar" / "bin"
    cache_dir = PROJECT_ROOT / "build"

    try:
        path = ensure_ripgrep(bin_dir, cache_dir=cache_dir)
    except RipgrepError as exc:
        print(f"Failed to install ripgrep: {exc}")
        sys.exit(1)

    print(f"ripgrep ready at {path}")
    _update_gitignore(PROJECT_ROOT)


if __name__ == "__main__":
    main()

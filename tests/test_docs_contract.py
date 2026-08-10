"""User-facing documentation contract (§15 documentation, §19).

§19 rejects any change that directs a normal user to `haydar-cli.exe`, pip, PATH
edits, or a manual OCR installation. That is the easiest rule in the whole
contract to regress, because it regresses by someone writing a helpful sentence.
These tests read the shipped docs and fail on the specific instructions the
contract forbids.

Engineering records are deliberately out of scope: `implementation-plan.md` and
the delivery handoff describe superseded designs, and CHANGELOG entries describe
releases as they shipped. None of them instruct a current user.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── user-facing documentation (§15 documentation, §19) ────────────────────────

# Live user copy. `implementation-plan.md` and the delivery handoff are
# engineering records of superseded designs, and CHANGELOG entries describe
# releases as they shipped; none of them instruct a current user.
USER_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "index.md",
    REPO_ROOT / "docs" / "installation.md",
    REPO_ROOT / "docs" / "quick-start.md",
    REPO_ROOT / "docs" / "configuration.md",
    REPO_ROOT / "docs" / "architecture.md",
    REPO_ROOT / "docs" / "performance.md",
)


@pytest.mark.parametrize("doc", USER_DOCS, ids=lambda p: p.name)
def test_no_user_doc_makes_the_cli_the_normal_path(doc):
    """§19: never direct a normal user to ``haydar-cli.exe``, pip, or a package
    manager.

    §19 was amended 2026-08-11 to allow documenting a manual Tesseract install,
    because one-click provisioning ships disabled and "unavailable" alone left
    users with no route to a feature that does work. The amendment is narrow:
    naming the engine and pointing at its own installer is permitted, while
    package-manager invocations, third-party mirrors, and PATH edits stay
    forbidden — those are the instructions that rot and that a normal user
    should never have to run.

    The forbidden strings are the specific instructions the contract names, not
    every mention of the CLI — documenting it as an optional expert tool is
    explicitly allowed.
    """
    text = doc.read_text(encoding="utf-8")
    for forbidden in (
        "haydar init",
        "haydar-cli.exe init",
        "pip install haydar[ocr]",
        "UB-Mannheim",
        "winget",
        "choco install",
        "scoop install",
        "setx",
        "Edit the PATH",
        "add it to your PATH",
    ):
        assert forbidden not in text, f"{doc.name} still tells users to: {forbidden}"


def test_the_readme_leads_with_downloading_and_launching_the_app():
    """§15: download and launch ``haydar.exe`` is the documented normal path."""
    text = REPO_ROOT / "README.md"
    body = text.read_text(encoding="utf-8")
    quick_start = body.index("## Quick Start")
    section = body[quick_start : body.index("##", quick_start + 5)]
    assert "haydar.exe" in section
    assert "haydar-cli.exe" not in section


@pytest.mark.parametrize(
    "promise",
    [
        "Documents",          # first run defaults to Documents only
        "before indexing",    # search opens before indexing completes
        "background",         # indexing continues in the background
    ],
)
def test_the_readme_states_the_first_run_contract(promise):
    assert promise in (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_the_docs_describe_ocr_as_optional_and_local():
    """§15: OCR is optional and local, and the docs match the shipped state.

    §15 also describes it as one-click, which is what the provisioner
    implements — but no reviewed engine exists (see `KNOWN_GAPS.md`), so the
    docs describe installing the engine directly instead (§19 amended
    2026-08-11). What §15 requires unconditionally, and what these assertions
    hold to, is that image search is optional, that nothing leaves the machine,
    and that no reindex is needed.
    """
    text = (REPO_ROOT / "docs" / "quick-start.md").read_text(encoding="utf-8")

    assert "not available in this build" in text
    assert "never uploaded" in text
    assert "reindex" in text


def test_no_doc_advertises_a_one_click_install_that_cannot_succeed():
    """The manifest is unreviewed, so "choose Install OCR" would be a false promise."""
    from haydar.ocr import OCR_ASSETS

    if any(asset.is_reviewed for asset in OCR_ASSETS):
        pytest.skip("a reviewed asset exists; one-click copy is accurate again")

    for doc in USER_DOCS:
        text = doc.read_text(encoding="utf-8")
        assert "choose **Install OCR**" not in text, doc.name
        assert "installed with one click" not in text, doc.name


def test_docs_links_point_at_the_repository_default_branch():
    """``blob/main`` 404s on a repository whose default branch is ``master``."""
    for path in (*USER_DOCS, REPO_ROOT / "mkdocs.yml"):
        text = path.read_text(encoding="utf-8")
        assert "sumitk-2908/haydar/blob/main/" not in text, path.name

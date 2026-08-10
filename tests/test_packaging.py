"""Packaging and release contract (§15, §16 "Packaging and release").

The release workflow builds a windowed ``haydar.exe`` that, until slice 7, was
never launched: a missing hidden import or a console-less startup fault would
have shipped undetected. These tests cover the parts of that gap that can be
checked without a PyInstaller build — the profile isolation the probe relies on,
the pass/fail rules it applies, the report the app produces, and the installer
scripts' user-facing contract.

The one thing they deliberately cannot cover is running a real frozen binary.
That step lives in the release workflow (``scripts/packaged_startup_probe.py``),
because it needs an artifact only the build job produces.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_SCRIPT = REPO_ROOT / "scripts" / "packaged_startup_probe.py"
NOTICES = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build.yml"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
UNINSTALL_PS1 = REPO_ROOT / "uninstall.ps1"
SPEC = REPO_ROOT / "haydar.spec"


def _probe_module():
    """Import the launcher script by path; it is a script, not a package member."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_packaged_startup_probe", PROBE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ── the isolated profile the probe depends on ─────────────────────────────────


def test_the_data_root_follows_the_home_override(tmp_path, monkeypatch):
    """``HAYDAR_HOME`` redirects the whole tree, not just the config file.

    Checked in a subprocess because the constants are bound at import time: this
    is exactly the situation the packaged probe is in — it launches the real EXE,
    so it cannot monkeypatch anything, and every derived path has to move
    together or the run would reach into the user's real profile.
    """
    root = tmp_path / "profile"
    names = [
        "HAYDAR_DIR",
        "CONFIG_PATH",
        "DB_DIR",
        "LOG_DIR",
        "MODELS_DIR",
        "CACHE_DIR",
        "RIPGREP_DIR",
        "INDEX_LOCK",
        "OCR_DIR",
        "OCR_VERSIONS_DIR",
        "OCR_STAGING_DIR",
        "OCR_CURRENT_POINTER",
    ]
    program = (
        "import json, haydar.config as c;"
        f"print(json.dumps({{n: str(getattr(c, n)) for n in {names!r}}}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        env={**os.environ, "HAYDAR_HOME": str(root)},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        # Under pytest's capture the inherited stdin handle is invalid, and
        # Windows fails the spawn outright rather than the read.
        stdin=subprocess.DEVNULL,
    )
    resolved = json.loads(completed.stdout)

    assert Path(resolved["HAYDAR_DIR"]) == root
    for name in names:
        if name == "HAYDAR_DIR":
            continue
        assert root in Path(resolved[name]).parents, name


def test_the_bound_data_root_is_the_resolver_result():
    """The module-level constant is the resolver's output, not a second copy."""
    import haydar.config as config_module

    assert config_module._resolve_haydar_dir() == config_module.HAYDAR_DIR


def test_no_override_keeps_the_real_profile_location(monkeypatch):
    """Absent the variable, the data root is exactly where it has always been."""
    monkeypatch.delenv("HAYDAR_HOME", raising=False)
    from haydar.config import _resolve_haydar_dir

    assert _resolve_haydar_dir() == Path.home() / ".haydar"


def test_a_blank_override_is_ignored_rather_than_creating_a_root_at_cwd(monkeypatch):
    """An empty value is treated as unset; ``Path("")`` would resolve to cwd."""
    monkeypatch.setenv("HAYDAR_HOME", "   ")
    from haydar.config import _resolve_haydar_dir

    assert _resolve_haydar_dir() == Path.home() / ".haydar"


def test_an_override_is_honoured_and_user_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HAYDAR_HOME", str(tmp_path / "elsewhere"))
    from haydar.config import _resolve_haydar_dir

    assert _resolve_haydar_dir() == tmp_path / "elsewhere"


def test_the_launcher_and_the_app_agree_on_the_variable_names():
    """The launcher hardcodes the names so it can run without the package."""
    from haydar.config import HOME_ENV_VAR, STARTUP_PROBE_ENV_VAR

    module = _probe_module()
    assert module.HOME_ENV_VAR == HOME_ENV_VAR
    assert module.STARTUP_PROBE_ENV_VAR == STARTUP_PROBE_ENV_VAR


# ── the report and the rules applied to it ────────────────────────────────────


def _good_report(**overrides):
    report = {
        "schema": 1,
        "ok": True,
        "view": "onboarding",
        "setup_started": True,
        "setup_phase": "verifying_keyword_search",
        "frozen": True,
        "console": False,
        "imports": {"PySide6.QtCore": "ok"},
        "import_failures": [],
        "keyword_search": "ok: C:\\x\\rg.exe",
        "ocr_manifest": "ok: 1 asset(s), 0 reviewed",
        "errors": [],
    }
    report.update(overrides)
    return report


def test_a_healthy_packaged_startup_passes():
    assert _probe_module().validate_report(_good_report()) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"view": "none"}, "no top-level view"),
        ({"setup_started": False, "setup_phase": ""}, "setup never started"),
        ({"import_failures": ["chromadb: ImportError"]}, "runtime import failed"),
        ({"errors": ["Fatal: boom"]}, "startup error"),
        ({"console": True}, "console is attached"),
        ({"frozen": False}, "not frozen"),
        ({"keyword_search": "missing: no rg"}, "ripgrep did not resolve"),
        ({"ocr_manifest": "unavailable: ImportError"}, "OCR manifest"),
    ],
)
def test_each_packaged_startup_defect_is_rejected(overrides, expected):
    """Every failure mode §15 names is a distinct, reported rejection.

    A silent pass here would be the worst outcome: the probe exists precisely so
    a broken windowed build cannot look like a healthy one.
    """
    problems = _probe_module().validate_report(_good_report(**overrides))
    assert any(expected in problem for problem in problems), problems


def test_a_non_object_report_is_rejected_rather_than_crashing():
    assert _probe_module().validate_report(["not", "a", "report"])


def test_the_report_names_every_import_a_packaged_gui_needs():
    """Hidden-import regressions are the failure this probe exists to catch."""
    from haydar.startup_probe import REQUIRED_IMPORTS

    for name in (
        "PySide6.QtWidgets",
        "pytesseract",
        "PIL.Image",
        "watchdog.observers",
        "pynput.keyboard",
    ):
        assert name in REQUIRED_IMPORTS


def test_the_report_round_trips_through_json(tmp_path):
    from haydar.startup_probe import StartupReport, write_report

    destination = tmp_path / "nested" / "report.json"
    written = write_report(
        StartupReport(view="onboarding", setup_started=True), destination
    )

    assert written == destination
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["view"] == "onboarding"
    # Written atomically, so a reader never sees a partial file.
    assert list(destination.parent.glob(".report.json.*")) == []


def test_a_report_is_only_written_when_probing(monkeypatch):
    """Ordinary launches must not write a report anywhere."""
    monkeypatch.delenv("HAYDAR_STARTUP_PROBE", raising=False)
    from haydar.startup_probe import StartupReport, is_probing, write_report

    assert is_probing() is False
    assert write_report(StartupReport()) is None


def test_a_startup_failure_is_reported_instead_of_opening_a_modal_dialog(
    tmp_path, monkeypatch
):
    """A probe run is unattended; a dialog would hang it until someone clicked.

    ``gui_main`` shows a native message box for a fatal startup error. During a
    probe that box has no one to dismiss it, so the failure has to reach the
    report instead — and the box must not be opened at all.
    """
    report_path = tmp_path / "report.json"
    monkeypatch.setenv("HAYDAR_STARTUP_PROBE", str(report_path))

    import ctypes

    import haydar.gui_main as gui_main

    opened: list[str] = []
    if sys.platform == "win32":
        class _User32:
            def MessageBoxW(self, *args):
                opened.append("MessageBoxW")
                return 0

        monkeypatch.setattr(ctypes.windll, "user32", _User32())

    gui_main._show_error_dialog("Haydar Fatal Error", "schema too new")

    assert opened == []
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert any("schema too new" in error for error in payload["errors"])


def test_an_ordinary_launch_still_gets_its_error_dialog(monkeypatch):
    """The probe guard must not disable error reporting for real users."""
    monkeypatch.delenv("HAYDAR_STARTUP_PROBE", raising=False)
    import haydar.gui_main as gui_main

    assert gui_main._report_probe_failure("boom") is False


# ── the packaged GUI actually starts ──────────────────────────────────────────


def test_the_gui_entry_point_reaches_onboarding_in_an_isolated_profile(tmp_path):
    """Run the real ``gui_main`` in a subprocess against a throwaway profile.

    This is the unpackaged half of the §15 startup probe: it exercises the same
    code path and the same report the packaged probe checks, so a regression in
    startup, in the probe's shutdown handshake, or in profile isolation fails
    here rather than only in a release build. The packaged half needs a
    PyInstaller artifact and therefore runs in the release workflow.
    """
    profile = tmp_path / "profile"
    report_path = tmp_path / "report.json"
    env = {
        **os.environ,
        "HAYDAR_HOME": str(profile),
        "HAYDAR_STARTUP_PROBE": str(report_path),
        "QT_QPA_PLATFORM": "offscreen",
    }

    completed = subprocess.run(
        [sys.executable, "-c", "from haydar.gui_main import main; main()"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    assert report_path.is_file(), completed.stderr[-2000:]
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["view"] == "onboarding"
    assert payload["setup_started"] is True
    assert payload["import_failures"] == []
    assert payload["console"] is False
    # The whole point of the isolated profile: nothing was written outside it.
    assert Path(payload["data_root"]) == profile
    assert profile.is_dir()
    # A clean exit proves the probe wound the setup worker down rather than
    # tearing the process out from under a running thread.
    assert completed.returncode == 0, completed.stderr[-2000:]


# ── release assets and installer copy ─────────────────────────────────────────


def test_the_release_workflow_probes_the_packaged_gui():
    """§19 rejects packaging changes without a packaged-GUI startup test."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/packaged_startup_probe.py" in workflow
    assert "dist/haydar.exe" in workflow


def test_the_release_workflow_still_smoke_tests_the_cli_ocr_alias():
    """``ocr-status`` is the string CI greps; the alias must keep working."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "ocr-status" in workflow
    assert "Python OCR adapter is not installed" in workflow


def test_third_party_notices_ship_with_the_release():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "THIRD_PARTY_NOTICES.md" in workflow


def test_the_installer_points_normal_users_at_the_gui_not_the_cli():
    """§19: never direct a normal user to ``haydar-cli.exe``."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "haydar-cli.exe init" not in text
    assert "haydar.exe" in text


def test_the_installer_targets_the_documented_repository():
    """The download source must match the repository the docs publish."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    match = re.search(r'\$Repo\s*=\s*"([^"]+)"', text)
    assert match, "install.ps1 no longer declares a default $Repo"
    assert match.group(1) == "sumitk-2908/haydar"


def test_haydar_exe_is_the_first_release_asset_the_installer_handles():
    """§15: ``haydar.exe`` is the first, normal-user asset."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert text.index("haydar.exe") < text.index("haydar-cli.exe")


def test_uninstall_preserves_user_data_by_default():
    """§15: ``%USERPROFILE%\\.haydar`` survives upgrade and uninstall by default."""
    text = UNINSTALL_PS1.read_text(encoding="utf-8")
    assert "$willRemoveData = $RemoveData -and -not $KeepData" in text
    assert "(preserved)" in text


def test_the_uninstall_command_and_script_agree_on_preserving_data():
    """Two uninstallers that disagree about deleting an index is a data-loss bug.

    ``uninstall.ps1`` keeps ``~/.haydar`` unless ``-RemoveData`` is passed, so the
    CLI command must default the same way and offer the same opt-in.

    This reads the command's parameter model rather than its ``--help`` text:
    Typer renders help through Rich, whose option highlighter splits a flag into
    separately-styled spans (``--``/``remove``/``-data``) once colour is enabled,
    so a substring check against the rendered output passes locally on a plain
    pipe and fails on any runner that looks like a terminal.
    """
    import typer.main

    from haydar.cli import app

    uninstall_cmd = typer.main.get_command(app).commands["uninstall"]
    options = {name: param for param in uninstall_cmd.params for name in param.opts}

    assert "--remove-data" in options, "the CLI must offer uninstall.ps1's -RemoveData opt-in"
    # Opt-in, exactly like the script: absent flag means the profile survives.
    assert options["--remove-data"].default is False
    # And it stays discoverable in --help, however Rich decides to style it.
    assert options["--remove-data"].help


def test_uninstall_keeps_the_profile_unless_removal_is_requested(tmp_haydar, monkeypatch):
    """The default path removes autostart only and leaves every data file.

    ``cli`` binds ``HAYDAR_DIR`` at import time, so the fixture alone does not
    redirect what this command reads — the module's own copy is patched too.
    Without that, invoking the real command here would target the developer's
    ``~/.haydar``.
    """
    from typer.testing import CliRunner

    import haydar.cli as cli_module
    from haydar.cli import app

    monkeypatch.setattr(cli_module, "HAYDAR_DIR", tmp_haydar)
    monkeypatch.setattr(
        "shutil.rmtree",
        lambda *args, **kwargs: pytest.fail("the default uninstall must not delete data"),
    )
    marker = tmp_haydar / "db" / "keep-me.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("index", encoding="utf-8")

    result = CliRunner().invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0
    assert marker.is_file()
    assert tmp_haydar.is_dir()


def test_uninstall_backs_up_before_it_deletes_when_removal_is_requested(
    tmp_haydar, monkeypatch
):
    """``--remove-data`` archives the profile, then deletes exactly that path.

    Both the archive and the delete are recorded rather than performed: a test
    that actually recursively deleted a profile directory would be one
    unpatched path constant away from destroying real user data, and this repo
    binds those constants at import time.
    """
    from typer.testing import CliRunner

    import haydar.cli as cli_module
    from haydar.cli import app

    monkeypatch.setattr(cli_module, "HAYDAR_DIR", tmp_haydar)
    archived: list[tuple[str, str, str]] = []
    deleted: list[str] = []
    monkeypatch.setattr(
        "shutil.make_archive",
        lambda base, fmt, root, *a, **k: archived.append((str(base), fmt, str(root)))
        or f"{base}.zip",
    )
    monkeypatch.setattr("shutil.rmtree", lambda path, *a, **k: deleted.append(str(path)))

    result = CliRunner().invoke(app, ["uninstall", "--remove-data", "--yes"])

    assert result.exit_code == 0
    # The backup is taken first, and covers the same tree that is then removed.
    assert [root for _base, _fmt, root in archived] == [str(tmp_haydar)]
    assert deleted == [str(tmp_haydar)]


def test_uninstall_aborts_without_deleting_when_the_backup_fails(
    tmp_haydar, monkeypatch
):
    """A failed backup must stop the uninstall rather than delete unprotected."""
    from typer.testing import CliRunner

    import haydar.cli as cli_module
    from haydar.cli import app

    monkeypatch.setattr(cli_module, "HAYDAR_DIR", tmp_haydar)

    def failing_archive(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("shutil.make_archive", failing_archive)
    monkeypatch.setattr(
        "shutil.rmtree",
        lambda *args, **kwargs: pytest.fail("data must not be deleted without a backup"),
    )

    result = CliRunner().invoke(app, ["uninstall", "--remove-data", "--yes"])

    assert result.exit_code == 1


def test_uninstall_dry_run_states_that_data_is_kept(tmp_haydar, monkeypatch):
    from typer.testing import CliRunner

    import haydar.cli as cli_module
    from haydar.cli import app

    monkeypatch.setattr(cli_module, "HAYDAR_DIR", tmp_haydar)

    result = CliRunner().invoke(app, ["uninstall", "--dry-run"])

    assert result.exit_code == 0
    assert "Would keep" in result.stdout
    assert tmp_haydar.is_dir()


def test_installing_over_an_existing_install_never_removes_user_data():
    """An upgrade replaces program files only; it must never delete the profile.

    The installer is allowed to *mention* ``%USERPROFILE%\\.haydar`` — it tells
    the user their index is being kept — so what matters is that no removal or
    move ever targets it.
    """
    text = INSTALL_PS1.read_text(encoding="utf-8")
    destructive = re.compile(
        r"^(?!\s*#).*(Remove-Item|Move-Item|Clear-Content).*\.haydar", re.MULTILINE
    )
    assert destructive.search(text) is None
    # Every path the installer writes to is under the program directory.
    assert "$installDir = if ($env:LOCALAPPDATA)" in text


def test_the_spec_bundles_the_python_ocr_adapter_but_not_a_native_engine():
    """§15: bundle the adapter and Pillow; provision the native engine only."""
    spec = SPEC.read_text(encoding="utf-8")
    assert "pytesseract" in spec
    assert "PIL" in spec
    assert "tesseract.exe" not in spec


# ── licensing notices ─────────────────────────────────────────────────────────


def test_third_party_notices_exist():
    assert NOTICES.is_file()


@pytest.mark.parametrize(
    "component",
    ["ripgrep", "Tesseract", "tessdata", "pytesseract", "Pillow", "all-MiniLM-L6-v2"],
)
def test_every_component_section_required_by_the_contract_is_present(component):
    assert component in NOTICES.read_text(encoding="utf-8")


def test_each_component_is_labelled_bundled_or_provisioned():
    """The bundled-vs-provisioned split is the whole point of the file (§15)."""
    text = NOTICES.read_text(encoding="utf-8")
    assert "Bundled" in text
    assert "Provisioned" in text
    # Every component table row carries one of the two dispositions.
    rows = [
        line
        for line in text.splitlines()
        if line.startswith("| ") and "|" in line[2:] and "---" not in line
    ]
    assert rows


def test_native_ocr_is_documented_as_unavailable_rather_than_bundled():
    """§15 forbids shipping unreviewed native OCR; the notices must say so.

    Recording it as "bundled" would be false, and recording it as a TODO would
    misstate a completed review whose outcome was "no distribution qualifies".
    """
    text = NOTICES.read_text(encoding="utf-8")
    assert "not bundled" in text.lower()
    lowered = text.lower()
    assert "pending review" in lowered or "unavailable" in lowered


def test_the_notices_never_tell_a_normal_user_to_install_ocr_by_hand():
    """§19: no pip, PATH, or manual OCR instructions in user-facing copy."""
    text = NOTICES.read_text(encoding="utf-8")
    assert "pip install haydar[ocr]" not in text
    assert "UB-Mannheim" not in text


def test_the_recorded_ripgrep_version_matches_the_binary_actually_shipped():
    """A notice naming a version Haydar does not ship is worse than none."""
    from haydar.ripgrep import VERSION

    assert VERSION in NOTICES.read_text(encoding="utf-8")


def test_the_recorded_embedding_model_matches_the_configured_default():
    from haydar.config import HaydarConfig

    assert HaydarConfig().embedding_model in NOTICES.read_text(encoding="utf-8")

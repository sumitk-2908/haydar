import json
from pathlib import Path

import pytest

from haydar.config import (
    CONFIG_FORMAT_VERSION,
    CURRENT_SCHEMA_VERSION,
    ConfigFormatError,
    HaydarConfig,
    get_rg_path,
    get_size_category,
    is_excluded,
    migrate_raw_config,
)


def test_config_defaults(tmp_path):
    config = HaydarConfig()
    assert config.embedding_model == "all-MiniLM-L6-v2"
    assert config.chunk_size == 500
    assert config.schema_version == CURRENT_SCHEMA_VERSION
    assert config.config_format_version == CONFIG_FORMAT_VERSION
    assert config.folders_configured is False
    assert config.search_ready is False
    assert config.initial_index_state == "not_started"


def test_default_folders_include_only_documents(monkeypatch, tmp_path):
    import haydar.config as config_module

    home = tmp_path / "home"
    for name in ("Documents", "Desktop", "Downloads"):
        (home / name).mkdir(parents=True)
    monkeypatch.setattr(config_module.Path, "home", lambda: home)
    # Force the portable fallback so the test does not depend on the real
    # machine's Windows known-folder location.
    monkeypatch.setattr(config_module.os, "name", "posix")

    assert config_module._default_folders() == [str(home / "Documents")]


def test_load_migrates_legacy_initialized_config(tmp_haydar, monkeypatch):
    import haydar.config as config_module

    config_path = tmp_haydar / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    folders = [r"C:\Users\Example\Documents", r"D:\Archive"]
    config_path.write_text(
        json.dumps({"initialized": True, "folders": folders}),
        encoding="utf-8",
    )

    loaded = HaydarConfig.load()

    # Folder order and contents are preserved byte for byte.
    assert loaded.folders == folders
    assert loaded.folders_configured is True
    assert loaded.search_ready is True
    assert loaded.initial_index_state == "complete"


def test_load_migrates_legacy_uninitialized_config(tmp_haydar, monkeypatch):
    import haydar.config as config_module

    config_path = tmp_haydar / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    folders = [r"C:\Users\Example\Notes"]
    config_path.write_text(
        json.dumps({"initialized": False, "folders": folders}),
        encoding="utf-8",
    )

    loaded = HaydarConfig.load()

    assert loaded.folders == folders
    assert loaded.folders_configured is True
    assert loaded.search_ready is False
    assert loaded.initial_index_state == "not_started"


def test_load_preserves_explicit_partial_state(tmp_haydar, monkeypatch):
    import haydar.config as config_module

    config_path = tmp_haydar / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    config_path.write_text(
        json.dumps({
            "initialized": True,
            "folders_configured": True,
            "search_ready": True,
            "initial_index_state": "paused",
        }),
        encoding="utf-8",
    )

    loaded = HaydarConfig.load()

    assert loaded.search_ready is True
    assert loaded.initial_index_state == "paused"


def test_partial_lifecycle_key_never_infers_complete_from_legacy_flag():
    """One explicit lifecycle key disables the legacy whole-config inference.

    A config mid-write must not be upgraded to ``complete`` just because the
    legacy mirror says ``initialized``; that would claim a finished crawl the
    user never had.
    """
    migrated = migrate_raw_config({
        "initialized": True,
        "folders": [r"C:\Docs"],
        "search_ready": True,
    })

    assert migrated["initial_index_state"] == "not_started"


def test_persisted_running_is_not_silently_mutated_on_load(tmp_haydar, monkeypatch):
    """Loading must report the truth; recovery is an auditable service transition."""
    import haydar.config as config_module

    config_path = tmp_haydar / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    config_path.write_text(
        json.dumps({
            "folders_configured": True,
            "search_ready": True,
            "initial_index_state": "running",
        }),
        encoding="utf-8",
    )

    assert HaydarConfig.load().initial_index_state == "running"


def test_migration_preserves_intentionally_empty_folder_list():
    migrated = migrate_raw_config({"initialized": True, "folders": []})

    assert migrated["folders"] == []


def test_migration_applies_defaults_only_when_folders_key_is_absent(monkeypatch):
    import haydar.config as config_module

    monkeypatch.setattr(config_module, "_default_folders", lambda: [r"C:\Documents"])

    assert migrate_raw_config({})["folders"] == [r"C:\Documents"]
    assert migrate_raw_config({"folders": []})["folders"] == []


def test_migration_is_idempotent():
    once = migrate_raw_config({"initialized": True, "folders": [r"C:\Docs"]})
    twice = migrate_raw_config(once)

    assert once == twice


def test_future_config_format_fails_closed_without_rewriting(tmp_haydar, monkeypatch):
    import haydar.config as config_module

    config_path = tmp_haydar / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    payload = json.dumps({
        "config_format_version": CONFIG_FORMAT_VERSION + 1,
        "folders": [r"C:\Docs"],
    })
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ConfigFormatError):
        HaydarConfig.load()

    # The file on disk is untouched, so the newer build still reads it.
    assert config_path.read_text(encoding="utf-8") == payload


def test_unknown_keys_survive_a_load_and_save_round_trip(tmp_haydar, monkeypatch):
    import haydar.config as config_module

    config_path = tmp_haydar / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "HAYDAR_DIR", tmp_haydar)
    config_path.write_text(
        json.dumps({"folders": [r"C:\Docs"], "future_option": {"nested": True}}),
        encoding="utf-8",
    )

    loaded = HaydarConfig.load()
    loaded.save()

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["future_option"] == {"nested": True}


def test_corrupt_config_is_preserved_rather_than_overwritten(tmp_haydar, monkeypatch):
    import haydar.config as config_module

    config_path = tmp_haydar / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    config_path.write_text("{not valid json", encoding="utf-8")

    loaded = HaydarConfig.load()

    assert loaded.search_ready is False
    backups = list(tmp_haydar.glob("config.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"


def test_save_derives_legacy_initialized_from_search_ready(tmp_haydar, monkeypatch):
    import haydar.config as config_module

    config_path = tmp_haydar / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "HAYDAR_DIR", tmp_haydar)

    config = HaydarConfig(folders=[r"C:\Docs"], search_ready=True, initialized=False)
    config.save()

    assert json.loads(config_path.read_text(encoding="utf-8"))["initialized"] is True


def test_get_size_category():
    assert get_size_category(".txt") == "text"
    assert get_size_category(".pdf") == "document"
    assert get_size_category(".png") == "image"


def test_is_excluded_name_match():
    patterns = ["node_modules", ".git", "*.egg-info"]
    assert is_excluded(Path("C:/proj/node_modules/x.js"), patterns)
    assert is_excluded(Path("C:/proj/.git/config"), patterns)
    assert is_excluded(Path("C:/proj/haydar.egg-info/PKG"), patterns)  # glob suffix
    assert not is_excluded(Path("C:/proj/src/main.py"), patterns)


def test_is_excluded_root_anchored():
    # parts[1] is the folder just inside the drive root.
    assert is_excluded(Path("C:/Windows/System32/x.dll"), [])
    assert not is_excluded(Path("C:/Users/me/doc.txt"), [])


def test_get_rg_path_finds_user_binary(tmp_haydar, monkeypatch):
    """get_rg_path returns a binary placed in the user bin dir."""
    import platform

    import haydar.config as config

    bin_dir = tmp_haydar / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "RIPGREP_DIR", bin_dir, raising=False)

    exe = "rg.exe" if platform.system().lower() == "windows" else "rg"
    target = bin_dir / exe
    target.write_bytes(b"binary")

    assert get_rg_path() == target



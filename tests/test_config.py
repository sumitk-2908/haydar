from pathlib import Path

from haydar.config import (
    CURRENT_SCHEMA_VERSION,
    HaydarConfig,
    get_rg_path,
    get_size_category,
    is_excluded,
)


def test_config_defaults(tmp_path):
    config = HaydarConfig()
    assert config.embedding_model == "all-MiniLM-L6-v2"
    assert config.chunk_size == 500
    assert config.schema_version == CURRENT_SCHEMA_VERSION

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



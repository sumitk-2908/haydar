from pathlib import Path

import haydar.changelog as changelog_module
from haydar.changelog import find_changelog, get_entry_for_version, parse_changelog


def _write_changelog(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_find_changelog_returns_repo_path(tmp_path, monkeypatch):
    changelog = _write_changelog(tmp_path, "dummy")
    mock_file = tmp_path / "src" / "haydar" / "changelog.py"
    monkeypatch.setattr(changelog_module, "__file__", str(mock_file))

    assert find_changelog() == changelog


def test_find_changelog_prefers_frozen_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundled_changelog = _write_changelog(bundle, "bundled")
    monkeypatch.setattr(changelog_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(changelog_module.sys, "_MEIPASS", str(bundle), raising=False)

    assert find_changelog() == bundled_changelog


def test_find_changelog_uses_packaged_copy(tmp_path, monkeypatch):
    package = tmp_path / "haydar"
    package.mkdir()
    packaged_changelog = _write_changelog(package, "packaged")
    monkeypatch.setattr(changelog_module, "__file__", str(package / "changelog.py"))

    assert find_changelog() == packaged_changelog


def test_find_changelog_returns_none_when_absent(tmp_path, monkeypatch):
    mock_file = tmp_path / "src" / "haydar" / "changelog.py"
    monkeypatch.setattr(changelog_module, "__file__", str(mock_file))

    assert find_changelog() is None


def test_parse_changelog_non_empty(tmp_path):
    path = _write_changelog(
        tmp_path,
        """## [0.2.0] - 2026-07-23
### Added
- Item 1

## [0.1.0] - 2026-06-01
### Fixed
- Bug fix
""",
    )

    assert len(parse_changelog(path)) == 2


def test_parse_minimal_changelog(tmp_path):
    path = _write_changelog(
        tmp_path,
        """## [0.2.0] - 2026-07-23
### Added
- First feature
""",
    )

    parsed = parse_changelog(path)
    assert parsed[0]["version"] == "0.2.0"
    assert parsed[0]["date"] == "2026-07-23"
    assert "First feature" in parsed[0]["sections"]["Added"]


def test_get_entry_for_version_found(tmp_path, monkeypatch):
    path = _write_changelog(
        tmp_path,
        """## [0.2.0] - 2026-07-23
### Added
- Feature
""",
    )
    monkeypatch.setattr(changelog_module, "find_changelog", lambda: path)

    entry = get_entry_for_version("0.2.0")
    assert entry is not None
    assert entry["version"] == "0.2.0"


def test_get_entry_for_version_not_found(tmp_path, monkeypatch):
    path = _write_changelog(tmp_path, "## [0.2.0] - 2026-07-23\n")
    monkeypatch.setattr(changelog_module, "find_changelog", lambda: path)

    assert get_entry_for_version("99.0.0") is None


def test_parse_unreleased_section(tmp_path):
    path = _write_changelog(
        tmp_path,
        """## [Unreleased]
### Added
- Something new
""",
    )

    parsed = parse_changelog(path)
    assert parsed[0]["version"] == "unreleased"
    assert parsed[0]["date"] is None


def test_malformed_version_deactivates_previous_entry(tmp_path):
    path = _write_changelog(
        tmp_path,
        """## [1.0.0] - 2026-01-01
### Added
- kept
## [2.0.0] -
### Added
- ignored after malformed version
""",
    )

    parsed = parse_changelog(path)
    assert len(parsed) == 1
    assert parsed[0]["sections"]["Added"] == ["kept"]


def test_parse_requires_exact_headers_and_ignores_unknown_sections(tmp_path):
    path = _write_changelog(
        tmp_path,
        """## [Unreleased] trailing text
### Added
- ignored without valid version
## [1.2.3] -
### Added
- ignored malformed date
## [1.0.0] - 2026-01-01
### Security
- ignored unsupported section
### Fixed
- kept
""",
    )

    parsed = parse_changelog(path)
    assert len(parsed) == 1
    assert parsed[0]["version"] == "1.0.0"
    assert parsed[0]["sections"]["Fixed"] == ["kept"]
    assert set(parsed[0]["sections"]) == {"Added", "Changed", "Fixed", "Removed"}


def test_parse_returns_empty_for_missing_directory_or_invalid_utf8(tmp_path):
    assert parse_changelog(tmp_path / "missing.md") == []
    assert parse_changelog(tmp_path) == []
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff\xfe")
    assert parse_changelog(invalid) == []


def test_parse_preserves_markup_looking_text_as_data(tmp_path):
    path = _write_changelog(
        tmp_path,
        """## [1.0.0] - 2026-01-01
### Added
- <img src=x onerror=alert(1)> café
""",
    )

    assert parse_changelog(path)[0]["sections"]["Added"] == [
        "<img src=x onerror=alert(1)> café"
    ]

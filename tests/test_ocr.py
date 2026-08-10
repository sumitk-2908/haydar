import subprocess
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import MagicMock

import pytest

from haydar.ocr import (
    TesseractInfo,
    TesseractStatus,
    detect_tesseract,
    get_install_instructions,
    get_tesseract_path,
)


def _result(stdout: str = "", stderr: str = "", returncode: int = 0) -> CompletedProcess[str]:
    return CompletedProcess(["tesseract", "--version"], returncode, stdout, stderr)


def _adapter_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())


def test_missing_python_package_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    which = MagicMock(return_value="C:/Program Files/Tesseract/tesseract.exe")
    monkeypatch.setattr("shutil.which", which)

    assert detect_tesseract() == TesseractInfo(TesseractStatus.PYTHON_PACKAGE_MISSING, None, None)
    which.assert_not_called()


def test_package_lookup_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.util.find_spec", MagicMock(side_effect=RuntimeError("broken metadata")))

    info = detect_tesseract()

    assert info.status is TesseractStatus.ERROR
    assert info.detail == "Python adapter lookup failed"


def test_detect_tesseract_not_found_does_not_probe_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _adapter_present(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda executable: None)
    monkeypatch.setattr("haydar.ocr._windows_tesseract_candidates", lambda: ())
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    assert detect_tesseract() == TesseractInfo(TesseractStatus.NOT_FOUND, None, None)
    run.assert_not_called()


def test_detect_tesseract_uses_windows_install_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _adapter_present(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda executable: None)
    executable = tmp_path / "tesseract.exe"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr("haydar.ocr._windows_tesseract_candidates", lambda: (executable,))
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=_result("tesseract 5.4.0")))

    assert detect_tesseract() == TesseractInfo(TesseractStatus.FOUND, "5.4.0", str(executable))


@pytest.mark.parametrize(
    "output, expected_version",
    [
        ("tesseract 5.3.1\nleptonica-1.82.0", "5.3.1"),
        ("  tesseract 5.3.1.20230401", "5.3.1.20230401"),
        ("tesseract v5.3.1", "5.3.1"),
        ("\n\ntesseract 5.3.1", "5.3.1"),
    ],
)
def test_detect_tesseract_found_for_realistic_versions(
    monkeypatch: pytest.MonkeyPatch, output: str, expected_version: str
) -> None:
    _adapter_present(monkeypatch)
    path = "C:/Program Files/Tesseract/tesseract.exe"
    monkeypatch.setattr("shutil.which", lambda executable: path)
    run = MagicMock(return_value=_result(stdout=output))
    monkeypatch.setattr("subprocess.run", run)

    assert detect_tesseract() == TesseractInfo(TesseractStatus.FOUND, expected_version, path)
    run.assert_called_once_with(
        [path, "--version"],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_detect_tesseract_reads_version_from_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    _adapter_present(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda executable: "/usr/bin/tesseract")
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=_result(stderr="tesseract 5.3.1")))

    assert detect_tesseract() == TesseractInfo(TesseractStatus.FOUND, "5.3.1", "/usr/bin/tesseract")


@pytest.mark.parametrize("version", ["1.0.0", "3.05.02"])
def test_detect_tesseract_wrong_version(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    _adapter_present(monkeypatch)
    path = "C:/Tesseract/tesseract.exe"
    monkeypatch.setattr("shutil.which", lambda executable: path)
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=_result(f"tesseract {version}")))

    assert detect_tesseract() == TesseractInfo(TesseractStatus.WRONG_VERSION, version, path)


@pytest.mark.parametrize(
    "result",
    [
        _result("tesseract 5.3.1", returncode=1),
        _result("unexpected output 5.3.1"),
        _result(""),
    ],
)
def test_invalid_probe_results_are_errors(monkeypatch: pytest.MonkeyPatch, result: CompletedProcess[str]) -> None:
    _adapter_present(monkeypatch)
    path = "/usr/bin/tesseract"
    monkeypatch.setattr("shutil.which", lambda executable: path)
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=result))

    info = detect_tesseract()

    assert info.status is TesseractStatus.ERROR
    assert info.path == path
    assert info.detail is not None


@pytest.mark.parametrize(
    "exception, detail",
    [
        (TimeoutExpired(["tesseract"], 5), "timed out"),
        (PermissionError(), "permission denied"),
    ],
)
def test_probe_exceptions_are_errors(
    monkeypatch: pytest.MonkeyPatch, exception: Exception, detail: str
) -> None:
    _adapter_present(monkeypatch)
    path = "/usr/bin/tesseract"
    monkeypatch.setattr("shutil.which", lambda executable: path)
    monkeypatch.setattr("subprocess.run", MagicMock(side_effect=exception))

    info = detect_tesseract()

    assert info.status is TesseractStatus.ERROR
    assert info.path == path
    assert detail in (info.detail or "")


def test_get_tesseract_path_uses_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    which = MagicMock(return_value="C:/Tesseract/tesseract.exe")
    monkeypatch.setattr("shutil.which", which)

    assert get_tesseract_path() == "C:/Tesseract/tesseract.exe"
    which.assert_called_once_with("tesseract")


def test_get_install_instructions_describe_the_one_click_path() -> None:
    """§19 forbids sending a normal user to pip, Winget, PATH, or the CLI.

    The copy follows the manifest: with a reviewed asset it describes the
    one-click install, and with none it describes installing the engine
    manually. §19 was amended 2026-08-11 to permit that once one-click
    provisioning proved undeliverable — withholding a fix that works is worse
    than naming the engine. The package-manager and PATH bans still apply to
    both states, and are asserted here.
    """
    from unittest.mock import patch

    from haydar.ocr import OcrAsset

    reviewed = OcrAsset(
        version="5.5.3",
        platform="windows",
        architecture="x86_64",
        url="https://example.invalid/ocr.zip",
        archive_filename="ocr.zip",
        sha256="a" * 64,
        executable_relative_path="tesseract.exe",
    )
    with patch("haydar.ocr.OCR_ASSETS", (reviewed,)):
        available = get_install_instructions().lower()
    with patch("haydar.ocr.OCR_ASSETS", ()):
        unavailable = get_install_instructions().lower()

    assert "install ocr" in available
    assert "indexed automatically" in available
    # Names the engine and the one action that enables it, so the user is not
    # left with "unavailable" and no route forward.
    assert "tesseract" in unavailable
    assert "restart haydar" in unavailable
    assert "reindex" in unavailable

    for lowered in (available, unavailable):
        for forbidden in ("pip install", "winget", "path", "haydar-cli", "github.com"):
            assert forbidden not in lowered
        # Says plainly that files stay on the machine.
        assert "never uploaded" in lowered

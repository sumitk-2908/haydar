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
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    assert detect_tesseract() == TesseractInfo(TesseractStatus.NOT_FOUND, None, None)
    run.assert_not_called()


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
        [path, "--version"], capture_output=True, check=False, text=True, timeout=5
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


def test_get_install_instructions_are_actionable() -> None:
    instructions = get_install_instructions()

    assert "haydar[ocr]" in instructions
    assert "https://github.com/UB-Mannheim/tesseract/wiki" in instructions
    assert "winget install UB-Mannheim.TesseractOCR" in instructions
    assert "restart Haydar" in instructions

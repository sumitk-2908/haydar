from unittest.mock import MagicMock

from haydar.indexer.extractors import Disposition, _extract_image, extract_text
from haydar.ocr import TesseractInfo, TesseractStatus


def test_extract_text_plain(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("Hello, world!", encoding="utf-8")

    result = extract_text(f)
    assert result is not None
    assert "Hello, world!" in result.text


def test_extract_image_defers_when_ocr_is_not_ready(monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    monkeypatch.setattr(
        "haydar.indexer.extractors.detect_tesseract",
        lambda: TesseractInfo(TesseractStatus.NOT_FOUND, None, None),
    )

    outcome = _extract_image(image)

    # A missing engine is a deferral, never an empty-but-done file: the image
    # must stay eligible for a later one-click OCR install.
    assert outcome.disposition is Disposition.OCR_DEFERRED
    assert outcome.text == ""


def test_extract_image_calls_pytesseract_when_ready(monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    ocr = MagicMock()
    ocr.image_to_string.return_value = "Recognized text"
    monkeypatch.setattr(
        "haydar.indexer.extractors.detect_tesseract",
        lambda: TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"),
    )
    monkeypatch.setitem(__import__("sys").modules, "pytesseract", ocr)

    outcome = _extract_image(image)

    assert outcome.disposition is Disposition.CONTENT
    assert outcome.text == "Recognized text"
    assert ocr.pytesseract.tesseract_cmd == "C:/Tesseract/tesseract.exe"
    ocr.image_to_string.assert_called_once_with(str(image), lang="eng")


def test_extract_image_marks_engine_version_on_ocr_success(monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    ocr = MagicMock()
    ocr.image_to_string.return_value = "Recognized text"
    monkeypatch.setattr(
        "haydar.indexer.extractors.detect_tesseract",
        lambda: TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"),
    )
    monkeypatch.setitem(__import__("sys").modules, "pytesseract", ocr)

    outcome = _extract_image(image)

    assert outcome.ocr_engine_version == "tesseract-5.3.1"


def test_extract_image_empty_ocr_is_empty_but_engine_tagged(monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    ocr = MagicMock()
    ocr.image_to_string.return_value = "   \n  "
    monkeypatch.setattr(
        "haydar.indexer.extractors.detect_tesseract",
        lambda: TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"),
    )
    monkeypatch.setitem(__import__("sys").modules, "pytesseract", ocr)

    outcome = _extract_image(image)

    # OCR ran and found nothing: finished work, tagged so an engine upgrade can
    # schedule an image-only refresh.
    assert outcome.disposition is Disposition.EMPTY
    assert outcome.is_complete is True
    assert outcome.ocr_engine_version == "tesseract-5.3.1"

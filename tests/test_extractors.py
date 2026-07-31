from unittest.mock import MagicMock

from haydar.indexer.extractors import _extract_image, extract_text
from haydar.ocr import TesseractInfo, TesseractStatus


def test_extract_text_plain(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("Hello, world!", encoding="utf-8")

    result = extract_text(f)
    assert result is not None
    assert "Hello, world!" in result.text


def test_extract_image_skips_when_ocr_is_not_ready(monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    monkeypatch.setattr(
        "haydar.indexer.extractors.detect_tesseract",
        lambda: TesseractInfo(TesseractStatus.NOT_FOUND, None, None),
    )

    assert _extract_image(image) is None


def test_extract_image_calls_pytesseract_when_ready(monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    ocr = MagicMock()
    ocr.image_to_string.return_value = "Recognized text"
    monkeypatch.setattr(
        "haydar.indexer.extractors.detect_tesseract",
        lambda: TesseractInfo(TesseractStatus.FOUND, "5.3.1", "C:/Tesseract/tesseract.exe"),
    )
    monkeypatch.setitem(__import__("sys").modules, "pytesseract", ocr)

    result = _extract_image(image)

    assert result is not None
    assert result.text == "Recognized text"
    ocr.image_to_string.assert_called_once_with(str(image), lang="eng")

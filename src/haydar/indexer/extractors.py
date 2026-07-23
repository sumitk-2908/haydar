"""
Text extraction module for Haydar indexer.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chardet
import docx
import pypdf

from haydar.config import (
    CACHE_DIR,
    CODE_EXTENSIONS,
    IMAGE_EXTENSIONS,
    TEXT_EXTENSIONS,
)

logger = logging.getLogger(__name__)

# Upper bound for the on-disk extraction cache (CACHE_DIR/*.txt). When the total
# size exceeds this, the oldest entries (by mtime) are evicted until it fits.
EXTRACTION_CACHE_MAX_BYTES = 512 * 1024 * 1024  # 512 MB


def prune_extraction_cache(max_bytes: int = EXTRACTION_CACHE_MAX_BYTES) -> int:
    """Evict oldest cached extraction files until the cache fits ``max_bytes``.

    Returns the number of bytes freed. Safe to call when the cache dir does not
    exist. Intended to run at ``init``/``reindex`` boundaries, not per-file.
    """
    if not CACHE_DIR.is_dir():
        return 0

    try:
        entries = []
        total = 0
        for path in CACHE_DIR.glob("*.txt"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size

        if total <= max_bytes:
            return 0

        freed = 0
        # Oldest first.
        for _mtime, size, path in sorted(entries, key=lambda e: e[0]):
            if total - freed <= max_bytes:
                break
            try:
                path.unlink()
                freed += size
            except OSError:
                continue

        if freed:
            logger.info("Pruned extraction cache: freed %d bytes", freed)
        return freed
    except Exception as e:
        logger.warning("Failed to prune extraction cache: %s", e)
        return 0

try:
    import pytesseract
    HAS_PYTESSERACT = True
except ImportError:
    HAS_PYTESSERACT = False


@dataclass
class ExtractedContent:
    """Dataclass holding the extracted text and associated metadata."""
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _extract_pdf(filepath: Path) -> ExtractedContent | None:
    try:
        reader = pypdf.PdfReader(filepath)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                logger.warning(f"Failed to decrypt PDF (empty password failed): {filepath}")
                return None

        text_parts = []
        for page in reader.pages:
            try:
                text_parts.append(page.extract_text(extraction_mode="layout") or "")
            except Exception as e:
                logger.warning(f"Failed to extract text from a page in {filepath}: {e}")

        return ExtractedContent(
            text="\n".join(text_parts),
            metadata={"page_count": len(reader.pages)}
        )
    except Exception as e:
        logger.warning(f"Failed to extract PDF {filepath}: {e}")
        return None


def _extract_docx(filepath: Path) -> ExtractedContent | None:
    try:
        doc = docx.Document(str(filepath))
        text_parts = []

        # Headers
        for section in doc.sections:
            for header_para in section.header.paragraphs:
                text_parts.append(header_para.text)

        # Body paragraphs
        for para in doc.paragraphs:
            text_parts.append(para.text)

        # Tables
        for table in doc.tables:
            if hasattr(table, 'iter_inner_content'):
                for content in table.iter_inner_content():
                    text_parts.append(getattr(content, 'text', ''))
            else:
                for row in table.rows:
                    for cell in row.cells:
                        text_parts.append(cell.text)

        # Footers
        for section in doc.sections:
            for footer_para in section.footer.paragraphs:
                text_parts.append(footer_para.text)

        return ExtractedContent(
            text="\n".join(t for t in text_parts if t.strip()),
            metadata={}
        )
    except Exception as e:
        logger.warning(f"Failed to extract DOCX {filepath}: {e}")
        return None


def _extract_text(filepath: Path) -> ExtractedContent | None:
    try:
        raw = filepath.read_bytes()
        sample = raw[:10240]
        result = chardet.detect(sample)
        encoding = result['encoding']
        confidence = result['confidence']

        if not encoding or confidence < 0.5:
            encoding = 'utf-8'

        text = raw.decode(encoding, errors='replace')
        return ExtractedContent(
            text=text,
            metadata={"encoding": encoding}
        )
    except Exception as e:
        logger.warning(f"Failed to extract text {filepath}: {e}")
        return None


def _extract_image(filepath: Path) -> ExtractedContent | None:
    if not HAS_PYTESSERACT:
        logger.warning("OCR requires pytesseract and Tesseract OCR engine. Install: pip install haydar[ocr] and download Tesseract from https://github.com/UB-Mannheim/tesseract/wiki")
        return None

    try:
        text = pytesseract.image_to_string(str(filepath), lang='eng')
        return ExtractedContent(
            text=text,
            metadata={}
        )
    except pytesseract.TesseractNotFoundError:
        logger.warning("Tesseract OCR engine is not installed or not in your PATH. Please install Tesseract from https://github.com/UB-Mannheim/tesseract/wiki and restart Haydar.")
        return None
    except Exception as e:
        logger.warning(f"Failed to extract image {filepath}: {e}")
        return None


def extract_text(filepath: Path, file_hash: str | None = None) -> ExtractedContent | None:
    """
    Dispatch text extraction based on file extension.
    """
    ext = filepath.suffix.lower()

    cache_path = None
    if file_hash:
        cache_path = CACHE_DIR / f"{file_hash}.txt"
        if cache_path.exists():
            try:
                text = cache_path.read_text(encoding="utf-8")
                return ExtractedContent(text=text, metadata={"cached": True})
            except Exception:
                pass

    if ext == ".pdf":
        result = _extract_pdf(filepath)
    elif ext == ".docx":
        result = _extract_docx(filepath)
    elif ext in TEXT_EXTENSIONS or ext in CODE_EXTENSIONS:
        result = _extract_text(filepath)
    elif ext in IMAGE_EXTENSIONS:
        result = _extract_image(filepath)
    else:
        logger.warning(f"Unsupported extension {ext} for {filepath}")
        return None

    if result and cache_path:
        try:
            cache_path.write_text(result.text, encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to write cache for {filepath}: {e}")

    return result

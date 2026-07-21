"""
Text extraction module for Haydar indexer.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import chardet
import pypdf
import docx

from haydar.config import (
    TEXT_EXTENSIONS,
    CODE_EXTENSIONS,
    IMAGE_EXTENSIONS,
)

logger = logging.getLogger(__name__)

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


def _extract_pdf(filepath: Path) -> Optional[ExtractedContent]:
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


def _extract_docx(filepath: Path) -> Optional[ExtractedContent]:
    try:
        doc = docx.Document(filepath)
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


def _extract_text(filepath: Path) -> Optional[ExtractedContent]:
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


def _extract_image(filepath: Path) -> Optional[ExtractedContent]:
    if not HAS_PYTESSERACT:
        logger.warning("OCR requires pytesseract and Tesseract OCR engine. Install: pip install haydar[ocr] and download Tesseract from https://github.com/UB-Mannheim/tesseract/wiki")
        return None
        
    try:
        text = pytesseract.image_to_string(str(filepath), lang='eng')
        return ExtractedContent(
            text=text,
            metadata={}
        )
    except Exception as e:
        logger.warning(f"Failed to extract image {filepath}: {e}")
        return None


def extract_text(filepath: Path) -> Optional[ExtractedContent]:
    """
    Dispatch text extraction based on file extension.
    """
    ext = filepath.suffix.lower()
    
    if ext == ".pdf":
        return _extract_pdf(filepath)
    elif ext == ".docx":
        return _extract_docx(filepath)
    elif ext in TEXT_EXTENSIONS or ext in CODE_EXTENSIONS:
        return _extract_text(filepath)
    elif ext in IMAGE_EXTENSIONS:
        return _extract_image(filepath)
    else:
        logger.warning(f"Unsupported extension {ext} for {filepath}")
        return None

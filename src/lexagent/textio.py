"""Текст из любого исходника: PDF, сканы, фото, Word.

Карта отказов: «файл открывался вверх ногами — картинку клали как хранится, а не как отображается».
Поэтому страница сканируется так, как её видит человек (рендер страницы), а ориентация определяется
распознаванием (OSD), а не флагами внутри файла.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
TEXT_SUFFIXES = {".txt", ".md"}
MIN_TEXT_PER_PAGE = 40  # меньше — значит, текстового слоя нет, это скан
OCR_DPI = 300
WINDOWS_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def tesseract():  # type: ignore[no-untyped-def]
    """pytesseract с путём к программе: на Windows установщик не всегда прописывает её в PATH."""
    import pytesseract

    if os.name == "nt" and not shutil.which("tesseract") and WINDOWS_TESSERACT.is_file():
        pytesseract.pytesseract.tesseract_cmd = str(WINDOWS_TESSERACT)
    return pytesseract


@dataclass(frozen=True)
class Page:
    number: int  # с 1, как видит человек
    text: str
    method: str  # text | ocr
    rotated: int = 0  # на сколько градусов повернули при распознавании


@dataclass(frozen=True)
class Extracted:
    pages: list[Page]

    @property
    def text(self) -> str:
        return "\n\n".join(f"[стр. {p.number}]\n{p.text}" for p in self.pages if p.text.strip())

    @property
    def ocr_used(self) -> bool:
        return any(p.method == "ocr" for p in self.pages)


class UnsupportedFileError(ValueError):
    pass


def extract(path: Path, lang: str = "rus+eng") -> Extracted:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf(path, lang)
    if suffix in IMAGE_SUFFIXES:
        with Image.open(path) as image:
            text, rotated = ocr_image(ImageOps.exif_transpose(image), lang)
        return Extracted([Page(1, text, "ocr", rotated)])
    if suffix == ".docx":
        return Extracted([Page(1, _docx(path), "text")])
    if suffix in TEXT_SUFFIXES:
        return Extracted([Page(1, path.read_text(encoding="utf-8", errors="replace"), "text")])
    raise UnsupportedFileError(f"не умею читать {suffix}: {path.name}")


def _pdf(path: Path, lang: str) -> Extracted:
    pages: list[Page] = []
    with fitz.open(path) as doc:
        for index, page in enumerate(doc, start=1):
            text = page.get_text("text").strip()
            if len(text) >= MIN_TEXT_PER_PAGE and not _garbage(text):
                pages.append(Page(index, text, "text"))
                continue
            image = render_page(page, OCR_DPI)
            ocr, rotated = ocr_image(image, lang)
            pages.append(Page(index, ocr, "ocr", rotated))
    return Extracted(pages)


def render_page(page: fitz.Page, dpi: int = 150) -> Image.Image:
    """Страница так, как её видит человек (с учётом поворота страницы), — а не встроенная картинка."""
    pixmap = page.get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")


def ocr_image(image: Image.Image, lang: str = "rus+eng") -> tuple[str, int]:
    pytesseract = tesseract()
    rotated = detect_rotation(image)
    if rotated:
        image = image.rotate(-rotated, expand=True)
    return pytesseract.image_to_string(image, lang=lang).strip(), rotated


def detect_rotation(image: Image.Image) -> int:
    """Градусы, на которые страница повёрнута (0/90/180/270), по распознаванию ориентации текста."""
    pytesseract = tesseract()
    try:
        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:  # мало текста для уверенного определения — считаем, что ровно
        return 0
    return int(osd.get("rotate", 0)) % 360


def _docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def _garbage(text: str) -> bool:
    """Текстовый слой из «кракозябр» (битая кодировка) хуже, чем его отсутствие: распознаём заново."""
    if "\x00" in text:
        return True
    letters = sum(ch.isalpha() for ch in text)
    readable = sum(("а" <= ch.lower() <= "я") or ("a" <= ch.lower() <= "z") or ch in "ёЁ" for ch in text)
    return letters > 0 and readable / letters < 0.6

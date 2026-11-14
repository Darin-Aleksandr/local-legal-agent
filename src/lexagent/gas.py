"""Проверка файлов перед подачей через ГАС «Правосудие» и автоисправление того, что можно исправить.

Карта отказов: «в одном файле несколько документов», «отсутствует связный текст», «страницы перевёрнуты»,
«имена файлов без количества листов», «указано 2 л. при 1 листе». Каждая причина — отдельная проверка.
Файл с проваленной проверкой в exports/ не попадает.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pymupdf as fitz

from lexagent.textio import _garbage, detect_rotation, render_page, tesseract

NAME = re.compile(r"^(?P<title>[^\\/:*?\"<>|]+)_от_(?P<date>\d{8})_(?P<sheets>\d+)л\.pdf$")
MIN_TEXT = 40
MAX_MB = 30
HEADINGS = re.compile(
    r"^\s*(ИСКОВОЕ ЗАЯВЛЕНИЕ|ПРЕТЕНЗИЯ|ДОВЕРЕННОСТЬ|АКТ ОСМОТРА|ЭКСПЕРТНОЕ ЗАКЛЮЧЕНИЕ|ЗАКЛЮЧЕНИЕ|"
    r"СТРАХОВОЙ ПОЛИС|ИЗВЕЩЕНИЕ О ДОРОЖНО|РЕШЕНИЕ|ОПРЕДЕЛЕНИЕ|ПОСТАНОВЛЕНИЕ|АПЕЛЛЯЦИОННАЯ ЖАЛОБА|"
    r"ЧАСТНАЯ ЖАЛОБА|ЗАЯВЛЕНИЕ|ДОГОВОР|КВИТАНЦИЯ|ОПИСЬ ВЛОЖЕНИЯ)\b",
    re.MULTILINE,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fixable: bool = False


@dataclass
class Verdict:
    file: Path
    pages: int
    sha256: str
    checks: list[Check] = field(default_factory=list)
    previews: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def lines(self) -> list[str]:
        head = f"{'✅' if self.ok else '❌'} {self.file.name} · {self.pages} л. · SHA256 {self.sha256[:16]}…"
        return [
            head,
            *(f"   {'✓' if c.ok else '✗'} {c.name}{': ' + c.detail if c.detail else ''}" for c in self.checks),
        ]


def gas_name(title: str, on: date, sheets: int) -> str:
    """«Исковое заявление» → «Исковое_заявление_от_15112026_4л.pdf»."""
    clean = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip()
    stem = re.sub(r"\s+", "_", clean)
    return f"{stem}_от_{on:%d%m%Y}_{sheets}л.pdf"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_pdf(
    path: Path, *, expected_sheets: int | None = None, previews: Path | None = None, orientation: bool = True
) -> Verdict:
    with fitz.open(path) as doc:
        verdict = Verdict(path, doc.page_count, sha256(path))
        texts = [page.get_text("text") for page in doc]
        verdict.checks.append(_text_layer(texts))
        if orientation:
            verdict.checks.append(_orientation(doc))
        verdict.checks.append(_one_document(texts))
        verdict.checks.append(_hanging_signature(texts))
        if previews:
            previews.mkdir(parents=True, exist_ok=True)
            for number, page in enumerate(doc, 1):
                target = previews / f"{path.stem}_стр{number}.png"
                render_page(page, 60).save(target)
                verdict.previews.append(target)
    verdict.checks.append(_name(path, verdict.pages))
    if expected_sheets is not None:
        verdict.checks.append(
            Check(
                "листы совпадают с перечнем приложений",
                expected_sheets == verdict.pages,
                f"в перечне {expected_sheets} л., в файле {verdict.pages} л.",
            )
        )
    size = path.stat().st_size / 1_048_576
    verdict.checks.append(Check(f"размер до {MAX_MB} МБ", size <= MAX_MB, f"{size:.1f} МБ"))
    return verdict


def _text_layer(texts: list[str]) -> Check:
    bad = [i for i, t in enumerate(texts, 1) if len(t.strip()) < MIN_TEXT or _garbage(t)]
    return Check(
        "связный текстовый слой", not bad, f"нет текста на стр. {', '.join(map(str, bad))}" if bad else "", fixable=True
    )


def _orientation(doc: fitz.Document) -> Check:
    turned = []
    for number, page in enumerate(doc, 1):
        rotation = detect_rotation(render_page(page, 100))
        if rotation:
            turned.append(f"{number} ({rotation}°)")
    return Check(
        "страницы не перевёрнуты", not turned, f"повёрнуты стр. {', '.join(turned)}" if turned else "", fixable=True
    )


def _one_document(texts: list[str]) -> Check:
    starts = []
    for number, text in enumerate(texts, 1):
        match = HEADINGS.search(text[:600])
        if match:
            starts.append((number, match.group(1)))
    kinds = list(dict.fromkeys(kind for _n, kind in starts))
    ok = len(kinds) <= 1
    detail = "" if ok else "похоже на несколько документов: " + ", ".join(f"стр. {n} — {k}" for n, k in starts)
    return Check("в файле один документ", ok, detail)


def _hanging_signature(texts: list[str]) -> Check:
    lonely = [
        n
        for n, t in enumerate(texts, 1)
        if n > 1 and len(" ".join(t.split())) < 160 and ("/" in t or "подпись" in t.lower())
    ]
    return Check(
        "подпись не на пустом листе",
        not lonely,
        f"на стр. {', '.join(map(str, lonely))} только подпись — подтяните текст" if lonely else "",
    )


def _name(path: Path, pages: int) -> Check:
    match = NAME.match(path.name)
    if not match:
        return Check("имя «Наименование_от_ДДММГГГГ_Nл.pdf»", False, f"сейчас «{path.name}»", fixable=True)
    sheets = int(match.group("sheets"))
    return Check(
        "число листов в имени верное", sheets == pages, f"в имени {sheets} л., в файле {pages} л.", fixable=True
    )


def repair(path: Path, out_dir: Path, *, title: str, on: date, lang: str = "rus+eng") -> Path:
    """Скан → правильная ориентация + невидимый текстовый слой + имя по правилам. Оригинал не трогаем."""
    pytesseract = tesseract()
    result = fitz.open()
    with fitz.open(path) as doc:
        for page in doc:
            text = page.get_text("text")
            image = render_page(page, 200)
            rotation = detect_rotation(image)
            if len(text.strip()) >= MIN_TEXT and not _garbage(text) and not rotation:
                result.insert_pdf(doc, from_page=page.number, to_page=page.number)
                continue
            if rotation:
                image = image.rotate(-rotation, expand=True)
            pdf_bytes = pytesseract.image_to_pdf_or_hocr(image, extension="pdf", lang=lang)
            with fitz.open("pdf", pdf_bytes) as ocr_page:
                result.insert_pdf(ocr_page)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / gas_name(title, on, result.page_count)
    result.save(target, garbage=3, deflate=True)
    result.close()
    return target


def split_by_headings(path: Path) -> list[tuple[int, int, str]]:
    """Где в общем скане начинается каждый документ: [(первая стр., последняя стр., заголовок)].

    Подсказка для разрезки пакета: страницы без собственного заголовка относятся к предыдущему документу.
    Окончательно границы подтверждает человек — по превью.
    """
    with fitz.open(path) as doc:
        texts = [page.get_text("text") for page in doc]
        count = doc.page_count
    bounds: list[tuple[int, str]] = []
    for number, text in enumerate(texts, 1):
        match = HEADINGS.search(text[:600])
        if match and (not bounds or match.group(1) != bounds[-1][1]):
            bounds.append((number, match.group(1)))
    if not bounds or bounds[0][0] != 1:
        bounds.insert(0, (1, "без заголовка"))
    return [
        (start, (bounds[i + 1][0] - 1) if i + 1 < len(bounds) else count, title)
        for i, (start, title) in enumerate(bounds)
    ]

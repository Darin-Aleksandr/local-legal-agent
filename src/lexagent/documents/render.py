"""DOCX для правки и PDF для подачи. Times New Roman 14, поля 30/15/20/20 мм, нумерация страниц."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document as Docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt
from docx.text.paragraph import Paragraph

from lexagent.documents.compose import Draft, tidy

FONT = "Times New Roman"


class RenderError(RuntimeError):
    pass


def to_docx(draft: Draft, path: Path, *, with_checks: bool = True) -> Path:
    doc = Docx()
    section = doc.sections[0]
    section.page_height, section.page_width = Mm(297), Mm(210)
    section.left_margin, section.right_margin = Mm(30), Mm(15)
    section.top_margin, section.bottom_margin = Mm(20), Mm(20)
    style = doc.styles["Normal"]
    style.font.name, style.font.size = FONT, Pt(14)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    style.paragraph_format.space_after = Pt(0)
    _page_numbers(section)

    for line in draft.header:
        p = doc.add_paragraph(tidy(line))
        p.paragraph_format.left_indent = Mm(85)
    doc.add_paragraph()
    _centered(doc, draft.title.upper(), bold=True)
    if draft.subtitle:
        _centered(doc, draft.subtitle)
    doc.add_paragraph()
    for text in draft.body:
        _justified(doc, text)
    for argument in draft.arguments:
        _justified(doc, f"{argument.text} ({'; '.join(argument.sources)}).")
    doc.add_paragraph()
    _justified(doc, "На основании изложенного, ПРОШУ:" if draft.kind == "иск" else "ПРОШУ:", indent=False)
    for number, demand in enumerate(draft.demands, 1):
        _justified(doc, f"{number}. {demand}")
    # Приложения и подпись — одним блоком: подпись не уезжает одна на пустой лист
    tail = [doc.add_paragraph(), doc.add_paragraph("Приложения:")]
    for number, item in enumerate(draft.attachments, 1):
        tail.append(_justified(doc, f"{number}. {item}"))
    tail.append(doc.add_paragraph())
    tail += [doc.add_paragraph(line) for line in draft.signature]
    for paragraph in tail[:-1]:
        paragraph.paragraph_format.keep_with_next = True

    doc.add_page_break()
    _centered(doc, "Расчёт требований", bold=True)
    for line in draft.calculation:
        _justified(doc, line, indent=False)
    if with_checks and draft.checks:
        doc.add_paragraph()
        _centered(doc, "Проверить перед подачей (удалите этот раздел)", bold=True)
        for item in draft.checks:
            _justified(doc, f"☐ {item}", indent=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def to_pdf(docx: Path, out_dir: Path) -> Path:
    """PDF через LibreOffice: текстовый слой, встроенные шрифты — то, что принимает ГАС «Правосудие»."""
    office = find_office()
    if office is None:
        raise RenderError("Не найден LibreOffice: установите его (install/windows.ps1 делает это сам)")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        result = subprocess.run(
            [
                office,
                f"-env:UserInstallation=file:///{Path(profile).as_posix().lstrip('/')}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out_dir),
                str(docx),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    pdf = out_dir / f"{docx.stem}.pdf"
    if result.returncode != 0 or not pdf.is_file():
        raise RenderError(f"LibreOffice не сконвертировал {docx.name}: {result.stderr.strip()[:300]}")
    return pdf


def find_office() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for root in (r"C:\Program Files\LibreOffice", r"C:\Program Files (x86)\LibreOffice"):
        candidate = Path(root) / "program" / "soffice.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _centered(doc: Docx, text: str, *, bold: bool = False) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(tidy(text)).bold = bold


def _justified(doc: Docx, text: str, *, indent: bool = True) -> Paragraph:
    p = doc.add_paragraph(tidy(text))
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if indent:
        p.paragraph_format.first_line_indent = Mm(12.5)
    return p


def _page_numbers(section: object) -> None:
    footer = section.footer.paragraphs[0]  # type: ignore[attr-defined]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run()
    for tag, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if tag:
            element = OxmlElement("w:fldChar")
            element.set(qn("w:fldCharType"), tag)
        else:
            element = OxmlElement("w:instrText")
            element.text = text
        run._r.append(element)

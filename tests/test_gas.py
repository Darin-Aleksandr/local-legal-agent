from datetime import date
from pathlib import Path

import pymupdf as fitz
import pytest

from lexagent import gas
from tests.conftest import make_pdf

TEXT = "ИСКОВОЕ ЗАЯВЛЕНИЕ\nо взыскании страхового возмещения\n" + "Истец обратился к страховщику с заявлением. " * 30


def test_good_file_passes(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "Исковое_заявление_от_20012026_2л.pdf", [TEXT, "Продолжение. " * 40 + "\n/ Сургаев"])
    verdict = gas.check_pdf(pdf, expected_sheets=2, previews=tmp_path / "превью")
    assert verdict.ok, verdict.lines()
    assert len(verdict.previews) == 2 and len(verdict.sha256) == 64


def test_wrong_name_and_sheet_count(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "Исковое_заявление_от_20012026_2л.pdf", [TEXT])
    verdict = gas.check_pdf(pdf, expected_sheets=2, orientation=False)
    failed = [c.name for c in verdict.checks if not c.ok]
    assert failed == ["число листов в имени верное", "листы совпадают с перечнем приложений"]
    loose = make_pdf(tmp_path / "иск.pdf", [TEXT])
    assert not gas.check_pdf(loose, orientation=False).ok


def test_two_documents_in_one_file(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "Пакет_от_20012026_2л.pdf", [TEXT, "ДОВЕРЕННОСТЬ\n" + "Текст доверенности. " * 20])
    check = next(c for c in gas.check_pdf(pdf, orientation=False).checks if c.name == "в файле один документ")
    assert not check.ok and "ДОВЕРЕННОСТЬ" in check.detail
    assert gas.split_by_headings(pdf) == [(1, 1, "ИСКОВОЕ ЗАЯВЛЕНИЕ"), (2, 2, "ДОВЕРЕННОСТЬ")]


def test_scan_without_text_is_repaired(tmp_path: Path) -> None:
    source = make_pdf(tmp_path / "src.pdf", [TEXT])
    with fitz.open(source) as doc:
        pix = doc[0].get_pixmap(dpi=200)
    scan = fitz.open()
    page = scan.new_page(width=595, height=842)
    page.insert_image(page.rect, pixmap=pix, rotate=180)  # перевёрнутый скан без текстового слоя
    scan_path = tmp_path / "скан.pdf"
    scan.save(scan_path)

    verdict = gas.check_pdf(scan_path)
    failed = {c.name for c in verdict.checks if not c.ok}
    assert "связный текстовый слой" in failed

    fixed = gas.repair(scan_path, tmp_path / "out", title="Исковое заявление", on=date(2026, 1, 20), lang="rus")
    assert fixed.name == "Исковое_заявление_от_20012026_1л.pdf"
    after = gas.check_pdf(fixed)
    assert after.ok, after.lines()
    with fitz.open(fixed) as doc:
        assert "страховщику" in doc[0].get_text()


def test_hanging_signature(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "Иск_от_20012026_2л.pdf", [TEXT, "______ / Сургаев И. П."])
    assert not gas.check_pdf(pdf, orientation=False).ok


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("Исковое заявление", "Исковое_заявление_от_01022026_3л.pdf"),
        ("Расчёт неустойки (приложение 1)", "Расчёт_неустойки_приложение_1_от_01022026_3л.pdf"),
    ],
)
def test_gas_name(title: str, name: str) -> None:
    assert gas.gas_name(title, date(2026, 2, 1), 3) == name

from pathlib import Path

from lexagent.cases import CaseStore, Source


def test_originals_are_immutable_and_deduplicated(tmp_path: Path) -> None:
    store = CaseStore(tmp_path / "Дела")
    case = store.create("Сургаев РЕСО", keywords=["ХХХ 0123456789"])
    scan = tmp_path / "скан.pdf"
    scan.write_bytes(b"%PDF-1.4 test")
    first = case.add_original(scan)
    again = case.add_original(scan)
    assert first == again
    assert not first.stat().st_mode & 0o222  # только чтение
    assert case.verify_originals() == []
    first.chmod(0o644)
    first.write_bytes(b"tampered")
    assert case.verify_originals() == [first.name]


def test_confirmed_fact_not_overwritten_by_ocr(tmp_path: Path) -> None:
    case = CaseStore(tmp_path).create("Дело")
    case.set_fact("claimant_name", "Сургаев Иван Петрович", source=None, confirmed=False, reason="ocr")
    case.confirm("claimant_name")
    case.set_fact(
        "claimant_name", "Сурчаев Иван Петрович", source=Source(file="акт.pdf"), confirmed=False, reason="ocr"
    )
    dossier = case.load()
    assert dossier.value("claimant_name") == "Сургаев Иван Петрович"
    assert "Сурчаев" in dossier.notes[-1]


def test_find_by_policy_and_versions(tmp_path: Path) -> None:
    store = CaseStore(tmp_path)
    case = store.create("Петров Альфа", keywords=["ХХХ 0123456789"])
    store.create("Иванов Росгосстрах", keywords=["ТТТ 5555555555"])
    assert store.find(["ххх-0123456789"]).root == case.root  # type: ignore[union-attr]
    assert store.find(["123"]) is None  # слишком короткое — не угадываем
    assert case.next_version().name == "v01"
    assert case.next_version().name == "v02"
    assert (case.root / "logs" / "audit.log").read_text(encoding="utf-8").count("\n") >= 1

"""Дело на диске — единственный источник истины.

Карта отказов (из практики): «контекст чата очищался, исправленные файлы умирали». Поэтому всё
состояние дела лежит в его папке, а не в памяти нейросети:

    Дела/2026_Сургаев_РЕСО/
        dossier.json      факты дела: каждое значение со ссылкой на источник и отметкой «подтверждено»
        originals/        исходные файлы, не изменяются никогда; hashes.json — SHA256 каждого
        text/             распознанный текст каждого исходника (для поиска по делу)
        work/vNN/         черновики, каждая версия отдельно
        exports/          замороженные файлы для подачи
        logs/audit.log    журнал: что, когда и на основании чего изменилось

Новая сессия начинается с чтения dossier.json — забывать нечего.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["ОСАГО", "КАСКО", "ПЕРЕВОЗКИ", "ЗоЗПП", "ДРУГОЕ"]

# Поля, без которых документы по ОСАГО не собрать. Подпись — для человека, тип — для проверки.
FIELDS: dict[str, tuple[str, str]] = {
    "claimant_name": ("ФИО потерпевшего (истца)", "text"),
    "claimant_address": ("Адрес потерпевшего", "text"),
    "claimant_phone": ("Телефон потерпевшего", "text"),
    "insurer_name": ("Страховая компания", "text"),
    "insurer_inn": ("ИНН страховой", "text"),
    "insurer_address": ("Адрес страховой", "text"),
    "policy_number": ("Полис ОСАГО (серия, номер)", "text"),
    "insurer_claim_number": ("Номер убытка у страховщика", "text"),
    "accident_date": ("Дата ДТП", "date"),
    "accident_place": ("Место ДТП", "text"),
    "vehicle": ("Автомобиль потерпевшего (марка, модель, госномер)", "text"),
    "culprit_name": ("Виновник ДТП", "text"),
    "application_date": ("Дата подачи заявления страховщику", "date"),
    "repair_cost_market": ("Рыночная стоимость ремонта по экспертизе, ₽", "money"),
    "repair_cost_unified": ("Стоимость ремонта по Единой методике без износа, ₽", "money"),
    "expertise_cost": ("Расходы на экспертизу, ₽", "money"),
    "fu_decision_number": ("Номер решения финансового уполномоченного", "text"),
    "fu_decision_date": ("Дата решения финансового уполномоченного", "date"),
    "court_name": ("Суд", "text"),
    "court_case_number": ("Номер дела в суде", "text"),
}


class Source(BaseModel):
    file: str  # имя файла в originals/
    page: int | None = None
    quote: str = ""  # дословный фрагмент, откуда взято значение


class Fact(BaseModel):
    value: str
    source: Source | None = None
    confirmed: bool = False  # распознанное — гипотеза, пока человек не подтвердил
    updated: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


class Payment(BaseModel):
    """Что страховщик уже заплатил: нужно для неустойки и цены иска."""

    paid_on: date
    amount: float
    note: str = ""
    source: Source | None = None
    confirmed: bool = False


class Party(BaseModel):
    role: str  # истец, ответчик, третье лицо, финансовый уполномоченный…
    name: str
    address: str = ""
    inn: str = ""
    mail_receipt: str = ""  # трек-номер отправки копий (РПО) — без него документ не подаётся


class Dossier(BaseModel):
    case_id: str
    title: str
    kind: Kind = "ОСАГО"
    created: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    facts: dict[str, Fact] = Field(default_factory=dict)
    payments: list[Payment] = Field(default_factory=list)
    parties: list[Party] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)  # номера полиса, убытка, дела — для раскладки входящих
    notes: list[str] = Field(default_factory=list)

    def value(self, key: str) -> str | None:
        fact = self.facts.get(key)
        return fact.value if fact else None

    def unconfirmed(self) -> list[str]:
        return [k for k, f in self.facts.items() if not f.confirmed] + [
            f"payment:{p.paid_on}" for p in self.payments if not p.confirmed
        ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\- ]+", "", text, flags=re.UNICODE).strip()
    return re.sub(r"\s+", "_", cleaned)[:60] or "дело"


class Case:
    def __init__(self, root: Path) -> None:
        self.root = root

    # ---------------------------------------------------------------- layout
    @property
    def dossier_path(self) -> Path:
        return self.root / "dossier.json"

    @property
    def originals(self) -> Path:
        return self.root / "originals"

    @property
    def text_dir(self) -> Path:
        return self.root / "text"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def hashes_path(self) -> Path:
        return self.originals / "hashes.json"

    # ---------------------------------------------------------------- dossier
    def load(self) -> Dossier:
        return Dossier.model_validate_json(self.dossier_path.read_text(encoding="utf-8"))

    def save(self, dossier: Dossier, reason: str) -> None:
        self.dossier_path.write_text(dossier.model_dump_json(indent=2), encoding="utf-8")
        self.log("dossier", reason=reason)

    def set_fact(self, key: str, value: str, *, source: Source | None, confirmed: bool, reason: str) -> None:
        dossier = self.load()
        old = dossier.facts.get(key)
        if old and old.confirmed and not confirmed and old.value != value:
            # подтверждённое человеком не перезаписывается распознаванием — только отмечается расхождение
            dossier.notes.append(
                f"Расхождение в «{FIELDS.get(key, (key,))[0]}»: подтверждено «{old.value}», "
                f"в {source.file if source else 'новом документе'} — «{value}»"
            )
            self.save(dossier, f"расхождение {key}")
            return
        dossier.facts[key] = Fact(value=value, source=source, confirmed=confirmed)
        self.save(dossier, reason)

    def confirm(self, key: str, value: str | None = None) -> None:
        dossier = self.load()
        fact = dossier.facts[key]
        if value is not None:
            fact.value = value
        fact.confirmed = True
        self.save(dossier, f"подтверждено человеком: {key}")

    # ---------------------------------------------------------------- originals
    def hashes(self) -> dict[str, str]:
        if not self.hashes_path.is_file():
            return {}
        return dict(json.loads(self.hashes_path.read_text(encoding="utf-8")))

    def add_original(self, path: Path) -> Path:
        """Копирует файл в originals/ под неизменяемым именем; повторная загрузка того же файла — без дубля."""
        digest = sha256(path)
        hashes = self.hashes()
        for name, known in hashes.items():
            if known == digest:
                return self.originals / name
        target = self.originals / path.name
        stem, n = path.stem, 2
        while target.exists():
            target = self.originals / f"{stem}_{n}{path.suffix}"
            n += 1
        shutil.copy2(path, target)
        target.chmod(0o444)  # только чтение: правки — в work/
        hashes[target.name] = digest
        self.hashes_path.write_text(json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log("original", file=target.name, sha256=digest)
        return target

    def verify_originals(self) -> list[str]:
        """Файлы, которые изменились после загрузки (подмена, порча) — их нельзя использовать молча."""
        return [
            name
            for name, digest in self.hashes().items()
            if not (self.originals / name).is_file() or sha256(self.originals / name) != digest
        ]

    def save_text(self, original: Path, text: str) -> Path:
        self.text_dir.mkdir(exist_ok=True)
        target = self.text_dir / f"{original.name}.txt"
        target.write_text(text, encoding="utf-8")
        return target

    def texts(self) -> dict[str, str]:
        if not self.text_dir.is_dir():
            return {}
        return {p.name.removesuffix(".txt"): p.read_text(encoding="utf-8") for p in sorted(self.text_dir.glob("*.txt"))}

    # ---------------------------------------------------------------- versions
    def next_version(self) -> Path:
        self.work.mkdir(exist_ok=True)
        numbers = [int(p.name[1:]) for p in self.work.glob("v[0-9][0-9]") if p.name[1:].isdigit()]
        target = self.work / f"v{max(numbers, default=0) + 1:02d}"
        target.mkdir()
        return target

    # ---------------------------------------------------------------- audit
    def log(self, event: str, **details: Any) -> None:
        (self.root / "logs").mkdir(exist_ok=True)
        line = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **details}
        with (self.root / "logs" / "audit.log").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


class CaseStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[Case]:
        return [Case(p) for p in sorted(self.root.iterdir()) if (p / "dossier.json").is_file()]

    def get(self, case_id: str) -> Case | None:
        case = Case(self.root / case_id)
        return case if case.dossier_path.is_file() else None

    def create(self, title: str, kind: Kind = "ОСАГО", keywords: list[str] | None = None) -> Case:
        case_id = f"{date.today().year}_{slugify(title)}"
        root, n = self.root / case_id, 2
        while root.exists():
            root = self.root / f"{case_id}_{n}"
            n += 1
        root.mkdir(parents=True)
        for sub in ("originals", "text", "work", "exports", "logs"):
            (root / sub).mkdir()
        case = Case(root)
        case.save(Dossier(case_id=root.name, title=title, kind=kind, keywords=keywords or []), "создано дело")
        return case

    def find(self, identifiers: list[str]) -> Case | None:
        """Дело по точному совпадению номера полиса, убытка, дела или ФИО — без угадывания."""
        wanted = {_norm(i) for i in identifiers if i and len(_norm(i)) >= 5}
        for case in self.all():
            dossier = case.load()
            known = {_norm(k) for k in dossier.keywords}
            known |= {
                _norm(f.value)
                for k, f in dossier.facts.items()
                if k in {"policy_number", "insurer_claim_number", "court_case_number", "claimant_name"}
            }
            if wanted & known:
                return case
        return None


def _norm(value: str) -> str:
    return re.sub(r"[\s№\-–«»\"'.,]", "", value).lower()

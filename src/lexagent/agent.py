"""Агент: юрист пишет задачу обычными словами — агент сам решает, что сделать.

«разбери входящие», «сделай иск по Сургаеву», «посчитай неустойку по делу РЕСО на 01.12.2026»,
«что ВС говорит про смену формы возмещения?», «проверь файлы для ГАС», «обнови библиотеку».
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lexagent import gas
from lexagent.cases import Case, CaseStore
from lexagent.config import Settings
from lexagent.dates import parse_date
from lexagent.documents.compose import DocKind, Facts, compose, compute
from lexagent.documents.render import RenderError, to_docx, to_pdf
from lexagent.inbox import process_inbox
from lexagent.library.fetch import Fetcher
from lexagent.library.store import Hit, Library
from lexagent.library.update import load_sources, update_library
from lexagent.llm import LLM, LLMError, parse

Action = Literal["inbox", "document", "calc", "ask", "check", "update", "dossier", "help"]
UPDATE_EVERY_HOURS = 24


class Intent(BaseModel):
    action: Action
    case: str = Field("", description="как юрист назвал дело: фамилия, страховая, номер; пусто — если не назвал")
    document: Literal["претензия", "обращение", "иск", ""] = ""
    on: str = Field("", description="дата расчёта/подачи ДД.ММ.ГГГГ, если названа")
    question: str = ""


ROUTE = (
    "Определи, что просит юрист. action: inbox — разобрать новые файлы; document — подготовить документ "
    "(претензия страховщику / обращение к финуполномоченному / иск); calc — посчитать суммы, неустойку, "
    "пошлину; ask — вопрос о нормах и практике; check — проверить PDF перед подачей в суд; update — обновить "
    "базу законов и практики; dossier — показать, что известно по делу и что не подтверждено; help — иное."
)


class Answer(BaseModel):
    text: str = Field(description="ответ, со ссылками вида [N] после каждого утверждения")
    used: list[int]


ANSWER = (
    "Ты юрист по ОСАГО. Ответь на вопрос ТОЛЬКО по пронумерованным фрагментам. После каждого утверждения "
    "ставь [N] — номер фрагмента. Если во фрагментах ответа нет — так и скажи: «в библиотеке нет ответа». "
    "Не называй номера дел и пункты, которых нет во фрагментах. Если отмечено, что позиция заменена более "
    "сильной, — опирайся на применяемую."
)


@dataclass
class Reply:
    text: str
    files: list[Path] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        settings: Settings,
        llm: LLM | None,
        *,
        fetcher: Fetcher | None = None,
        sources: Path = Path("sources.toml"),
    ) -> None:
        settings.ensure()
        self.settings = settings
        self.llm = llm
        self.store = CaseStore(settings.cases)
        self.library = Library(settings.library / "library.sqlite")
        self.fetcher = fetcher or Fetcher(settings.user_agent)
        self.sources = sources

    @property
    def embed(self):  # type: ignore[no-untyped-def]
        return self.llm.embed if self.llm else None

    # ------------------------------------------------------------ entry point
    def handle(self, task: str) -> Reply:
        intent = self.route(task)
        if intent.action == "inbox":
            return self.inbox()
        if intent.action == "update":
            return self.update()
        if intent.action == "ask":
            return self.ask(intent.question or task)
        if intent.action == "check":
            return self.check(self._case(intent.case))
        case = self._case(intent.case)
        if case is None:
            names = ", ".join(c.load().title for c in self.store.all()) or "дел пока нет — положите файлы во «Входящие»"
            return Reply(f"Не понял, о каком деле речь. Есть: {names}.")
        on = parse_date(intent.on) if intent.on else None
        if intent.action == "document":
            return self.document(case, intent.document or "иск", on=on)
        if intent.action == "calc":
            return self.calc(case, on=on)
        return self.dossier(case)

    def route(self, task: str) -> Intent:
        guess = _keywords(task)
        if self.llm is None:
            return guess
        try:
            intent = parse(self.llm, ROUTE, task, Intent)
        except LLMError:
            return guess
        return intent if intent.action != "help" else guess

    # ------------------------------------------------------------ actions
    def inbox(self) -> Reply:
        if self.llm is None:
            return Reply("Для разбора документов нужна нейросеть: запустите Ollama.")
        results = process_inbox(
            self.settings.inbox,
            self.store,
            self.llm,
            library=self.library,
            embed=self.embed,
            lang=self.settings.tesseract_lang,
        )
        if not results:
            return Reply("Во «Входящих» пусто.")
        lines = []
        for r in results:
            where = "в библиотеку" if r.library else f"в дело «{r.case}»" if r.case else "не разобран"
            lines.append(f"**{r.file}** — {r.doc_type}, {where}" + (f" ⚠ {r.error}" if r.error else ""))
            lines += [f"  • {fact} (не подтверждено)" for fact in r.facts]
            lines += [f"  • ✗ отброшено: {d}" for d in r.dropped]
        lines.append("\nПроверьте распознанное во вкладке «Дела» и нажмите «Подтвердить».")
        return Reply("\n".join(lines))

    def update(self, *, force: bool = True) -> Reply:
        stamp = self.settings.library / ".updated"
        if not force and stamp.is_file():
            last = datetime.fromisoformat(stamp.read_text().strip())
            if (datetime.now(UTC) - last).total_seconds() < UPDATE_EVERY_HOURS * 3600:
                return Reply("Библиотека обновлялась менее суток назад.")
        report = update_library(
            self.library,
            self.fetcher,
            load_sources(self.sources),
            embed=self.embed,
            llm=self.llm,
            links_file=self.settings.inbox / "ссылки.txt",
            reports_dir=self.settings.library / "отчёты",
        )
        stamp.write_text(datetime.now(UTC).isoformat())
        return Reply(report.markdown())

    def ask(self, question: str, case: Case | None = None) -> Reply:
        hits = self.library.search(question, k=8, embed=self.embed)
        if not hits:
            return Reply("В библиотеке ничего не нашлось. Обновите библиотеку или положите практику во «Входящие».")
        sources = "\n".join(f"[{i}] {_cite(h)}" for i, h in enumerate(hits, 1))
        if self.llm is None:
            quotes = "\n\n".join(f"[{i}] {_cite(h)}\n> {h.text[:600]}" for i, h in enumerate(hits[:4], 1))
            return Reply(f"Нашёл (без нейросети — только цитаты):\n\n{quotes}")
        context = "\n\n".join(f"[{i}] {_cite(h)}\n{h.text[:1500]}" for i, h in enumerate(hits, 1))
        answer = parse(self.llm, ANSWER, f"Вопрос: {question}\n\nФрагменты:\n{context}", Answer)
        cited = {int(n) for n in re.findall(r"\[(\d+)]", answer.text)}
        bogus = sorted(n for n in cited if not 1 <= n <= len(hits))
        warning = f"\n\n⚠ Ссылки {bogus} не существуют — эти утверждения не проверены." if bogus else ""
        if not cited:
            warning += "\n\n⚠ В ответе нет ни одной ссылки на источник — не используйте его без проверки."
        return Reply(f"{answer.text}{warning}\n\n**Источники:**\n{sources}")

    def calc(self, case: Case, *, on: date | None = None) -> Reply:
        facts = Facts(case.load())
        money = compute(facts, on or date.today())
        checks = "\n".join(f"- {c}" for c in facts.checks)
        case.log("расчёт", on=str(on or date.today()), price=str(money.price))
        return Reply("\n".join(money.lines()) + (f"\n\n**Проверить:**\n{checks}" if checks else ""))

    def document(self, case: Case, kind: DocKind, *, on: date | None = None) -> Reply:
        on = on or date.today()
        dossier = case.load()
        draft = compose(kind, dossier, library=self.library, llm=self.llm, embed=self.embed, on=on)
        version = case.next_version()
        docx = to_docx(draft, version / f"{draft.title}.docx")
        files, lines = [docx], [f"Черновик: {version.name}/{docx.name}"]
        try:
            clean = to_docx(draft, version / "для_подачи" / f"{draft.title}.docx", with_checks=False)
            pdf = to_pdf(clean, version / "для_подачи")
            verdict = gas.check_pdf(pdf, orientation=False)
            final = version / gas.gas_name(draft.title, on, verdict.pages)
            shutil.move(pdf, final)
            verdict = gas.check_pdf(final, orientation=False, previews=version / "превью")
            lines += ["", *verdict.lines()]
            files.append(final)
        except RenderError as exc:
            lines.append(f"PDF не собран: {exc}")
        if draft.checks:
            lines += ["", f"**Проверить перед подачей ({len(draft.checks)}):**", *[f"- {c}" for c in draft.checks]]
        else:
            lines.append("\nВсе данные подтверждены. Можно переносить PDF в exports/ кнопкой «Заморозить».")
        case.log("документ", kind=kind, version=version.name, checks=len(draft.checks))
        return Reply("\n".join(lines), files)

    def freeze(self, case: Case, pdf: Path) -> Reply:
        """Окончательный файл для подачи: копия в exports/ + SHA256 в журнале — для споров о подлинности."""
        verdict = gas.check_pdf(pdf)
        if not verdict.ok:
            return Reply("Не заморожено — проверка не пройдена:\n" + "\n".join(verdict.lines()))
        target = case.exports / pdf.name
        if target.exists():
            if gas.sha256(target) == verdict.sha256:
                return Reply(f"Уже заморожен: {target.name}\nSHA256: {verdict.sha256}", [target])
            target.chmod(0o644)
            target.rename(target.with_name(f"{target.stem}.заменён_{date.today():%d%m%Y}.pdf"))
        shutil.copy2(pdf, target)
        target.chmod(0o444)
        case.log("заморожен", file=target.name, sha256=verdict.sha256)
        return Reply(f"Заморожен {target.name}\nSHA256: {verdict.sha256}", [target])

    def check(self, case: Case | None) -> Reply:
        folder = case.exports if case else self.settings.inbox
        pdfs = sorted(folder.rglob("*.pdf"))
        if not pdfs:
            return Reply(f"В {folder} нет PDF для проверки.")
        lines: list[str] = []
        for pdf in pdfs:
            lines += gas.check_pdf(pdf).lines()
        return Reply("\n".join(lines))

    def dossier(self, case: Case) -> Reply:
        dossier = case.load()
        lines = [f"**{dossier.title}**"]
        for key, fact in dossier.facts.items():
            mark = "✓" if fact.confirmed else "?"
            where = f" — {fact.source.file}, стр. {fact.source.page}" if fact.source else ""
            lines.append(f"{mark} {key}: {fact.value}{where}")
        lines += [
            f"• выплата {p.amount:,.2f} ₽ от {p.paid_on:%d.%m.%Y}{'' if p.confirmed else ' (?)'}"
            for p in dossier.payments
        ]
        lines += [f"⚠ {n}" for n in dossier.notes[-5:]]
        broken = case.verify_originals()
        if broken:
            lines.append(f"❌ Исходники изменены после загрузки: {', '.join(broken)}")
        return Reply("\n".join(lines))

    def _case(self, hint: str) -> Case | None:
        cases = self.store.all()
        if not hint:
            return cases[0] if len(cases) == 1 else None
        found = self.store.find([hint])
        if found:
            return found
        # «по Сургаеву» ↔ «Сургаев РЕСО»: сравниваем основы слов, падежные окончания отбрасываем
        stems = [w[: max(4, len(w) - 2)] for w in re.findall(r"\w+", hint.lower()) if len(w) >= 3]

        def haystack(case: Case) -> str:
            dossier = case.load()
            surname = (dossier.value("claimant_name") or "").split(" ")[0]  # без отчества: «Петрович» ≠ «Петров»
            return " ".join(
                [dossier.title, case.root.name.replace("_", " "), surname, dossier.value("insurer_name") or ""]
            ).lower()

        def hit(case: Case) -> bool:
            words = re.findall(r"\w+", haystack(case))
            return all(any(w.startswith(s) for w in words) for s in stems)

        matches = [c for c in cases if stems and hit(c)]
        return matches[0] if len(matches) == 1 else None


def _cite(hit: Hit) -> str:
    return f"{hit.heading}, {hit.document.cite()}" if hit.heading else hit.document.cite()


def _keywords(task: str) -> Intent:
    """Разбор без нейросети: на случай, если модель не ответила."""
    low = task.lower()
    kind: Literal["претензия", "обращение", "иск", ""] = (
        "претензия"
        if "претенз" in low
        else "обращение"
        if "обращен" in low or "фу" in low.split()
        else "иск"
        if "иск" in low
        else ""
    )
    when = re.search(r"\d{2}\.\d{2}\.\d{4}", task)
    case = re.search(r"(?:по делу|по)\s+([А-ЯЁA-Z][\w\-]+)", task)
    on, hint = (when.group(0) if when else ""), (case.group(1) if case else "")
    if "входящ" in low or "разбер" in low:
        return Intent(action="inbox")
    if "обнов" in low and ("библиот" in low or "практик" in low or "закон" in low):
        return Intent(action="update")
    if "гас" in low or ("провер" in low and "pdf" in low):
        return Intent(action="check", case=hint)
    if kind:
        return Intent(action="document", case=hint, document=kind, on=on)
    if "неустойк" in low or "посчита" in low or "пошлин" in low or "расч" in low:
        return Intent(action="calc", case=hint, on=on)
    if "досье" in low or "что извест" in low:
        return Intent(action="dossier", case=hint)
    return Intent(action="ask", question=task)

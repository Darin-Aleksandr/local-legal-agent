"""Окно в браузере (http://127.0.0.1:7860): «Задача», «Дела», «Библиотека». Работает без интернета.

Всё, что делает интерфейс, — вызывает Agent; логики здесь нет.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

import gradio as gr

from lexagent.agent import Agent
from lexagent.cases import FIELDS, Case
from lexagent.inbox import normalize_value
from lexagent.llm import LLMError

HELP = """**Как работать:** перетащите файлы дела (сканы, фото, PDF, Word) в поле ниже
и напишите задачу обычными словами.

Примеры: «разбери входящие» · «сделай претензию по Сургаеву» · «посчитай неустойку по делу РЕСО на 01.12.2026» ·
«обращение к ФУ по Иванову» · «иск по Сургаеву» · «что ВС говорит о смене формы возмещения?» · «проверь файлы для ГАС».

Распознанное из документов — всегда «не подтверждено», пока вы не проверите его во вкладке «Дела»."""


LOCK = threading.RLock()  # одна операция за раз: база дел и библиотеки — общие


def locked[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with LOCK:
            return func(*args, **kwargs)

    return wrapper


def build(agent: Agent) -> gr.Blocks:
    with gr.Blocks(title="ЮрАгент — ОСАГО") as app:
        gr.Markdown("# ⚖️ ЮрАгент · ОСАГО\nЛокальный помощник: всё хранится и считается на этом ноутбуке.")
        with gr.Tab("Задача"):
            gr.Markdown(HELP)
            uploads = gr.File(label="Документы во «Входящие»", file_count="multiple")
            task = gr.Textbox(label="Задача", placeholder="например: разбери входящие и сделай претензию", lines=2)
            run = gr.Button("Выполнить", variant="primary")
            answer = gr.Markdown()
            files = gr.File(label="Готовые файлы", interactive=False)
            run.click(lambda f, t: _run(agent, f, t), [uploads, task], [answer, files, uploads])
            task.submit(lambda f, t: _run(agent, f, t), [uploads, task], [answer, files, uploads])

        with gr.Tab("Дела"):
            gr.Markdown(
                "Проверьте распознанное: исправьте значение, если нужно, и нажмите «Подтвердить». "
                "Документы собираются только из подтверждённых данных — остальное помечается [ПРОВЕРИТЬ]."
            )
            choices = _cases(agent)
            case = gr.Dropdown(
                label="Дело", choices=choices, value=choices[0][1] if choices else None, interactive=True
            )
            refresh = gr.Button("Обновить список")
            table = gr.Dataframe(headers=["Поле", "Значение", "Подтверждено", "Источник"], interactive=False, wrap=True)
            with gr.Row():
                field = gr.Dropdown(label="Поле", choices=[(label, key) for key, (label, _t) in FIELDS.items()])
                value = gr.Textbox(label="Значение (пусто — подтвердить как есть)")
                confirm = gr.Button("Подтвердить")
            with gr.Row():
                buttons = {
                    kind: gr.Button(title)
                    for kind, title in (
                        ("претензия", "📝 Претензия"),
                        ("обращение", "📨 Обращение к ФУ"),
                        ("иск", "⚖️ Иск"),
                    )
                }
            status = gr.Markdown()
            case_files = gr.File(label="Файлы документа", interactive=False)
            for kind, button in buttons.items():
                button.click(lambda c, k=kind: _document(agent, c, k), [case], [status, case_files])
            case.change(lambda c: _table(agent, c), [case], [table], api_name="case_table")
            app.load(lambda c: _table(agent, c), [case], [table])
            refresh.click(lambda: gr.update(choices=_cases(agent)), None, [case])
            confirm.click(lambda c, f, v: _confirm(agent, c, f, v), [case, field, value], [table, status])

        with gr.Tab("Библиотека"):
            gr.Markdown(
                "Законы и практика скачиваются с официальных сайтов автоматически раз в сутки. "
                "Новая редакция вытесняет старую; при противоречии применяется более сильный и более свежий "
                "документ. Чтобы добавить свой документ — положите его во «Входящие» или ссылку в "
                "«Входящие/ссылки.txt»."
            )
            update = gr.Button("Обновить сейчас")
            report = gr.Markdown(_last_report(agent))
            docs = gr.Dataframe(
                value=_documents(agent),
                headers=["Документ", "Вид", "Редакция/дата", "Статус"],
                interactive=False,
                wrap=True,
            )
            update.click(lambda: (_update(agent), _documents(agent)), None, [report, docs])
    return app


@locked
def _run(agent: Agent, uploaded: list[Any] | None, task: str) -> tuple[str, list[str], None]:
    for item in uploaded or []:
        source = Path(item if isinstance(item, str) else item.name)
        shutil.copy2(source, agent.settings.inbox / source.name)
    if not task.strip():
        task = "разбери входящие" if uploaded else ""
    if not task:
        return "Напишите задачу.", [], None
    try:
        reply = agent.handle(task)
    except LLMError as exc:
        return f"⚠ {exc}", [], None
    return reply.text, [str(f) for f in reply.files], None


def _cases(agent: Agent) -> list[tuple[str, str]]:
    return [(c.load().title, c.root.name) for c in agent.store.all()]


def _get(agent: Agent, case_id: str | None) -> Case | None:
    return agent.store.get(case_id) if case_id else None


@locked
def _table(agent: Agent, case_id: str | None) -> list[list[str]]:
    case = _get(agent, case_id)
    if case is None:
        return []
    dossier = case.load()
    rows = []
    for key, (label, _kind) in FIELDS.items():
        fact = dossier.facts.get(key)
        if fact is None:
            rows.append([label, "—", "нет данных", ""])
            continue
        source = (
            f"{fact.source.file}, стр. {fact.source.page}: «{fact.source.quote[:80]}»" if fact.source else "вручную"
        )
        rows.append([label, fact.value, "✓" if fact.confirmed else "проверьте", source])
    return rows


@locked
def _confirm(agent: Agent, case_id: str | None, key: str | None, value: str) -> tuple[list[list[str]], str]:
    case = _get(agent, case_id)
    if case is None or not key:
        return _table(agent, case_id), "Выберите дело и поле."
    dossier = case.load()
    if key in dossier.facts:
        case.confirm(key, normalize_value(key, value) if value.strip() else None)
    elif value.strip():
        case.set_fact(key, normalize_value(key, value), source=None, confirmed=True, reason="введено вручную")
    else:
        return _table(agent, case_id), "Значения нет — впишите его."
    return _table(agent, case_id), f"✓ {FIELDS[key][0]} подтверждено."


@locked
def _document(agent: Agent, case_id: str | None, kind: str) -> tuple[str, list[str]]:
    case = _get(agent, case_id)
    if case is None:
        return "Выберите дело.", []
    reply = agent.document(case, kind)  # type: ignore[arg-type]
    return reply.text, [str(f) for f in reply.files]


@locked
def _update(agent: Agent) -> str:
    return agent.update().text


@locked
def _documents(agent: Agent) -> list[list[str]]:
    return [
        [d.cite(), d.kind.value, d.edition or (f"{d.issued:%d.%m.%Y}" if d.issued else ""), d.status]
        for d in agent.library.documents(status=None)
    ]


def _last_report(agent: Agent) -> str:
    reports = sorted((agent.settings.library / "отчёты").glob("*.md"))
    return reports[-1].read_text(encoding="utf-8") if reports else "Библиотека ещё не обновлялась."


def serve(agent: Agent, *, port: int = 7860, open_browser: bool = True) -> None:
    # обновление библиотеки в фоне при старте — не чаще раза в сутки, окно открывается сразу
    threading.Thread(target=lambda: _quiet_update(agent), daemon=True).start()
    build(agent).launch(server_name="127.0.0.1", server_port=port, inbrowser=open_browser)


@locked
def _quiet_update(agent: Agent) -> None:
    try:
        agent.update(force=False)
    except Exception as exc:
        print(f"Обновление библиотеки не удалось: {exc}")

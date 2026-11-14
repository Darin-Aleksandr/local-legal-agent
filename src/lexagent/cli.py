"""Командная строка. Обычно нужна одна команда — `lexagent` (откроет окно в браузере)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from lexagent import config, gas
from lexagent.agent import Agent
from lexagent.dates import parse_date
from lexagent.llm import LLMError, Ollama


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lexagent", description="Локальный ИИ-агент для документов по ОСАГО")
    parser.add_argument("--config", type=Path, help="путь к config.toml")
    parser.add_argument("--sources", type=Path, default=Path("sources.toml"), help="реестр источников")
    sub = parser.add_subparsers(dest="command")
    ui = sub.add_parser("ui", help="открыть окно (по умолчанию)")
    ui.add_argument("--port", type=int, default=7860)
    ui.add_argument("--no-browser", action="store_true")
    sub.add_parser("doctor", help="проверить, что всё установлено")
    sub.add_parser("inbox", help="разобрать «Входящие»")
    sub.add_parser("update", help="обновить библиотеку законов и практики")
    task = sub.add_parser("task", help="выполнить задачу словами")
    task.add_argument("text", nargs="+")
    check = sub.add_parser("check", help="проверить PDF перед подачей в ГАС «Правосудие»")
    check.add_argument("files", nargs="+", type=Path)
    fix = sub.add_parser("fix", help="исправить скан для ГАС: повернуть, добавить текстовый слой, назвать")
    fix.add_argument("file", type=Path)
    fix.add_argument("--title", required=True, help="наименование документа, например «Акт осмотра»")
    fix.add_argument("--date", help="дата документа ДД.ММ.ГГГГ (по умолчанию сегодня)")
    args = parser.parse_args(argv)

    settings = config.load(args.config)
    if args.command == "check":
        return _check(args.files)
    if args.command == "fix":
        on = parse_date(args.date) if args.date else date.today()
        if on is None:
            print("Дата в формате ДД.ММ.ГГГГ")
            return 2
        target = gas.repair(args.file, args.file.parent, title=args.title, on=on, lang=settings.tesseract_lang)
        print(f"Готово: {target}")
        return _check([target])
    if args.command == "doctor":
        return doctor(settings)

    agent = Agent(settings, Ollama(settings), sources=args.sources)
    try:
        if args.command == "inbox":
            print(agent.inbox().text)
        elif args.command == "update":
            print(agent.update().text)
        elif args.command == "task":
            reply = agent.handle(" ".join(args.text))
            print(reply.text, *(f"📎 {f}" for f in reply.files), sep="\n")
        else:
            from lexagent.ui import serve

            port = getattr(args, "port", 7860)
            serve(agent, port=port, open_browser=not getattr(args, "no_browser", False))
    except LLMError as exc:
        print(f"⚠ {exc}")
        return 1
    return 0


def _check(files: list[Path]) -> int:
    failed = 0
    for path in files:
        if path.suffix.lower() != ".pdf" or not path.is_file():
            print(f"❌ {path.name}: не PDF-файл")
            failed += 1
            continue
        try:
            verdict = gas.check_pdf(path)
        except RuntimeError as exc:  # битый файл
            print(f"❌ {path.name}: не открывается ({exc})")
            failed += 1
            continue
        print("\n".join(verdict.lines()))
        failed += not verdict.ok
    return 1 if failed else 0


def doctor(settings: config.Settings) -> int:
    """Что установлено и что нет — понятными словами, с подсказкой, как починить."""
    ok = True

    def line(good: bool, text: str, fix: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"{'✅' if good else '❌'} {text}" + (f"\n   → {fix}" if not good and fix else ""))

    try:
        models = Ollama(settings).models()
        line(True, f"Ollama запущена ({settings.ollama_url})")
        for model in (settings.models.chat, settings.models.embed):
            present = any(m == model or m.startswith(model + ":") for m in models)
            line(present, f"модель {model}", f"выполните: ollama pull {model}")
    except LLMError:
        line(False, "Ollama не отвечает", "установите с ollama.com и запустите приложение Ollama")
    try:
        from lexagent.textio import tesseract

        langs = set(tesseract().get_languages())
        line(
            {"rus", "osd"} <= langs,
            f"Tesseract: языки {', '.join(sorted(langs & {'rus', 'eng', 'osd'}))}",
            "переустановите Tesseract с русским языком (install/windows.ps1)",
        )
    except Exception:
        line(False, "Tesseract не найден", "запустите install/windows.ps1")
    from lexagent.documents.render import find_office

    line(find_office() is not None, "LibreOffice (для PDF)", "winget install TheDocumentFoundation.LibreOffice")
    settings.ensure()
    line(True, f"папка данных: {settings.home}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

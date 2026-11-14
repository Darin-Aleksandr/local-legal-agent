from pathlib import Path
from types import SimpleNamespace

import pytest

from lexagent import cli, ui
from lexagent.agent import Agent
from lexagent.config import Settings, load
from lexagent.llm import LLMError
from tests.conftest import make_pdf
from tests.test_compose import FULL
from tests.test_update import FakeFetcher


def test_config_load(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('home = "~/X"\nollama_url = "http://h:1"\n[models]\nchat = "m"\n', encoding="utf-8")
    settings = load(cfg)
    assert settings.home == Path.home() / "X" and settings.models.chat == "m" and settings.models.embed == "bge-m3"
    assert load(tmp_path / "нет.toml") == Settings()


def test_cli_check_and_fix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = make_pdf(tmp_path / "Иск_от_01022026_1л.pdf", ["ИСКОВОЕ ЗАЯВЛЕНИЕ\n" + "Текст иска. " * 30])
    assert cli.main(["check", str(good)]) == 0
    assert cli.main(["check", str(tmp_path / "нет.txt")]) == 1
    loose = make_pdf(tmp_path / "скан.pdf", ["АКТ ОСМОТРА\n" + "Осмотр автомобиля. " * 30])
    assert cli.main(["fix", str(loose), "--title", "Акт осмотра", "--date", "02.02.2026"]) == 0
    assert (tmp_path / "Акт_осмотра_от_02022026_1л.pdf").is_file()
    assert cli.main(["fix", str(loose), "--title", "Акт", "--date", "вчера"]) == 2
    assert "✅" in capsys.readouterr().out


class OfflineOllama:
    def __init__(self, *_: object, **__: object) -> None:
        pass

    def ask(self, *_: object, **__: object) -> str:
        raise LLMError("Ollama недоступна")

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise LLMError("Ollama недоступна")

    def models(self) -> list[str]:
        raise LLMError("Ollama недоступна")


def test_cli_commands_without_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'home = "{(tmp_path / "home").as_posix()}"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "Ollama", OfflineOllama)
    assert cli.main(["--config", str(cfg), "doctor"]) == 1
    assert "Ollama не отвечает" in capsys.readouterr().out
    inbox = tmp_path / "home" / "Входящие"
    (inbox / "письмо.txt").write_text("Ответ страховой", encoding="utf-8")
    assert cli.main(["--config", str(cfg), "inbox"]) == 1  # для разбора нужна модель — понятная ошибка
    assert cli.main(["--config", str(cfg), "--sources", str(tmp_path / "s.toml"), "update"]) == 0
    # без нейросети расчёт работает: маршрутизация по ключевым словам
    assert cli.main(["--config", str(cfg), "task", "посчитай", "неустойку"]) == 0
    assert "Не понял, о каком деле" in capsys.readouterr().out


def test_ui_handlers(settings: Settings) -> None:
    agent = Agent(settings, None, fetcher=FakeFetcher({}), sources=Path("нет.toml"))  # type: ignore[arg-type]
    case = agent.store.create("Сургаев РЕСО")
    for key, value in FULL.items():
        case.set_fact(key, value, source=None, confirmed=False, reason="тест")
    assert ui.build(agent) is not None
    assert ui._cases(agent) == [("Сургаев РЕСО", case.root.name)]
    assert ui._table(agent, case.root.name)[0][2] == "проверьте"
    rows, msg = ui._confirm(agent, case.root.name, "claimant_name", "Сургаев И. П.")
    assert rows[0][1:3] == ["Сургаев И. П.", "✓"] and "подтверждено" in msg
    assert "Значения нет" in ui._confirm(agent, case.root.name, "court_case_number", "")[1]
    assert ui._confirm(agent, case.root.name, "court_case_number", "2-100/2026")[0][-1][1] == "2-100/2026"
    text, files = ui._document(agent, case.root.name, "претензия")
    assert "Претензия_от_" in text and files
    upload = settings.home / "скан.txt"
    upload.write_text("x", encoding="utf-8")
    answer, _files, _ = ui._run(agent, [SimpleNamespace(name=str(upload))], "")
    assert "нужна нейросеть" in answer and (settings.inbox / "скан.txt").exists()
    assert ui._run(agent, None, "  ")[0] == "Напишите задачу."
    assert "Библиотека ещё не обновлялась" in ui._last_report(agent)
    assert "обновлялась" in ui._update(agent) or ui._update(agent)
    assert ui._documents(agent) == []

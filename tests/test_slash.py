"""Slash commands in the prompt bar: parsing, settings changes (validated + saved), dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from pdd2uipath.core.config import ExecutorKind, Settings, read_toml
from pdd2uipath.tui.app import Pdd2UipathApp
from pdd2uipath.tui.screens import DetailScreen
from pdd2uipath.tui.slash import SETTABLE_KEYS, apply_setting, get_setting, parse, tokenize


def test_tokenize_and_parse() -> None:
    assert tokenize('open "C:\\My PDDs\\x.docx" --x') == ["open", "C:\\My PDDs\\x.docx", "--x"]
    assert parse("plain instruction") is None
    assert parse("/agent copilot gpt-6.1-sol") == ("agent", ["copilot", "gpt-6.1-sol"])
    assert parse("/") == ("help", [])
    assert parse("  /SET agent.model none ") == ("set", ["agent.model", "none"])


def test_apply_setting_validates_and_preserves_source() -> None:
    s = Settings(workspace_root="C:\\ws")
    s.__dict__["_config_path"] = Path("C:/cfg.toml")
    s2 = apply_setting(s, "agent.model", "github-copilot/gpt-6.1-sol")
    assert s2.agent.model == "github-copilot/gpt-6.1-sol" and not s.agent.model
    assert s2.__dict__["_config_path"] == Path("C:/cfg.toml")
    s3 = apply_setting(s2, "agent.model", "none")
    assert not s3.agent.model  # back to "no model" (None or empty, both mean backend default)
    assert apply_setting(s, "repair.max_rounds", "4").repair.max_rounds == 4
    assert apply_setting(s, "packages.prefer_prerelease", "yes").packages.prefer_prerelease is True
    assert apply_setting(s, "executor", "wsl-manual").executor == ExecutorKind.WSL_MANUAL
    with pytest.raises(ValueError):
        apply_setting(s, "executor", "teleport")
    with pytest.raises(ValueError):
        apply_setting(s, "agent.secret_key", "x")  # not settable
    with pytest.raises(ValueError):
        apply_setting(s, "repair.max_rounds", "many")
    for key in SETTABLE_KEYS:  # every advertised key resolves
        get_setting(s, key)


async def _submit(app: Pdd2UipathApp, pilot, text: str) -> None:  # type: ignore[no-untyped-def]
    bar = app.query_one("#prompt-bar", Input)
    bar.value = text
    bar.focus()
    await pilot.press("enter")
    await pilot.pause()


async def test_slash_commands_without_pipeline(tmp_path: Path) -> None:
    cfg = tmp_path / "pdd2uipath.toml"
    app = Pdd2UipathApp(Settings(workspace_root="C:\\ws"), config_path=cfg)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert "/agent" in app.slash.names() and "/copilot" in app.slash.names()
        await _submit(app, pilot, "/help")
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("escape")
        await pilot.pause()
        await _submit(app, pilot, "/agent copilot gpt-6.1-sol")
        assert app.settings.agent.backend == "copilot"
        assert app.settings.agent.model == "gpt-6.1-sol"
        saved = read_toml(cfg)
        assert saved["agent"]["backend"] == "copilot" and saved["agent"]["model"] == "gpt-6.1-sol"
        await _submit(app, pilot, "/model none")
        assert not app.settings.agent.model and not read_toml(cfg)["agent"].get("model")
        await _submit(app, pilot, "/executor wsl-manual")
        assert app.settings.executor == ExecutorKind.WSL_MANUAL
        await _submit(app, pilot, "/rounds 3")
        assert app.settings.repair.max_rounds == 3
        await _submit(app, pilot, "/set extraction.backend copilot")
        assert app.settings.extraction.backend == "copilot"
        before = app.settings.model_dump()
        await _submit(app, pilot, "/set executor teleport")  # rejected, no crash
        await _submit(app, pilot, "/nonsense")  # unknown, no crash
        await _submit(app, pilot, "/run")  # needs a pipeline, no crash
        assert app.settings.model_dump() == before
        assert app.query_one("#prompt-bar", Input).value == ""
        await _submit(app, pilot, "/config show")
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("escape")
        await pilot.pause()
        await _submit(app, pilot, "/quit")

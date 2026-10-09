from __future__ import annotations

from pathlib import Path

from pdd2uipath.core.config import ExecutorKind, Settings, load_settings
from pdd2uipath.core.platform import WINDOWS, WSL_INTEROP, WSL_NO_INTEROP
from pdd2uipath.doctor.checks import (
    apply_recommendation,
    find_skill_root,
    opencode_model_status,
    run_doctor,
)
from pdd2uipath.executors.factory import recommend_executor
from tests.conftest import windows_only


def test_recommendation_rules() -> None:
    assert recommend_executor(WINDOWS) == ExecutorKind.NATIVE
    assert recommend_executor(WSL_INTEROP) == ExecutorKind.WSL_INTEROP
    assert recommend_executor(WSL_INTEROP, interop_allowed=False) == ExecutorKind.WSL_BRIDGE
    assert recommend_executor(WSL_NO_INTEROP) == ExecutorKind.WSL_BRIDGE


def test_find_skill_root_prefers_config_then_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = home / ".uipath" / ".skills" / "skills" / "uipath-rpa"
    store.mkdir(parents=True)
    (store / "SKILL.md").write_text("# skill", encoding="utf-8")
    s = Settings(workspace_root="C:\\ws")
    path, where = find_skill_root(s, home=home)
    assert path == store and where == "uip skills store"
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# skill", encoding="utf-8")
    s.skills_root = str(custom)
    assert find_skill_root(s, home=home)[0] == custom
    s.skills_root = None
    assert find_skill_root(s, home=tmp_path / "nowhere")[0] is None


@windows_only
def test_doctor_with_fake_uip(
    win_root: str,
    fake_uip_command: list[str],
    fake_uip_log: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    s = Settings(
        workspace_root=win_root,
        uip_windows_command=fake_uip_command,
        uip_brain_command=fake_uip_command,
    )
    report = run_doctor(s, platform=WINDOWS, probe_network=False, probe_studio=True)
    ids = {c.id: c for c in report.checks}
    assert ids["platform"].status == "ok" and ids["workspace"].status == "ok"
    assert ids["uip-windows"].status == "ok" and "9.9.9-fake" in ids["uip-windows"].detail
    assert ids["rpa-tool"].status == "ok"
    assert ids["studio-host"].status == "ok"
    assert ids["executor"].detail == "native"
    assert report.surface is not None and report.surface.has(["rpa", "validate"])
    assert (
        ids["cli-surface"].status == "ok"
        and report.surface_md is not None
        and report.surface_md.is_file()
    )
    assert "| `validate` | `uip rpa validate` |" in report.surface_md.read_text(encoding="utf-8")
    cfg = tmp_path / "pdd2uipath.toml"
    apply_recommendation(s, report, cfg)
    loaded = load_settings(cfg)
    assert loaded.executor == ExecutorKind.NATIVE and loaded.uip_windows_command == fake_uip_command


import pytest  # noqa: E402


def test_opencode_model_status_cases() -> None:
    listing = """github-copilot/gpt-5-mini
github-copilot/gpt-6.1-sol
"""
    assert opencode_model_status(0, listing, "github-copilot/gpt-6.1-sol")[0] == "ok"
    status, detail, fix = opencode_model_status(0, listing, "github-copilot/claude-opus-5.5")
    assert status == "fail" and "not listed" in detail and "gpt-6.1-sol" in detail
    status, detail, fix = opencode_model_status(
        1, "Error: Provider not found: github-copilot", "github-copilot/gpt-5-mini"
    )
    assert status == "fail" and "not available" in detail and "model_picker_enabled" in fix

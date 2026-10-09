"""End-to-end pipeline tests with the fake uip, the fixture extraction and the fake agent."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

import pytest

from pdd2uipath.core.config import ExecutorKind, Settings
from pdd2uipath.core.events import EventBus, GateWaiting, StageFinished
from pdd2uipath.core.platform import WINDOWS
from pdd2uipath.core.runner import Pipeline, PipelineStop, PlainGateHandler, build_author_prompt
from tests.conftest import FIXTURES, ROOT, windows_only

FAKE_AGENT = ROOT / "fake_agent" / "fake_agent.py"


def _settings(win_root: str, fake_uip_command: list[str], tmp_path: Path, **agent: Any) -> Settings:
    s = Settings(
        workspace_root=win_root, executor=ExecutorKind.NATIVE, uip_windows_command=fake_uip_command
    )
    s.extraction.backend = "fixture"
    s.extraction.fixture_dir = str(FIXTURES / "extraction")
    s.agent.backend = "fake"
    s.agent.fake_script = str(FAKE_AGENT)
    s.repair.max_rounds = agent.pop("max_rounds", 2)
    return s


@windows_only
def test_full_run_pdd01_with_approval_gate(
    pdd01: Path, win_root: str, fake_uip_command: list[str], fake_uip_log: Path, tmp_path: Path
) -> None:
    settings = _settings(win_root, fake_uip_command, tmp_path)
    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe(seen.append)
    p = Pipeline(settings, pdd=pdd01, bus=bus, platform=WINDOWS, gates=PlainGateHandler())
    # 1. run-all pauses at the plan gate when nobody approves
    with pytest.raises(PipelineStop) as stop:
        p.run_all()
    assert stop.value.waiting_for == "plan_approval"
    assert p.state.statuses()["plan"] == "waiting" and p.state.statuses()["scaffold"] == "pending"
    assert any(isinstance(e, GateWaiting) and e.gate == "plan_approval" for e in seen)
    assert (
        p.paths.plan.is_file()
        and (p.paths.run_dir / "spec.json").is_file()
        and (p.paths.run_dir / "design.json").is_file()
    )
    # 2. quitting is safe: a fresh Pipeline resumes at the exact gate
    p2 = Pipeline(
        settings, run_dir=p.paths.run_dir, bus=bus, platform=WINDOWS, gates=PlainGateHandler()
    )
    assert p2.state.current_gate() == "plan_approval"
    # 3. approve and finish
    p2.approve_plan("Tester")
    p2.run_all()
    st = p2.state.statuses()
    assert all(
        st[s] == "done"
        for s in ("ingest", "extract", "design", "plan", "scaffold", "author", "verify", "report")
    ), st
    project = p2.project_dir
    assert (project / "project.json").is_file() and (
        project / "docs" / "plans" / f"{p2.paths.slug}.md"
    ).is_file()
    assert (project / ".gitattributes").is_file() and (project / ".git").is_dir()
    assert (project / ".pdd2uipath" / "bin" / "uip").is_file()
    # the Windows gate ran per-file validate + build through the executor
    calls = [
        line
        for line in fake_uip_log.read_text(encoding="utf-8").splitlines()
        if '"rpa", "validate"' in line
    ]
    assert len(calls) >= 12
    assert '"rpa", "build"' in fake_uip_log.read_text(encoding="utf-8")
    # verification artefacts
    assert p2.last_gate is not None and p2.last_gate.verdict.passed
    assert p2.last_gate.verdict.expected_stub_errors, (
        "stub mode must report the expected unconfigured-target errors"
    )
    rows = list(csv.DictReader(p2.paths.traceability.open(encoding="utf-8")))
    step_ids = {r["step_id"] for r in rows}
    assert step_ids == {f"S{i:02d}" for i in range(1, 13)}
    assert not [r for r in rows if r["status"] in ("missing", "log-only")]
    assert any(r["status"] == "stub" and r["step_id"] == "S06" for r in rows)
    report = p2.paths.report.read_text(encoding="utf-8")
    assert "## TODO Indicate" in report and "AP_Invoices" in report and "PASSED" in report
    assert not re.search(r'(?i)password\s*=\s*"[^"\[]', report)
    assert p2.state.repair_round == 0
    # 4. re-running with unchanged inputs is a no-op for the deterministic stages
    seen.clear()
    p2.ingest()
    p2.extract()
    p2.design_stage()
    p2.plan()
    skipped = [e.stage for e in seen if isinstance(e, StageFinished) and e.status == "skipped"]
    assert skipped == ["ingest", "extract", "design", "plan"]
    # 5. an edited plan after approval is rejected at scaffold/author
    p2.paths.plan.write_text(
        p2.paths.plan.read_text(encoding="utf-8") + "\n- [ ] extra\n", encoding="utf-8"
    )
    with pytest.raises(PipelineStop, match="edited after approval"):
        p2.author(force=True)


@windows_only
def test_repair_round_fixes_log_only_stubs(
    pdd01: Path,
    win_root: str,
    fake_uip_command: list[str],
    fake_uip_log: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(win_root, fake_uip_command, tmp_path)
    p = Pipeline(settings, pdd=pdd01, platform=WINDOWS, gates=PlainGateHandler(approver="Tester"))
    p.run_all(stop_after="scaffold")
    monkeypatch.setenv("FAKE_AGENT_MODE", "log-only")
    p.author()
    # the first gate finds Log-only UI steps; the repair round (agent in stub mode) fixes them
    monkeypatch.setenv("FAKE_AGENT_MODE", "stub")
    p.verify()
    assert p.state.repair_round == 1
    assert p.last_audit is not None and not p.last_audit.log_only_ui_steps


@windows_only
def test_gate_fails_after_max_rounds(
    pdd01: Path,
    win_root: str,
    fake_uip_command: list[str],
    fake_uip_log: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(win_root, fake_uip_command, tmp_path, max_rounds=1)
    p = Pipeline(settings, pdd=pdd01, platform=WINDOWS, gates=PlainGateHandler(approver="Tester"))
    p.run_all(stop_after="scaffold")
    monkeypatch.setenv("FAKE_AGENT_MODE", "skip:S08")
    p.author()
    with pytest.raises(PipelineStop, match="gate failed after 1 repair round"):
        p.verify()
    assert p.state.statuses()["verify"] == "failed"
    p.report()
    assert "FAILED" in p.paths.report.read_text(encoding="utf-8")
    rows = list(csv.DictReader(p.paths.traceability.open(encoding="utf-8")))
    assert any(r["step_id"] == "S08" and r["status"] == "missing" for r in rows)


@windows_only
def test_pdd03_rejected_at_ingest(
    pdd03: Path, win_root: str, fake_uip_command: list[str], tmp_path: Path
) -> None:
    p = Pipeline(_settings(win_root, fake_uip_command, tmp_path), pdd=pdd03, platform=WINDOWS)
    with pytest.raises(PipelineStop, match="rejected at the approval gate") as stop:
        p.run_all()
    assert "Draft" in str(stop.value) and "Pending" in str(stop.value)
    assert p.state.statuses()["ingest"] == "failed" and p.state.statuses()["extract"] == "pending"


@windows_only
def test_pdd02_stops_at_design(
    pdd02: Path, win_root: str, fake_uip_command: list[str], fake_uip_log: Path, tmp_path: Path
) -> None:
    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe(seen.append)
    p = Pipeline(
        _settings(win_root, fake_uip_command, tmp_path), pdd=pdd02, bus=bus, platform=WINDOWS
    )
    with pytest.raises(PipelineStop, match="contradiction") as stop:
        p.run_all()
    assert "blocking open question" in str(stop.value)
    assert p.state.statuses()["design"] == "stopped" and p.state.statuses()["plan"] == "pending"
    gate = next(e for e in seen if isinstance(e, GateWaiting) and e.gate == "open_questions")
    assert gate.payload["contradiction"] and [q["id"] for q in gate.payload["questions"]] == [
        "OQ-1",
        "OQ-3",
        "OQ-4",
        "OQ-5",
    ]
    assert p.design is not None and p.design.contradiction is not None


def test_author_prompt_mentions_plan_rules_and_manual_mode() -> None:
    text = build_author_prompt(
        "docs/plans/x.md", manual_mode=True, repair_text=None, deny_tools=["shell(uip solution:*)"]
    )
    assert (
        "docs/plans/x.md" in text
        and "TODO Indicate" in text
        and "wsl-manual" in text
        and "shell(uip solution:*)" in text
    )
    text2 = build_author_prompt(
        "docs/plans/x.md", manual_mode=False, repair_text="ERRORS", deny_tools=[]
    )
    assert "REPAIR ROUND. ERRORS" in text2 and "uip rpa validate" in text2

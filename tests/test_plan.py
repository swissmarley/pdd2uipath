from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pdd2uipath.core.config import Settings
from pdd2uipath.core.state import RunState
from pdd2uipath.design.models import DesignResult
from pdd2uipath.design.rules import design
from pdd2uipath.ingest.parse import parse_document
from pdd2uipath.plan.render import (
    PlanApprovalError,
    PlanContext,
    approve,
    build_tasks,
    check_approval,
    render_plan,
    write_plan,
)
from pdd2uipath.spec.extract import FixtureBackend, extract_spec
from pdd2uipath.spec.models import ProcessSpec
from tests.conftest import FIXTURES
from tests.test_design import _search, _versions

GOLDEN = FIXTURES / "golden"


def _plan(pdd01: Path, *, manual: bool = False) -> tuple[str, ProcessSpec, DesignResult]:
    spec = extract_spec(
        parse_document(pdd01),
        Settings(workspace_root="C:\\ws"),
        FixtureBackend(FIXTURES / "extraction"),
    )
    result = design(
        spec, Settings(workspace_root="C:\\ws"), template_search=_search, package_versions=_versions
    )
    ctx = PlanContext(
        pdd_filename=pdd01.name,
        pdd_sha256=spec.extraction.pdd_sha256,
        project_dir_windows="C:\\ws\\pdd-fin-ap-014\\VendorInvoiceProcessing",
        plan_path="docs/plans/pdd-fin-ap-014.md",
        executor="wsl-manual" if manual else "wsl-bridge",
        manual_mode=manual,
        generated_on="2026-10-09",
    )
    return render_plan(spec, result, ctx), spec, result


def test_plan_structure_pdd01(pdd01: Path) -> None:
    text, _spec, result = _plan(pdd01)
    assert text.startswith("# Vendor Invoice Processing (PO-based) Implementation Plan")
    for header in (
        "**Execution autonomy:** autonomous",
        "**UI capture:** stub",
        "**Target framework:** Windows",
        "**Expression language:** VisualBasic",
    ):
        assert header in text
    assert "## Stop conditions" in text and "## Task T0 — uipath-rpa — Install packages" in text
    assert (
        "id=UiPath.Mail.Activities,version=2.12.10" in text
        and "id=UiPath.PDF.Activities,version=3.22.3" in text
    )
    assert "## Task T1 — uipath-rpa — Configure the REFramework" in text and "`AP_Invoices`" in text
    tasks = re.findall(r"^## Task T(\d+) — uipath-rpa — `([^`]+)` \(steps? ([^)]+)\)", text, re.M)
    assert len(tasks) == len({w.file for w in result.workflows})
    assert any(t[1] == "Performer/S06_LogInToSAP.xaml" and t[2] == "S06" for t in tasks)
    assert "(§4.3/S06)" in text and "TODO Indicate" in text
    rule = "Use values, mappings, and structure exactly as documented in the plan at docs/plans/pdd-fin-ap-014.md. Do not infer or guess."
    assert text.count(rule) == len(tasks) + 3
    assert f"## Task T{len(tasks) + 2} — uipath-rpa — Project build gate" in text
    assert "Validate:" in text and "deferred to the Windows gate" not in text
    assert "AP_SAP_Credential" in text
    assert not re.search(r'(?i)password\s*=\s*"[^"\[]', text)  # names only, never values


def test_plan_manual_mode_defers_validation(pdd01: Path) -> None:
    text, _, _ = _plan(pdd01, manual=True)
    assert "Executor is `wsl-manual`" in text and text.count("deferred to the Windows gate") >= 3


def test_plan_golden_pdd01(pdd01: Path) -> None:
    text, _, _ = _plan(pdd01)
    golden = GOLDEN / "plan_pdd01.md"
    if os.environ.get("UPDATE_GOLDEN") or not golden.exists():
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8", newline="\n")
    expected = golden.read_text(encoding="utf-8")
    assert text == expected, "plan rendering changed; run with UPDATE_GOLDEN=1 to accept"


def test_build_tasks_groups_by_file(pdd01: Path) -> None:
    _, spec, result = _plan(pdd01)
    tasks = build_tasks(spec, result, first_number=2)
    assert (
        tasks[0]["number"] == 2
        and tasks[0]["file"] == "Framework/InitAllSettings.xaml"
        and tasks[0]["is_template_file"]
    )
    s06 = next(t for t in tasks if "S06" in t["step_ids"])
    assert s06["ui"] and s06["application"] == "SAP GUI"
    assert s06["blocked_by"] == "T0, T1"


def test_approval_round_trip_and_invalidation(pdd01: Path, tmp_path: Path) -> None:
    text, _, _ = _plan(pdd01)
    plan_path = tmp_path / "docs" / "plans" / "pdd-fin-ap-014.md"
    sha = write_plan(plan_path, text)
    state = RunState(
        run_id="r1",
        slug="pdd-fin-ap-014",
        pdd_path=str(pdd01),
        pdd_sha256="x",
        project_name="P",
        run_dir_windows="C:\\ws\\r",
    )
    ok, reason = check_approval(state, plan_path)
    assert not ok and "not been approved" in reason
    with pytest.raises(PlanApprovalError):
        approve(state, plan_path, "  ", tmp_path / "approval.json")
    record = approve(state, plan_path, "Marco Rossi", tmp_path / "approval.json")
    assert record.plan_sha256 == sha and (tmp_path / "approval.json").exists()
    assert record.approved_at.tzinfo is not None and record.approved_at <= datetime.now(UTC)
    ok, reason = check_approval(state, plan_path)
    assert ok and "Marco Rossi" in reason
    plan_path.write_text(text + "\n- [ ] sneaky extra task\n", encoding="utf-8")
    ok, reason = check_approval(state, plan_path)
    assert not ok and "edited after approval" in reason

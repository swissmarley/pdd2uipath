from __future__ import annotations

from pathlib import Path
from typing import Any

from pdd2uipath.core.config import Settings
from pdd2uipath.design.rules import (
    classify_step,
    decide_framework,
    decide_template,
    design,
    map_workflows,
    resolve_template,
)
from pdd2uipath.ingest.parse import parse_document
from pdd2uipath.spec.extract import FixtureBackend, extract_spec
from pdd2uipath.spec.models import ProcessSpec
from tests.conftest import FIXTURES

OFFICIAL = {
    "packageId": "UiPath.Template.REFramework",
    "version": "26.10.1",
    "source": "Official",
    "title": "REFramework",
}
MARKET = {
    "packageId": "Enhanced.REFrameWork",
    "version": "1.0.1",
    "source": "Marketplace",
    "title": "Enhanced REFrameWork",
}
VERSIONS = {
    "UiPath.Mail.Activities": ["2.12.10", "2.12.9-preview", "2.11.8"],
    "UiPath.PDF.Activities": ["3.22.3", "3.20.1-preview"],
    "UiPath.UIAutomation.Activities": ["26.10.5", "25.10.42"],
    "UiPath.Excel.Activities": ["3.2.1"],
}


def _spec(path: Path) -> ProcessSpec:
    return extract_spec(
        parse_document(path),
        Settings(workspace_root="C:\\ws"),
        FixtureBackend(FIXTURES / "extraction"),
    )


def _search(query: str) -> list[dict[str, Any]]:
    return [OFFICIAL, MARKET]


def _versions(pid: str) -> list[str]:
    return VERSIONS.get(pid, ["1.0.0"])


def test_pdd01_design_is_reframework_windows(pdd01: Path) -> None:
    spec = _spec(pdd01)
    settings = Settings(workspace_root="C:\\ws")
    result = design(spec, settings, template_search=_search, package_versions=_versions)
    assert not result.stopped, result.stop_reasons
    assert result.target_framework == "Windows" and result.contradiction is None
    assert result.template.kind == "reframework" and result.template.status == "resolved"
    assert (
        result.template.package_id == "UiPath.Template.REFramework"
        and result.template.package_version == "26.10.1"
    )
    assert result.project_name == "VendorInvoiceProcessing"
    files = {w.step_id: (w.file, w.role, w.invoked_from) for w in result.workflows}
    assert files["S01"] == ("Framework/InitAllSettings.xaml", "settings", "Main.xaml (Init)")
    assert files["S02"][0].startswith("Dispatcher/S02_") and files["S02"][1] == "dispatcher"
    assert files["S06"] == (
        "Performer/S06_LogInToSAP.xaml",
        "app-init",
        "Framework/InitAllApplications.xaml",
    )
    assert files["S10"][1] == "process-step" and files["S10"][2].startswith(
        "Framework/Process.xaml"
    )
    assert files["S12"][1] == "end-of-run"
    ui = [w.step_id for w in result.workflows if w.ui]
    assert ui == ["S06", "S08", "S10"]
    installs = {p.id: p.version for p in result.installs()}
    assert installs == {"UiPath.Mail.Activities": "2.12.10", "UiPath.PDF.Activities": "3.22.3"}
    uia = next(p for p in result.packages if p.id == "UiPath.UIAutomation.Activities")
    assert uia.from_template and not uia.install  # stub mode keeps the template version
    assert result.config_values["queue_name"] == "AP_Invoices"
    assert (
        "AP_SAP_Credential" in result.config_values["assets"]
        and "AP_Mailbox" in result.config_values["assets"]
    )
    assert result.config_values["max_retry_number"] == 2
    topics = [d.topic for d in result.decisions]
    assert (
        "target framework" in topics
        and "project template" in topics
        and "activity packages" in topics
    )
    assert all(d.rationale for d in result.decisions)


def test_pdd02_stops_on_framework_contradiction(pdd02: Path) -> None:
    spec = _spec(pdd02)
    result = design(
        spec, Settings(workspace_root="C:\\ws"), template_search=_search, package_versions=_versions
    )
    assert result.stopped
    assert result.contradiction is not None
    assert any("linux" in s or "serverless" in s for s in result.portable_signals)
    assert any(".xlsm" in s or "macro" in s for s in result.windows_signals)
    assert result.target_framework == ""
    assert result.template.kind == "blank"
    assert [q.id for q in result.blocking_questions] == ["OQ-1", "OQ-3", "OQ-4", "OQ-5"]
    assert any("contradiction" in s for s in result.stop_reasons)
    assert any("blocking open question" in s for s in result.stop_reasons)


def test_template_resolution_rules() -> None:
    choice, decision, stop = resolve_template("reframework", lambda q: [OFFICIAL, MARKET])
    assert choice.status == "resolved" and decision is not None and stop is None
    choice, _, stop = resolve_template(
        "reframework",
        lambda q: [OFFICIAL, {**OFFICIAL, "packageId": "UiPath.Template.REFramework.CSharp"}],
    )
    assert choice.status == "ambiguous" and stop and "ambiguous" in stop
    choice, _, stop = resolve_template("reframework", lambda q: [MARKET])
    assert choice.status == "ambiguous" and stop and "Marketplace" in stop
    choice, _, stop = resolve_template("reframework", lambda q: [])
    assert stop and "no REFramework template" in stop
    choice, _, stop = resolve_template("reframework", None)
    assert choice.status == "unresolved" and stop
    choice, _, stop = resolve_template("blank", None)
    assert choice.template_id == "BlankTemplate" and stop is None


def test_framework_default_when_no_signal(pdd01: Path) -> None:
    spec = _spec(pdd01)
    bare = spec.model_copy(
        update={
            "applications": [],
            "steps": [],
            "robot_environment": spec.robot_environment.model_copy(
                update={"description": "", "os_hints": []}
            ),
        }
    )
    fw, contradiction, decision = decide_framework(bare, "Portable")
    assert fw == "Portable" and contradiction is None and "default" in decision.rationale


def test_template_rule_blank_for_single_run(pdd02: Path) -> None:
    kind, decision = decide_template(_spec(pdd02))
    assert (
        kind == "blank" and "single run" in decision.rationale
    ) or "No queue" in decision.rationale


def test_step_classification_and_blank_mapping(pdd01: Path) -> None:
    spec = _spec(pdd01)
    roles = {s.step_id: classify_step(s, "S12") for s in spec.steps}
    assert (
        roles["S01"] == "settings"
        and roles["S06"] == "app-init"
        and roles["S12"] == "end-of-run"
        and roles["S07"] == "process-step"
    )
    blank = map_workflows(spec, "blank")
    assert all(
        w.file.startswith("Steps/") and w.invoked_from.startswith("Main.xaml") for w in blank
    )


def test_unresolved_versions_stop(pdd01: Path) -> None:
    result = design(
        _spec(pdd01),
        Settings(workspace_root="C:\\ws"),
        template_search=_search,
        package_versions=None,
    )
    assert any("not resolved" in s for s in result.stop_reasons)


def test_live_capture_requires_uia_minimum(pdd01: Path) -> None:
    settings = Settings(workspace_root="C:\\ws")
    settings.ui_capture = "live"
    result = design(_spec(pdd01), settings, template_search=_search, package_versions=_versions)
    uia = next(p for p in result.packages if p.id == "UiPath.UIAutomation.Activities")
    assert uia.install and uia.min_version == "26.10.2" and uia.version == "26.10.5"

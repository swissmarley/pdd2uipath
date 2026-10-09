from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from pdd2uipath.core.config import Settings
from pdd2uipath.ingest.parse import parse_document
from pdd2uipath.spec import extract as extract_module
from pdd2uipath.spec.extract import (
    ExtractionError,
    FixtureBackend,
    build_prompt,
    choose_backend,
    extract_spec,
)
from pdd2uipath.spec.models import ProcessSpec, ProcessSpecDraft, strict_json_schema
from pdd2uipath.spec.tables import extract_facts
from tests.conftest import FIXTURES

FIXTURE_DIR = FIXTURES / "extraction"


def test_strict_schema_requires_everything() -> None:
    schema = strict_json_schema(ProcessSpecDraft)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    step = schema["$defs"]["Step"]
    assert step["additionalProperties"] is False and "source_ref" in step["required"]
    assert "default" not in json.dumps(step["properties"]["details"])


def test_deterministic_facts_pdd01(pdd01: Path) -> None:
    facts = extract_facts(parse_document(pdd01))
    assert facts.document_control["Document ID"] == "PDD-FIN-AP-014"
    assert [s.step_id for s in facts.steps] == [f"S{i:02d}" for i in range(1, 13)]
    s10 = next(s for s in facts.steps if s.step_id == "S10")
    assert (
        s10.part == "Performer" and s10.business_rules == ["BR-06"] and s10.exceptions == ["SE-03"]
    )
    assert [r.id for r in facts.business_rules] == [f"BR-0{i}" for i in range(1, 8)]
    assert {e.id for e in facts.exceptions} == {f"BE-0{i}" for i in range(1, 7)} | {
        f"SE-0{i}" for i in range(1, 5)
    }
    assert next(e for e in facts.exceptions if e.id == "SE-04").kind == "system"
    assert [a.name for a in facts.applications][:2] == [
        "Microsoft Outlook (classic desktop)",
        "SAP GUI for Windows",
    ]
    assert facts.operational["Robot environment"].startswith("Unattended robots on Windows 11 VDI")
    assert len(facts.fields) == 9 and facts.fields[0].mandatory and not facts.fields[-1].mandatory
    assert "steps" in facts.fields_found() and "open_points" not in facts.fields_found()
    assert "S10 [§4.2]" in facts.render()


def test_deterministic_facts_pdd02(pdd02: Path) -> None:
    facts = extract_facts(parse_document(pdd02))
    assert [s.step_id for s in facts.steps] == ["S01", "S02", "S03", "S04", "S05", "S06"]
    assert facts.operational["Robot environment"].startswith("Linux serverless")
    assert [o.id for o in facts.open_points] == ["OP-1", "OP-2", "OP-3"]
    assert next(e for e in facts.exceptions if e.id == "BE-01").handling == "TBD"


def test_prompt_contains_anchors_and_facts(pdd01: Path) -> None:
    doc = parse_document(pdd01)
    prompt = build_prompt(doc, extract_facts(doc))
    assert (
        "DETERMINISTIC FACTS" in prompt
        and "[§4.3/S06]" in prompt
        and "STEPS (from the summary table)" in prompt
    )


def test_extract_with_fixture_backend_pdd01(pdd01: Path) -> None:
    settings = Settings(workspace_root="C:\\ws")
    spec = extract_spec(parse_document(pdd01), settings, FixtureBackend(FIXTURE_DIR))
    assert isinstance(spec, ProcessSpec)
    assert spec.meta.document_id == "PDD-FIN-AP-014"
    assert [s.step_id for s in spec.steps] == [f"S{i:02d}" for i in range(1, 13)]
    assert [s.step_id for s in spec.ui_steps()] == ["S06", "S08", "S10"]
    assert spec.transaction.queue_name == "AP_Invoices" and spec.transaction.dispatcher_performer
    assert spec.credential_assets() == ["AP_SAP_Credential"]
    assert spec.extraction.backend == "fixture" and "steps" in spec.extraction.deterministic_fields
    assert spec.blocking_questions() == []
    for s in spec.steps:
        assert s.source_ref.startswith("§")


def test_extract_with_fixture_backend_pdd02(pdd02: Path) -> None:
    settings = Settings(workspace_root="C:\\ws")
    spec = extract_spec(parse_document(pdd02), settings, FixtureBackend(FIXTURE_DIR))
    assert spec.robot_environment.robot_type == "serverless"
    assert [q.id for q in spec.blocking_questions()] == ["OQ-1", "OQ-3", "OQ-4", "OQ-5"]
    assert not spec.transaction.dispatcher_performer


def test_merge_adds_missing_steps_and_open_points(pdd02: Path) -> None:
    class Partial:
        name = "partial"
        model = ""

        def extract(
            self, system: str, prompt: str, schema: dict[str, Any], context: dict[str, str]
        ) -> dict[str, Any]:
            data = json.loads((FIXTURE_DIR / "pdd-fin-tre-007.json").read_text(encoding="utf-8"))
            data["steps"] = [s for s in data["steps"] if s["step_id"] != "S05"]
            data["open_questions"] = []
            return data

    spec = extract_spec(parse_document(pdd02), Settings(workspace_root="C:\\ws"), Partial())
    assert [s.step_id for s in spec.steps] == ["S01", "S02", "S03", "S04", "S05", "S06"]
    s05 = spec.step("S05")
    assert s05 is not None and s05.is_ui_interaction
    assert any("omitted step S05" in w for w in spec.extraction.warnings)
    assert [q.id for q in spec.open_questions] == ["OP-1", "OP-2", "OP-3"]


def test_retry_once_then_fail(pdd01: Path) -> None:
    calls: list[str] = []

    class Bad:
        name = "bad"
        model = ""

        def extract(
            self, system: str, prompt: str, schema: dict[str, Any], context: dict[str, str]
        ) -> dict[str, Any]:
            calls.append(prompt[-120:])
            return {"meta": {"title": "x"}}

    with pytest.raises(ExtractionError, match="failed twice"):
        extract_spec(parse_document(pdd01), Settings(workspace_root="C:\\ws"), Bad())
    assert len(calls) == 2 and ("failed schema validation" in calls[1] or "Fix it" in calls[1])


def test_loads_json_tolerates_fences_and_prose() -> None:
    assert extract_module._loads_json('Sure! ```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_module._loads_json('{"a": {"b": [1, 2]}} trailing') == {"a": {"b": [1, 2]}}
    with pytest.raises(ExtractionError):
        extract_module._loads_json("no json here")


def test_final_text_picks_last_json_event() -> None:
    out = '{"type":"step","text":"thinking"}\n{"type":"result","text":"{\\"meta\\": 1}"}\n'
    assert extract_module._final_text(out) == '{"meta": 1}'
    assert extract_module._final_text('plain {"x": 1}') == 'plain {"x": 1}'


def test_choose_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(workspace_root="C:\\ws")
    s.extraction.backend = "fixture"
    assert choose_backend(s).name == "fixture"
    s.extraction.backend = "openai-compatible"
    with pytest.raises(ExtractionError, match="base_url"):
        choose_backend(s)
    s.extraction.base_url = "http://localhost:11434/v1"
    s.extraction.model = "qwen2.5-coder"
    assert choose_backend(s).name == "openai-compatible"
    s.extraction.backend = "auto"
    assert choose_backend(s).name == "openai-compatible"
    s.extraction.base_url = ""
    monkeypatch.setattr(
        extract_module, "_which", lambda name: "/usr/bin/opencode" if name == "opencode" else None
    )
    assert choose_backend(s).name == "opencode"
    monkeypatch.setattr(
        extract_module, "_which", lambda name: "/usr/bin/copilot" if name == "copilot" else None
    )
    assert choose_backend(s).name == "copilot"
    monkeypatch.setattr(extract_module, "_which", lambda name: None)
    with pytest.raises(ExtractionError, match="no extraction backend"):
        choose_backend(s)
    s.extraction.backend = "command"
    with pytest.raises(ExtractionError, match="command is required"):
        choose_backend(s)
    s.extraction.command = ["my-llm", "--prompt-file", "{prompt_file}"]
    assert choose_backend(s).name == "command"


def test_command_backend_runs_script(tmp_path: Path) -> None:
    script = tmp_path / "echo_json.py"
    script.write_text(
        "import sys, json, pathlib\n"
        "text = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "print('log line')\n"
        "print(json.dumps({'ok': 'JSON SCHEMA' in text and 'DETERMINISTIC FACTS' in text}))\n",
        encoding="utf-8",
    )
    backend = extract_module.CommandBackend(
        [sys.executable, "-I", str(script), "{prompt_file}"], name="test"
    )
    out = backend.extract("SYS", "DETERMINISTIC FACTS: x", {"type": "object"}, {})
    assert out == {"ok": True}

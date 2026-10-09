"""Stage 4: render ``docs/plans/<slug>.md`` and manage its human approval."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from pdd2uipath import __version__
from pdd2uipath.core.state import ApprovalRecord, RunState, atomic_write_text, sha256_file
from pdd2uipath.design.models import DesignResult
from pdd2uipath.plan.template import PLAN_TEMPLATE
from pdd2uipath.spec.models import ProcessSpec, Step

_env = Environment(
    undefined=StrictUndefined, autoescape=False, trim_blocks=False, lstrip_blocks=False
)
_env.filters["join"] = lambda seq, sep=", ": sep.join(str(x) for x in seq)


@dataclass(frozen=True)
class PlanContext:
    pdd_filename: str
    pdd_sha256: str
    project_dir_windows: str
    plan_path: str
    executor: str
    manual_mode: bool
    generated_on: str | None = None


def _understanding(spec: ProcessSpec, design: DesignResult) -> str:
    parts = [spec.meta.purpose.strip() or f"Automate '{spec.meta.title}'."]
    if spec.transaction.unit:
        parts.append(
            f"Transaction unit: {spec.transaction.unit}"
            + (f"; queue '{spec.transaction.queue_name}'" if spec.transaction.queue_name else "")
            + (f"; {spec.transaction.volumes}" if spec.transaction.volumes else "")
            + "."
        )
    if spec.schedule.schedule:
        parts.append(
            f"Schedule: {spec.schedule.schedule}"
            + (f"; SLA: {spec.schedule.sla}" if spec.schedule.sla else "")
            + "."
        )
    ui = [s.step_id for s in spec.ui_steps()]
    parts.append(
        f"{len(spec.steps)} PDD steps map to {len({w.file for w in design.workflows})} workflow files; "
        f"UI steps {', '.join(ui) if ui else 'none'} are authored as placeholder-selector stubs for the developer to Indicate in Studio."
    )
    return " ".join(parts)


def _app_type(spec: ProcessSpec) -> str:
    kinds = {a.kind for a in spec.applications}
    labels = []
    if kinds & {"desktop", "excel", "word"}:
        labels.append("desktop")
    if "web" in kinds:
        labels.append("web")
    return " / ".join(labels) or "N/A"


def _step_data_items(spec: ProcessSpec, step: Step) -> list[str]:
    """Data items a step works with, spelled out so the agent never has to open the PDD.

    A step that refers to an appendix or a field list gets the whole table; otherwise the
    items whose names appear in its inputs, outputs or details.
    """
    text = " ".join([step.action, *step.details, *step.inputs, *step.outputs]).lower()
    wants_all = "appendix" in text or "field list" in text or "all fields" in text
    out: list[str] = []
    for d in spec.data_items:
        if wants_all or d.name.lower() in text:
            out.append(
                f"{d.name} ({d.direction}; format {d.format or '-'}; "
                f"{'mandatory' if d.mandatory else 'optional'}; target {d.target or '-'})"
            )
    return out


def build_tasks(
    spec: ProcessSpec, design: DesignResult, *, first_number: int
) -> list[dict[str, Any]]:
    rules = {r.id: r.text for r in spec.business_rules}
    excs = {e.id: f"{e.id}: {e.description} -> {e.handling}" for e in spec.exceptions}
    tasks: list[dict[str, Any]] = []
    number = first_number
    for file, mappings in design.by_file().items():
        steps: list[dict[str, Any]] = []
        for m in mappings:
            s = spec.step(m.step_id)
            steps.append(
                {
                    "step_id": m.step_id,
                    "name": m.step_name,
                    "pdd_ref": m.pdd_ref,
                    "action": s.action if s else "",
                    "details": s.details if s else [],
                    "business_rules": m.business_rules,
                    "rules_text": [f"{r}: {rules.get(r, '?')}" for r in m.business_rules],
                    "exceptions": m.exceptions,
                    "exceptions_text": [excs.get(e, e) for e in m.exceptions],
                    "data_items": _step_data_items(spec, s) if s else [],
                }
            )
        first = mappings[0]
        tasks.append(
            {
                "number": number,
                "file": file,
                "is_template_file": file.startswith("Framework/"),
                "steps": steps,
                "step_ids": [m.step_id for m in mappings],
                "location": first.location,
                "invoked_from": first.invoked_from,
                "ui": any(m.ui for m in mappings),
                "application": first.application,
                "notes": [n for m in mappings for n in m.notes],
                "blocked_by": "T0, T1" if design.template.kind == "reframework" else "T0",
            }
        )
        number += 1
    return tasks


def render_plan(spec: ProcessSpec, design: DesignResult, ctx: PlanContext) -> str:
    first = 2 if design.template.kind == "reframework" else 1
    tasks = build_tasks(spec, design, first_number=first)
    gate_number = first + len(tasks)
    template_label = {
        "reframework": f"REFramework template {design.template.package_id or ''} {design.template.package_version or ''}".strip(),
        "blank": "BlankTemplate",
        "library": "LibraryProcessTemplate",
        "test": "TestAutomationProjectTemplate",
    }[design.template.kind]
    text = _env.from_string(PLAN_TEMPLATE).render(
        title=spec.meta.title,
        goal=(spec.meta.purpose.split(".")[0].strip() + ".")
        if spec.meta.purpose
        else spec.meta.title,
        meta=spec.meta,
        pdd_filename=ctx.pdd_filename,
        pdd_sha256=ctx.pdd_sha256,
        template_label=template_label,
        template_kind=design.template.kind,
        project_name=design.project_name,
        project_dir_windows=ctx.project_dir_windows,
        expression_language=design.expression_language,
        target_framework=design.target_framework,
        ui_capture=design.ui_capture,
        app_type=_app_type(spec),
        version=__version__,
        generated_on=ctx.generated_on or datetime.now(UTC).strftime("%Y-%m-%d"),
        executor=ctx.executor,
        manual_mode=ctx.manual_mode,
        understanding=_understanding(spec, design),
        decisions=design.decisions,
        nonblocking_questions=design.nonblocking_questions,
        config=design.config_values,
        template_packages=[p.id for p in design.packages if p.from_template],
        installs=design.installs(),
        data_items=spec.data_items,
        business_rules=spec.business_rules,
        exceptions=spec.exceptions,
        applications=spec.applications,
        transaction=spec.transaction,
        schedule=spec.schedule,
        plan_path=ctx.plan_path,
        tasks=tasks,
        gate_task_number=gate_number,
        gate_blocked_by=", ".join(f"T{t['number']}" for t in tasks) or "T0",
    )
    return text.replace("\r\n", "\n")


def write_plan(path: Path, text: str) -> str:
    atomic_write_text(path, text)
    return sha256_file(path)


class PlanApprovalError(RuntimeError):
    pass


def approve(state: RunState, plan_path: Path, approver: str, approval_path: Path) -> ApprovalRecord:
    approver = approver.strip()
    if not approver:
        raise PlanApprovalError("an approver name is required")
    if not plan_path.is_file():
        raise PlanApprovalError(f"plan not found: {plan_path}")
    record = ApprovalRecord(
        approver=approver,
        approved_at=datetime.now(UTC),
        plan_path=str(plan_path),
        plan_sha256=sha256_file(plan_path),
    )
    state.approval = record
    atomic_write_text(approval_path, json.dumps(record.model_dump(mode="json"), indent=2))
    return record


def check_approval(state: RunState, plan_path: Path) -> tuple[bool, str]:
    """``(ok, reason)``: the recorded approval must match the plan's current hash."""
    if state.approval is None:
        return (
            False,
            "the plan has not been approved (run `pdd2uipath approve-plan --approver <name>` or approve it in the TUI)",
        )
    if not plan_path.is_file():
        return False, f"plan file missing: {plan_path}"
    current = sha256_file(plan_path)
    if current != state.approval.plan_sha256:
        return (
            False,
            f"the plan was edited after approval by {state.approval.approver} "
            f"(approved sha256 {state.approval.plan_sha256[:12]}, current {current[:12]}); re-approve it",
        )
    return (
        True,
        f"approved by {state.approval.approver} at {state.approval.approved_at.isoformat(timespec='seconds')}",
    )

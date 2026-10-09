"""Independent Python checks on the authored project (never trust the agent's claims)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from pdd2uipath.design.models import DesignResult
from pdd2uipath.spec.models import ProcessSpec

UIX_NS = "http://schemas.uipath.com/workflow/activities/uix"
UI_NS = "http://schemas.uipath.com/workflow/activities"
SAP2010_NS = "http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation"

#: Classic (non-"N") UI Automation activities in the ``ui`` namespace.
CLASSIC_UIA = {
    "Click",
    "TypeInto",
    "GetText",
    "GetValue",
    "OpenApplication",
    "OpenBrowser",
    "AttachWindow",
    "AttachBrowser",
    "SelectItem",
    "Check",
    "SendHotkey",
    "GetAttribute",
    "ElementExists",
    "FindElement",
    "Hover",
    "SetText",
    "TypeSecureText",
    "ClickImage",
    "DoubleClick",
    "NavigateTo",
    "CloseApplication",
    "CloseWindow",
    "ExtractData",
    "GetOCRText",
}
LOG_ACTIVITIES = {"LogMessage", "WriteLine"}
_STEP_RE = re.compile(r"\bS\d{2}\b")
_TODO_RE = re.compile(r"TODO\s*Indicate", re.I)
_CONFIRM_RE = re.compile(r"TODO\s*Confirm", re.I)
_SKIP_DIRS = (
    ".local",
    ".templates",
    ".tmh",
    ".screenshots",
    ".objects",
    ".git",
    ".pdd2uipath",
    ".project",
    ".settings",
    "Tests",
)
_CRED_ATTR_RE = re.compile(
    r'\b(Password|SecurePassword|Secret|ApiKey|Token|ClientSecret)\s*=\s*"(?P<val>[^"]*)"', re.I
)
_CRED_TEXT_RE = re.compile(
    r"""(?i)\b(password|passwd|pwd|secret|api[_-]?key|token)\b\s*[:=]\s*["'](?P<val>[^"'\[\{]{4,})["']"""
)
_ASSET_LIKE_RE = re.compile(r"^[A-Z][A-Za-z0-9]*_[A-Za-z0-9_]+$")


@dataclass
class WorkflowInfo:
    path: Path
    rel: str
    step_ids: list[str] = field(default_factory=list)
    uia_activities: list[tuple[str, str]] = field(default_factory=list)  # (tag, DisplayName)
    log_only: bool = False
    todo_indicate: list[str] = field(default_factory=list)
    todo_confirm: list[str] = field(default_factory=list)  # agent's own assumptions to confirm
    credential_hits: list[str] = field(default_factory=list)
    activity_count: int = 0
    parse_error: str = ""


@dataclass
class TraceRow:
    step_id: str
    pdd_ref: str
    file: str
    activity_display_name: str
    status: str  # implemented | stub | log-only | missing


@dataclass
class AuditResult:
    workflows: list[WorkflowInfo]
    rows: list[TraceRow]
    missing_steps: list[str]
    log_only_ui_steps: list[str]
    todo_indicate_total: int
    credential_hits: list[str]
    todo_by_file: dict[str, list[str]]
    confirm_by_file: dict[str, list[str]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.missing_steps and not self.log_only_ui_steps and not self.credential_hits

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.missing_steps:
            out.append(f"steps without a workflow: {', '.join(self.missing_steps)}")
        if self.log_only_ui_steps:
            out.append(
                f"UI steps implemented with Log/WriteLine only (no UIA activity): {', '.join(self.log_only_ui_steps)}"
            )
        if self.credential_hits:
            out.append(f"credential-like literals: {'; '.join(self.credential_hits[:5])}")
        return out


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def scan_workflow(path: Path, project_dir: Path) -> WorkflowInfo:
    rel = path.relative_to(project_dir).as_posix()
    info = WorkflowInfo(path=path, rel=rel)
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        info.parse_error = str(exc)
        return info
    ids = set(_STEP_RE.findall(path.stem))
    for m in _CRED_ATTR_RE.finditer(raw):
        val = m.group("val").strip()
        if val and not val.startswith(("[", "{")) and not _ASSET_LIKE_RE.match(val):
            info.credential_hits.append(f'{rel}: {m.group(1)}="{val[:3]}..."')
    for m in _CRED_TEXT_RE.finditer(raw):
        val = m.group("val")
        if not _ASSET_LIKE_RE.match(val):
            info.credential_hits.append(f"{rel}: {m.group(1)} literal")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        info.parse_error = f"XML parse error: {exc}"
        info.step_ids = sorted(ids)
        return info
    has_uia = False
    has_log = False
    for el in root.iter():
        ns, local = _local(el.tag)
        if "." in local:  # property elements like NApplicationCard.Body
            continue
        display = el.get("DisplayName", "")
        annotation = el.get(f"{{{SAP2010_NS}}}Annotation.AnnotationText", "")
        for text in (display, annotation):
            ids.update(_STEP_RE.findall(text))
            if text and _CONFIRM_RE.search(text) and text not in info.todo_confirm:
                info.todo_confirm.append(text)
        if (ns == UIX_NS and local.startswith("N")) or (ns == UI_NS and local in CLASSIC_UIA):
            has_uia = True
            info.uia_activities.append((local, display))
            if _TODO_RE.search(display):
                info.todo_indicate.append(display)
        elif ns == UI_NS and local in LOG_ACTIVITIES:
            has_log = True
        if ns in (UI_NS, UIX_NS) or local in {
            "Sequence",
            "Assign",
            "If",
            "ForEach",
            "TryCatch",
            "Switch",
            "While",
            "DoWhile",
            "Flowchart",
            "StateMachine",
        }:
            info.activity_count += 1
    info.log_only = has_log and not has_uia
    info.step_ids = sorted(ids)
    return info


def scan_project(project_dir: Path) -> list[WorkflowInfo]:
    out: list[WorkflowInfo] = []
    for p in sorted(project_dir.rglob("*.xaml")):
        rel = p.relative_to(project_dir)
        if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
            continue
        out.append(scan_workflow(p, project_dir))
    return out


def audit(spec: ProcessSpec, design: DesignResult, project_dir: Path) -> AuditResult:
    workflows = scan_project(project_dir)
    by_step: dict[str, list[WorkflowInfo]] = {}
    for w in workflows:
        for sid in w.step_ids:
            by_step.setdefault(sid, []).append(w)
    rows: list[TraceRow] = []
    missing: list[str] = []
    log_only: list[str] = []
    mapping = {m.step_id: m for m in design.workflows}
    for step in spec.steps:
        m = mapping.get(step.step_id)
        planned = m.file if m else ""
        found = by_step.get(step.step_id, [])
        preferred = next((w for w in found if w.rel == planned), None) or (
            found[0] if found else None
        )
        if preferred is None:
            missing.append(step.step_id)
            rows.append(TraceRow(step.step_id, step.source_ref, planned, "", "missing"))
            continue
        if step.is_ui_interaction:
            uia_in_any = [w for w in found if w.uia_activities]
            if not uia_in_any:
                log_only.append(step.step_id)
                rows.append(TraceRow(step.step_id, step.source_ref, preferred.rel, "", "log-only"))
                continue
            w = uia_in_any[0] if preferred not in uia_in_any else preferred
            for tag, display in w.uia_activities:
                status = "stub" if _TODO_RE.search(display) else "implemented"
                rows.append(TraceRow(step.step_id, step.source_ref, w.rel, display or tag, status))
        else:
            rows.append(
                TraceRow(
                    step.step_id,
                    step.source_ref,
                    preferred.rel,
                    f"{preferred.activity_count} activities",
                    "implemented",
                )
            )
    todo_by_file = {w.rel: w.todo_indicate for w in workflows if w.todo_indicate}
    confirm_by_file = {w.rel: w.todo_confirm for w in workflows if w.todo_confirm}
    return AuditResult(
        workflows=workflows,
        rows=rows,
        missing_steps=missing,
        log_only_ui_steps=log_only,
        todo_indicate_total=sum(len(v) for v in todo_by_file.values()),
        credential_hits=[h for w in workflows for h in w.credential_hits],
        todo_by_file=todo_by_file,
        confirm_by_file=confirm_by_file,
    )


def traceability_csv(rows: list[TraceRow]) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["step_id", "pdd_ref", "file", "activity_display_name", "status"])
    for r in rows:
        writer.writerow([r.step_id, r.pdd_ref, r.file, r.activity_display_name, r.status])
    return buf.getvalue()

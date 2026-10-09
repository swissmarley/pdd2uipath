"""Deterministic facts parsed from the PDD's tables before any model call."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from pdd2uipath.ingest.models import Document, Table

_ID_RE = re.compile(r"\b(BR|BE|SE)-\d+\b")
_STEP_RE = re.compile(r"\bS\d{2}\b")


class FactStep(BaseModel):
    step_id: str
    part: str = ""
    application: str = ""
    action: str
    business_rules: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    source_ref: str


class FactRule(BaseModel):
    id: str
    text: str
    step_ids: list[str] = Field(default_factory=list)
    source_ref: str


class FactException(BaseModel):
    id: str
    kind: str
    description: str
    handling: str
    source_ref: str


class FactApplication(BaseModel):
    name: str
    type: str = ""
    access: str = ""
    used_for: str = ""
    source_ref: str


class FactOpenPoint(BaseModel):
    id: str
    text: str
    owner: str = ""
    status: str = ""
    source_ref: str


class FactField(BaseModel):
    name: str
    mandatory: bool = False
    format: str = ""
    target: str = ""
    source_ref: str


class DeterministicFacts(BaseModel):
    document_control: dict[str, str] = Field(default_factory=dict)
    document_control_ref: str = ""
    operational: dict[str, str] = Field(default_factory=dict)
    operational_ref: str = ""
    steps: list[FactStep] = Field(default_factory=list)
    business_rules: list[FactRule] = Field(default_factory=list)
    exceptions: list[FactException] = Field(default_factory=list)
    applications: list[FactApplication] = Field(default_factory=list)
    open_points: list[FactOpenPoint] = Field(default_factory=list)
    fields: list[FactField] = Field(default_factory=list)
    title: str = ""

    def fields_found(self) -> list[str]:
        out: list[str] = []
        if self.document_control:
            out.append("document_control")
        if self.steps:
            out.append("steps")
        if self.business_rules:
            out.append("business_rules")
        if self.exceptions:
            out.append("exceptions")
        if self.applications:
            out.append("applications")
        if self.operational:
            out.append("operational")
        if self.open_points:
            out.append("open_points")
        if self.fields:
            out.append("fields")
        return out

    def render(self) -> str:
        lines = [f"TITLE: {self.title}"]
        if self.document_control:
            lines.append(f"DOCUMENT CONTROL [{self.document_control_ref}]:")
            lines.extend(f"  {k}: {v}" for k, v in self.document_control.items())
        if self.steps:
            lines.append("STEPS (from the summary table):")
            for s in self.steps:
                lines.append(
                    f"  {s.step_id} [{s.source_ref}] part={s.part or '-'} app={s.application or '-'}: "
                    f"{s.action} | rules={','.join(s.business_rules) or '-'} exceptions={','.join(s.exceptions) or '-'}"
                )
        if self.business_rules:
            lines.append("BUSINESS RULES:")
            lines.extend(
                f"  {r.id} [{r.source_ref}] steps={','.join(r.step_ids) or '-'}: {r.text}"
                for r in self.business_rules
            )
        if self.exceptions:
            lines.append("EXCEPTIONS:")
            lines.extend(
                f"  {e.id} ({e.kind}) [{e.source_ref}]: {e.description} -> {e.handling}"
                for e in self.exceptions
            )
        if self.applications:
            lines.append("APPLICATIONS:")
            lines.extend(
                f"  {a.name} [{a.source_ref}] type={a.type} access={a.access} used_for={a.used_for}"
                for a in self.applications
            )
        if self.operational:
            lines.append(f"OPERATIONAL [{self.operational_ref}]:")
            lines.extend(f"  {k}: {v}" for k, v in self.operational.items())
        if self.open_points:
            lines.append("OPEN POINTS:")
            lines.extend(
                f"  {o.id} [{o.source_ref}] owner={o.owner} status={o.status}: {o.text}"
                for o in self.open_points
            )
        if self.fields:
            lines.append("FIELDS:")
            lines.extend(
                f"  {f.name} mandatory={f.mandatory} format={f.format} target={f.target}"
                for f in self.fields
            )
        return "\n".join(lines)


def _col(row: dict[str, str], *needles: str) -> str:
    for k, v in row.items():
        kl = k.lower()
        if any(n in kl for n in needles):
            return v
    return ""


def _ids(text: str, prefix: str) -> list[str]:
    return [m.group(0) for m in _ID_RE.finditer(text) if m.group(1) == prefix]


def _parse_steps(t: Table) -> list[FactStep]:
    out: list[FactStep] = []
    for row in t.as_dicts():
        sid = _col(row, "step id", "step")
        if not _STEP_RE.fullmatch(sid.strip()):
            continue
        rules_text = _col(row, "rules", "exception")
        out.append(
            FactStep(
                step_id=sid.strip(),
                part=_col(row, "part"),
                application=_col(row, "application", "system"),
                action=_col(row, "action", "description"),
                business_rules=_ids(rules_text, "BR"),
                exceptions=_ids(rules_text, "BE") + _ids(rules_text, "SE"),
                source_ref=t.ref,
            )
        )
    return out


def _parse_rules(t: Table) -> list[FactRule]:
    out: list[FactRule] = []
    for row in t.as_dicts():
        rid = _col(row, "id")
        if not rid.startswith("BR-"):
            continue
        out.append(
            FactRule(
                id=rid.strip(),
                text=_col(row, "rule", "description"),
                step_ids=_STEP_RE.findall(_col(row, "step")),
                source_ref=t.ref,
            )
        )
    return out


def _parse_exceptions(t: Table) -> list[FactException]:
    out: list[FactException] = []
    for row in t.as_dicts():
        eid = _col(row, "id").strip()
        if not eid.startswith(("BE-", "SE-")):
            continue
        out.append(
            FactException(
                id=eid,
                kind="business" if eid.startswith("BE-") else "system",
                description=_col(row, "exception", "description"),
                handling=_col(row, "handling", "action"),
                source_ref=t.ref,
            )
        )
    return out


def _parse_applications(t: Table) -> list[FactApplication]:
    out: list[FactApplication] = []
    for row in t.as_dicts():
        name = _col(row, "application", "system")
        if not name:
            continue
        out.append(
            FactApplication(
                name=name,
                type=_col(row, "type", "interface"),
                access=_col(row, "version", "access"),
                used_for=_col(row, "used for", "purpose", "role"),
                source_ref=t.ref,
            )
        )
    return out


def _parse_open_points(t: Table) -> list[FactOpenPoint]:
    out: list[FactOpenPoint] = []
    for n, row in enumerate(t.as_dicts(), 1):
        text = _col(row, "open point", "question", "topic")
        if not text:
            continue
        out.append(
            FactOpenPoint(
                id=f"OP-{_col(row, '#') or n}",
                text=text,
                owner=_col(row, "owner"),
                status=_col(row, "status"),
                source_ref=t.ref,
            )
        )
    return out


def _parse_fields(t: Table) -> list[FactField]:
    out: list[FactField] = []
    for row in t.as_dicts():
        name = _col(row, "field")
        if not name:
            continue
        out.append(
            FactField(
                name=name,
                mandatory=_col(row, "mandatory").strip().lower() in {"yes", "y", "true", "x"},
                format=_col(row, "format"),
                target=_col(row, "target"),
                source_ref=t.ref,
            )
        )
    return out


def extract_facts(doc: Document) -> DeterministicFacts:
    facts = DeterministicFacts(title=doc.title)
    for t in doc.tables:
        if t.header_has("step id") and t.header_has("action"):
            facts.steps.extend(_parse_steps(t))
        elif t.header_has("id") and t.header_has("rule"):
            facts.business_rules.extend(_parse_rules(t))
        elif t.header_has("id") and t.header_has("exception"):
            facts.exceptions.extend(_parse_exceptions(t))
        elif t.header_has("application") and t.header_has("type"):
            facts.applications.extend(_parse_applications(t))
        elif t.header_has("open point"):
            facts.open_points.extend(_parse_open_points(t))
        elif t.header_has("field") and t.header_has("mandatory"):
            facts.fields.extend(_parse_fields(t))
        elif t.is_key_value:
            kv = t.key_values()
            keys = {k.lower() for k in kv}
            if "document id" in keys or ("status" in keys and "version" in keys):
                facts.document_control.update(kv)
                facts.document_control_ref = facts.document_control_ref or t.ref
            elif any(
                k.startswith(("schedule", "robot environment", "sla", "credentials")) for k in keys
            ):
                facts.operational.update(kv)
                facts.operational_ref = facts.operational_ref or t.ref
    return facts

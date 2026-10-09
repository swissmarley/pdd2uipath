"""Approval gate: pluggable rules that decide whether a PDD may enter the pipeline."""

from __future__ import annotations

import re
from datetime import date
from typing import Protocol

from pydantic import BaseModel, Field

from pdd2uipath.ingest.models import Document, Table

_DATE_PATTERNS = (
    re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$"),
    re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"),
)


def parse_date(text: str) -> date | None:
    s = text.strip()
    if not s:
        return None
    m = _DATE_PATTERNS[0].match(s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = _DATE_PATTERNS[1].match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


class Evidence(BaseModel):
    ref: str
    text: str
    ok: bool


class ApprovalVerdict(BaseModel):
    approved: bool
    rule: str
    reasons: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    status: str | None = None
    signoff: list[dict[str, str]] = Field(default_factory=list)

    def summary(self) -> str:
        if self.approved:
            return f"approved ({self.rule}): status '{self.status}', {len(self.signoff)} sign-off row(s) approved with dates"
        return "NOT approved: " + "; ".join(self.reasons)


class ApprovalRule(Protocol):
    name: str

    def check(self, doc: Document) -> ApprovalVerdict: ...


def _find_signoff_table(doc: Document) -> Table | None:
    best: Table | None = None
    for t in doc.tables:
        if t.header_has("decision") and t.header_has("date"):
            if "sign" in t.context.lower() or "approval" in t.context.lower():
                return t
            best = best or t
    return best


class DocumentControlSignoffRule:
    """Document Control -> Status = "Approved" AND every "Approval and Sign-off" row has
    Decision = "Approved" with a date."""

    name = "document-control-and-signoff"

    def check(self, doc: Document) -> ApprovalVerdict:
        reasons: list[str] = []
        evidence: list[Evidence] = []
        status_lookup = doc.lookup_key("Status")
        status: str | None = None
        if status_lookup is None:
            reasons.append("Document Control has no 'Status' row")
        else:
            status, ref = status_lookup
            ok = status.strip().lower() == "approved"
            evidence.append(Evidence(ref=ref, text=f"Status = {status!r}", ok=ok))
            if not ok:
                reasons.append(f"Document Control status is {status!r} (expected 'Approved')")
        table = _find_signoff_table(doc)
        rows: list[dict[str, str]] = []
        if table is None:
            reasons.append("No 'Approval and Sign-off' table with Decision/Date columns found")
        else:
            rows = table.as_dicts()
            if not rows:
                reasons.append("The sign-off table has no rows")
            for row in rows:
                decision = self._col(row, "decision")
                when = self._col(row, "date")
                who = (
                    " / ".join(x for x in (self._col(row, "role"), self._col(row, "name")) if x)
                    or "?"
                )
                dec_ok = decision.strip().lower() == "approved"
                date_ok = parse_date(when) is not None
                evidence.append(
                    Evidence(
                        ref=table.ref,
                        text=f"{who}: decision={decision or '(empty)'!r} date={when or '(empty)'!r}",
                        ok=dec_ok and date_ok,
                    )
                )
                if not dec_ok:
                    reasons.append(
                        f"sign-off row '{who}' has decision {decision or '(empty)'!r}, not 'Approved'"
                    )
                if not date_ok:
                    reasons.append(f"sign-off row '{who}' has no valid date ({when or 'empty'!r})")
        banner = next((b for b in doc.blocks if b.text.upper().startswith("STATUS:")), None)
        if banner is not None:
            evidence.append(
                Evidence(ref=banner.ref, text=banner.text, ok="APPROVED" in banner.text.upper())
            )
        approved = not reasons
        return ApprovalVerdict(
            approved=approved,
            rule=self.name,
            reasons=reasons,
            evidence=evidence,
            status=status,
            signoff=rows,
        )

    @staticmethod
    def _col(row: dict[str, str], needle: str) -> str:
        for k, v in row.items():
            if needle in k.lower():
                return v
        return ""


RULES: dict[str, type[ApprovalRule]] = {DocumentControlSignoffRule.name: DocumentControlSignoffRule}


def get_rule(name: str) -> ApprovalRule:
    try:
        return RULES[name]()
    except KeyError as exc:
        raise KeyError(f"unknown approval rule {name!r}; known: {sorted(RULES)}") from exc

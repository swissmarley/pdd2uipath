from __future__ import annotations

from pathlib import Path

import pytest

from pdd2uipath.ingest.approval import get_rule, parse_date
from pdd2uipath.ingest.parse import IngestError, parse_document


def test_parse_docx_pdd01(pdd01: Path) -> None:
    doc = parse_document(pdd01)
    assert doc.format == "docx"
    assert doc.title == "Vendor Invoice Processing (PO-based)"
    heads = [h.text for h in doc.headings()]
    assert "4.2 Process Steps Summary" in heads and "S06 – Log in to SAP" in heads
    steps = doc.find_table("Step ID", "Action")
    assert steps is not None and len(steps.rows) == 12 and steps.rows[0][0] == "S01"
    assert steps.ref == "§4.2"
    assert doc.lookup_key("Document ID") == ("PDD-FIN-AP-014", "front")
    assert doc.lookup_key("Status")[0] == "Approved"
    # step detail blocks are anchored to their step heading
    s06 = [b for b in doc.blocks if b.ref == "§4.3/S06"]
    assert any("SAP Logon" in b.text for b in s06)
    txt = doc.text_with_refs()
    assert "[§4.2] TABLE" in txt and "[§4.3/S10]" in txt


def test_parse_pdf_pdd02(pdd02: Path) -> None:
    doc = parse_document(pdd02)
    assert doc.format == "pdf" and doc.page_count == 5
    assert doc.title == "Daily FX Reference Rate Update"
    steps = doc.find_table("Step ID", "Action")
    assert steps is not None and [r[0] for r in steps.rows] == [
        "S01",
        "S02",
        "S03",
        "S04",
        "S05",
        "S06",
    ]
    assert steps.ref.startswith("p.3 §4.2")
    assert doc.lookup_key("Robot environment")[0].startswith("Linux serverless")
    assert doc.lookup_key("Status") == ("Approved", "p.1 front")
    # boilerplate header/footer lines are dropped
    assert not any("Page 2 of 5" in b.text for b in doc.blocks)
    assert not any(b.text.startswith("Nordhorn Logistik AG | PDD-FIN-TRE-007") for b in doc.blocks)
    open_points = doc.find_table("Open point", "Owner")
    assert open_points is not None and len(open_points.rows) == 3


def test_parse_docx_pdd03(pdd03: Path) -> None:
    doc = parse_document(pdd03)
    assert doc.title == "Customer Address Change Requests"
    assert doc.lookup_key("Status")[0].startswith("Draft")


def test_parse_unsupported(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hi")
    with pytest.raises(IngestError):
        parse_document(f)


def test_parse_date() -> None:
    assert parse_date("18.09.2026").isoformat() == "2026-09-18"
    assert parse_date("2026-09-18").isoformat() == "2026-09-18"
    assert parse_date("") is None and parse_date("tbd") is None and parse_date("31.02.2026") is None


def test_approval_rule_pdd01_and_pdd02_approved(pdd01: Path, pdd02: Path) -> None:
    rule = get_rule("document-control-and-signoff")
    v1 = rule.check(parse_document(pdd01))
    assert v1.approved, v1.reasons
    assert v1.status == "Approved" and len(v1.signoff) == 5
    assert all(e.ok for e in v1.evidence)
    v2 = rule.check(parse_document(pdd02))
    assert v2.approved, v2.reasons
    assert len(v2.signoff) == 4


def test_approval_rule_pdd03_rejected(pdd03: Path) -> None:
    v = get_rule("document-control-and-signoff").check(parse_document(pdd03))
    assert not v.approved
    text = " ".join(v.reasons)
    assert "Draft" in text and "Pending" in text and "Changes requested" in text
    assert any("no valid date" in r for r in v.reasons)
    assert v.summary().startswith("NOT approved")


def test_unknown_rule() -> None:
    with pytest.raises(KeyError):
        get_rule("nope")

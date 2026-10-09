"""Markdown PDDs (Confluence pages exported or fetched as Markdown through MCP).

Headings, paragraphs, list items and GitHub-style pipe tables become the same ``Document``
blocks and tables as DOCX/PDF input, with the same section/step references, so the approval
rule and the deterministic table facts work unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

from pdd2uipath.core.state import sha256_file
from pdd2uipath.ingest.models import Block, BlockKind, Document, Table
from pdd2uipath.ingest.parse import IngestError, _clean, _RefTracker

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [_clean(c.replace("<br>", " ").replace("<br/>", " ")) for c in body.split("|")]


def parse_markdown(path: Path) -> Document:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise IngestError(f"cannot read {path}: {exc}") from exc
    lines = text.splitlines()
    tracker = _RefTracker(page_aware=False)
    blocks: list[Block] = []
    tables: list[Table] = []
    index = 0
    last_heading = ""
    title: str | None = None
    para: list[str] = []
    in_code = False

    def flush_para() -> None:
        nonlocal index, para
        if para:
            body = _clean(" ".join(para))
            if body:
                is_h, lvl = tracker.see(body)
                kind: BlockKind = "heading" if is_h else "paragraph"
                blocks.append(
                    Block(kind=kind, text=body, ref=tracker.ref(None), index=index, level=lvl)
                )
                index += 1
            para = []

    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            flush_para()
            in_code = not in_code
            i += 1
            continue
        if in_code:
            para.append(raw)
            i += 1
            continue
        if not stripped or _RULE.match(stripped):
            flush_para()
            i += 1
            continue
        h = _HEADING.match(stripped)
        if h:
            flush_para()
            level = len(h.group(1))
            heading = _clean(h.group(2))
            if title is None and level == 1:
                title = heading
            tracker.see(heading)
            last_heading = heading
            blocks.append(
                Block(kind="heading", text=heading, ref=tracker.ref(None), index=index, level=level)
            )
            index += 1
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1]):
            flush_para()
            header = _cells(stripped)
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            tbl = Table(
                ref=tracker.ref(None),
                index=len(tables),
                header=header,
                rows=rows,
                context=last_heading,
            )
            tables.append(tbl)
            blocks.append(
                Block(kind="table", text=f"table {len(tables)}", ref=tbl.ref, index=index)
            )
            index += 1
            continue
        li = _LIST.match(raw)
        if li:
            flush_para()
            item = _clean(li.group(1))
            if item:
                blocks.append(Block(kind="list", text=item, ref=tracker.ref(None), index=index))
                index += 1
            i += 1
            continue
        para.append(stripped)
        i += 1
    flush_para()
    if not blocks and not tables:
        raise IngestError(f"{path.name} contains no Markdown content")
    return Document(
        path=str(path),
        format="markdown",
        sha256=sha256_file(path),
        title=title or path.stem,
        blocks=blocks,
        tables=tables,
        page_count=None,
        core_title=title,
    )


__all__ = ["parse_markdown"]

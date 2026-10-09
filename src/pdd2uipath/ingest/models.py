"""Parsed PDD document model with page/section anchors."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

BlockKind = Literal["heading", "paragraph", "list", "table"]


class Block(BaseModel):
    kind: BlockKind
    text: str
    ref: str
    index: int
    level: int = 0
    page: int | None = None


class Table(BaseModel):
    ref: str
    index: int
    header: list[str]
    rows: list[list[str]]
    context: str = ""
    page: int | None = None

    @property
    def is_key_value(self) -> bool:
        return bool(self.rows) and all(len(r) == 2 for r in [self.header, *self.rows] if r)

    def as_dicts(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for row in self.rows:
            d: dict[str, str] = {}
            for i, h in enumerate(self.header):
                d[h.strip()] = row[i].strip() if i < len(row) else ""
            out.append(d)
        return out

    def key_values(self) -> dict[str, str]:
        kv: dict[str, str] = {}
        rows = [self.header, *self.rows] if self.header else self.rows
        for row in rows:
            if len(row) >= 2 and row[0].strip():
                kv[row[0].strip()] = " ".join(c.strip() for c in row[1:] if c.strip())
        return kv

    def header_has(self, *needles: str) -> bool:
        joined = " | ".join(h.lower() for h in self.header)
        return all(n.lower() in joined for n in needles)

    def render(self) -> str:
        lines = [" | ".join(self.header)] if self.header else []
        lines.extend(" | ".join(r) for r in self.rows)
        return "\n".join(lines)


class Document(BaseModel):
    path: str
    format: Literal["docx", "pdf"]
    title: str
    sha256: str
    blocks: list[Block] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    page_count: int | None = None
    core_title: str | None = None

    def headings(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "heading"]

    def find_table(self, *needles: str) -> Table | None:
        for t in self.tables:
            if t.header_has(*needles):
                return t
        return None

    def find_tables(self, *needles: str) -> list[Table]:
        return [t for t in self.tables if t.header_has(*needles)]

    def key_value_tables(self) -> list[Table]:
        return [t for t in self.tables if t.is_key_value]

    def lookup_key(self, key: str) -> tuple[str, str] | None:
        """Find ``key`` in any key/value table; returns ``(value, ref)``."""
        for t in self.tables:
            for k, v in t.key_values().items():
                if k.strip().rstrip(":").lower() == key.lower():
                    return v, t.ref
        return None

    def text_with_refs(self, max_chars: int | None = None) -> str:
        """Linear rendering used as the model's input: every block carries its ref."""
        out: list[str] = []
        table_by_index = {t.index: t for t in self.tables}
        for b in self.blocks:
            if b.kind == "table":
                t = table_by_index.get(b.index)
                if t is not None:
                    out.append(f"[{t.ref}] TABLE ({t.context}):\n{t.render()}")
                continue
            if b.kind == "heading":
                out.append(f"\n[{b.ref}] {'#' * max(1, b.level)} {b.text}")
            elif b.kind == "list":
                out.append(f"[{b.ref}] - {b.text}")
            else:
                out.append(f"[{b.ref}] {b.text}")
        text = "\n".join(out)
        if max_chars is not None and len(text) > max_chars:
            return text[:max_chars] + "\n[... truncated ...]"
        return text

    def section_of(self, ref: str) -> str:
        for b in self.blocks:
            if b.ref == ref and b.kind == "heading":
                return b.text
        return ref

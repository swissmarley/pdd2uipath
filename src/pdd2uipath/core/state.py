"""Resumable run state persisted at ``<run_dir>/.pdd2uipath/state.json``."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

STAGES: list[str] = [
    "doctor",
    "ingest",
    "extract",
    "design",
    "plan",
    "scaffold",
    "author",
    "verify",
    "report",
]

StageStatus = Literal["pending", "running", "done", "failed", "stopped", "waiting"]


class StageRecord(BaseModel):
    status: StageStatus = "pending"
    started: datetime | None = None
    finished: datetime | None = None
    input_hash: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = 0
    waiting_for: str | None = None


class ApprovalRecord(BaseModel):
    approver: str
    approved_at: datetime
    plan_path: str
    plan_sha256: str


class RunState(BaseModel):
    run_id: str
    slug: str
    pdd_path: str
    pdd_sha256: str
    project_name: str
    run_dir_windows: str
    project_dir_windows: str | None = None
    executor: str = ""
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    repair_round: int = 0
    approval: ApprovalRecord | None = None
    agent_session_id: str | None = None
    agent_notes: list[str] = Field(default_factory=list)  # final message per authoring session
    pending_round: str | None = None
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def stage(self, name: str) -> StageRecord:
        return self.stages.setdefault(name, StageRecord())

    def statuses(self) -> dict[str, str]:
        return {s: self.stage(s).status for s in STAGES}

    def current_gate(self) -> str | None:
        for s in STAGES:
            rec = self.stage(s)
            if rec.status == "waiting":
                return rec.waiting_for
        return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_inputs(*parts: str | Path | None) -> str:
    h = hashlib.sha256()
    for p in parts:
        if p is None:
            h.update(b"\x00none")
        elif isinstance(p, Path):
            h.update(sha256_file(p).encode() if p.is_file() else b"\x00missing")
        else:
            h.update(p.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "run"


def pascal_case(text: str, max_len: int = 48) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[A-Za-z0-9]+", text)
    out = "".join(w[:1].upper() + w[1:] for w in words)
    if out and out[0].isdigit():
        out = "P" + out
    return out[:max_len] or "Process"


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class RunStore:
    """Load/save ``RunState`` atomically."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.meta_dir = run_dir / ".pdd2uipath"
        self.path = self.meta_dir / "state.json"

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> RunState:
        return RunState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, state: RunState) -> None:
        state.updated = datetime.now(UTC)
        atomic_write_text(self.path, json.dumps(state.model_dump(mode="json"), indent=2))

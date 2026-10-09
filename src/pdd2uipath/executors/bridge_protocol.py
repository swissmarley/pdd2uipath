"""File-based job protocol shared by the bridge worker and the manual rounds.

Layout (all under ``<workspace>/.pdd2uipath-bridge/``)::

    jobs/<id>.json        canonical job (schema ``pdd2uipath.bridge.job/1``)
    jobs/<id>.args        sidecar for the cmd.exe worker (one field per line)
    results/<id>.json     result metadata (written atomically: ``.tmp`` then rename)
    results/<id>.out|.err captured stdout / stderr
    heartbeat.json        touched every few seconds by the worker
    rounds/<round>/       manual-mode rounds (same job/result files + run.cmd/run.ps1)
    worker.ps1, worker.cmd, STOP

The worker is a command channel, so it only executes an allowlist of ``uip`` verbs on
paths inside the workspace root. The Python side validates with the same rules before
writing a job (defence in depth).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from pdd2uipath.core.paths import is_inside, looks_like_path, to_windows, windows_join
from pdd2uipath.core.state import atomic_write_text
from pdd2uipath.executors.base import PATH_VALUE_FLAGS, POSITIONAL_PATH_VERBS, JobRejected

JOB_SCHEMA = "pdd2uipath.bridge.job/1"
RESULT_SCHEMA = "pdd2uipath.bridge.result/1"

#: ``uip`` verbs the Windows worker may execute. ``None`` means "no sub-verb".
ALLOWED_VERBS: dict[str, frozenset[str] | None] = {
    "rpa": frozenset(
        {
            "templates",
            "init",
            "restore",
            "packages",
            "activities",
            "validate",
            "build",
            "analyze",
            "analyzer-rules",
            "studio",
            "instances",
            "get-object-repository",
            "workflow-examples",
        }
    ),
    "login": frozenset({"status", "which"}),
    "--version": None,
    "--help": None,
}
#: Characters refused in any token (cmd.exe metacharacters and line breaks).
FORBIDDEN_CHARS = frozenset('&|<>^"%!\r\n\x00')


class BridgeJob(BaseModel):
    schema_id: str = Field(default=JOB_SCHEMA, alias="schema")
    id: str
    created: str
    cwd: str
    argv: list[str]
    timeout_s: float = 900.0
    label: str = ""

    model_config = {"populate_by_name": True}


class BridgeResult(BaseModel):
    schema_id: str = Field(default=RESULT_SCHEMA, alias="schema")
    id: str
    status: Literal["ok", "rejected", "error"] = "ok"
    exit_code: int = 0
    stdout_file: str = ""
    stderr_file: str = ""
    started: str = ""
    finished: str = ""
    worker: str = ""
    reason: str = ""

    model_config = {"populate_by_name": True}

    @field_validator("exit_code", mode="before")
    @classmethod
    def _null_exit_code(cls, value: object) -> object:
        return 1 if value is None else value


@dataclass(frozen=True)
class BridgeLayout:
    """Brain-side and Windows-side views of the bridge directory."""

    root: Path
    root_windows: str

    @property
    def jobs(self) -> Path:
        return self.root / "jobs"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def rounds(self) -> Path:
        return self.root / "rounds"

    @property
    def heartbeat(self) -> Path:
        return self.root / "heartbeat.json"

    @property
    def stop_file(self) -> Path:
        return self.root / "STOP"

    @property
    def worker_ps1(self) -> Path:
        return self.root / "worker.ps1"

    @property
    def worker_cmd(self) -> Path:
        return self.root / "worker.cmd"

    def windows(self, *parts: str) -> str:
        return windows_join(self.root_windows, *parts)

    def ensure(self) -> None:
        for d in (self.jobs, self.results, self.rounds):
            d.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def validate_job(argv: Sequence[str], cwd: str, workspace_root: str) -> None:
    """Raise :class:`JobRejected` unless the job is allowlisted and stays in the workspace."""
    args = list(argv)
    if not args:
        raise JobRejected("empty argv")
    for tok in args:
        if not isinstance(tok, str):
            raise JobRejected("argv tokens must be strings")
        bad = sorted(set(tok) & FORBIDDEN_CHARS)
        if bad:
            raise JobRejected(f"token {tok!r} contains forbidden characters {bad}")
    head = args[0]
    if head not in ALLOWED_VERBS:
        raise JobRejected(f"'uip {head}' is not allowlisted for the Windows worker")
    subs = ALLOWED_VERBS[head]
    if subs is not None and (len(args) < 2 or args[1] not in subs):
        raise JobRejected(
            f"'uip {head} {args[1] if len(args) > 1 else ''}' is not allowlisted "
            f"(allowed: {', '.join(sorted(subs))})"
        )
    if not is_inside(workspace_root, cwd):
        raise JobRejected(f"cwd {cwd!r} is outside the workspace root {workspace_root!r}")
    verb = args[1] if head == "rpa" and len(args) > 1 else None
    i = 0
    positional_seen = False
    while i < len(args):
        tok = args[i]
        if tok in PATH_VALUE_FLAGS and i + 1 < len(args):
            value = args[i + 1]
            if not is_inside(workspace_root, value):
                raise JobRejected(f"{tok} {value!r} is outside the workspace root")
            i += 2
            continue
        if tok == "--file-path" and i + 1 < len(args):
            value = args[i + 1]
            if (len(value) > 1 and value[1] == ":") and not is_inside(workspace_root, value):
                raise JobRejected(f"--file-path {value!r} is outside the workspace root")
            i += 2
            continue
        if (
            verb in POSITIONAL_PATH_VERBS
            and i > 1
            and not positional_seen
            and not tok.startswith("-")
            and looks_like_path(tok)
            and args[i - 1] not in PATH_VALUE_FLAGS
        ):
            if not is_inside(workspace_root, tok):
                raise JobRejected(f"project path {tok!r} is outside the workspace root")
            positional_seen = True
        i += 1


def make_job(
    argv: Sequence[str], cwd: str, *, timeout_s: float, job_id: str, label: str = ""
) -> BridgeJob:
    return BridgeJob(
        id=job_id,
        created=now_iso(),
        cwd=to_windows(cwd),
        argv=list(argv),
        timeout_s=timeout_s,
        label=label,
    )


def args_sidecar(job: BridgeJob) -> str:
    lines = [f"cwd={job.cwd}", f"timeout={int(job.timeout_s)}"]
    lines.extend(f"arg={a}" for a in job.argv)
    return "\r\n".join(lines) + "\r\n"


def write_job(jobs_dir: Path, job: BridgeJob) -> Path:
    """Write ``<id>.args`` first, then the canonical ``<id>.json`` atomically.

    Workers only react to ``*.json``, so the sidecar is always complete when they start.
    """
    jobs_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(jobs_dir / f"{job.id}.args", args_sidecar(job))
    atomic_write_text(jobs_dir / f"{job.id}.json", job.model_dump_json(by_alias=True, indent=2))
    return jobs_dir / f"{job.id}.json"


def read_job(path: Path) -> BridgeJob:
    return BridgeJob.model_validate_json(path.read_text(encoding="utf-8-sig"))


def read_result(results_dir: Path, job_id: str) -> BridgeResult | None:
    path = results_dir / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        return BridgeResult.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return None


def write_result(results_dir: Path, result: BridgeResult) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        results_dir / f"{result.id}.json", result.model_dump_json(by_alias=True, indent=2)
    )
    return results_dir / f"{result.id}.json"


def read_capture(results_dir: Path, result: BridgeResult) -> tuple[str, str]:
    def _read(name: str) -> str:
        if not name:
            return ""
        p = results_dir / name
        try:
            return p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return ""

    return _read(result.stdout_file), _read(result.stderr_file)


def heartbeat_age(layout: BridgeLayout, now: float | None = None) -> float | None:
    """Seconds since the worker last touched the heartbeat file (``None`` if absent).

    Uses the file's mtime, because Windows-side writes under ``/mnt/c`` do not raise
    inotify events in WSL and partial writes must not be parsed.
    """
    try:
        mtime = layout.heartbeat.stat().st_mtime
    except OSError:
        return None
    return (now if now is not None else time.time()) - mtime


def touch_heartbeat(layout: BridgeLayout, worker: str) -> None:
    payload = json.dumps({"worker": worker, "pid": os.getpid(), "ts": now_iso()})
    atomic_write_text(layout.heartbeat, payload)

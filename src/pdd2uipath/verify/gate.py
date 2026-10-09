"""Stage 7: the Windows gate (validate per file + build) and the lock pre-check."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from pdd2uipath.core.events import EventBus
from pdd2uipath.executors.base import Executor, Job, UipResult
from pdd2uipath.executors.surface import UipCommands
from pdd2uipath.executors.wsl_manual import RoundAborted, RoundPending
from pdd2uipath.verify.checks import _SKIP_DIRS
from pdd2uipath.verify.diagnostics import (
    Diagnostic,
    GateVerdict,
    classify,
    parse_build,
    parse_validate,
)


@dataclass
class GateResult:
    verdict: GateVerdict
    validate_results: list[UipResult] = field(default_factory=list)
    build_result: UipResult | None = None
    files: list[str] = field(default_factory=list)


def workflow_files(project_dir: Path) -> list[str]:
    out: list[str] = []
    for p in sorted(project_dir.rglob("*.xaml")):
        rel = p.relative_to(project_dir)
        if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
            continue
        out.append(rel.as_posix())
    return out


def run_gate(
    executor: Executor,
    commands: UipCommands,
    *,
    project_windows: str,
    project_dir: Path,
    stub_mode: bool,
    timeout_s: float = 900.0,
    bus: EventBus | None = None,
    files: list[str] | None = None,
) -> GateResult:
    rels = files if files is not None else workflow_files(project_dir)
    jobs = [
        Job(
            commands.validate(project_windows, rel),
            project_windows,
            timeout_s,
            label=f"validate {rel}",
        )
        for rel in rels
    ]
    jobs.append(Job(commands.build(project_windows), project_windows, timeout_s * 2, label="build"))
    results = executor.run_batch(jobs)
    diags: list[Diagnostic] = []
    validate_results = results[:-1]
    build_result = results[-1]
    for rel, res in zip(rels, validate_results, strict=True):
        diags.extend(parse_validate(res, project_windows, rel))
    diags.extend(parse_build(build_result, project_windows))
    verdict = classify(_dedupe(diags), stub_mode=stub_mode)
    if bus:
        bus.log(f"gate: {verdict.summary()}", level="info" if verdict.passed else "warning")
    return GateResult(
        verdict=verdict, validate_results=validate_results, build_result=build_result, files=rels
    )


def _dedupe(diags: list[Diagnostic]) -> list[Diagnostic]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Diagnostic] = []
    for d in diags:
        key = (d.file.lower(), d.severity, d.message.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


# ---------------------------------------------------------------- locks


@dataclass
class LockReport:
    locked_files: list[str] = field(default_factory=list)
    studio_instances: list[str] = field(default_factory=list)
    probe_error: str | None = None

    @property
    def blocked(self) -> bool:
        # An idle Studio Desktop instance that uip itself opened serves the gate; only real file
        # locks block it. The instance list is reported for the user.
        return bool(self.locked_files)

    def message(self) -> str:
        parts: list[str] = []
        if self.studio_instances:
            parts.append(
                "Studio Desktop has the project open ("
                + "; ".join(self.studio_instances)
                + "): close it in Studio (File > Close) and retry"
            )
        if self.locked_files:
            parts.append("files locked by another process: " + ", ".join(self.locked_files[:8]))
        if self.probe_error:
            parts.append(f"instances probe failed: {self.probe_error}")
        return ". ".join(parts)


def check_locks(
    project_dir: Path,
    project_windows: str,
    *,
    executor: Executor | None = None,
    commands: UipCommands | None = None,
    timeout_s: float = 120.0,
) -> LockReport:
    """Detect an open Studio Desktop instance or locked XAML/project.json before a gate."""
    report = LockReport()
    if os.name == "nt":
        for rel in ["project.json", *workflow_files(project_dir)]:
            p = project_dir / rel
            try:
                with p.open("r+b"):
                    pass
            except PermissionError:
                report.locked_files.append(rel)
            except OSError:
                continue
    if executor is not None and commands is not None:
        try:
            res = executor.run(commands.instances_list(), cwd=project_windows, timeout_s=timeout_s)
        except (RoundPending, RoundAborted):
            raise  # wsl-manual: the round must reach the runner, not vanish here
        except Exception as exc:  # the probe must never block the gate
            res = None
            report.probe_error = f"{type(exc).__name__}: {exc}"[:200]
        if res is not None and isinstance(res.data, list):
            for inst in res.data:
                if not isinstance(inst, dict):
                    continue
                proj = str(inst.get("ProjectDirectory", ""))
                if proj and proj.lower().rstrip("\\") == project_windows.lower().rstrip("\\"):
                    report.studio_instances.append(
                        f"pid {inst.get('ProcessId')} ({inst.get('ExecutionStatus', '?')})"
                    )
    return report

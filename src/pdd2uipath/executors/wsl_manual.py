"""The ``wsl-manual`` executor: batch Windows operations into one round the user runs.

A round is a directory ``<bridge>/rounds/<round-id>/`` with the job files, a
``run.cmd`` and a ``run.ps1`` that execute every job in order and write the same
result files the bridge worker would. The pipeline shows the exact command once,
then polls for results and continues automatically.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pdd2uipath.core.paths import to_windows
from pdd2uipath.core.platform import Platform, detect_platform
from pdd2uipath.core.state import atomic_write_text
from pdd2uipath.executors.base import (
    BaseExecutor,
    ExecutorError,
    ExecutorState,
    Job,
    UipResult,
    parse_envelope,
    translate_argv,
)
from pdd2uipath.executors.bridge_protocol import (
    BridgeJob,
    BridgeLayout,
    make_job,
    read_capture,
    read_result,
    validate_job,
    write_job,
)


class RoundAborted(ExecutorError):
    """The user cancelled the Windows round."""


class RoundPending(ExecutorError):
    """Non-interactive mode: the round was generated; results will be collected on resume."""

    def __init__(self, round_: ManualRound) -> None:
        super().__init__(f"Windows round pending: run {round_.run_cmd_windows}")
        self.round = round_


@dataclass
class ManualRound:
    id: str
    dir: Path
    dir_windows: str
    jobs: list[BridgeJob]
    run_cmd_windows: str
    run_ps1_windows: str
    created: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def jobs_dir(self) -> Path:
        return self.dir / "jobs"

    @property
    def results_dir(self) -> Path:
        return self.dir / "results"

    def instructions(self) -> str:
        lines = [
            f"Windows round {self.id}: {len(self.jobs)} uip operation(s) must run in your Windows session.",
            "Open a Windows terminal (cmd.exe or Windows Terminal) and run:",
            "",
            f"    {self.run_cmd_windows}",
            "",
            "or, if cmd scripts are blocked:",
            "",
            f'    powershell -NoProfile -ExecutionPolicy Bypass -File "{self.run_ps1_windows}"',
            "",
            "pdd2uipath keeps polling for the results and continues automatically.",
            "Jobs:",
        ]
        lines.extend(
            f"  [{i + 1}/{len(self.jobs)}] uip {' '.join(j.argv)}" for i, j in enumerate(self.jobs)
        )
        return "\n".join(lines)

    def pending_ids(self) -> list[str]:
        return [j.id for j in self.jobs if read_result(self.results_dir, j.id) is None]


class RoundHandler(Protocol):
    """How the UI presents a round. ``announce`` must not block; ``tick`` may raise
    :class:`RoundAborted` to cancel or :class:`RoundPending` to defer collection."""

    def announce(self, round_: ManualRound) -> None: ...

    def tick(self, round_: ManualRound, waited_s: float) -> None: ...

    def finished(self, round_: ManualRound) -> None: ...


class PlainRoundHandler:
    """CLI handler: print the instructions once, then wait (or defer with ``wait=False``)."""

    def __init__(self, *, wait: bool = True, echo: Callable[[str], None] = print) -> None:
        self.wait = wait
        self.echo = echo
        self._last_note = 0.0

    def announce(self, round_: ManualRound) -> None:
        self.echo(round_.instructions())

    def tick(self, round_: ManualRound, waited_s: float) -> None:
        if not self.wait:
            raise RoundPending(round_)
        if waited_s - self._last_note >= 30:
            self._last_note = waited_s
            self.echo(
                f"  waiting for Windows results... ({waited_s:.0f}s, {len(round_.pending_ids())} pending)"
            )

    def finished(self, round_: ManualRound) -> None:
        self.echo(f"Windows round {round_.id} complete.")


def _cmd_quote(tok: str) -> str:
    return f'"{tok}"'


def _ps_quote(tok: str) -> str:
    return "'" + tok.replace("'", "''") + "'"


def render_round_cmd(round_: ManualRound, uip_command: Sequence[str]) -> str:
    """A self-contained cmd.exe script. Every line runs on its own, so no delayed expansion."""
    head = uip_command[0]
    rest = " ".join(_cmd_quote(t) for t in uip_command[1:])
    if head.lower().endswith((".cmd", ".bat")):
        launcher = f'call "{head}" {rest}'.rstrip()
    else:
        launcher = f'"{head}" {rest}'.rstrip()
    lines = [
        "@echo off",
        "setlocal",
        'set "ROUND=%~dp0"',
        f"echo pdd2uipath Windows round {round_.id}: {len(round_.jobs)} job(s)",
        'if not exist "%ROUND%results" mkdir "%ROUND%results"',
    ]
    for i, job in enumerate(round_.jobs, 1):
        args = " ".join(_cmd_quote(a) for a in job.argv)
        lines += [
            f"echo [{i}/{len(round_.jobs)}] uip {' '.join(job.argv)}",
            f'cd /d "{job.cwd}"',
            f'{launcher} {args} > "%ROUND%results\\{job.id}.out" 2> "%ROUND%results\\{job.id}.err"',
            'set "EC=%ERRORLEVEL%"',
            f'> "%ROUND%results\\{job.id}.json.tmp" echo {{"schema":"pdd2uipath.bridge.result/1","id":"{job.id}","status":"ok","exit_code":%EC%,"stdout_file":"{job.id}.out","stderr_file":"{job.id}.err","worker":"manual-cmd","started":"","finished":""}}',
            f'move /y "%ROUND%results\\{job.id}.json.tmp" "%ROUND%results\\{job.id}.json" >nul',
        ]
    lines += ["echo Round complete. You can close this window.", "endlocal", "exit /b 0", ""]
    return "\r\n".join(lines)


def render_round_ps1(round_: ManualRound, uip_command: Sequence[str]) -> str:
    launcher = ", ".join(_ps_quote(t) for t in uip_command)
    lines = [
        "$ErrorActionPreference = 'Continue'",
        "$Round = Split-Path -Parent $MyInvocation.MyCommand.Definition",
        f"$Uip = @({launcher})",
        'New-Item -ItemType Directory -Force -Path (Join-Path $Round "results") | Out-Null',
        f'Write-Host "pdd2uipath Windows round {round_.id}: {len(round_.jobs)} job(s)"',
        "function Invoke-Job($Id, $Cwd, $ArgList) {",
        "  Set-Location -LiteralPath $Cwd",
        '  $out = Join-Path $Round "results\\$Id.out"; $err = Join-Path $Round "results\\$Id.err"',
        "  $started = (Get-Date).ToUniversalTime().ToString('o')",
        "  $exe = $Uip[0]; $pre = @(); if ($Uip.Count -gt 1) { $pre = $Uip[1..($Uip.Count-1)] }",
        "  if ($exe -match '\\.(cmd|bat)$') { $exe = 'cmd.exe'; $pre = @('/c', $Uip[0]) + $pre }",
        "  $p = Start-Process -FilePath $exe -ArgumentList (($pre + $ArgList) | ForEach-Object { '\"' + $_ + '\"' }) -NoNewWindow -Wait -PassThru -RedirectStandardOutput $out -RedirectStandardError $err",
        "  $finished = (Get-Date).ToUniversalTime().ToString('o')",
        "  $res = @{ schema = 'pdd2uipath.bridge.result/1'; id = $Id; status = 'ok'; exit_code = $p.ExitCode; stdout_file = \"$Id.out\"; stderr_file = \"$Id.err\"; worker = 'manual-ps1'; started = $started; finished = $finished }",
        '  $tmp = Join-Path $Round "results\\$Id.json.tmp"',
        "  ($res | ConvertTo-Json -Compress) | Set-Content -LiteralPath $tmp -Encoding UTF8",
        '  Move-Item -LiteralPath $tmp -Destination (Join-Path $Round "results\\$Id.json") -Force',
        "}",
    ]
    for i, job in enumerate(round_.jobs, 1):
        args = ", ".join(_ps_quote(a) for a in job.argv)
        lines += [
            f'Write-Host "[{i}/{len(round_.jobs)}] uip {" ".join(job.argv)}"',
            f"Invoke-Job {_ps_quote(job.id)} {_ps_quote(job.cwd)} @({args})",
        ]
    lines += ['Write-Host "Round complete. You can close this window."', ""]
    return "\r\n".join(lines)


class WslManualExecutor(BaseExecutor):
    name = "wsl-manual"

    def __init__(
        self,
        layout: BridgeLayout,
        workspace_root_windows: str,
        uip_command: Sequence[str],
        handler: RoundHandler,
        *,
        poll_interval_s: float = 1.0,
        platform: Platform | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.layout = layout
        self.workspace_root = to_windows(workspace_root_windows)
        self.uip_command = list(uip_command) or ["uip.cmd"]
        self.handler = handler
        self.poll_interval_s = poll_interval_s
        self.platform = platform or detect_platform()
        self._sleep = sleep
        self._clock = clock
        self.last_round: ManualRound | None = None
        self.pending_round_id: str | None = None

    def status(self) -> ExecutorState:
        return ExecutorState(self.name, True, "manual Windows rounds")

    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, timeout_s: float = 900.0
    ) -> UipResult:
        return self.run_batch([Job(list(argv), cwd, timeout_s)])[0]

    # -- rounds ---------------------------------------------------------------
    def prepare_round(self, jobs: Sequence[Job]) -> ManualRound:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        existing = (
            sorted(p.name for p in self.layout.rounds.glob(f"{stamp}*"))
            if self.layout.rounds.exists()
            else []
        )
        rid = f"{stamp}-{len(existing) + 1:02d}"
        rdir = self.layout.rounds / rid
        rdir_w = self.layout.windows("rounds", rid)
        (rdir / "jobs").mkdir(parents=True, exist_ok=True)
        (rdir / "results").mkdir(parents=True, exist_ok=True)
        bjobs: list[BridgeJob] = []
        for n, j in enumerate(jobs, 1):
            args = translate_argv(j.argv, self.platform)
            cwd_w = to_windows(j.cwd, self.platform) if j.cwd else self.workspace_root
            validate_job(args, cwd_w, self.workspace_root)
            bj = make_job(args, cwd_w, timeout_s=j.timeout_s, job_id=f"j{n:02d}", label=j.label)
            write_job(rdir / "jobs", bj)
            bjobs.append(bj)
        round_ = ManualRound(
            id=rid,
            dir=rdir,
            dir_windows=rdir_w,
            jobs=bjobs,
            run_cmd_windows=f"{rdir_w}\\run.cmd",
            run_ps1_windows=f"{rdir_w}\\run.ps1",
        )
        atomic_write_text(
            rdir / "manifest.json",
            json.dumps(
                {"round": rid, "jobs": [bj.model_dump(by_alias=True, mode="json") for bj in bjobs]},
                indent=2,
            ),
        )
        atomic_write_text(rdir / "run.cmd", render_round_cmd(round_, self.uip_command))
        atomic_write_text(rdir / "run.ps1", render_round_ps1(round_, self.uip_command))
        atomic_write_text(rdir / "README.txt", round_.instructions() + "\n")
        self.last_round = round_
        return round_

    def collect(self, round_: ManualRound, timeout_s: float | None = None) -> list[UipResult]:
        """Poll until every job of the round has a result (or the handler aborts/defers)."""
        started = self._clock()
        total_timeout = (
            timeout_s if timeout_s is not None else sum(j.timeout_s for j in round_.jobs) + 60
        )
        while round_.pending_ids():
            waited = self._clock() - started
            self.handler.tick(round_, waited)
            if waited > total_timeout:
                raise ExecutorError(
                    f"Windows round {round_.id} not completed after {total_timeout:.0f}s "
                    f"({len(round_.pending_ids())} job(s) pending)"
                )
            self._sleep(self.poll_interval_s)
        self.handler.finished(round_)
        results: list[UipResult] = []
        for bj in round_.jobs:
            res = read_result(round_.results_dir, bj.id)
            assert res is not None
            out, err = read_capture(round_.results_dir, res)
            code = res.exit_code if res.status == "ok" else (res.exit_code or 126)
            if res.status != "ok":
                err = (err + "\n" if err else "") + f"worker {res.status}: {res.reason}"
            results.append(
                UipResult(
                    argv=tuple(bj.argv),
                    cwd=bj.cwd,
                    exit_code=code,
                    stdout=out,
                    stderr=err,
                    duration_s=self._clock() - started,
                    side=self.name,
                    envelope=parse_envelope(out),
                )
            )
        return results

    def run_batch(self, jobs: Sequence[Job]) -> list[UipResult]:
        if not jobs:
            return []
        reuse = self._reusable_round(jobs)
        if reuse is not None:
            if reuse.pending_ids():
                self.handler.announce(reuse)
            return self.collect(reuse)
        round_ = self.prepare_round(jobs)
        self.handler.announce(round_)
        return self.collect(round_)

    def _reusable_round(self, jobs: Sequence[Job]) -> ManualRound | None:
        """A round generated earlier for exactly these jobs (the user may have run it since)."""
        rid = self.pending_round_id
        if not rid:
            return None
        try:
            rnd = self.load_round(rid)
        except ExecutorError:
            self.pending_round_id = None
            return None
        wanted = [translate_argv(j.argv, self.platform) for j in jobs]
        if [j.argv for j in rnd.jobs] != wanted:
            return None  # another call first (e.g. a probe); keep it for the matching one
        self.pending_round_id = None  # consumed: an identical later batch gets a fresh round
        self.last_round = rnd
        return rnd

    def load_round(self, round_id: str) -> ManualRound:
        rdir = self.layout.rounds / round_id
        if not rdir.is_dir():
            raise ExecutorError(f"unknown round {round_id}")
        from pdd2uipath.executors.bridge_protocol import BridgeJob, read_job

        manifest = rdir / "manifest.json"
        if manifest.is_file():  # written with the round; job files may have been consumed
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            jobs = [BridgeJob.model_validate(j) for j in data.get("jobs", [])]
        else:
            jobs = [read_job(p) for p in sorted((rdir / "jobs").glob("*.json"))]
        rdir_w = self.layout.windows("rounds", round_id)
        return ManualRound(
            id=round_id,
            dir=rdir,
            dir_windows=rdir_w,
            jobs=jobs,
            run_cmd_windows=f"{rdir_w}\\run.cmd",
            run_ps1_windows=f"{rdir_w}\\run.ps1",
        )

"""Pipeline commands for the Typer CLI (plain, scriptable mode)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from pdd2uipath.core.config import Settings, load_settings
from pdd2uipath.core.events import (
    AgentMessage,
    AgentToolResult,
    AgentToolUse,
    CommandFinished,
    EventBus,
    GateWaiting,
    LogEvent,
    RepairRound,
    StageFinished,
    StageStarted,
)
from pdd2uipath.core.runner import Pipeline, PipelineStop, PlainGateHandler
from pdd2uipath.core.state import STAGES
from pdd2uipath.executors.base import ExecutorError
from pdd2uipath.executors.wsl_manual import RoundAborted, RoundPending

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_WAITING_APPROVAL = 3
EXIT_WAITING_ROUND = 4
EXIT_STOPPED = 5

PddOpt = Annotated[
    Path | None, typer.Option("--pdd", help="PDD file (.docx/.pdf); derives the run directory")
]
RunOpt = Annotated[
    Path | None, typer.Option("--run", help="Existing run directory (brain-side path)")
]
ForceOpt = Annotated[bool, typer.Option("--force", help="Re-run even if inputs are unchanged")]
ApproverOpt = Annotated[
    str | None, typer.Option("--approver", help="Approve the plan non-interactively as this person")
]
NoWaitOpt = Annotated[
    bool,
    typer.Option(
        "--no-wait", help="wsl-manual: generate the Windows round and exit (resume later)"
    ),
]


class PlainPrinter:
    """Subscriber that renders the event stream as readable lines."""

    def __init__(self, console: Console, *, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose

    def __call__(self, ev: Any) -> None:
        c = self.console
        if isinstance(ev, StageStarted):
            c.print(f"[bold cyan]== {ev.stage} ==[/]")
        elif isinstance(ev, StageFinished):
            colour = {
                "done": "green",
                "skipped": "dim",
                "waiting": "yellow",
                "stopped": "yellow",
                "failed": "red",
            }[ev.status]
            c.print(f"[{colour}]-- {ev.stage}: {ev.status}[/] {ev.detail}")
        elif isinstance(ev, CommandFinished):
            mark = "[green]ok[/]" if ev.ok else "[red]FAIL[/]"
            c.print(
                f"  uip {' '.join(ev.argv[:4])}{' ...' if len(ev.argv) > 4 else ''} {mark} {ev.duration_s:.1f}s ({ev.side})"
            )
            if not ev.ok and ev.summary:
                c.print(f"    {ev.summary[:400]}")
        elif isinstance(ev, LogEvent):
            style = {"debug": "dim", "info": "", "warning": "yellow", "error": "red"}[ev.level]
            if ev.level != "debug" or self.verbose:
                c.print(f"  [{style}]{ev.message}[/]" if style else f"  {ev.message}")
        elif isinstance(ev, AgentToolUse):
            c.print(f"  [magenta]tool[/] {ev.tool}: {ev.summary[:160]}")
        elif isinstance(ev, AgentToolResult):
            c.print(
                f"  [magenta]result[/] {ev.tool}: {'ERROR ' if ev.is_error else ''}{ev.summary[:160]}"
            )
        elif isinstance(ev, AgentMessage):
            if ev.final or self.verbose:
                c.print(f"  [blue]{ev.role}>[/] {ev.text[:2000]}")
        elif isinstance(ev, GateWaiting):
            c.print(
                f"[bold yellow]GATE[/] {ev.gate}: {ev.payload.get('reason') or ev.payload.get('message') or ''}"
            )
        elif isinstance(ev, RepairRound):
            c.print(
                f"[bold yellow]repair round {ev.round}/{ev.max_rounds}[/] ({ev.errors} error(s))"
            )


def _settings(ctx: typer.Context) -> Settings:
    cfg = ctx.obj.get("config") if ctx.obj else None
    return load_settings(cfg)


def _pipeline(
    ctx: typer.Context,
    pdd: Path | None,
    run: Path | None,
    *,
    approver: str | None = None,
    wait: bool = True,
) -> Pipeline:
    settings = _settings(ctx)
    console = (
        Console(stderr=True, force_terminal=False, no_color=True)
        if not sys.stderr.isatty()
        else Console(stderr=True)
    )
    bus = EventBus()
    bus.subscribe(PlainPrinter(console))
    gates = PlainGateHandler(
        approver=approver,
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        wait_for_rounds=wait,
        echo=lambda s: console.print(s, markup=False),
    )
    if run is not None:
        return Pipeline(settings, run_dir=run, bus=bus, gates=gates)
    if pdd is None:
        raise typer.BadParameter("give --pdd <file> or --run <dir>")
    return Pipeline(settings, pdd=pdd, bus=bus, gates=gates)


def _run(fn: Any) -> None:
    try:
        fn()
    except PipelineStop as stop:
        typer.echo(f"\n{stop}", err=True)
        if stop.waiting_for == "plan_approval":
            raise typer.Exit(EXIT_WAITING_APPROVAL) from None
        if stop.waiting_for in {"windows_round", "windows_side"}:
            raise typer.Exit(EXIT_WAITING_ROUND) from None
        raise typer.Exit(EXIT_STOPPED if stop.status != "failed" else EXIT_ERROR) from None
    except RoundPending as pending:
        typer.echo(
            f"\nWindows round pending: run {pending.round.run_cmd_windows} then `pdd2uipath resume-round`",
            err=True,
        )
        raise typer.Exit(EXIT_WAITING_ROUND) from None
    except RoundAborted:
        typer.echo("\nWindows round aborted", err=True)
        raise typer.Exit(EXIT_STOPPED) from None
    except ExecutorError as exc:
        typer.echo(f"\nexecutor error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from None


def register(app: typer.Typer) -> None:
    @app.command()
    def ingest(
        ctx: typer.Context,
        pdd: Annotated[Path, typer.Argument(help="PDD file (.docx/.pdf)")],
        force: ForceOpt = False,
    ) -> None:
        """Stage 1: parse the PDD and apply the approval gate."""
        p = _pipeline(ctx, pdd, None)
        _run(lambda: p.ingest(force=force))
        typer.echo(f"run directory: {p.paths.run_dir}")

    @app.command()
    def extract(
        ctx: typer.Context, pdd: PddOpt = None, run: RunOpt = None, force: ForceOpt = False
    ) -> None:
        """Stage 2: extract the ProcessSpec (model-backed, every field cites the PDD)."""
        p = _pipeline(ctx, pdd, run)
        _run(lambda: p.extract(force=force))

    @app.command()
    def design(
        ctx: typer.Context,
        pdd: PddOpt = None,
        run: RunOpt = None,
        force: ForceOpt = False,
        no_wait: NoWaitOpt = False,
    ) -> None:
        """Stage 3: deterministic design rules (+ template/package probes on Windows)."""
        p = _pipeline(ctx, pdd, run, wait=not no_wait)
        _run(lambda: p.design_stage(force=force))

    @app.command()
    def plan(
        ctx: typer.Context, pdd: PddOpt = None, run: RunOpt = None, force: ForceOpt = False
    ) -> None:
        """Stage 4: render docs/plans/<slug>.md."""
        p = _pipeline(ctx, pdd, run)
        _run(lambda: p.plan(force=force))
        _ok, reason = p.approval_status()
        typer.echo(f"plan: {p.paths.plan} ({reason})")

    @app.command("approve-plan")
    def approve_plan(
        ctx: typer.Context,
        approver: Annotated[str, typer.Option("--approver", help="Your name")],
        pdd: PddOpt = None,
        run: RunOpt = None,
    ) -> None:
        """Record the human approval of the current plan (hash-bound)."""
        p = _pipeline(ctx, pdd, run)
        _run(lambda: p.approve_plan(approver))
        typer.echo(p.approval_status()[1])

    @app.command()
    def scaffold(
        ctx: typer.Context,
        pdd: PddOpt = None,
        run: RunOpt = None,
        force: ForceOpt = False,
        no_wait: NoWaitOpt = False,
    ) -> None:
        """Stage 5: uip rpa init/restore/packages install on the Windows side."""
        p = _pipeline(ctx, pdd, run, wait=not no_wait)
        _run(lambda: p.scaffold(force=force))

    @app.command()
    def author(
        ctx: typer.Context, pdd: PddOpt = None, run: RunOpt = None, force: ForceOpt = False
    ) -> None:
        """Stage 6: run the coding agent with the uipath-rpa skill on the plan."""
        p = _pipeline(ctx, pdd, run)
        _run(lambda: p.author(force=force))

    @app.command()
    def verify(
        ctx: typer.Context, pdd: PddOpt = None, run: RunOpt = None, no_wait: NoWaitOpt = False
    ) -> None:
        """Stage 7: Windows gate (validate + build) + Python audit, with bounded repair rounds."""
        p = _pipeline(ctx, pdd, run, wait=not no_wait)
        _run(lambda: p.verify())
        if p.last_gate:
            typer.echo(p.last_gate.verdict.summary())

    @app.command()
    def report(ctx: typer.Context, pdd: PddOpt = None, run: RunOpt = None) -> None:
        """Stage 8: report.md + traceability.csv."""
        p = _pipeline(ctx, pdd, run)
        _run(lambda: p.report())
        typer.echo(f"report: {p.paths.report}\ntraceability: {p.paths.traceability}")

    @app.command("run-all")
    def run_all(
        ctx: typer.Context,
        pdd: Annotated[Path, typer.Argument(help="PDD file (.docx/.pdf)")],
        approver: ApproverOpt = None,
        stop_after: Annotated[
            str | None, typer.Option("--stop-after", help="Stop after this stage")
        ] = None,
        force: ForceOpt = False,
        no_wait: NoWaitOpt = False,
    ) -> None:
        """Run every stage; pauses at the plan approval gate (and Windows rounds in wsl-manual mode)."""
        p = _pipeline(ctx, pdd, None, approver=approver, wait=not no_wait)
        _run(lambda: p.run_all(stop_after=stop_after, force=force))
        typer.echo(f"done: {p.paths.run_dir}")

    @app.command("resume-round")
    def resume_round(
        ctx: typer.Context, pdd: PddOpt = None, run: RunOpt = None, no_wait: NoWaitOpt = False
    ) -> None:
        """wsl-manual: collect the results of the pending Windows round and continue."""
        p = _pipeline(ctx, pdd, run, wait=not no_wait)
        _run(lambda: p.resume_round())
        typer.echo(f"done: {p.paths.run_dir}")

    @app.command("open-studio")
    def open_studio(ctx: typer.Context, pdd: PddOpt = None, run: RunOpt = None) -> None:
        """Open the generated project in Studio Desktop (Windows side)."""
        p = _pipeline(ctx, pdd, run)
        _run(lambda: typer.echo(p.open_studio()))

    @app.command()
    def status(ctx: typer.Context, pdd: PddOpt = None, run: RunOpt = None) -> None:
        """Show the run state (stages, gates, repair rounds)."""
        p = _pipeline(ctx, pdd, run)
        table = Table(title=f"run {p.state.run_id} ({p.paths.slug})")
        table.add_column("stage")
        table.add_column("status")
        table.add_column("detail")
        for name in STAGES:
            rec = p.state.stage(name)
            detail = rec.error or str(rec.outputs.get("summary", ""))
            table.add_row(
                name,
                rec.status + (f" ({rec.waiting_for})" if rec.waiting_for else ""),
                detail[:100],
            )
        Console().print(table)
        __ok, reason = p.approval_status() if p.paths.plan.exists() else (False, "no plan yet")
        typer.echo(
            f"approval: {reason}; repair round {p.state.repair_round}; executor {p.executor_kind.value}"
        )

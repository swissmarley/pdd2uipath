"""Typer CLI. ``pdd2uipath`` with no arguments in a TTY opens the TUI."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from pdd2uipath import __version__
from pdd2uipath.core.config import ExecutorKind, load_settings
from pdd2uipath.core.paths import PathTranslationError, to_windows
from pdd2uipath.core.platform import detect_platform
from pdd2uipath.executors.base import ExecutorError
from pdd2uipath.executors.factory import build_executor, recommend_executor

app = typer.Typer(
    name="pdd2uipath",
    help="Approved PDD -> UiPath Studio project pipeline (WSL + Windows).",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
)
bridge_app = typer.Typer(help="Manage the Windows bridge worker (wsl-bridge executor).")
app.add_typer(bridge_app, name="bridge")
from pdd2uipath.cli_mcp import mcp_app  # noqa: E402

app.add_typer(mcp_app, name="mcp")

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Path to pdd2uipath.toml")]
PlainOpt = Annotated[bool, typer.Option("--plain", help="Scriptable output, no TUI")]


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context, config: ConfigOpt = None, plain: PlainOpt = False) -> None:
    ctx.obj = {"config": config, "plain": plain}
    if ctx.invoked_subcommand is None:
        if plain or not sys.stdin.isatty() or not sys.stdout.isatty():
            typer.echo(ctx.get_help())
            raise typer.Exit(0)
        from pdd2uipath.tui.app import run_tui

        run_tui(config)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(f"pdd2uipath {__version__}")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def forward(
    ctx: typer.Context,
    config: ConfigOpt = None,
    project: Annotated[
        str | None, typer.Option("--project", help="Windows path of the project (fallback cwd)")
    ] = None,
) -> None:
    """Target of the per-project `uip` shim: run `uip ARGS...` through the active executor."""
    args = [a for a in ctx.args if a != "--"]
    settings = load_settings(config)
    platform = detect_platform()
    kind = (
        settings.executor
        if settings.executor != ExecutorKind.AUTO
        else recommend_executor(platform)
    )
    if kind == ExecutorKind.WSL_MANUAL:
        sys.stderr.write(
            "uip is not available from this session (executor = wsl-manual).\n"
            "Author only; validation and build run in the Windows gate afterwards.\n"
        )
        raise typer.Exit(2)
    try:
        cwd = to_windows(os.getcwd(), platform)
    except PathTranslationError:
        cwd = project or settings.workspace_root
    try:
        executor = build_executor(settings, platform, kind=kind)
        result = executor.run(args, cwd=cwd, timeout_s=settings.bridge.job_timeout_s)
    except (ExecutorError, PathTranslationError) as exc:
        sys.stderr.write(f"pdd2uipath forward: {exc}\n")
        raise typer.Exit(2) from exc
    sys.stdout.write(result.stdout)
    sys.stdout.flush()
    sys.stderr.write(result.stderr)
    sys.stderr.flush()
    raise typer.Exit(result.exit_code)


@bridge_app.command("install")
def bridge_install(config: ConfigOpt = None) -> None:
    """Generate worker.ps1 / worker.cmd under <workspace>/.pdd2uipath-bridge."""
    from pdd2uipath.doctor.bridge import install_bridge

    settings = load_settings(config)
    info = install_bridge(settings)
    typer.echo(f"Bridge installed at {info.layout.root_windows}")
    typer.echo(f"  worker (cmd): {info.layout.windows('worker.cmd')}")
    typer.echo(f"  worker (ps1): {info.layout.windows('worker.ps1')}")
    typer.echo(f"  uip command : {' '.join(info.uip_command)}")
    typer.echo("Start it once per Windows session, in a Windows terminal:")
    typer.echo("  " + info.start_hint().replace("\n", "\n  "))


@bridge_app.command("status")
def bridge_status_cmd(config: ConfigOpt = None) -> None:
    """Show whether the Windows worker is online (heartbeat)."""
    from pdd2uipath.doctor.bridge import bridge_status

    state = bridge_status(load_settings(config))
    typer.echo(f"wsl-bridge: {'online' if state.online else 'offline'} - {state.detail}")
    raise typer.Exit(0 if state.online else 1)


@bridge_app.command("stop")
def bridge_stop(config: ConfigOpt = None) -> None:
    """Ask a running worker to exit (creates the STOP file)."""
    from pdd2uipath.doctor.bridge import stop_bridge

    path = stop_bridge(load_settings(config))
    typer.echo(f"STOP file written: {path}")


@app.command()
def doctor(
    config: ConfigOpt = None,
    write_config: Annotated[
        bool,
        typer.Option(
            "--write-config",
            help="Write the recommended executor and discovered paths to the config file",
        ),
    ] = False,
    probe_studio: Annotated[
        bool,
        typer.Option(
            "--probe-studio/--no-probe-studio",
            help="Call the headless Studio host once (slow on first use)",
        ),
    ] = True,
    network: Annotated[
        bool, typer.Option("--network/--no-network", help="Probe registries/feeds reachability")
    ] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Stage 0: preflight checks, executor recommendation, live CLI surface (docs/cli-surface.md)."""
    import json as _json

    from rich.console import Console
    from rich.table import Table

    from pdd2uipath.core.config import default_user_config_path, settings_source_path
    from pdd2uipath.doctor.checks import apply_recommendation, run_doctor

    settings = load_settings(config)
    report = run_doctor(settings, probe_studio=probe_studio, probe_network=network)
    if as_json:
        typer.echo(
            _json.dumps(
                {
                    "ok": report.ok,
                    "recommended_executor": report.recommended_executor.value,
                    "checks": [c.__dict__ for c in report.checks],
                },
                indent=2,
            )
        )
    else:
        table = Table(title="pdd2uipath doctor", show_lines=False)
        table.add_column("status", width=6)
        table.add_column("check")
        table.add_column("detail / fix")
        glyph = {
            "ok": "[green]ok[/]",
            "warn": "[yellow]warn[/]",
            "fail": "[red]FAIL[/]",
            "skip": "[dim]skip[/]",
        }
        for c in report.checks:
            fix = f"\n[dim]fix:[/] {c.fix}" if c.fix and c.status != "ok" else ""
            detail = c.detail + fix
            table.add_row(glyph[c.status], c.title, detail)
        Console().print(table)
        typer.echo(f"recommended executor: {report.recommended_executor.value}")
        if report.surface_md:
            typer.echo(f"CLI surface written to {report.surface_md}")
    if write_config:
        target = settings_source_path(settings) or default_user_config_path()
        apply_recommendation(settings, report, target)
        typer.echo(f"config written: {target}")
    raise typer.Exit(0 if report.ok else 1)


def _register_pipeline_commands() -> None:
    try:
        from pdd2uipath.cli_pipeline import register
    except ImportError:  # pragma: no cover - pipeline commands not built yet
        return
    register(app)


_register_pipeline_commands()


def main() -> None:
    app()


if __name__ == "__main__":
    main()

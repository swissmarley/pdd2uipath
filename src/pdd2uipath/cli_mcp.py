"""``pdd2uipath mcp ...``: talk to the configured MCP servers from the command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from pdd2uipath.core.config import Settings, load_settings
from pdd2uipath.mcp.client import McpClient, McpError
from pdd2uipath.mcp.recipes import RecipeError, fetch_pdd, resolve_servers, run_recipe

mcp_app = typer.Typer(help="MCP client: servers, tools, resources, recipes, PDD fetch.")

ServerArg = Annotated[
    str, typer.Argument(help="server name from [mcp.servers] or OpenCode's config")
]
OutOpt = Annotated[
    Path | None, typer.Option("--out", help="write the result (blob or text) to this file")
]
JsonOpt = Annotated[bool, typer.Option("--json", help="machine-readable output")]


def _settings(ctx: typer.Context) -> Settings:
    cfg = ctx.obj.get("config") if ctx.obj else None
    return load_settings(cfg)


def _server(settings: Settings, name: str) -> McpClient:
    servers = resolve_servers(settings)
    if name not in servers:
        known = ", ".join(sorted(servers)) or "none"
        raise typer.BadParameter(f"unknown MCP server {name!r}; configured: {known}")
    return McpClient(name, servers[name])


def _fail(exc: Exception) -> None:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(1)


@mcp_app.command("servers")
def servers(ctx: typer.Context, json_out: JsonOpt = False) -> None:
    """List the configured MCP servers (and recipes)."""
    settings = _settings(ctx)
    found = resolve_servers(settings)
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "servers": {
                        k: v.model_dump(exclude={"env", "headers"}) for k, v in found.items()
                    },
                    "recipes": {k: v.model_dump() for k, v in settings.mcp.recipes.items()},
                },
                indent=2,
            )
        )
        return
    if not found:
        typer.echo("no MCP servers configured ([mcp.servers.<name>] or mcp.import_opencode = true)")
    for name, cfg in found.items():
        target = " ".join(cfg.command) if cfg.transport == "stdio" else cfg.url
        typer.echo(f"{name:16} {cfg.transport:6} {target}  {cfg.description}")
    for name, r in settings.mcp.recipes.items():
        steps = ", ".join(s.tool for s in r.all_steps())
        typer.echo(f"recipe {name:12} -> {r.server}: {steps}  params: {', '.join(r.params) or '-'}")


@mcp_app.command("tools")
def tools(ctx: typer.Context, server: ServerArg, json_out: JsonOpt = False) -> None:
    """List the tools a server offers."""
    try:
        with _server(_settings(ctx), server) as c:
            found = c.list_tools()
    except McpError as exc:
        _fail(exc)
        return
    if json_out:
        typer.echo(json.dumps([t.__dict__ for t in found], indent=2))
        return
    typer.echo(
        f"{server}: {c.server_info.get('name', '?')} {c.server_info.get('version', '')} ({len(found)} tools)"
    )
    for t in found:
        typer.echo("  " + t.summary())


@mcp_app.command("call")
def call(
    ctx: typer.Context,
    server: ServerArg,
    tool: Annotated[str, typer.Argument(help="tool name")],
    args: Annotated[str, typer.Option("--args", help="JSON object of arguments")] = "{}",
    out: OutOpt = None,
) -> None:
    """Call one tool; prints its text (or saves the blob with --out)."""
    try:
        arguments: Any = json.loads(args)
    except ValueError as exc:
        raise typer.BadParameter(f"--args must be a JSON object: {exc}") from exc
    try:
        with _server(_settings(ctx), server) as c:
            result = c.call_tool(tool, arguments)
    except McpError as exc:
        _fail(exc)
        return
    if out is not None:
        typer.echo(str(result.save(out)))
        return
    typer.echo(result.text or json.dumps(result.raw, indent=2))


@mcp_app.command("resources")
def resources(ctx: typer.Context, server: ServerArg) -> None:
    """List the resources a server exposes."""
    try:
        with _server(_settings(ctx), server) as c:
            found = c.list_resources()
    except McpError as exc:
        _fail(exc)
        return
    for r in found:
        typer.echo(f"{r.uri}  {r.mime_type}  {r.name}")


@mcp_app.command("read")
def read(ctx: typer.Context, server: ServerArg, uri: str, out: OutOpt = None) -> None:
    """Read a resource; prints text or saves the blob with --out."""
    try:
        with _server(_settings(ctx), server) as c:
            result = c.read_resource(uri)
    except McpError as exc:
        _fail(exc)
        return
    if out is not None:
        typer.echo(str(result.save(out)))
        return
    typer.echo(result.text or "(binary; use --out)")


@mcp_app.command("recipe")
def recipe(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="recipe name from [mcp.recipes]")],
    params: Annotated[list[str] | None, typer.Argument(help="key=value parameters")] = None,
    out: OutOpt = None,
) -> None:
    """Run a configured recipe (a sequence of tool calls) with key=value parameters."""
    kv: dict[str, Any] = {}
    for item in params or []:
        if "=" not in item:
            raise typer.BadParameter(f"parameters are key=value, got {item!r}")
        k, v = item.split("=", 1)
        kv[k] = v
    if out is not None:
        kv["out"] = str(out)
    try:
        outcome = run_recipe(_settings(ctx), name, kv, log=lambda m: typer.echo(m, err=True))
    except (RecipeError, McpError) as exc:
        _fail(exc)
        return
    for p in outcome.saved:
        typer.echo(f"saved {p}")
    if out is not None and not outcome.saved:
        typer.echo(str(outcome.last.save(out)))
    elif not outcome.saved:
        typer.echo(outcome.text or json.dumps(outcome.last.raw, indent=2))


@mcp_app.command("fetch-pdd")
def fetch(
    ctx: typer.Context,
    ref: Annotated[
        str, typer.Argument(help="page id, URL or attachment reference the recipe understands")
    ],
    out: OutOpt = None,
    recipe_name: Annotated[
        str | None, typer.Option("--recipe", help="recipe name (default: mcp.fetch_recipe)")
    ] = None,
) -> None:
    """Fetch a PDD through the fetch recipe and print the local path (then run-all on it)."""
    try:
        path = fetch_pdd(
            _settings(ctx), ref, out=out, recipe=recipe_name, log=lambda m: typer.echo(m, err=True)
        )
    except (RecipeError, McpError) as exc:
        _fail(exc)
        return
    typer.echo(str(path))


__all__ = ["mcp_app"]

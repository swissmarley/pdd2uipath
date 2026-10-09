"""Recipes: named tool-call sequences with templated arguments, and the PDD fetch convention.

Every MCP server names its tools differently (one Confluence server has ``get_page``, another
``confluence_read_page``), so pdd2uipath never hard-codes tool names. A recipe in the config
maps a pdd2uipath intent (fetch this PDD, create that branch) onto the tools of *your* server:

    [mcp.recipes.fetch-pdd]
    server = "confluence"
    tool = "get_attachment"
    args = { page_id = "{ref}", filename = "PDD.docx" }

Templates: ``{ref}`` and any other parameter, ``{steps.0.text}`` / ``{steps.0.json.key}`` for
earlier results, ``{out}`` for the output path chosen by the caller. A value that is exactly
one placeholder keeps its type (numbers, objects), so structured arguments pass through.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdd2uipath.core.config import McpRecipeConfig, McpServerConfig, Settings
from pdd2uipath.core.state import slugify
from pdd2uipath.mcp.client import McpClient, McpError, McpResult, open_client
from pdd2uipath.mcp.opencode_import import import_opencode_servers

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")
ClientFactory = Callable[[str, McpServerConfig], McpClient]


class RecipeError(RuntimeError):
    pass


@dataclass
class RecipeOutcome:
    name: str
    server: str
    results: list[McpResult] = field(default_factory=list)
    saved: list[Path] = field(default_factory=list)

    @property
    def last(self) -> McpResult:
        if not self.results:
            raise RecipeError("recipe produced no result")
        return self.results[-1]

    @property
    def text(self) -> str:
        return self.last.text


def resolve_servers(settings: Settings) -> dict[str, McpServerConfig]:
    """Configured servers plus, when enabled, those of OpenCode's config; ours win on a name clash."""
    servers: dict[str, McpServerConfig] = {}
    if settings.mcp.import_opencode:
        servers.update(import_opencode_servers(settings.mcp.opencode_config))
    servers.update(settings.mcp.servers)
    return {k: v for k, v in servers.items() if v.enabled}


def _lookup(path: str, ctx: dict[str, Any]) -> Any:
    obj: Any = ctx
    for part in path.split("."):
        if isinstance(obj, dict):
            if part not in obj:
                raise RecipeError(f"unknown placeholder {{{path}}} (missing '{part}')")
            obj = obj[part]
        elif isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError) as exc:
                raise RecipeError(f"bad index in placeholder {{{path}}}") from exc
        else:
            if not hasattr(obj, part):
                raise RecipeError(f"unknown placeholder {{{path}}} (no '{part}')")
            obj = getattr(obj, part)
    return obj


def render(value: Any, ctx: dict[str, Any]) -> Any:
    """Expand placeholders in strings, recursively through dicts and lists."""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole:
            return _lookup(whole.group(1), ctx)
        return _PLACEHOLDER.sub(lambda m: str(_lookup(m.group(1), ctx)), value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


def run_recipe(
    settings: Settings,
    name: str,
    params: dict[str, Any],
    *,
    client_factory: ClientFactory = open_client,
    log: Callable[[str], None] | None = None,
) -> RecipeOutcome:
    recipe = settings.mcp.recipes.get(name)
    if recipe is None:
        known = ", ".join(sorted(settings.mcp.recipes)) or "none"
        raise RecipeError(f"unknown recipe {name!r}; configured: {known}")
    servers = resolve_servers(settings)
    if recipe.server not in servers:
        raise RecipeError(f"recipe {name!r} uses server {recipe.server!r}, which is not configured")
    missing = [p for p in recipe.params if p not in params]
    if missing:
        raise RecipeError(f"recipe {name!r} needs parameters: {', '.join(missing)}")
    steps = recipe.all_steps()
    if not steps:
        raise RecipeError(f"recipe {name!r} has no steps")
    outcome = RecipeOutcome(name=name, server=recipe.server)
    ctx: dict[str, Any] = {**params, "params": params, "steps": []}
    with client_factory(recipe.server, servers[recipe.server]) as client:
        for i, step in enumerate(steps):
            args = render(step.args, ctx)
            if log:
                log(f"recipe {name} step {i + 1}/{len(steps)}: {step.tool}({', '.join(args)})")
            try:
                result = client.call_tool(step.tool, args)
            except McpError as exc:
                raise RecipeError(str(exc)) from exc
            outcome.results.append(result)
            ctx["steps"].append({"text": result.text, "json": result.json, "raw": result.raw})
            if step.save_as:
                target = Path(str(render(step.save_as, ctx))).expanduser()
                outcome.saved.append(result.save(target))
    return outcome


def _recipe_for(settings: Settings, name: str) -> McpRecipeConfig:
    recipe = settings.mcp.recipes.get(name)
    if recipe is None:
        raise RecipeError(
            f"no recipe {name!r}: add [mcp.recipes.{name}] with server/tool/args to the config "
            "(see docs/handbook.md, MCP section)"
        )
    return recipe


def fetch_pdd(
    settings: Settings,
    ref: str,
    *,
    out: Path | None = None,
    recipe: str | None = None,
    extra: dict[str, Any] | None = None,
    client_factory: ClientFactory = open_client,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Run the fetch recipe for ``ref`` and return the local PDD path.

    The recipe may save the file itself (``save_as``); otherwise the last result is written to
    ``out`` or to ``<inbox>/<slug of ref><extension by mime type>``.
    """
    name = recipe or settings.mcp.fetch_recipe
    _recipe_for(settings, name)
    inbox = Path(settings.mcp.inbox)
    if out is None:
        out = inbox / f"{slugify(ref) or 'pdd'}"
    params: dict[str, Any] = {"ref": ref, "out": str(out), **(extra or {})}
    outcome = run_recipe(settings, name, params, client_factory=client_factory, log=log)
    if outcome.saved:
        return outcome.saved[-1]
    result = outcome.last
    if not result.content:
        raise RecipeError(f"recipe {name!r} returned nothing for {ref!r}")
    target = out if out.suffix else out.with_suffix(result.suggested_extension())
    return result.save(target)


__all__ = ["RecipeError", "RecipeOutcome", "fetch_pdd", "render", "resolve_servers", "run_recipe"]

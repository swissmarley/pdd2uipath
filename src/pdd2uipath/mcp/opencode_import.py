"""Read the ``mcp`` section of OpenCode's config so already-configured servers can be reused.

OpenCode (``~/.config/opencode/opencode.json`` or ``.jsonc``) declares servers as::

    "mcp": {
      "confluence": {"type": "local", "command": ["npx", "-y", "@acme/confluence-mcp"],
                     "environment": {"CONFLUENCE_TOKEN": "${CONFLUENCE_TOKEN}"}},
      "github": {"type": "remote", "url": "http://localhost:8081/mcp",
                 "headers": {"Authorization": "Bearer ${GITHUB_MCP_TOKEN}"}}
    }

``local`` becomes a stdio server, ``remote`` an http server. Comments in JSONC are stripped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pdd2uipath.core.config import McpServerConfig


def default_opencode_config() -> Path | None:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "opencode"
    for name in ("opencode.json", "opencode.jsonc"):
        p = base / name
        if p.is_file():
            return p
    return None


def strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments outside of strings."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_opencode_mcp(data: dict[str, Any]) -> dict[str, McpServerConfig]:
    servers: dict[str, McpServerConfig] = {}
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return servers
    for name, raw in mcp.items():
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type", "local"))
        enabled = bool(raw.get("enabled", True))
        if kind == "local":
            cmd = raw.get("command") or []
            if isinstance(cmd, str):
                cmd = cmd.split()
            servers[name] = McpServerConfig(
                transport="stdio",
                command=[str(c) for c in cmd],
                env={str(k): str(v) for k, v in (raw.get("environment") or {}).items()},
                enabled=enabled,
                description=f"from OpenCode config ({kind})",
            )
        elif kind == "remote":
            servers[name] = McpServerConfig(
                transport="http",
                url=str(raw.get("url", "")),
                headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
                enabled=enabled,
                description=f"from OpenCode config ({kind})",
            )
    return servers


def import_opencode_servers(path: str | None = None) -> dict[str, McpServerConfig]:
    cfg = Path(path).expanduser() if path else default_opencode_config()
    if cfg is None or not cfg.is_file():
        return {}
    try:
        data = json.loads(strip_jsonc(cfg.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        return {}
    return parse_opencode_mcp(data) if isinstance(data, dict) else {}


__all__ = [
    "default_opencode_config",
    "import_opencode_servers",
    "parse_opencode_mcp",
    "strip_jsonc",
]

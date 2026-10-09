"""A small Model Context Protocol client: stdio and streamable-HTTP transports, stdlib only.

Why stdlib: the corporate environment installs Python packages from an Artifactory mirror and
runs behind proxies; ``urllib`` honours ``HTTPS_PROXY`` / ``NO_PROXY`` and the OS trust store
through ``SSL_CERT_FILE``, and a stdio server is just a subprocess. The client speaks JSON-RPC
2.0: ``initialize`` + ``notifications/initialized``, then ``tools/list``, ``tools/call``,
``resources/list``, ``resources/read`` and ``prompts/list``. Server-initiated ``ping`` requests
are answered; other server requests get a "method not found" error so a server never blocks.

Secrets never live in the config: ``${VAR}`` in ``env`` and ``headers`` values is expanded from
the process environment at connect time and never logged.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdd2uipath.core.config import McpServerConfig
from pdd2uipath.core.exe import resolve_argv

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "pdd2uipath", "version": "0.1.0"}
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpError(RuntimeError):
    """Transport failure, protocol error, or a JSON-RPC error returned by the server."""


def expand_env(value: str, env: dict[str, str] | None = None) -> str:
    """Replace ``${VAR}`` with the environment value; a missing variable raises."""
    src = os.environ if env is None else env

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in src:
            raise McpError(f"environment variable {name} is not set (referenced as ${{{name}}})")
        return src[name]

    return _VAR_RE.sub(_sub, value)


# ----------------------------------------------------------------------------- results
@dataclass
class McpContent:
    type: str
    text: str = ""
    data: bytes | None = None
    mime_type: str = ""
    uri: str = ""

    @classmethod
    def from_json(cls, item: dict[str, Any]) -> McpContent:
        kind = str(item.get("type", "text"))
        if kind == "resource" and isinstance(item.get("resource"), dict):
            return cls.from_resource(item["resource"])
        if "type" not in item and ("blob" in item or "uri" in item):
            return cls.from_resource(item)  # resources/read returns bare resource contents
        data = item.get("data")
        return cls(
            type=kind,
            text=str(item.get("text", "") or ""),
            data=base64.b64decode(data) if isinstance(data, str) and data else None,
            mime_type=str(item.get("mimeType", "") or ""),
            uri=str(item.get("uri", "") or ""),
        )

    @classmethod
    def from_resource(cls, res: dict[str, Any]) -> McpContent:
        blob = res.get("blob")
        return cls(
            type="resource",
            text=str(res.get("text", "") or ""),
            data=base64.b64decode(blob) if isinstance(blob, str) and blob else None,
            mime_type=str(res.get("mimeType", "") or ""),
            uri=str(res.get("uri", "") or ""),
        )


_EXT_BY_MIME = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/html": ".html",
    "application/json": ".json",
}


@dataclass
class McpResult:
    content: list[McpContent] = field(default_factory=list)
    is_error: bool = False
    structured: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(c.text for c in self.content if c.text)

    @property
    def json(self) -> Any:
        """``structuredContent`` when present, else the first text item parsed as JSON, else None."""
        if self.structured is not None:
            return self.structured
        for c in self.content:
            if c.text.strip().startswith(("{", "[")):
                try:
                    return json.loads(c.text)
                except ValueError:
                    continue
        return None

    @property
    def first_blob(self) -> McpContent | None:
        for c in self.content:
            if c.data is not None:
                return c
        return None

    def suggested_extension(self) -> str:
        blob = self.first_blob
        if blob is not None:
            if blob.mime_type in _EXT_BY_MIME:
                return _EXT_BY_MIME[blob.mime_type]
            head = blob.data or b""
            if head[:4] == b"%PDF":
                return ".pdf"
            if head[:2] == b"PK":
                return ".docx"
            return ".bin"
        for c in self.content:
            if c.mime_type in _EXT_BY_MIME:
                return _EXT_BY_MIME[c.mime_type]
        return ".md"

    def save(self, path: Path) -> Path:
        """Write the first binary item, else the joined text, to ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = self.first_blob
        if blob is not None and blob.data is not None:
            path.write_bytes(blob.data)
        else:
            path.write_text(self.text, encoding="utf-8", newline="\n")
        return path

    @classmethod
    def from_json(cls, result: dict[str, Any]) -> McpResult:
        items = result.get("content") or result.get("contents") or []
        content = [McpContent.from_json(i) for i in items if isinstance(i, dict)]
        return cls(
            content=content,
            is_error=bool(result.get("isError", False)),
            structured=result.get("structuredContent"),
            raw=result,
        )


@dataclass
class McpTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        props = (
            self.input_schema.get("properties", {}) if isinstance(self.input_schema, dict) else {}
        )
        required = (
            set(self.input_schema.get("required", []))
            if isinstance(self.input_schema, dict)
            else set()
        )
        args = ", ".join(f"{k}{'*' if k in required else ''}" for k in props) or "-"
        first = (self.description or "").strip().splitlines()[0] if self.description else ""
        return f"{self.name}({args})  {first}"


@dataclass
class McpResource:
    uri: str
    name: str = ""
    mime_type: str = ""
    description: str = ""


# ----------------------------------------------------------------------------- transports
class _StdioTransport:
    def __init__(self, cfg: McpServerConfig) -> None:
        if not cfg.command:
            raise McpError("stdio server has no command")
        env = dict(os.environ)
        for k, v in cfg.env.items():
            env[k] = expand_env(v)
        argv = resolve_argv(list(cfg.command))
        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=cfg.cwd or None,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
        except OSError as exc:
            raise McpError(f"cannot start MCP server {argv[0]}: {exc}") from exc
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._stderr.append(f"[stdout] {line[:200]}")
                continue
            if isinstance(msg, dict):
                self._queue.put(msg)
        self._queue.put(None)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-50]

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr[-10:])

    def send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(f"MCP server pipe closed: {exc}; {self.stderr_tail()}") from exc

    def receive(self, timeout: float) -> dict[str, Any]:
        try:
            msg = self._queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise McpError(f"MCP server did not answer within {timeout:.0f}s") from exc
        if msg is None:
            code = self.proc.poll()
            raise McpError(f"MCP server exited (code {code}); {self.stderr_tail()}")
        return msg

    def close(self) -> None:
        with _suppress():
            if self.proc.stdin:
                self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class _HttpTransport:
    """Streamable HTTP: every message is a POST; replies are JSON or an SSE stream."""

    def __init__(self, cfg: McpServerConfig) -> None:
        if not cfg.url:
            raise McpError("http server has no url")
        self.url = cfg.url
        self.headers = {k: expand_env(v) for k, v in cfg.headers.items()}
        self.session_id: str | None = None
        self.timeout = cfg.timeout_s
        self._pending: list[dict[str, Any]] = []

    def _post(self, msg: dict[str, Any], *, expect_reply: bool) -> list[dict[str, Any]]:
        body = json.dumps(msg).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise McpError(f"MCP server {self.url} answered HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise McpError(f"cannot reach MCP server {self.url}: {exc}") from exc
        if not expect_reply or not raw.strip():
            return []
        if "text/event-stream" in ctype:
            return list(_parse_sse(raw.decode("utf-8", "replace")))
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise McpError(f"MCP server returned invalid JSON: {raw[:200]!r}") from exc
        if isinstance(parsed, list):
            return [m for m in parsed if isinstance(m, dict)]
        return [parsed] if isinstance(parsed, dict) else []

    def send(self, msg: dict[str, Any]) -> None:
        replies = self._post(msg, expect_reply="id" in msg)
        self._pending.extend(replies)

    def receive(self, timeout: float) -> dict[str, Any]:
        if not self._pending:
            raise McpError("MCP server returned no message for the request")
        return self._pending.pop(0)

    def close(self) -> None:
        if self.session_id:
            with _suppress():
                req = urllib.request.Request(
                    self.url, headers={"Mcp-Session-Id": self.session_id}, method="DELETE"
                )
                urllib.request.urlopen(req, timeout=5).close()


def _parse_sse(text: str) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line.strip() == "" and data_lines:
            payload = "\n".join(data_lines)
            data_lines = []
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict):
                yield msg


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


# ----------------------------------------------------------------------------- client
class McpClient:
    """One connection to one MCP server; use as a context manager."""

    def __init__(self, name: str, cfg: McpServerConfig) -> None:
        self.name = name
        self.cfg = cfg
        self._transport: _StdioTransport | _HttpTransport | None = None
        self._next_id = 0
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.protocol_version = PROTOCOL_VERSION

    # -- lifecycle
    def connect(self) -> McpClient:
        if self.cfg.transport == "stdio":
            self._transport = _StdioTransport(self.cfg)
        elif self.cfg.transport == "http":
            self._transport = _HttpTransport(self.cfg)
        else:
            raise McpError(f"unknown transport {self.cfg.transport!r} (stdio | http)")
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        self.server_info = dict(result.get("serverInfo") or {})
        self.capabilities = dict(result.get("capabilities") or {})
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.notify("notifications/initialized")
        return self

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def __enter__(self) -> McpClient:
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- JSON-RPC
    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self._transport is not None
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._transport.send(msg)

    def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        assert self._transport is not None, "connect() first"
        self._next_id += 1
        rid = self._next_id
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._transport.send(msg)
        deadline = time.monotonic() + (timeout or self.cfg.timeout_s)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"{self.name}: {method} timed out after {self.cfg.timeout_s:.0f}s")
            reply = self._transport.receive(remaining)
            if reply.get("id") == rid:
                if "error" in reply:
                    err = reply["error"] or {}
                    raise McpError(
                        f"{self.name}: {method} failed: {err.get('message', err)} (code {err.get('code')})"
                    )
                result = reply.get("result")
                return result if isinstance(result, dict) else {"value": result}
            self._handle_unsolicited(reply)

    def _handle_unsolicited(self, msg: dict[str, Any]) -> None:
        """Server-initiated requests and notifications that arrive before our reply."""
        if "method" not in msg:
            return  # a reply to something we are not waiting for
        method = msg["method"]
        if "id" not in msg:
            return  # notification (progress, logging, list_changed): ignored
        assert self._transport is not None
        if method == "ping":
            self._transport.send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        else:
            self._transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {
                        "code": -32601,
                        "message": f"{method} is not supported by this client",
                    },
                }
            )

    # -- MCP operations
    def list_tools(self) -> list[McpTool]:
        tools: list[McpTool] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params)
            for t in result.get("tools") or []:
                if isinstance(t, dict):
                    tools.append(
                        McpTool(
                            name=str(t.get("name", "")),
                            description=str(t.get("description", "") or ""),
                            input_schema=dict(t.get("inputSchema") or {}),
                        )
                    )
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> McpResult:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        out = McpResult.from_json(result)
        if out.is_error:
            raise McpError(f"{self.name}: tool {name} reported an error: {out.text[:500]}")
        return out

    def list_resources(self) -> list[McpResource]:
        resources: list[McpResource] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self.request("resources/list", params)
            for r in result.get("resources") or []:
                if isinstance(r, dict):
                    resources.append(
                        McpResource(
                            uri=str(r.get("uri", "")),
                            name=str(r.get("name", "") or ""),
                            mime_type=str(r.get("mimeType", "") or ""),
                            description=str(r.get("description", "") or ""),
                        )
                    )
            cursor = result.get("nextCursor")
            if not cursor:
                return resources

    def read_resource(self, uri: str) -> McpResult:
        return McpResult.from_json(self.request("resources/read", {"uri": uri}))

    def list_prompts(self) -> list[dict[str, Any]]:
        result = self.request("prompts/list", {})
        return [p for p in result.get("prompts") or [] if isinstance(p, dict)]


def open_client(name: str, cfg: McpServerConfig) -> McpClient:
    return McpClient(name, cfg).connect()


__all__ = [
    "PROTOCOL_VERSION",
    "McpClient",
    "McpContent",
    "McpError",
    "McpResource",
    "McpResult",
    "McpTool",
    "expand_env",
    "open_client",
]

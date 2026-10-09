"""A tiny MCP server for tests: stdio by default, streamable HTTP with ``--http PORT``.

Tools: ``echo(text)``, ``get_pdd(ref)`` (returns the file named by ``FAKE_MCP_PDD_FILE`` as a
resource blob, or markdown text when the file is ``.md``), ``whoami()`` (returns JSON),
``fail()`` (isError). Resources: ``pdd://<ref>``. With ``FAKE_MCP_PING=1`` the server sends a
``ping`` request to the client before answering ``tools/list`` (unsolicited-request path).
``--sse`` makes the HTTP variant answer with ``text/event-stream`` bodies.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

mimetypes.add_type(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"
)
mimetypes.add_type("text/markdown", ".md")

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "get_pdd",
        "description": "Return the PDD document for a reference (attachment blob or page markdown).",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "whoami",
        "description": "Return caller info as JSON.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fail",
        "description": "Always fails.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _pdd_content(ref: str) -> dict[str, Any]:
    file = os.environ.get("FAKE_MCP_PDD_FILE")
    if not file:
        return {"type": "text", "text": f"# PDD {ref}\n\nno FAKE_MCP_PDD_FILE configured\n"}
    p = Path(file)
    data = p.read_bytes()
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if p.suffix.lower() in (".md", ".txt"):
        return {
            "type": "resource",
            "resource": {"uri": f"pdd://{ref}", "mimeType": mime, "text": data.decode("utf-8")},
        }
    return {
        "type": "resource",
        "resource": {
            "uri": f"pdd://{ref}",
            "mimeType": mime,
            "blob": base64.b64encode(data).decode("ascii"),
        },
    }


class Handler:
    def __init__(self, send: Any) -> None:
        self.send = send  # callable(dict) for server-initiated messages (stdio only)
        self.initialized = False

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        rid = msg.get("id")
        if method is None:  # a response to our ping
            return None
        if rid is None:  # notification
            if method == "notifications/initialized":
                self.initialized = True
            return None
        try:
            result = self.dispatch(method, msg.get("params") or {})
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            if os.environ.get("FAKE_MCP_PING") == "1" and self.send is not None:
                self.send({"jsonrpc": "2.0", "id": "srv-ping-1", "method": "ping"})
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                return {"content": [{"type": "text", "text": str(args.get("text", ""))}]}
            if name == "get_pdd":
                return {"content": [_pdd_content(str(args.get("ref", "")))]}
            if name == "whoami":
                payload = {"user": "fake", "ref": args.get("ref")}
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload,
                }
            if name == "fail":
                return {"content": [{"type": "text", "text": "boom"}], "isError": True}
            raise ValueError(f"unknown tool {name}")
        if method == "resources/list":
            return {
                "resources": [
                    {"uri": "pdd://PDD-01", "name": "PDD-01", "mimeType": "application/pdf"}
                ]
            }
        if method == "resources/read":
            uri = str(params.get("uri", ""))
            return {
                "contents": [
                    _pdd_content(uri.split("://", 1)[-1]).get("resource", {"uri": uri, "text": "?"})
                ]
            }
        if method == "prompts/list":
            return {"prompts": []}
        raise ValueError(f"method not found: {method}")


def serve_stdio() -> int:
    out_lock = threading.Lock()

    def send(msg: dict[str, Any]) -> None:
        with out_lock:
            sys.stdout.write(json.dumps(msg) + "\n")
            sys.stdout.flush()

    handler = Handler(send)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handler.handle(msg)
        if reply is not None:
            send(reply)
    return 0


def serve_http(port: int, sse: bool) -> int:
    handler = Handler(None)
    session = "sess-123"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(length).decode("utf-8"))
            if msg.get("method") != "initialize" and self.headers.get("Mcp-Session-Id") != session:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"missing session")
                return
            reply = handler.handle(msg)
            if reply is None:
                self.send_response(202)
                self.end_headers()
                return
            if sse:
                body = f"event: message\ndata: {json.dumps(reply)}\n\n".encode()
                ctype = "text/event-stream"
            else:
                body = json.dumps(reply).encode("utf-8")
                ctype = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if msg.get("method") == "initialize":
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self) -> None:
            self.send_response(204)
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    sys.stdout.write(f"listening {httpd.server_address[1]}\n")
    sys.stdout.flush()
    httpd.serve_forever()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=None)
    ap.add_argument("--sse", action="store_true")
    a = ap.parse_args()
    if a.http is not None:
        return serve_http(a.http, a.sse)
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main())

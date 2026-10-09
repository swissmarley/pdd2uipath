"""MCP client (stdio + streamable HTTP), recipes, the OpenCode importer, Markdown PDDs, CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pdd2uipath.core.config import McpRecipeConfig, McpServerConfig, Settings, save_settings
from pdd2uipath.ingest.approval import get_rule
from pdd2uipath.ingest.parse import parse_document
from pdd2uipath.mcp.client import McpClient, McpError, expand_env
from pdd2uipath.mcp.opencode_import import parse_opencode_mcp, strip_jsonc
from pdd2uipath.mcp.recipes import RecipeError, fetch_pdd, render, resolve_servers, run_recipe
from tests.conftest import ROOT, SAMPLES

FAKE_MCP = ROOT / "fake_mcp" / "server.py"
PDD01 = SAMPLES / "PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx"
MINI_MD = ROOT / "fixtures" / "pdd-mini.md"


def _stdio(env: dict[str, str] | None = None) -> McpServerConfig:
    return McpServerConfig(
        transport="stdio",
        command=[sys.executable, "-I", "-X", "utf8", str(FAKE_MCP)],
        env=env or {},
        timeout_s=30,
    )


@pytest.fixture
def pdd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MCP_PDD_FILE", str(PDD01))
    monkeypatch.delenv("FAKE_MCP_PING", raising=False)


def test_stdio_roundtrip(pdd_env: None) -> None:
    with McpClient("fake", _stdio()) as c:
        assert c.server_info.get("name") == "fake-mcp"
        names = [t.name for t in c.list_tools()]
        assert names == ["echo", "get_pdd", "whoami", "fail"]
        assert c.call_tool("echo", {"text": "hi"}).text == "hi"
        who = c.call_tool("whoami", {"ref": "x"})
        assert who.json == {"user": "fake", "ref": "x"}
        with pytest.raises(McpError):
            c.call_tool("fail")
        pdd = c.call_tool("get_pdd", {"ref": "PDD-01"})
        blob = pdd.first_blob
        assert blob is not None and blob.data == PDD01.read_bytes()
        assert pdd.suggested_extension() == ".docx"
        assert [r.uri for r in c.list_resources()] == ["pdd://PDD-01"]
        assert c.read_resource("pdd://PDD-01").first_blob is not None


def test_server_ping_before_reply_is_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MCP_PING", "1")
    with McpClient("fake", _stdio()) as c:
        assert len(c.list_tools()) == 4


def test_env_expansion_and_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    assert expand_env("Bearer ${MY_TOKEN}") == "Bearer s3cret"
    with pytest.raises(McpError):
        expand_env("${DEFINITELY_MISSING_VAR}")
    with pytest.raises(McpError):
        McpClient(
            "x", McpServerConfig(transport="stdio", command=["no-such-mcp-server-exe"])
        ).connect()


@pytest.fixture(params=["json", "sse"])
def http_server(request: pytest.FixtureRequest, pdd_env: None) -> Iterator[str]:
    args = [sys.executable, "-I", "-X", "utf8", str(FAKE_MCP), "--http", "0"]
    if request.param == "sse":
        args.append("--sse")
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8"
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    port = int(line.split()[-1])
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_http_transport(http_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOKEN", "abc")
    cfg = McpServerConfig(
        transport="http",
        url=http_server,
        headers={"Authorization": "Bearer ${MCP_TOKEN}"},
        timeout_s=20,
    )
    with McpClient("remote", cfg) as c:
        assert c.server_info.get("name") == "fake-mcp"
        assert [t.name for t in c.list_tools()][:2] == ["echo", "get_pdd"]
        assert c.call_tool("echo", {"text": "over http"}).text == "over http"
        assert c.call_tool("get_pdd", {"ref": "p"}).first_blob is not None


def _settings(tmp_path: Path) -> Settings:
    s = Settings(workspace_root="C:\\ws")
    s.mcp.servers["fake"] = _stdio()
    s.mcp.inbox = str(tmp_path / "inbox")
    s.mcp.recipes["fetch-pdd"] = McpRecipeConfig(
        server="fake", tool="get_pdd", args={"ref": "{ref}"}
    )
    s.mcp.recipes["chain"] = McpRecipeConfig(
        server="fake",
        params=["who"],
        steps=[
            {"tool": "whoami", "args": {"ref": "{who}"}},
            {"tool": "echo", "args": {"text": "user={steps.0.json.user} ref={steps.0.json.ref}"}},
        ],
    )
    s.mcp.recipes["save"] = McpRecipeConfig(
        server="fake", tool="get_pdd", args={"ref": "{ref}"}, save_as="{out}"
    )
    return s


def test_render_placeholders() -> None:
    ctx = {"ref": "123", "n": 7, "steps": [{"json": {"a": {"b": "deep"}}, "text": "t"}]}
    assert render("page {ref}", ctx) == "page 123"
    assert render("{n}", ctx) == 7  # a lone placeholder keeps its type
    assert render({"x": ["{steps.0.json.a.b}", "{steps.0.text}"]}, ctx) == {"x": ["deep", "t"]}
    with pytest.raises(RecipeError):
        render("{missing}", ctx)


def test_recipes_and_fetch_pdd(tmp_path: Path, pdd_env: None) -> None:
    s = _settings(tmp_path)
    path = fetch_pdd(s, "PDD-01")
    assert path.parent == tmp_path / "inbox" and path.suffix == ".docx"
    assert path.read_bytes() == PDD01.read_bytes()
    out = tmp_path / "explicit" / "pdd.docx"
    assert fetch_pdd(s, "PDD-01", out=out) == out and out.is_file()
    saved = fetch_pdd(s, "PDD-01", out=tmp_path / "via-save.bin", recipe="save")
    assert saved == tmp_path / "via-save.bin" and saved.is_file()
    chain = run_recipe(s, "chain", {"who": "alice"})
    assert chain.text == "user=fake ref=alice"
    with pytest.raises(RecipeError):
        run_recipe(s, "chain", {})  # missing param
    with pytest.raises(RecipeError):
        run_recipe(s, "nope", {})
    s.mcp.recipes["bad"] = McpRecipeConfig(server="ghost", tool="echo")
    with pytest.raises(RecipeError):
        run_recipe(s, "bad", {})


def test_opencode_import_and_precedence(tmp_path: Path) -> None:
    text = """{
      // OpenCode config with comments
      "$schema": "https://opencode.ai/config.json",
      "mcp": {
        "confluence": {"type": "local", "command": ["npx", "-y", "@acme/confluence-mcp"],
                       "environment": {"CONFLUENCE_TOKEN": "${CONFLUENCE_TOKEN}"}},
        /* remote */
        "github": {"type": "remote", "url": "http://localhost:8081/mcp",
                   "headers": {"Authorization": "Bearer ${GITHUB_MCP_TOKEN}"}, "enabled": true},
        "off": {"type": "local", "command": "x y", "enabled": false}
      }
    }"""
    data = json.loads(strip_jsonc(text))
    servers = parse_opencode_mcp(data)
    assert servers["confluence"].transport == "stdio" and servers["confluence"].command[0] == "npx"
    assert servers["confluence"].env == {"CONFLUENCE_TOKEN": "${CONFLUENCE_TOKEN}"}
    assert servers["github"].transport == "http" and servers["github"].headers[
        "Authorization"
    ].startswith("Bearer ")
    assert servers["off"].enabled is False
    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text(text, encoding="utf-8")
    s = Settings(workspace_root="C:\\ws")
    s.mcp.import_opencode = True
    s.mcp.opencode_config = str(cfg)
    s.mcp.servers["github"] = McpServerConfig(transport="http", url="http://ours/mcp")
    resolved = resolve_servers(s)
    assert set(resolved) == {"confluence", "github"}  # "off" is disabled
    assert resolved["github"].url == "http://ours/mcp"  # our config wins


def test_markdown_pdd_ingest_and_approval() -> None:
    doc = parse_document(MINI_MD)
    assert doc.format == "markdown" and doc.title.startswith("PDD-FIN-MINI-001")
    assert [b.text for b in doc.blocks if b.kind == "heading"][:3] == [
        "PDD-FIN-MINI-001 Supplier Master Check",
        "1 Document Control",
        "1.1 Approval and Sign-off",
    ]
    assert len(doc.tables) == 5
    steps = next(t for t in doc.tables if t.header_has("step id"))
    assert [r[0] for r in steps.rows] == ["S01", "S02", "S03"]
    assert any(b.kind == "list" and b.text.startswith("Volumes") for b in doc.blocks)
    assert doc.lookup_key("Status") is not None
    verdict = get_rule("document-control-and-signoff").check(doc)
    assert verdict.approved, verdict.reasons


def test_cli_mcp_commands(tmp_path: Path, pdd_env: None) -> None:
    from pdd2uipath.cli import app

    s = _settings(tmp_path)
    cfg = tmp_path / "pdd2uipath.toml"
    save_settings(s, cfg)
    runner = CliRunner()
    res = runner.invoke(app, ["--plain", "-c", str(cfg), "mcp", "servers"])
    assert res.exit_code == 0, res.output
    assert "fake" in res.output and "recipe fetch-pdd" in res.output
    res = runner.invoke(app, ["--plain", "-c", str(cfg), "mcp", "tools", "fake"])
    assert res.exit_code == 0 and "get_pdd(ref*)" in res.output
    res = runner.invoke(
        app, ["--plain", "-c", str(cfg), "mcp", "call", "fake", "echo", "--args", '{"text": "cli"}']
    )
    assert res.exit_code == 0 and res.output.strip() == "cli"
    out = tmp_path / "fetched.docx"
    res = runner.invoke(
        app, ["--plain", "-c", str(cfg), "mcp", "fetch-pdd", "PDD-01", "--out", str(out)]
    )
    assert res.exit_code == 0, res.output
    assert out.is_file() and out.read_bytes()[:2] == b"PK"
    res = runner.invoke(app, ["--plain", "-c", str(cfg), "mcp", "tools", "ghost"])
    assert res.exit_code != 0


def test_fetched_markdown_pdd_runs_through_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MCP_PDD_FILE", str(MINI_MD))
    s = _settings(tmp_path)
    path = fetch_pdd(s, "mini")
    assert path.suffix == ".md"
    doc = parse_document(path)
    assert get_rule("document-control-and-signoff").check(doc).approved
    assert os.path.basename(path) == "mini.md"

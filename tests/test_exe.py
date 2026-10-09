"""npm ``.cmd`` shims (copilot, opencode) are expanded to node + loader so no cmd.exe is involved."""

from __future__ import annotations

import os
from pathlib import Path

from pdd2uipath.core.exe import resolve_argv, resolve_cli, resolve_npm_cmd_shim, which_cli
from tests.conftest import windows_only

SHIM = (
    "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\n"
    'CALL :find_dp0\r\nIF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
    '  SET "_prog=node"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n'
    'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
    '"%dp0%\\node_modules\\@github\\copilot\\npm-loader.js" %*\r\n'
)


def _npm_dir(tmp_path: Path) -> Path:
    npm = tmp_path / "npm"
    loader = npm / "node_modules" / "@github" / "copilot" / "npm-loader.js"
    loader.parent.mkdir(parents=True)
    loader.write_text("// loader", encoding="utf-8")
    (npm / "node.exe").write_bytes(b"MZ")
    (npm / "copilot.cmd").write_text(SHIM, encoding="utf-8")
    return npm


def test_shim_expands_to_node_and_loader(tmp_path: Path) -> None:
    npm = _npm_dir(tmp_path)
    assert resolve_npm_cmd_shim(npm / "copilot.cmd") == [
        str(npm / "node.exe"),
        str(npm / "node_modules" / "@github" / "copilot" / "npm-loader.js"),
    ]
    (npm / "copilot.cmd").write_text("@echo not an npm shim", encoding="utf-8")
    assert resolve_npm_cmd_shim(npm / "copilot.cmd") is None


@windows_only
def test_resolve_cli_on_windows_path(tmp_path: Path) -> None:
    npm = _npm_dir(tmp_path)
    path = str(npm) + os.pathsep + os.environ.get("PATH", "")
    assert which_cli("copilot", path=path) is not None
    argv = resolve_argv(["copilot", "-p", "hi"], path=path)
    assert argv[0] == str(npm / "node.exe") and argv[1].endswith("npm-loader.js")
    assert argv[2:] == ["-p", "hi"]
    # a plain executable and an unknown name pass through
    exe = npm / "tool.exe"
    exe.write_bytes(b"MZ")
    assert [x.lower() for x in resolve_cli("tool", path=path)] == [str(exe).lower()]  # PATHEXT case
    assert resolve_cli("definitely-missing-cli", path=str(npm)) == ["definitely-missing-cli"]
    assert resolve_cli(str(exe), path=path) == [str(exe)]

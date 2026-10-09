"""Generate the per-project ``uip`` shim used by the coding agent.

``<project>/.pdd2uipath/bin/uip`` (POSIX) and ``uip.cmd`` (Windows) forward every call
to ``pdd2uipath forward``, which runs it through the active executor. The skill's own
``uip rpa validate/build`` calls therefore work unchanged from WSL.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

SH_TEMPLATE = """#!/bin/sh
# pdd2uipath uip shim: forwards to the active executor (the Windows side).
# Generated for project {project_w}
exec "{python}" -m pdd2uipath forward{config_arg} --project "{project_w}" -- "$@"
"""

CMD_TEMPLATE = (
    "@echo off\r\n"
    "rem pdd2uipath uip shim: forwards to the active executor (the Windows side).\r\n"
    '"{python}" -m pdd2uipath forward{config_arg} --project "{project_w}" -- %*\r\n'
    "exit /b %ERRORLEVEL%\r\n"
)


@dataclass(frozen=True)
class ShimInfo:
    bin_dir: Path
    sh_path: Path
    cmd_path: Path

    def path_env(self, base: str | None = None) -> str:
        base = os.environ.get("PATH", "") if base is None else base
        return str(self.bin_dir) + (os.pathsep + base if base else "")


def install_shim(
    project_dir: Path,
    project_windows: str,
    *,
    config_path: Path | None = None,
    python: str | None = None,
) -> ShimInfo:
    bin_dir = project_dir / ".pdd2uipath" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = python or sys.executable
    config_arg = f' --config "{config_path}"' if config_path else ""
    sh_path = bin_dir / "uip"
    cmd_path = bin_dir / "uip.cmd"
    sh_path.write_text(
        SH_TEMPLATE.format(python=py, config_arg=config_arg, project_w=project_windows),
        encoding="utf-8",
        newline="\n",
    )
    cmd_path.write_text(
        CMD_TEMPLATE.format(python=py, config_arg=config_arg, project_w=project_windows),
        encoding="utf-8",
        newline="",
    )
    with contextlib.suppress(OSError):
        sh_path.chmod(sh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return ShimInfo(bin_dir=bin_dir, sh_path=sh_path, cmd_path=cmd_path)

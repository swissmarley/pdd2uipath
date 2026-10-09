"""`pdd2uipath bridge install|status|stop`: render and manage the Windows worker scripts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pdd2uipath.core.config import Settings
from pdd2uipath.core.paths import to_windows
from pdd2uipath.core.platform import Platform, detect_platform
from pdd2uipath.executors.base import ExecutorState
from pdd2uipath.executors.bridge_protocol import BridgeLayout, heartbeat_age
from pdd2uipath.executors.factory import bridge_layout, discover_windows_uip

TEMPLATES = Path(__file__).resolve().parent.parent / "executors" / "bridge_worker"


def _ps_quote(tok: str) -> str:
    return "'" + tok.replace("'", "''") + "'"


def render_worker_ps1(
    workspace_root: str, uip_command: Sequence[str], *, poll_ms: int = 1000
) -> str:
    text = (TEMPLATES / "worker.ps1").read_text(encoding="utf-8")
    cmd = ", ".join(_ps_quote(t) for t in uip_command) or "'uip.cmd'"
    return (
        text.replace("__WORKSPACE_ROOT__", to_windows(workspace_root).replace("'", "''"))
        .replace("__UIP_COMMAND__", cmd)
        .replace("__POLL_MS__", str(int(poll_ms)))
    )


def render_worker_cmd(workspace_root: str, uip_command: Sequence[str], *, poll_s: int = 1) -> str:
    text = (TEMPLATES / "worker.cmd").read_text(encoding="utf-8")
    cmd = list(uip_command) or ["uip.cmd"]
    pre = " ".join(f'"{t}"' for t in cmd[1:])
    out = (
        text.replace("__WORKSPACE_ROOT__", to_windows(workspace_root))
        .replace("__UIP_EXE__", cmd[0])
        .replace("__UIP_PRE__", pre)
        .replace("__POLL_S__", str(max(1, int(poll_s))))
    )
    return out.replace("\r\n", "\n").replace("\n", "\r\n")


README = """pdd2uipath Windows bridge
=========================

This folder is the job queue between the pipeline (running in WSL) and the Windows
session, where every `uip rpa` command must execute.

Start the worker ONCE per Windows session, in a Windows terminal, and leave it running:

    {cmd}

or, if cmd scripts are blocked but PowerShell is allowed:

    powershell -NoProfile -ExecutionPolicy Bypass -File "{ps1}"

The worker only executes an allowlist of `uip` verbs (rpa templates/init/restore/packages/
activities/validate/build/analyze/analyzer-rules/studio/instances, login status) on paths inside

    {root}

It writes a heartbeat every few seconds; the pipeline shows `bridge * online` while it runs.
Stop it with Ctrl+C or by creating a file named STOP in this folder.
"""


@dataclass(frozen=True)
class BridgeInstall:
    layout: BridgeLayout
    worker_ps1: Path
    worker_cmd: Path
    readme: Path
    uip_command: list[str]

    def start_hint(self) -> str:
        return (
            f"{self.layout.windows('worker.cmd')}\n"
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{self.layout.windows("worker.ps1")}"'
        )


def install_bridge(settings: Settings, platform: Platform | None = None) -> BridgeInstall:
    platform = platform or detect_platform()
    layout = bridge_layout(settings, platform)
    layout.ensure()
    command = list(settings.uip_windows_command) or discover_windows_uip(platform) or ["uip.cmd"]
    poll_ms = max(200, int(settings.bridge.poll_interval_s * 1000))
    layout.worker_ps1.write_text(
        render_worker_ps1(settings.workspace_root, command, poll_ms=poll_ms),
        encoding="utf-8",
        newline="\r\n",
    )
    layout.worker_cmd.write_text(
        render_worker_cmd(
            settings.workspace_root, command, poll_s=max(1, round(settings.bridge.poll_interval_s))
        ),
        encoding="utf-8",
        newline="",
    )
    readme = layout.root / "README.txt"
    readme.write_text(
        README.format(
            cmd=layout.windows("worker.cmd"),
            ps1=layout.windows("worker.ps1"),
            root=to_windows(settings.workspace_root),
        ),
        encoding="utf-8",
    )
    return BridgeInstall(layout, layout.worker_ps1, layout.worker_cmd, readme, command)


def bridge_status(settings: Settings, platform: Platform | None = None) -> ExecutorState:
    layout = bridge_layout(settings, platform)
    age = heartbeat_age(layout)
    if age is None:
        installed = layout.worker_cmd.exists()
        return ExecutorState(
            "wsl-bridge",
            False,
            "worker not running"
            + ("" if installed else " (not installed: run `pdd2uipath bridge install`)"),
        )
    if age > settings.bridge.heartbeat_timeout_s:
        return ExecutorState("wsl-bridge", False, f"heartbeat stale ({age:.0f}s ago)")
    return ExecutorState("wsl-bridge", True, f"online (heartbeat {age:.0f}s ago)")


def stop_bridge(settings: Settings, platform: Platform | None = None) -> Path:
    layout = bridge_layout(settings, platform)
    layout.ensure()
    layout.stop_file.write_text("stop\n", encoding="utf-8")
    return layout.stop_file

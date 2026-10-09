"""Build the configured executor (auto-detected by ``doctor``, overridable in config)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from pdd2uipath.core.config import ExecutorKind, Settings
from pdd2uipath.core.events import EventBus
from pdd2uipath.core.paths import to_brain, to_windows, windows_join
from pdd2uipath.core.platform import Platform, detect_platform
from pdd2uipath.executors.base import Executor, ExecutorError, TracingExecutor
from pdd2uipath.executors.bridge_protocol import BridgeLayout
from pdd2uipath.executors.native import NativeExecutor
from pdd2uipath.executors.wsl_bridge import WslBridgeExecutor
from pdd2uipath.executors.wsl_interop import WslInteropExecutor
from pdd2uipath.executors.wsl_manual import PlainRoundHandler, RoundHandler, WslManualExecutor


def recommend_executor(platform: Platform, *, interop_allowed: bool = True) -> ExecutorKind:
    if platform.is_windows:
        return ExecutorKind.NATIVE
    if platform.is_wsl:
        if platform.interop_enabled and interop_allowed:
            return ExecutorKind.WSL_INTEROP
        return ExecutorKind.WSL_BRIDGE
    return ExecutorKind.WSL_MANUAL


def resolve_npm_shim(shim: Path) -> list[str]:
    """Turn an npm ``uip.cmd`` shim into ``[node.exe, .../@uipath/cli/dist/index.js]``."""
    npm_dir = shim.parent
    entry = npm_dir / "node_modules" / "@uipath" / "cli" / "dist" / "index.js"
    node = npm_dir / "node.exe"
    if not node.exists():
        found = shutil.which("node.exe") or shutil.which("node")
        node = Path(found) if found else node
    if entry.exists() and node.exists():
        return [str(node), str(entry)]
    return [str(shim)]


def discover_windows_uip(platform: Platform | None = None) -> list[str]:
    """Best-effort discovery of the Windows ``uip`` launcher."""
    platform = platform or detect_platform()
    if platform.is_windows:
        found = shutil.which("uip.cmd") or shutil.which("uip")
        if found:
            p = Path(found)
            if p.suffix.lower() == ".cmd":
                return resolve_npm_shim(p)
            return [str(p)]
        appdata = os.environ.get("APPDATA")
        if appdata and (Path(appdata) / "npm" / "uip.cmd").exists():
            return resolve_npm_shim(Path(appdata) / "npm" / "uip.cmd")
        return []
    if platform.is_wsl:
        for user_dir in (
            sorted(Path("/mnt/c/Users").glob("*")) if Path("/mnt/c/Users").exists() else []
        ):
            shim = user_dir / "AppData" / "Roaming" / "npm" / "uip.cmd"
            if shim.exists():
                entry = shim.parent / "node_modules" / "@uipath" / "cli" / "dist" / "index.js"
                node = Path("/mnt/c/Program Files/nodejs/node.exe")
                if entry.exists() and node.exists():
                    return [to_windows(node, platform), to_windows(entry, platform)]
                return [to_windows(shim, platform)]
    return []


def bridge_layout(settings: Settings, platform: Platform | None = None) -> BridgeLayout:
    platform = platform or detect_platform()
    root_w = windows_join(settings.workspace_root, settings.bridge.dirname)
    return BridgeLayout(root=to_brain(root_w, platform), root_windows=root_w)


def build_executor(
    settings: Settings,
    platform: Platform | None = None,
    *,
    bus: EventBus | None = None,
    round_handler: RoundHandler | None = None,
    kind: ExecutorKind | None = None,
    pending_round: str | None = None,
) -> Executor:
    platform = platform or detect_platform()
    kind = kind or settings.executor
    if kind == ExecutorKind.AUTO:
        kind = recommend_executor(platform)
    command = settings.uip_windows_command or discover_windows_uip(platform)
    executor: Executor
    if kind == ExecutorKind.NATIVE:
        if not platform.is_windows:
            raise ExecutorError(
                "the native executor needs a Windows brain; use wsl-interop/bridge/manual"
            )
        executor = NativeExecutor(command or ["uip.cmd"], platform=platform)
    elif kind == ExecutorKind.WSL_INTEROP:
        executor = WslInteropExecutor(command or ["uip.cmd"], platform=platform)
    elif kind == ExecutorKind.WSL_BRIDGE:
        executor = WslBridgeExecutor(
            bridge_layout(settings, platform),
            settings.workspace_root,
            poll_interval_s=settings.bridge.poll_interval_s,
            heartbeat_timeout_s=settings.bridge.heartbeat_timeout_s,
            platform=platform,
        )
    elif kind == ExecutorKind.WSL_MANUAL:
        manual = WslManualExecutor(
            bridge_layout(settings, platform),
            settings.workspace_root,
            command or ["uip.cmd"],
            round_handler or PlainRoundHandler(),
            poll_interval_s=settings.bridge.poll_interval_s,
            platform=platform,
        )
        manual.pending_round_id = pending_round
        executor = manual
    else:  # pragma: no cover - enum is exhaustive
        raise ExecutorError(f"unknown executor kind {kind}")
    return TracingExecutor(executor, bus) if bus is not None else executor


def brain_executor(settings: Settings, platform: Platform | None = None) -> NativeExecutor | None:
    """``uip`` on the brain side (used for ``uip skills`` and probes); ``None`` if absent."""
    platform = platform or detect_platform()
    command = list(settings.uip_brain_command)
    if command and not platform.is_windows and len(command[0]) > 1 and command[0][1] == ":":
        command = []  # a Windows launcher recorded by a Windows doctor run is useless here
    if not command:
        found = shutil.which("uip.cmd" if platform.is_windows else "uip")
        if found:
            p = Path(found)
            command = resolve_npm_shim(p) if p.suffix.lower() == ".cmd" else [str(p)]
    if not command:
        return None
    return NativeExecutor(command, platform=platform, side="brain", translate=False)

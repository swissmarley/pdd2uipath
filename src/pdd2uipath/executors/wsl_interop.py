"""WSL -> Windows ``uip`` through WSL interop (binfmt), when it is enabled and allowed."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from pdd2uipath.core.paths import to_brain, to_windows
from pdd2uipath.core.platform import Platform, detect_platform
from pdd2uipath.executors.base import (
    BaseExecutor,
    ExecutorState,
    UipResult,
    WindowsSideUnavailable,
    parse_envelope,
    translate_argv,
)
from pdd2uipath.executors.native import run_subprocess, subprocess_env

CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"
#: Variables forwarded to the Windows process through ``WSLENV`` (``/u`` = to Windows,
#: ``/p`` = translate as a path).
_WSLENV_FORWARD = {
    "HTTPS_PROXY": "u",
    "HTTP_PROXY": "u",
    "NO_PROXY": "u",
    "NODE_EXTRA_CA_CERTS": "up",
}


class WslInteropExecutor(BaseExecutor):
    """Invoke the Windows ``uip`` from WSL.

    Prefers calling ``node.exe`` with the CLI entry point directly (binfmt executes
    ``.exe`` files); falls back to ``cmd.exe /c uip.cmd`` for a bare npm shim.
    """

    name = "wsl-interop"

    def __init__(self, windows_command: Sequence[str], *, platform: Platform | None = None) -> None:
        if not windows_command:
            raise ValueError("WslInteropExecutor needs the Windows uip command")
        self.windows_command = list(windows_command)
        self.platform = platform or detect_platform()

    def _launcher(self) -> list[str]:
        head = self.windows_command[0]
        if head.lower().endswith((".cmd", ".bat")):
            return [CMD_EXE, "/c", to_windows(head, self.platform), *self.windows_command[1:]]
        if head.lower().endswith(".exe") or ":" in head:
            return [str(to_brain(head, self.platform)), *self.windows_command[1:]]
        return [CMD_EXE, "/c", head, *self.windows_command[1:]]

    def _env(self) -> dict[str, str]:
        env = subprocess_env()
        flags = [f"{k}/{f}" for k, f in _WSLENV_FORWARD.items() if os.environ.get(k)]
        existing = os.environ.get("WSLENV", "")
        env["WSLENV"] = ":".join(x for x in [existing, *flags] if x)
        return env

    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, timeout_s: float = 900.0
    ) -> UipResult:
        if not self.platform.interop_enabled:
            raise WindowsSideUnavailable(
                "WSL interop is disabled (/proc/sys/fs/binfmt_misc/WSLInterop); "
                "use the wsl-bridge or wsl-manual executor"
            )
        args = translate_argv(argv, self.platform)
        cwd_brain = str(to_brain(cwd, self.platform)) if cwd else None
        code, out, err, dur = run_subprocess(
            [*self._launcher(), *args], cwd=cwd_brain, timeout_s=timeout_s, env=self._env()
        )
        return UipResult(
            argv=tuple(args),
            cwd=to_windows(cwd, self.platform) if cwd else None,
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_s=dur,
            side=self.name,
            envelope=parse_envelope(out),
        )

    def status(self) -> ExecutorState:
        if not self.platform.interop_enabled:
            return ExecutorState(self.name, False, "interop disabled")
        launcher = self._launcher()[0]
        return ExecutorState(self.name, Path(launcher).exists(), launcher)

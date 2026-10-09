"""Run ``uip`` as a local subprocess (native Windows, or brain-side ``uip`` in WSL)."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence

from pdd2uipath.core.platform import Platform, detect_platform
from pdd2uipath.executors.base import (
    BaseExecutor,
    ExecutorState,
    UipResult,
    parse_envelope,
    translate_argv,
)

PROXY_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)


def subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Inherit the environment (proxy and CA settings included) and force UTF-8 output."""
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env["NO_COLOR"] = "1"
    if extra:
        env.update(extra)
    return env


def run_subprocess(
    full_argv: Sequence[str],
    *,
    cwd: str | None,
    timeout_s: float,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, float]:
    started = time.monotonic()
    try:
        proc = subprocess.run(  # argument list, never a shell string
            list(full_argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env or subprocess_env(),
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = (
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        err = (
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return (
            124,
            str(out),
            f"{err}\ncommand timed out after {timeout_s:.0f}s",
            time.monotonic() - started,
        )
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}", time.monotonic() - started
    return proc.returncode, proc.stdout, proc.stderr, time.monotonic() - started


class NativeExecutor(BaseExecutor):
    """Call ``uip`` directly. ``command`` is the resolved launcher, e.g.
    ``["C:\\Program Files\\nodejs\\node.exe", "C:\\...\\@uipath\\cli\\dist\\index.js"]``."""

    name = "native"

    def __init__(
        self,
        command: Sequence[str],
        *,
        platform: Platform | None = None,
        side: str = "native",
        translate: bool = True,
        env: dict[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("NativeExecutor needs a resolved uip command")
        self.command = list(command)
        self.platform = platform or detect_platform()
        self.name = side
        self.translate = translate
        self.env = env

    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, timeout_s: float = 900.0
    ) -> UipResult:
        args = translate_argv(argv, self.platform) if self.translate else list(argv)
        code, out, err, dur = run_subprocess(
            [*self.command, *args], cwd=cwd, timeout_s=timeout_s, env=subprocess_env(self.env)
        )
        return UipResult(
            argv=tuple(args),
            cwd=cwd,
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_s=dur,
            side=self.name,
            envelope=parse_envelope(out),
        )

    def status(self) -> ExecutorState:
        exists = (
            os.path.isfile(self.command[0]) or os.path.basename(self.command[0]) == self.command[0]
        )
        return ExecutorState(self.name, exists, " ".join(self.command))

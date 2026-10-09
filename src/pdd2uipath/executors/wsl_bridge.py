"""The ``wsl-bridge`` executor: a file-based job queue served by a Windows worker."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from pdd2uipath.core.paths import to_windows
from pdd2uipath.core.platform import Platform, detect_platform
from pdd2uipath.executors.base import (
    BaseExecutor,
    ExecutorError,
    ExecutorState,
    UipResult,
    WindowsSideUnavailable,
    parse_envelope,
    translate_argv,
)
from pdd2uipath.executors.bridge_protocol import (
    BridgeLayout,
    BridgeResult,
    heartbeat_age,
    make_job,
    read_capture,
    read_result,
    validate_job,
    write_job,
)


class WslBridgeExecutor(BaseExecutor):
    name = "wsl-bridge"

    def __init__(
        self,
        layout: BridgeLayout,
        workspace_root_windows: str,
        *,
        poll_interval_s: float = 1.0,
        heartbeat_timeout_s: float = 15.0,
        platform: Platform | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.layout = layout
        self.workspace_root = to_windows(workspace_root_windows)
        self.poll_interval_s = poll_interval_s
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.platform = platform or detect_platform()
        self._sleep = sleep
        self._clock = clock

    # -- status ---------------------------------------------------------------
    def status(self) -> ExecutorState:
        age = heartbeat_age(self.layout)
        if age is None:
            return ExecutorState(self.name, False, "no heartbeat (worker not started)")
        if age > self.heartbeat_timeout_s:
            return ExecutorState(self.name, False, f"heartbeat stale ({age:.0f}s)")
        return ExecutorState(self.name, True, f"heartbeat {age:.0f}s ago")

    def _offline_message(self) -> str:
        return (
            "The Windows bridge worker is not running. Start it once in your Windows session:\n"
            f"    {self.layout.windows('worker.cmd')}\n"
            f'  or: powershell -NoProfile -ExecutionPolicy Bypass -File "{self.layout.windows("worker.ps1")}"'
        )

    # -- execution ------------------------------------------------------------
    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, timeout_s: float = 900.0
    ) -> UipResult:
        args = translate_argv(argv, self.platform)
        cwd_w = to_windows(cwd, self.platform) if cwd else self.workspace_root
        validate_job(args, cwd_w, self.workspace_root)
        state = self.status()
        if not state.online:
            raise WindowsSideUnavailable(self._offline_message())
        self.layout.ensure()
        from uuid import uuid4

        job = make_job(args, cwd_w, timeout_s=timeout_s, job_id=uuid4().hex[:12])
        write_job(self.layout.jobs, job)
        started = self._clock()
        deadline = started + timeout_s + 30.0
        result: BridgeResult | None = None
        while True:
            result = read_result(self.layout.results, job.id)
            if result is not None:
                break
            if self._clock() > deadline:
                raise ExecutorError(
                    f"bridge job {job.id} timed out after {timeout_s:.0f}s "
                    f"(job file: {self.layout.windows('jobs', job.id + '.json')})"
                )
            age = heartbeat_age(self.layout)
            if age is None or age > self.heartbeat_timeout_s:
                raise WindowsSideUnavailable(
                    f"bridge worker went offline while job {job.id} was pending.\n"
                    + self._offline_message()
                )
            self._sleep(self.poll_interval_s)
        out, err = read_capture(self.layout.results, result)
        if result.status != "ok":
            err = (err + "\n" if err else "") + f"worker {result.status}: {result.reason}"
            code = result.exit_code or 126
        else:
            code = result.exit_code
        return UipResult(
            argv=tuple(args),
            cwd=cwd_w,
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_s=self._clock() - started,
            side=self.name,
            envelope=parse_envelope(out),
        )

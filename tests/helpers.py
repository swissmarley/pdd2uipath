"""Test helpers: a Python stand-in for the Windows side of the bridge."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from pdd2uipath.core.paths import to_windows
from pdd2uipath.executors.base import JobRejected
from pdd2uipath.executors.bridge_protocol import (
    BridgeLayout,
    BridgeResult,
    now_iso,
    read_job,
    touch_heartbeat,
    validate_job,
    write_result,
)


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


class SimulatedWindowsWorker:
    """Plays the Windows worker in-process: polls jobs, validates, runs the fake uip."""

    def __init__(
        self,
        layout: BridgeLayout,
        uip_command: Sequence[str],
        workspace_root: str,
        *,
        poll_s: float = 0.05,
        heartbeat: bool = True,
        local_cwd: Path | None = None,
        keep_jobs: bool = False,
    ) -> None:
        self.layout = layout
        self.uip_command = list(uip_command)
        self.workspace_root = to_windows(workspace_root)
        self.poll_s = poll_s
        self.heartbeat = heartbeat
        self.local_cwd = local_cwd
        self.keep_jobs = keep_jobs
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.served: list[str] = []

    def start(self) -> SimulatedWindowsWorker:
        self.layout.ensure()
        if self.heartbeat:
            touch_heartbeat(self.layout, "sim")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def __enter__(self) -> SimulatedWindowsWorker:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def serve_dir(self, jobs_dir: Path, results_dir: Path) -> int:
        n = 0
        for jf in sorted(jobs_dir.glob("*.json")):
            self.serve_job(jf, results_dir)
            n += 1
        return n

    def serve_job(self, jf: Path, results_dir: Path) -> None:
        jid = jf.stem
        started = now_iso()
        try:
            job = read_job(jf)
            validate_job(job.argv, job.cwd, self.workspace_root)
        except JobRejected as exc:
            write_result(
                results_dir,
                BridgeResult(
                    id=jid,
                    status="rejected",
                    exit_code=126,
                    reason=str(exc),
                    started=started,
                    finished=now_iso(),
                    worker="sim",
                ),
            )
            jf.unlink(missing_ok=True)
            return
        except Exception as exc:  # malformed
            write_result(
                results_dir,
                BridgeResult(
                    id=jid,
                    status="error",
                    exit_code=125,
                    reason=f"malformed job: {exc}",
                    started=started,
                    finished=now_iso(),
                    worker="sim",
                ),
            )
            jf.unlink(missing_ok=True)
            return
        cwd = self.local_cwd or (job.cwd if os.name == "nt" else None)
        proc = subprocess.run(
            [*self.uip_command, *job.argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=job.timeout_s,
        )
        (results_dir / f"{jid}.out").write_text(proc.stdout, encoding="utf-8")
        (results_dir / f"{jid}.err").write_text(proc.stderr, encoding="utf-8")
        write_result(
            results_dir,
            BridgeResult(
                id=jid,
                status="ok",
                exit_code=proc.returncode,
                stdout_file=f"{jid}.out",
                stderr_file=f"{jid}.err",
                started=started,
                finished=now_iso(),
                worker="sim",
            ),
        )
        if not self.keep_jobs:  # manual round scripts leave the job files in place
            jf.unlink(missing_ok=True)
            (jf.parent / f"{jid}.args").unlink(missing_ok=True)
        self.served.append(jid)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.heartbeat:
                touch_heartbeat(self.layout, "sim")
            self.serve_dir(self.layout.jobs, self.layout.results)
            time.sleep(self.poll_s)

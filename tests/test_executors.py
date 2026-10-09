from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from pdd2uipath.core.events import CommandFinished, CommandStarted, EventBus
from pdd2uipath.core.platform import WINDOWS, WSL_INTEROP
from pdd2uipath.executors.base import (
    ExecutorError,
    Job,
    JobRejected,
    TracingExecutor,
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
    touch_heartbeat,
    validate_job,
    write_job,
    write_result,
)
from pdd2uipath.executors.native import NativeExecutor
from pdd2uipath.executors.wsl_bridge import WslBridgeExecutor
from pdd2uipath.executors.wsl_manual import (
    ManualRound,
    PlainRoundHandler,
    RoundAborted,
    RoundPending,
    WslManualExecutor,
)
from tests.conftest import windows_only
from tests.helpers import SimulatedWindowsWorker, read_log

# ---------------------------------------------------------------- envelope / argv


def test_parse_envelope_plain_and_with_log_lines() -> None:
    assert parse_envelope('{"Result": "Success", "Data": {"a": 1}}') == {
        "Result": "Success",
        "Data": {"a": 1},
    }
    text = '[Information] Starting...\n[WARN] something\n{\n  "Result": "Failure",\n  "Message": "x"\n}\n'
    assert parse_envelope(text) == {"Result": "Failure", "Message": "x"}
    assert parse_envelope("not json") is None
    assert parse_envelope("") is None
    assert parse_envelope("[1,2]") is None


def test_uipresult_ok_and_summary() -> None:
    ok = UipResult(
        ("rpa", "build"),
        None,
        0,
        '{"Result":"Success","Code":"Build","Data":{"Success":true}}',
        "",
        1.0,
        "native",
        {"Result": "Success", "Code": "Build", "Data": {"Success": True}},
    )
    assert ok.ok and ok.data == {"Success": True} and ok.summary() == "Success"
    bad = UipResult(
        ("rpa", "build"),
        None,
        1,
        "",
        "boom",
        1.0,
        "native",
        {"Result": "Failure", "Message": "Build failed: x"},
    )
    assert not bad.ok and bad.message == "Build failed: x" and "Failure" in bad.summary()
    crash = UipResult(("rpa",), None, 3, "garbage", "fatal: simulated crash", 0.1, "native", None)
    assert not crash.ok and crash.message == "fatal: simulated crash"


def test_translate_argv_flags_positionals_and_relative_file_path() -> None:
    argv = [
        "rpa",
        "validate",
        "--file-path",
        "Framework/Process.xaml",
        "--project-dir",
        "/mnt/c/ws/p",
        "--output",
        "json",
    ]
    assert translate_argv(argv, WSL_INTEROP) == [
        "rpa",
        "validate",
        "--file-path",
        "Framework/Process.xaml",
        "--project-dir",
        "C:\\ws\\p",
        "--output",
        "json",
    ]
    assert translate_argv(["rpa", "build", "/mnt/c/ws/p", "--output", "json"], WSL_INTEROP) == [
        "rpa",
        "build",
        "C:\\ws\\p",
        "--output",
        "json",
    ]
    assert translate_argv(["rpa", "init", "--location=/mnt/d/x y", "--name", "N"], WSL_INTEROP) == [
        "rpa",
        "init",
        "--location=D:\\x y",
        "--name",
        "N",
    ]
    assert (
        translate_argv(["rpa", "validate", "--file-path", "/mnt/c/ws/p/Main.xaml"], WSL_INTEROP)[3]
        == "C:\\ws\\p\\Main.xaml"
    )
    with pytest.raises(Exception, match="wsl"):
        translate_argv(["rpa", "build", "\\\\wsl$\\Ubuntu\\home\\p"], WSL_INTEROP)


# ---------------------------------------------------------------- native executor


def test_native_executor_runs_fake_uip(fake_uip_command: list[str], fake_uip_log: Path) -> None:
    ex = NativeExecutor(fake_uip_command, platform=WINDOWS, translate=False)
    res = ex.run(["--version"])
    assert res.ok and res.stdout.strip() == "9.9.9-fake"
    res = ex.run(["rpa", "templates", "search", "--query", "REFramework", "--output", "json"])
    assert res.ok and res.result_code == "Success"
    assert res.data[0]["source"] == "Official"
    calls = read_log(fake_uip_log)
    assert calls[-1]["argv"][:3] == ["rpa", "templates", "search"]


def test_native_executor_failure_and_crash(
    fake_uip_command: list[str], fake_uip_log: Path, scenario
) -> None:
    ex = NativeExecutor(fake_uip_command, platform=WINDOWS, translate=False)
    res = ex.run(["rpa", "nope"])
    assert not res.ok and res.exit_code == 1 and res.result_code == "Failure"
    scenario("exit_nonzero")
    res = ex.run(["rpa", "build", "x"])
    assert res.exit_code == 3 and res.envelope is None and "simulated crash" in res.message


def test_native_executor_timeout(
    fake_uip_command: list[str], fake_uip_log: Path, scenario, monkeypatch
) -> None:
    scenario("slow")
    monkeypatch.setenv("FAKE_UIP_SLOW_S", "5")
    ex = NativeExecutor(fake_uip_command, platform=WINDOWS, translate=False)
    res = ex.run(["--version"], timeout_s=0.5)
    assert res.exit_code == 124 and "timed out" in res.stderr


def test_native_executor_missing_command() -> None:
    ex = NativeExecutor(["definitely-not-a-real-binary-xyz"], platform=WINDOWS, translate=False)
    res = ex.run(["--version"])
    assert res.exit_code == 127


def test_tracing_executor_publishes_events(fake_uip_command: list[str], fake_uip_log: Path) -> None:
    bus = EventBus()
    seen: list = []
    bus.subscribe(seen.append)
    ex = TracingExecutor(NativeExecutor(fake_uip_command, platform=WINDOWS, translate=False), bus)
    ex.run(["--version"])
    assert isinstance(seen[0], CommandStarted) and isinstance(seen[1], CommandFinished)
    assert seen[1].ok and seen[1].argv == ["--version"]


# ---------------------------------------------------------------- bridge protocol


def test_validate_job_allowlist_and_boundaries() -> None:
    root = "C:\\ws"
    validate_job(
        ["rpa", "validate", "--file-path", "Main.xaml", "--project-dir", "C:\\ws\\p"],
        "C:\\ws\\p",
        root,
    )
    validate_job(["--version"], "C:\\ws", root)
    validate_job(["login", "status"], "C:\\ws", root)
    with pytest.raises(JobRejected, match="not allowlisted"):
        validate_job(["rpa", "publish", "x"], "C:\\ws", root)
    with pytest.raises(JobRejected, match="not allowlisted"):
        validate_job(["solution", "publish"], "C:\\ws", root)
    with pytest.raises(JobRejected, match="not allowlisted"):
        validate_job(["login"], "C:\\ws", root)
    with pytest.raises(JobRejected, match="outside the workspace"):
        validate_job(["rpa", "build", "D:\\other"], "C:\\ws", root)
    with pytest.raises(JobRejected, match="outside the workspace"):
        validate_job(["rpa", "validate", "--project-dir", "C:\\ws2\\p"], "C:\\ws", root)
    with pytest.raises(JobRejected, match="cwd"):
        validate_job(["rpa", "build", "C:\\ws\\p"], "C:\\elsewhere", root)
    with pytest.raises(JobRejected, match="forbidden"):
        validate_job(["rpa", "validate", "--file-path", "a.xaml & del *"], "C:\\ws", root)
    with pytest.raises(JobRejected, match="empty"):
        validate_job([], "C:\\ws", root)


def test_job_round_trip_and_sidecar(tmp_path: Path) -> None:
    job = make_job(["rpa", "build", "C:\\ws\\p"], "C:\\ws\\p", timeout_s=30, job_id="abc123")
    path = write_job(tmp_path / "jobs", job)
    assert path.name == "abc123.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == "pdd2uipath.bridge.job/1" and data["argv"] == [
        "rpa",
        "build",
        "C:\\ws\\p",
    ]
    args = (tmp_path / "jobs" / "abc123.args").read_bytes().decode("utf-8")
    assert args.split("\r\n")[:4] == ["cwd=C:\\ws\\p", "timeout=30", "arg=rpa", "arg=build"]
    assert read_result(tmp_path / "results", "abc123") is None
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "abc123.out").write_text('{"Result":"Success"}', encoding="utf-8")
    write_result(
        tmp_path / "results",
        BridgeResult(id="abc123", exit_code=0, stdout_file="abc123.out", stderr_file=""),
    )
    res = read_result(tmp_path / "results", "abc123")
    assert res is not None and res.exit_code == 0
    assert read_capture(tmp_path / "results", res) == ('{"Result":"Success"}', "")
    assert not list((tmp_path / "results").glob("*.tmp"))


def test_heartbeat_age(tmp_path: Path) -> None:
    layout = BridgeLayout(tmp_path / "bridge", "C:\\ws\\.pdd2uipath-bridge")
    assert heartbeat_age(layout) is None
    layout.ensure()
    touch_heartbeat(layout, "test")
    age = heartbeat_age(layout)
    assert age is not None and age < 5


def _layout(tmp_path: Path) -> BridgeLayout:
    return BridgeLayout(tmp_path / "bridge", "C:\\ws\\.pdd2uipath-bridge")


def test_bridge_executor_offline_raises(tmp_path: Path) -> None:
    ex = WslBridgeExecutor(_layout(tmp_path), "C:\\ws", poll_interval_s=0.01, platform=WINDOWS)
    assert not ex.status().online
    with pytest.raises(WindowsSideUnavailable, match=r"worker\.cmd"):
        ex.run(["--version"], cwd="C:\\ws")


def test_bridge_executor_round_trip(
    tmp_path: Path, fake_uip_command: list[str], fake_uip_log: Path
) -> None:
    layout = _layout(tmp_path)
    with SimulatedWindowsWorker(layout, fake_uip_command, "C:\\ws", local_cwd=tmp_path):
        ex = WslBridgeExecutor(
            layout, "C:\\ws", poll_interval_s=0.02, heartbeat_timeout_s=5, platform=WINDOWS
        )
        assert ex.status().online
        res = ex.run(
            ["rpa", "templates", "search", "--query", "REFramework", "--output", "json"],
            cwd="C:\\ws\\run",
        )
        assert (
            res.ok
            and res.side == "wsl-bridge"
            and res.data[0]["packageId"] == "UiPath.Template.REFramework"
        )
        missing = ex.run(["rpa", "build", r"C:\ws\p", "--output", "json"], cwd=r"C:\ws\p")
        assert missing.exit_code == 1 and not missing.ok  # fake: project not found
    assert not list(layout.jobs.glob("*.json"))
    assert read_log(fake_uip_log)[0]["argv"][:3] == ["rpa", "templates", "search"]


def test_bridge_executor_rejects_before_submit(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.ensure()
    touch_heartbeat(layout, "x")
    ex = WslBridgeExecutor(layout, "C:\\ws", poll_interval_s=0.01, platform=WINDOWS)
    with pytest.raises(JobRejected):
        ex.run(["rpa", "publish"], cwd="C:\\ws")
    assert not list(layout.jobs.glob("*"))


def test_bridge_worker_side_rejection_and_malformed(
    tmp_path: Path, fake_uip_command: list[str], fake_uip_log: Path
) -> None:
    layout = _layout(tmp_path)
    worker = SimulatedWindowsWorker(layout, fake_uip_command, "C:\\ws", local_cwd=tmp_path).start()
    try:
        # a job that bypassed python-side validation (hand-crafted) is rejected by the worker
        job = make_job(["rpa", "publish"], "C:\\ws", timeout_s=5, job_id="evil01")
        write_job(layout.jobs, job)
        (layout.jobs / "broken1.json").write_text("{not json", encoding="utf-8")
        deadline = time.time() + 5
        while time.time() < deadline and (
            read_result(layout.results, "evil01") is None
            or read_result(layout.results, "broken1") is None
        ):
            time.sleep(0.02)
        evil = read_result(layout.results, "evil01")
        broken = read_result(layout.results, "broken1")
        assert evil is not None and evil.status == "rejected" and "not allowlisted" in evil.reason
        assert broken is not None and broken.status == "error" and "malformed" in broken.reason
    finally:
        worker.stop()


def test_bridge_executor_heartbeat_timeout_while_pending(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.ensure()
    touch_heartbeat(layout, "x")
    clock = [0.0]
    ex = WslBridgeExecutor(
        layout,
        "C:\\ws",
        poll_interval_s=0.0,
        heartbeat_timeout_s=1.0,
        platform=WINDOWS,
        sleep=lambda s: None,
        clock=lambda: clock[0],
    )
    # make the heartbeat look old by rewinding its mtime
    old = time.time() - 100
    os.utime(layout.heartbeat, (old, old))
    with pytest.raises(WindowsSideUnavailable):
        ex.run(["--version"], cwd="C:\\ws")


def test_bridge_executor_job_timeout(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.ensure()
    clock = [0.0]

    def fake_sleep(_s: float) -> None:
        clock[0] += 10
        touch_heartbeat(layout, "x")

    touch_heartbeat(layout, "x")
    ex = WslBridgeExecutor(
        layout,
        "C:\\ws",
        poll_interval_s=0.0,
        heartbeat_timeout_s=100,
        platform=WINDOWS,
        sleep=fake_sleep,
        clock=lambda: clock[0],
    )
    with pytest.raises(ExecutorError, match="timed out"):
        ex.run(["--version"], cwd="C:\\ws", timeout_s=5)


# ---------------------------------------------------------------- manual rounds


class ScriptedHandler:
    """Plays the user: on announce, run the round via a simulated worker (or the real run.cmd)."""

    def __init__(self, worker: SimulatedWindowsWorker, *, use_cmd: bool = False) -> None:
        self.worker = worker
        self.use_cmd = use_cmd
        self.announced: list[ManualRound] = []
        self.ticks = 0
        self.done: list[str] = []

    def announce(self, round_: ManualRound) -> None:
        self.announced.append(round_)
        if self.use_cmd:
            subprocess.run(
                ["cmd.exe", "/c", str(round_.dir / "run.cmd")],
                check=True,
                capture_output=True,
                timeout=120,
            )
        else:
            threading.Thread(
                target=self.worker.serve_dir,
                args=(round_.jobs_dir, round_.results_dir),
                daemon=True,
            ).start()

    def tick(self, round_: ManualRound, waited_s: float) -> None:
        self.ticks += 1

    def finished(self, round_: ManualRound) -> None:
        self.done.append(round_.id)


def test_manual_executor_round_trip(
    tmp_path: Path, fake_uip_command: list[str], fake_uip_log: Path
) -> None:
    layout = _layout(tmp_path)
    worker = SimulatedWindowsWorker(
        layout, fake_uip_command, "C:\\ws", local_cwd=tmp_path, heartbeat=False
    )
    handler = ScriptedHandler(worker)
    ex = WslManualExecutor(
        layout, "C:\\ws", fake_uip_command, handler, poll_interval_s=0.02, platform=WINDOWS
    )
    jobs = [
        Job(["rpa", "templates", "search", "--query", "REFramework", "--output", "json"], "C:\\ws"),
        Job(["--version"], "C:\\ws"),
    ]
    results = ex.run_batch(jobs)
    assert [r.ok for r in results] == [True, True]
    assert results[1].stdout.strip() == "9.9.9-fake"
    rnd = handler.announced[0]
    assert (
        (rnd.dir / "run.cmd").exists()
        and (rnd.dir / "run.ps1").exists()
        and (rnd.dir / "README.txt").exists()
    )
    assert "run.cmd" in rnd.instructions() and rnd.instructions().count("uip ") >= 2
    assert handler.done == [rnd.id]
    cmd_text = (rnd.dir / "run.cmd").read_text(encoding="utf-8")
    assert '"rpa" "templates" "search"' in cmd_text and "results\\j01.json" in cmd_text


def test_manual_executor_pending_and_abort(tmp_path: Path, fake_uip_command: list[str]) -> None:
    layout = _layout(tmp_path)
    out: list[str] = []
    ex = WslManualExecutor(
        layout,
        "C:\\ws",
        fake_uip_command,
        PlainRoundHandler(wait=False, echo=out.append),
        poll_interval_s=0.0,
        platform=WINDOWS,
    )
    with pytest.raises(RoundPending) as info:
        ex.run(["--version"], cwd="C:\\ws")
    assert info.value.round.pending_ids() == ["j01"]
    assert any("run.cmd" in line for line in out)
    # resume: load the round and collect after someone produced results
    rnd = ex.load_round(info.value.round.id)
    worker = SimulatedWindowsWorker(
        layout, fake_uip_command, "C:\\ws", local_cwd=tmp_path, heartbeat=False
    )
    worker.serve_dir(rnd.jobs_dir, rnd.results_dir)
    res = ex.collect(rnd)
    assert res[0].ok and res[0].stdout.strip() == "9.9.9-fake"

    class Abort:
        def announce(self, r: ManualRound) -> None: ...

        def tick(self, r: ManualRound, w: float) -> None:
            raise RoundAborted("user cancelled")

        def finished(self, r: ManualRound) -> None: ...

    ex2 = WslManualExecutor(
        layout, "C:\\ws", fake_uip_command, Abort(), poll_interval_s=0.0, platform=WINDOWS
    )
    with pytest.raises(RoundAborted):
        ex2.run(["--version"], cwd="C:\\ws")


@windows_only
def test_manual_round_cmd_script_really_runs(
    tmp_path: Path, fake_uip_command: list[str], fake_uip_log: Path, win_root: str
) -> None:
    layout = BridgeLayout(
        Path(win_root) / ".pdd2uipath-bridge", str(Path(win_root) / ".pdd2uipath-bridge")
    )
    worker = SimulatedWindowsWorker(layout, fake_uip_command, win_root, heartbeat=False)
    handler = ScriptedHandler(worker, use_cmd=True)
    ex = WslManualExecutor(
        layout, win_root, fake_uip_command, handler, poll_interval_s=0.05, platform=WINDOWS
    )
    results = ex.run_batch(
        [
            Job(
                ["rpa", "templates", "search", "--query", "REFramework", "--output", "json"],
                win_root,
            ),
            Job(["--version"], win_root),
        ]
    )
    assert results[0].ok and results[0].data[0]["source"] == "Official"
    assert results[1].stdout.strip() == "9.9.9-fake"


# ---------------------------------------------------------------- real worker scripts (Windows)


def _install_worker(bridge_dir: Path, root: str, uip_command: list[str]) -> tuple[Path, Path]:
    from pdd2uipath.doctor.bridge import render_worker_cmd, render_worker_ps1

    ps1 = bridge_dir / "worker.ps1"
    cmd = bridge_dir / "worker.cmd"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    ps1.write_text(render_worker_ps1(root, uip_command, poll_ms=200), encoding="utf-8")
    cmd.write_text(render_worker_cmd(root, uip_command, poll_s=1), encoding="utf-8")
    return ps1, cmd


def _run_with_real_worker(
    start_cmd: list[str], layout: BridgeLayout, root: str, fake_uip_log: Path
) -> None:
    proc = subprocess.Popen(
        start_cmd, cwd=str(layout.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        ex = WslBridgeExecutor(
            layout, root, poll_interval_s=0.1, heartbeat_timeout_s=20, platform=WINDOWS
        )
        deadline = time.time() + 20
        while time.time() < deadline and not ex.status().online:
            time.sleep(0.2)
        assert ex.status().online, "worker never produced a heartbeat"
        res = ex.run(
            ["rpa", "templates", "search", "--query", "REFramework", "--output", "json"],
            cwd=root,
            timeout_s=60,
        )
        assert res.ok, res.stderr
        assert res.data[0]["source"] == "Official"
        res2 = ex.run(["--version"], cwd=root, timeout_s=60)
        assert res2.stdout.strip() == "9.9.9-fake"
        assert read_log(fake_uip_log)[0]["argv"][:3] == ["rpa", "templates", "search"]
    finally:
        layout.stop_file.write_text("stop", encoding="utf-8")
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


@windows_only
def test_real_worker_ps1(
    tmp_path: Path, fake_uip_command: list[str], fake_uip_log: Path, win_root: str
) -> None:
    bridge = Path(win_root) / ".pdd2uipath-bridge"
    ps1, _ = _install_worker(bridge, win_root, fake_uip_command)
    layout = BridgeLayout(bridge, str(bridge))
    _run_with_real_worker(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
        layout,
        win_root,
        fake_uip_log,
    )


@windows_only
def test_real_worker_cmd(
    tmp_path: Path, fake_uip_command: list[str], fake_uip_log: Path, win_root: str
) -> None:
    bridge = Path(win_root) / ".pdd2uipath-bridge"
    _, cmd = _install_worker(bridge, win_root, fake_uip_command)
    layout = BridgeLayout(bridge, str(bridge))
    _run_with_real_worker(["cmd.exe", "/c", str(cmd)], layout, win_root, fake_uip_log)


@windows_only
def test_real_worker_cmd_rejects_outside_workspace(
    tmp_path: Path, fake_uip_command: list[str], fake_uip_log: Path, win_root: str
) -> None:
    bridge = Path(win_root) / ".pdd2uipath-bridge"
    _, cmd = _install_worker(bridge, win_root, fake_uip_command)
    layout = BridgeLayout(bridge, str(bridge))
    layout.ensure()
    proc = subprocess.Popen(
        ["cmd.exe", "/c", str(cmd)],
        cwd=str(layout.root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        write_job(
            layout.jobs, make_job(["rpa", "publish", "x"], win_root, timeout_s=10, job_id="bad01")
        )
        write_job(
            layout.jobs,
            make_job(
                ["rpa", "build", "C:\\elsewhere"], "C:\\elsewhere", timeout_s=10, job_id="bad02"
            ),
        )
        deadline = time.time() + 20
        while time.time() < deadline and (
            read_result(layout.results, "bad01") is None
            or read_result(layout.results, "bad02") is None
        ):
            time.sleep(0.2)
        r1, r2 = read_result(layout.results, "bad01"), read_result(layout.results, "bad02")
        assert r1 is not None and r1.status == "rejected" and "not allowlisted" in r1.reason
        assert r2 is not None and r2.status == "rejected" and "outside" in r2.reason
        assert not fake_uip_log.exists() or read_log(fake_uip_log) == []
    finally:
        layout.stop_file.write_text("stop", encoding="utf-8")
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

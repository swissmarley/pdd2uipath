"""wsl-manual: a pending round is reused on the next run instead of generating a new script."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdd2uipath.core.config import ExecutorKind, Settings
from pdd2uipath.core.platform import WINDOWS
from pdd2uipath.core.runner import Pipeline, PlainGateHandler
from pdd2uipath.executors.base import Job, TracingExecutor
from pdd2uipath.executors.bridge_protocol import BridgeLayout
from pdd2uipath.executors.wsl_manual import PlainRoundHandler, RoundPending, WslManualExecutor
from tests.conftest import FIXTURES, ROOT, windows_only
from tests.helpers import SimulatedWindowsWorker


def test_pending_round_is_reused_after_the_user_ran_it(
    tmp_path: Path, fake_uip_command: list[str], fake_uip_log: Path
) -> None:
    layout = BridgeLayout(tmp_path / "bridge", "C:\\ws\\.pdd2uipath-bridge")
    jobs = [
        Job(["--version"], "C:\\ws"),
        Job(["rpa", "templates", "search", "--query", "REFramework", "--output", "json"], "C:\\ws"),
    ]
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
        ex.run_batch(jobs)
    rid = info.value.round.id
    assert len(list(layout.rounds.iterdir())) == 1
    # the user runs the script (simulated), then the stage is re-run with the pending round id
    worker = SimulatedWindowsWorker(
        layout, fake_uip_command, "C:\\ws", local_cwd=tmp_path, heartbeat=False
    )
    worker.serve_dir(layout.rounds / rid / "jobs", layout.rounds / rid / "results")
    ex2 = WslManualExecutor(
        layout,
        "C:\\ws",
        fake_uip_command,
        PlainRoundHandler(wait=False, echo=out.append),
        poll_interval_s=0.0,
        platform=WINDOWS,
    )
    ex2.pending_round_id = rid
    results = ex2.run_batch(jobs)
    assert [r.ok for r in results] == [True, True] and results[0].stdout.strip() == "9.9.9-fake"
    assert len(list(layout.rounds.iterdir())) == 1, "no second round was generated"
    # a different job list does not reuse the round
    ex3 = WslManualExecutor(
        layout,
        "C:\\ws",
        fake_uip_command,
        PlainRoundHandler(wait=False, echo=out.append),
        poll_interval_s=0.0,
        platform=WINDOWS,
    )
    ex3.pending_round_id = rid
    with pytest.raises(RoundPending):
        ex3.run_batch([Job(["--version"], "C:\\ws")])
    assert len(list(layout.rounds.iterdir())) == 2


def _serve_pending_round(p: Pipeline, fake_uip_command: list[str], win_root: str) -> str:
    """Pretend the user ran ``run.cmd`` of the round the pipeline is waiting for."""
    ex = p.executor()
    inner = ex.inner if isinstance(ex, TracingExecutor) else ex
    assert isinstance(inner, WslManualExecutor) and inner.last_round is not None
    assert p.state.pending_round == inner.last_round.id
    worker = SimulatedWindowsWorker(inner.layout, fake_uip_command, win_root, heartbeat=False)
    worker.serve_dir(inner.last_round.jobs_dir, inner.last_round.results_dir)
    return inner.last_round.id


@windows_only
def test_pipeline_no_wait_resumes_each_round_exactly_once(
    pdd01: Path, win_root: str, fake_uip_command: list[str], fake_uip_log: Path
) -> None:
    """``run-all --no-wait`` / ``resume-round --no-wait`` until done: every generated round
    script is run once and reused on resume - including the scaffold round that creates the
    project on Windows (resuming must collect it, not refuse because ``project.json`` exists).
    """
    settings = Settings(
        workspace_root=win_root,
        executor=ExecutorKind.WSL_MANUAL,
        uip_windows_command=fake_uip_command,
    )
    settings.extraction.backend = "fixture"
    settings.extraction.fixture_dir = str(FIXTURES / "extraction")
    settings.agent.backend = "fake"
    settings.agent.fake_script = str(ROOT / "fake_agent" / "fake_agent.py")
    gates = PlainGateHandler(approver="Tester", wait_for_rounds=False)
    p = Pipeline(settings, pdd=pdd01, platform=WINDOWS, gates=gates)
    rounds: list[str] = []
    waiting_stages: list[str] = []
    with pytest.raises(RoundPending):
        p.run_all()
    for _ in range(8):
        statuses = p.state.statuses()
        waiting_stages.append(next(s for s, st in statuses.items() if st == "waiting"))
        rounds.append(_serve_pending_round(p, fake_uip_command, win_root))
        p = Pipeline(settings, run_dir=p.paths.run_dir, platform=WINDOWS, gates=gates)
        try:
            p.resume_round()
        except RoundPending:
            continue
        break
    assert p.state.statuses()["report"] == "done" and p.state.pending_round is None
    assert p.last_gate is not None and p.last_gate.verdict.passed
    assert waiting_stages[:2] == ["design", "scaffold"] and "verify" in waiting_stages
    ex = p.executor()
    inner = ex.inner if isinstance(ex, TracingExecutor) else ex
    assert isinstance(inner, WslManualExecutor)
    # one script per round, none regenerated
    assert sorted(d.name for d in inner.layout.rounds.iterdir()) == sorted(rounds)

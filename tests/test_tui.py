"""Textual pilot and snapshot tests for the TUI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import DataTable, Input

from pdd2uipath.core.config import ExecutorKind, Settings
from pdd2uipath.core.platform import WINDOWS
from pdd2uipath.executors.base import TracingExecutor
from pdd2uipath.executors.wsl_manual import WslManualExecutor
from pdd2uipath.tui.app import Pdd2UipathApp
from pdd2uipath.tui.screens import PlanApprovalScreen, WindowsRoundScreen
from pdd2uipath.tui.widgets import Inspector, PipelineSidebar, StageItem
from tests.conftest import FIXTURES, ROOT, windows_only
from tests.helpers import SimulatedWindowsWorker

FAKE_AGENT = ROOT / "fake_agent" / "fake_agent.py"


def _settings(
    win_root: str, fake_uip_command: list[str], executor: ExecutorKind = ExecutorKind.NATIVE
) -> Settings:
    s = Settings(workspace_root=win_root, executor=executor, uip_windows_command=fake_uip_command)
    s.extraction.backend = "fixture"
    s.extraction.fixture_dir = str(FIXTURES / "extraction")
    s.agent.backend = "fake"
    s.agent.fake_script = str(FAKE_AGENT)
    s.bridge.poll_interval_s = 0.05
    return s


async def _wait(pilot: Any, predicate: Any, timeout: float = 180.0, detail: Any = None) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await pilot.pause(0.1)
        if predicate():
            return
    extra = f": {detail()}" if detail is not None else ""
    raise AssertionError(f"timed out waiting for the TUI condition{extra}")


async def _approve(pilot: Any, screen: Any, name: str = "Tester") -> None:
    """Type the approver and click Approve once the modal is laid out.

    A click before the first layout lands on an empty region and the gate waits forever.
    """
    await _wait(pilot, lambda: screen.query_one("#approve").region.width > 0, timeout=60)
    screen.query_one("#approver", Input).value = name
    await pilot.click("#approve")


def _statuses(app: Pdd2UipathApp) -> dict[str, str]:
    return {item.stage: item.status for item in app.query(StageItem)}


async def test_app_composes_without_pipeline_at_80x24() -> None:
    app = Pdd2UipathApp(Settings(workspace_root="C:\\ws"))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert len(app.query(StageItem)) == 9
        assert app.query_one(Inspector).display
        await pilot.press("i")
        assert not app.query_one(Inspector).display
        await pilot.press("t")
        assert app.theme == "pdd2uipath-light"


@windows_only
async def test_tui_full_run_with_plan_approval(
    pdd01: Path, win_root: str, fake_uip_command: list[str], fake_uip_log: Path
) -> None:
    app = Pdd2UipathApp(_settings(win_root, fake_uip_command), pdd=pdd01, platform=WINDOWS)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_run_all()
        await _wait(pilot, lambda: isinstance(app.screen, PlanApprovalScreen))
        assert _statuses(app)["design"] == "done"
        await _approve(pilot, app.screen)
        assert app.pipeline is not None
        await _wait(
            pilot,
            lambda: not app.busy and app.pipeline.state.statuses()["report"] == "done",
            timeout=300,
            detail=lambda: f"busy={app.busy} last_error={app.last_error!r} {_statuses(app)}",
        )
        st = _statuses(app)
        assert all(
            st[s] == "done"
            for s in (
                "ingest",
                "extract",
                "design",
                "plan",
                "scaffold",
                "author",
                "verify",
                "report",
            )
        ), st
        await pilot.pause(0.5)
        insp = app.query_one(Inspector)
        assert insp.query_one("#validation-table", DataTable).row_count >= 1
        assert insp.query_one("#trace-table", DataTable).row_count >= 12
        # stage filter hides cards of other stages
        app.query_one(PipelineSidebar).post_message(PipelineSidebar.StageSelected("verify"))
        await pilot.pause()


@windows_only
async def test_tui_manual_windows_rounds(
    pdd01: Path, win_root: str, fake_uip_command: list[str], fake_uip_log: Path
) -> None:
    settings = _settings(win_root, fake_uip_command, ExecutorKind.WSL_MANUAL)
    app = Pdd2UipathApp(settings, pdd=pdd01, platform=WINDOWS)
    rounds_seen: list[str] = []
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_run_all()
        assert app.pipeline is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 300
        while loop.time() < deadline:
            await pilot.pause(0.1)
            screen = app.screen
            if isinstance(screen, WindowsRoundScreen) and screen.round_id not in rounds_seen:
                rounds_seen.append(screen.round_id)
                assert "run.cmd" in screen.command
                ex = app.pipeline.executor()
                inner = ex.inner if isinstance(ex, TracingExecutor) else ex
                assert isinstance(inner, WslManualExecutor) and inner.last_round is not None
                worker = SimulatedWindowsWorker(
                    inner.layout, fake_uip_command, win_root, heartbeat=False
                )
                worker.serve_dir(inner.last_round.jobs_dir, inner.last_round.results_dir)
            elif isinstance(screen, PlanApprovalScreen):
                await _approve(pilot, screen)
                await pilot.pause(0.2)
            elif not app.busy and app.pipeline.state.statuses()["report"] == "done":
                break
        assert app.pipeline.state.statuses()["report"] == "done", app.pipeline.state.statuses()
        assert len(rounds_seen) >= 3  # design probes, scaffold, verify gate


@windows_only
async def test_tui_resumes_at_the_gate_after_quit(
    pdd01: Path, win_root: str, fake_uip_command: list[str], fake_uip_log: Path
) -> None:
    settings = _settings(win_root, fake_uip_command)
    app = Pdd2UipathApp(settings, pdd=pdd01, platform=WINDOWS)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_run_all()
        await _wait(pilot, lambda: isinstance(app.screen, PlanApprovalScreen))
        run_dir = app.pipeline.paths.run_dir  # type: ignore[union-attr]
        await pilot.press("escape")
        await pilot.pause(0.2)
        app.exit()
    app2 = Pdd2UipathApp(settings, run_dir=run_dir, platform=WINDOWS)
    async with app2.run_test(size=(120, 40)) as pilot:
        await _wait(pilot, lambda: isinstance(app2.screen, PlanApprovalScreen), timeout=20)
        assert _statuses(app2)["plan"] == "waiting" and _statuses(app2)["design"] == "done"
        await pilot.press("escape")


def test_snapshot_empty_main(snap_compare: Any) -> None:
    app = Pdd2UipathApp(Settings(workspace_root="C:\\ws"))
    assert snap_compare(app, terminal_size=(100, 30))


def test_snapshot_plan_modal(snap_compare: Any) -> None:
    app = Pdd2UipathApp(Settings(workspace_root="C:\\ws"))

    async def run_before(pilot: Any) -> None:
        await pilot.pause()
        pilot.app.push_screen(
            PlanApprovalScreen(
                "# Demo plan\n\n**Execution autonomy:** autonomous\n\n## Task T0\n- [ ] install",
                "abc123def456",
                ["UiPath.Mail.Activities 2.12.10"],
            )
        )
        await pilot.pause()

    assert snap_compare(app, terminal_size=(100, 30), run_before=run_before)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])

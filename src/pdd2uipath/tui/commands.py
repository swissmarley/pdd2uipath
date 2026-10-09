"""Command palette provider (ctrl+p)."""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import TYPE_CHECKING

from textual.command import DiscoveryHit, Hit, Hits, Provider

from pdd2uipath.core.config import ExecutorKind
from pdd2uipath.core.state import STAGES

if TYPE_CHECKING:
    from pdd2uipath.tui.app import Pdd2UipathApp


class Pdd2UipathCommands(Provider):
    def _commands(self) -> Iterable[tuple[str, str, object]]:
        app = self.app
        assert hasattr(app, "action_run_all")
        tui: Pdd2UipathApp = app  # type: ignore[assignment]
        yield "Open PDD…", "Pick a PDD file and create/resume its run", tui.action_open_pdd
        yield "Run all stages", "Run the pipeline; pauses at the plan gate", tui.action_run_all
        for s in STAGES[1:]:
            yield (
                f"Run stage: {s}",
                f"Run or resume the '{s}' stage",
                partial(tui.action_run_stage, s),
            )
        yield "Run doctor", "Preflight checks and executor recommendation", tui.action_doctor
        yield (
            "Slash commands help",
            "/help: every /command of the prompt bar and the settings keys",
            partial(tui.slash.run, "/help"),
        )
        yield "Approve plan", "Open the plan approval modal", tui.action_approve_plan
        for kind in ExecutorKind:
            if kind != ExecutorKind.AUTO:
                yield (
                    f"Switch executor: {kind.value}",
                    "Change where uip runs (config, this session)",
                    partial(tui.action_switch_executor, kind),
                )
        yield (
            "Install bridge worker",
            "Generate worker.ps1/worker.cmd under the workspace",
            tui.action_install_bridge,
        )
        yield (
            "Restart bridge worker",
            "Ask the running worker to stop; start it again in Windows",
            tui.action_restart_bridge,
        )
        yield (
            "Open in Studio",
            "uip rpa studio start on the generated project",
            tui.action_open_studio,
        )
        yield "Open report", "Show report.md in the inspector", tui.action_open_report
        yield "Toggle inspector", "Show/hide the right pane", tui.action_toggle_inspector
        yield "Toggle theme", "Dark / light", tui.action_toggle_theme
        yield (
            "Quit",
            "Quit (state is persisted; reopening resumes at the exact gate)",
            tui.action_quit,
        )

    async def discover(self) -> Hits:
        for name, help_text, cmd in self._commands():
            yield DiscoveryHit(name, cmd, help=help_text)  # type: ignore[arg-type]

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, cmd in self._commands():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), cmd, help=help_text)  # type: ignore[arg-type]

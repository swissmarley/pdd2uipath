"""The Textual TUI: a thin, keyboard-first view over the pipeline's event bus."""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.suggester import SuggestFromList
from textual.widgets import DataTable, Footer

from pdd2uipath.core.config import (
    ExecutorKind,
    Settings,
    load_settings,
    save_settings,
    settings_source_path,
)
from pdd2uipath.core.events import (
    EventBus,
    ExecutorStatus,
    RepairRound,
    RunStateChanged,
    StageFinished,
    StageStarted,
)
from pdd2uipath.core.exe import resolve_argv
from pdd2uipath.core.runner import Pipeline, PipelineStop
from pdd2uipath.core.state import STAGES
from pdd2uipath.executors.base import ExecutorError
from pdd2uipath.executors.wsl_manual import RoundAborted, RoundPending
from pdd2uipath.tui.commands import Pdd2UipathCommands
from pdd2uipath.tui.gates import TuiGateHandler
from pdd2uipath.tui.screens import DetailScreen, PickPddScreen, PlanApprovalScreen
from pdd2uipath.tui.slash import SlashCommands, apply_setting, config_target_path, get_setting
from pdd2uipath.tui.theme import DARK, LIGHT, no_color, pick_glyphs
from pdd2uipath.tui.widgets import Inspector, PipelineSidebar, PromptBar, SessionLog, StatusHeader


class Pdd2UipathApp(App[None]):
    TITLE = "pdd2uipath"
    AUTO_FOCUS = "#stages"
    CSS_PATH = "app.tcss"
    COMMANDS = App.COMMANDS | {Pdd2UipathCommands}
    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Commands"),
        Binding("r", "run_all", "Run all"),
        Binding("a", "approve_plan", "Approve plan"),
        Binding("i", "toggle_inspector", "Inspector"),
        Binding("t", "toggle_theme", "Theme"),
        Binding("o", "open_pdd", "Open PDD"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        settings: Settings,
        *,
        pdd: Path | None = None,
        run_dir: Path | None = None,
        pipeline: Pipeline | None = None,
        platform: Any = None,
        config_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.config_path = config_path or settings_source_path(settings)
        self.pdd = pdd
        self.run_dir = run_dir
        self.platform = platform
        self.pipeline: Pipeline | None = pipeline
        self.bus = pipeline.bus if pipeline is not None else EventBus()
        self.glyphs = pick_glyphs(settings.ui.glyphs)
        self.started_at: float | None = None
        self.busy = False
        self.closing = False  # set on exit so blocked gate waits in the worker thread return
        self.last_error = ""
        self._unsubscribe: Callable[[], None] | None = None
        self.slash = SlashCommands(self)

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield StatusHeader(self.glyphs)
        with Horizontal(id="body"):
            yield PipelineSidebar(self.glyphs)
            yield SessionLog(self.glyphs)
            yield Inspector(id="inspector")
        yield PromptBar()
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(DARK)
        self.register_theme(LIGHT)
        self.theme = "pdd2uipath-light" if self.settings.ui.theme == "light" else "pdd2uipath-dark"
        if no_color():
            self.ansi_color = True
        self._unsubscribe = self.bus.subscribe(self._on_bus_event)
        self.query_one(PromptBar).suggester = SuggestFromList(
            self.slash.names(), case_sensitive=False
        )
        self.set_interval(1.0, self._tick)
        self.set_interval(3.0, self._refresh_executor_status)
        if self.pipeline is None and (self.pdd or self.run_dir):
            self._open_pipeline(self.pdd, self.run_dir)
        elif self.pipeline is not None:
            self._after_open()
        else:
            self.notify("press o to open a PDD, ctrl+p for commands", timeout=5)

    # ------------------------------------------------------------------ pipeline wiring
    def _open_pipeline(self, pdd: Path | None, run_dir: Path | None) -> None:
        try:
            self.pipeline = Pipeline(
                self.settings,
                pdd=pdd,
                run_dir=run_dir,
                bus=self.bus,
                platform=self.platform,
                gates=TuiGateHandler(self),
            )
        except Exception as exc:  # show, never crash the UI
            self.notify(f"cannot open: {exc}", severity="error", timeout=8)
            return
        self._after_open()

    def _after_open(self) -> None:
        assert self.pipeline is not None
        if self.pipeline.gates.__class__.__name__ != "TuiGateHandler":
            self.pipeline.gates = TuiGateHandler(self)
        header = self.query_one(StatusHeader)
        header.pdd = self.pipeline.pdd.name
        header.project = (
            self.pipeline.state.project_dir_windows or self.pipeline.paths.run_dir_windows
        )
        header.executor = self.pipeline.executor_kind.value
        header.agent = self.settings.agent.backend
        header.model = self.settings.agent.model or ""
        header.refresh_line()
        self.query_one(PipelineSidebar).set_statuses(
            self.pipeline.state.statuses(),
            self.pipeline.state.repair_round,
            self.settings.repair.max_rounds,
        )
        self._refresh_inspector()
        gate = self.pipeline.state.current_gate()
        if gate == "plan_approval":
            self.notify("resumed at the plan approval gate (a to approve)", timeout=6)
            self.call_later(self.action_approve_plan)
        elif gate == "windows_round":
            self.notify(
                "a Windows round is pending: run its script, then run the stage again",
                severity="warning",
                timeout=8,
            )

    def _on_bus_event(self, ev: Any) -> None:
        # called on the worker thread
        self.call_from_thread(self._apply_event, ev)

    def _apply_event(self, ev: Any) -> None:
        self.query_one(SessionLog).handle(ev)
        sidebar = self.query_one(PipelineSidebar)
        if isinstance(ev, StageStarted):
            sidebar.set_running(ev.stage)
        elif isinstance(ev, RunStateChanged):
            sidebar.set_statuses(ev.stages, ev.repair_round, self.settings.repair.max_rounds)
        elif isinstance(ev, RepairRound):
            sidebar.set_statuses(
                self.pipeline.state.statuses() if self.pipeline else {}, ev.round, ev.max_rounds
            )
        elif isinstance(ev, StageFinished):
            self._refresh_inspector()
            if self.pipeline is not None:
                header = self.query_one(StatusHeader)
                header.project = (
                    self.pipeline.state.project_dir_windows or self.pipeline.paths.run_dir_windows
                )
                header.refresh_line()
        elif isinstance(ev, ExecutorStatus):
            header = self.query_one(StatusHeader)
            header.online = ev.online
            header.refresh_line()

    def _tick(self) -> None:
        if self.busy and self.started_at is not None:
            header = self.query_one(StatusHeader)
            header.elapsed = f"{time.monotonic() - self.started_at:.0f}s"
            header.refresh_line()
            self.query_one(PipelineSidebar).tick()

    @work(thread=True, exclusive=False, group="status")
    def _refresh_executor_status(self) -> None:
        if self.pipeline is None:
            return
        try:
            state = self.pipeline.executor().status()
        except ExecutorError:
            return
        self.bus.publish(
            ExecutorStatus(executor=state.kind, online=state.online, detail=state.detail)
        )

    def _refresh_inspector(self) -> None:
        if self.pipeline is None:
            return
        p = self.pipeline
        insp = self.query_one(Inspector)
        try:
            if p.paths.document.is_file():
                verdict = (
                    json.loads(p.paths.verdict.read_text(encoding="utf-8"))
                    if p.paths.verdict.is_file()
                    else None
                )
                insp.load_document(
                    json.loads(p.paths.document.read_text(encoding="utf-8")), verdict
                )
            if p.paths.spec.is_file():
                insp.load_spec(json.loads(p.paths.spec.read_text(encoding="utf-8")))
            if p.paths.design.is_file():
                insp.load_design(json.loads(p.paths.design.read_text(encoding="utf-8")))
            if p.paths.plan.is_file():
                text = p.paths.plan.read_text(encoding="utf-8")
                _ok, reason = p.approval_status()
                approved = p.paths.run_dir / "approval-plan.md"
                insp.load_plan(
                    text,
                    reason,
                    approved.read_text(encoding="utf-8") if approved.is_file() else None,
                )
            gate = p.paths.gate_json(p.state.repair_round)
            if gate.is_file():
                data = json.loads(gate.read_text(encoding="utf-8"))
                insp.load_validation(data.get("verdict", {}).get("diagnostics", []))
            insp.load_traceability(p.paths.traceability)
        except (OSError, ValueError) as exc:
            self.notify(f"inspector: {exc}", severity="warning")

    # ------------------------------------------------------------------ actions
    def _start(self, fn: Any, label: str) -> None:
        if self.pipeline is None:
            self.notify("open a PDD first (o)", severity="warning")
            return
        if self.busy:
            self.notify("a stage is already running", severity="warning")
            return
        self.busy = True
        self.started_at = time.monotonic()
        self._run_pipeline(fn, label)

    def _notify_safe(self, message: str, **kwargs: Any) -> None:
        if self.closing:
            return
        with contextlib.suppress(RuntimeError):  # app already shut down
            self.call_from_thread(self.notify, message, **kwargs)

    @work(thread=True, exclusive=True, group="pipeline")
    def _run_pipeline(self, fn: Any, label: str) -> None:
        try:
            fn()
            self._notify_safe(f"{label}: done", timeout=4)
        except PipelineStop as stop:
            self.last_error = str(stop)
            sev = "warning" if stop.status in ("waiting", "stopped") else "error"
            self._notify_safe(str(stop)[:300], severity=sev, timeout=8)
        except RoundPending as pending:
            self._notify_safe(
                f"Windows round pending: {pending.round.run_cmd_windows}",
                severity="warning",
                timeout=10,
            )
        except RoundAborted:
            self._notify_safe("Windows round aborted", severity="warning")
        except Exception as exc:  # keep the UI alive
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._notify_safe(self.last_error[:300], severity="error", timeout=10)
        finally:
            self.busy = False
            if not self.closing:
                with contextlib.suppress(RuntimeError):
                    self.call_from_thread(self._refresh_inspector)

    def action_run_all(self) -> None:
        if self.pipeline is not None:
            self._start(self.pipeline.run_all, "run-all")

    def action_run_stage(self, stage: str) -> None:
        if self.pipeline is None:
            return
        p = self.pipeline
        fns = {
            "ingest": lambda: p.ingest(force=True),
            "extract": lambda: p.extract(force=True),
            "design": lambda: p.design_stage(force=True),
            "plan": lambda: p.plan(force=True),
            "scaffold": p.scaffold,
            "author": p.author,
            "verify": p.verify,
            "report": p.report,
        }
        if stage in fns:
            self._start(fns[stage], stage)

    def action_doctor(self) -> None:
        from pdd2uipath.doctor.checks import run_doctor

        def _do() -> None:
            report = run_doctor(
                self.settings, platform=self.platform, probe_studio=False, probe_network=False
            )
            body = "\n".join(
                f"[{c.status}] {c.title}: {c.detail}"
                + (f"\n      fix: {c.fix}" if c.fix and c.status != "ok" else "")
                for c in report.checks
            )
            self.call_from_thread(self.push_screen, DetailScreen("doctor", body))

        self.run_worker(_do, thread=True, group="doctor")

    def action_approve_plan(self) -> None:
        if self.pipeline is None or not self.pipeline.paths.plan.is_file():
            self.notify("no plan to approve yet", severity="warning")
            return
        p = self.pipeline
        text = p.paths.plan.read_text(encoding="utf-8")
        from pdd2uipath.core.state import sha256_file

        installs = [f"{x.id} {x.version}" for x in p.design.installs()] if p.design else []
        _ok, reason = p.approval_status()

        def _done(name: str | None) -> None:
            if name:
                self.approve_plan_as(name)

        self.push_screen(
            PlanApprovalScreen(text, sha256_file(p.paths.plan), installs, reason), _done
        )

    def action_switch_executor(self, kind: ExecutorKind) -> None:
        self.settings.executor = kind
        if self.pipeline is not None:
            self.pipeline._executor = None
            self.query_one(StatusHeader).executor = kind.value
            self.query_one(StatusHeader).refresh_line()
        self.notify(f"executor: {kind.value} (session only; /executor {kind.value} saves it)")

    def action_install_bridge(self) -> None:
        from pdd2uipath.doctor.bridge import install_bridge

        try:
            info = install_bridge(self.settings, self.platform)
        except Exception as exc:
            self.notify(f"bridge install failed: {exc}", severity="error")
            return
        self.push_screen(
            DetailScreen(
                "Bridge installed",
                f"Start once per Windows session:\n\n{info.start_hint()}\n\nworkspace: {info.layout.root_windows}",
            )
        )

    def action_restart_bridge(self) -> None:
        from pdd2uipath.doctor.bridge import install_bridge, stop_bridge

        try:
            stop_bridge(self.settings, self.platform)
            info = install_bridge(self.settings, self.platform)
        except Exception as exc:
            self.notify(f"bridge restart failed: {exc}", severity="error")
            return
        self.push_screen(
            DetailScreen(
                "Bridge worker stopping",
                f"STOP file written. Start it again in Windows:\n\n{info.start_hint()}",
            )
        )

    def action_open_studio(self) -> None:
        if self.pipeline is None:
            return
        p = self.pipeline

        def _do() -> None:
            try:
                msg = p.open_studio()
            except (PipelineStop, ExecutorError) as exc:
                msg = str(exc)
            self.call_from_thread(self.notify, msg[:300], timeout=8)

        self.run_worker(_do, thread=True, group="studio")

    def action_open_report(self) -> None:
        if self.pipeline is None or not self.pipeline.paths.report.is_file():
            self.notify("no report yet", severity="warning")
            return
        self.push_screen(
            DetailScreen("report.md", self.pipeline.paths.report.read_text(encoding="utf-8"))
        )

    def on_unmount(self) -> None:
        self.closing = True

    async def action_quit(self) -> None:
        self.closing = True
        self.exit()

    def action_toggle_inspector(self) -> None:
        insp = self.query_one(Inspector)
        insp.display = not insp.display

    def action_toggle_theme(self) -> None:
        self.theme = "pdd2uipath-light" if self.theme == "pdd2uipath-dark" else "pdd2uipath-dark"

    def action_open_pdd(self) -> None:
        candidates = [
            str(p)
            for d in (Path.cwd(), Path.cwd() / "samples")
            if d.is_dir()
            for p in sorted(d.glob("*.docx")) + sorted(d.glob("*.pdf"))
        ]

        def _picked(value: str | None) -> None:
            if value:
                self._open_pipeline(Path(value), None)

        self.push_screen(PickPddScreen(candidates), _picked)

    # ------------------------------------------------------------------ slash command support
    def show_detail(self, title: str, body: str) -> None:
        self.push_screen(DetailScreen(title, body))

    def open_pdd_path(self, path: Path) -> None:
        self._open_pipeline(path, None)

    def start_pipeline(self, fn: Any, label: str) -> None:
        self._start(fn, label)

    def approve_plan_as(self, name: str) -> None:
        """Record the approval and continue the run when nothing else is waiting."""
        p = self.pipeline
        if p is None:
            return
        try:
            p.approve_plan(name)
        except Exception as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"plan approved by {name}")
        self._refresh_inspector()
        if p.state.current_gate() is None and p.state.stage("scaffold").status == "pending":
            self._start(p.run_all, "run-all")

    def config_target(self) -> Path:
        return config_target_path(self.settings, self.config_path)

    def save_config(self) -> Path:
        target = self.config_target()
        save_settings(self.settings, target)
        self.config_path = target
        return target

    def update_setting(self, key: str, value: str, *, quiet: bool = False) -> None:
        """Validate, apply to the session (pipeline caches reset), persist, show."""
        new = apply_setting(self.settings, key, value)
        self.settings = new
        if self.pipeline is not None:
            self.pipeline.settings = new
            self.pipeline._executor = None
            self.pipeline._agent = None
            self.pipeline._commands = None
        self._refresh_header()
        path = self.save_config()
        if not quiet:
            self.notify(f"{key} = {get_setting(new, key)!r} (saved to {path.name})", timeout=6)

    def _refresh_header(self) -> None:
        header = self.query_one(StatusHeader)
        header.agent = self.settings.agent.backend
        header.model = self.settings.agent.model or ""
        header.executor = (
            self.pipeline.executor_kind.value if self.pipeline else self.settings.executor.value
        )
        header.refresh_line()

    def run_streaming(self, argv: list[str], *, label: str, hint: str = "") -> None:
        """Run an external CLI (e.g. copilot login) and stream its lines into the session log."""
        if hint:
            self.notify(hint, timeout=10)

        def _do() -> None:
            self.bus.log(f"{label}: {' '.join(argv)}")
            try:
                proc = subprocess.Popen(
                    resolve_argv(argv),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                )
            except OSError as exc:
                self.bus.log(f"{label}: cannot start: {exc}", level="error")
                return
            assert proc.stdout is not None
            for line in proc.stdout:
                if line.strip():
                    self.bus.log(f"{label}: {line.rstrip()}")
            code = proc.wait()
            self.bus.log(f"{label}: exit {code}", level="error" if code else "info")
            self._notify_safe(f"{label}: exit {code}", severity="error" if code else "information")

        self.run_worker(_do, thread=True, group="external")

    def run_capture(self, argv: list[str], *, title: str) -> None:
        """Run an external CLI and show its output in the detail view."""

        def _do() -> None:
            try:
                proc = subprocess.run(
                    resolve_argv(argv),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                    shell=False,
                )
                body = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
            except (OSError, subprocess.TimeoutExpired) as exc:
                body = str(exc)
            if not self.closing:
                with contextlib.suppress(RuntimeError):
                    self.call_from_thread(
                        self.push_screen, DetailScreen(title, body.strip() or "(no output)")
                    )

        self.run_worker(_do, thread=True, group="external")

    def clear_session_log(self) -> None:
        self.query_one(SessionLog).remove_children()

    # ------------------------------------------------------------------ events from widgets
    def on_pipeline_sidebar_stage_selected(self, msg: PipelineSidebar.StageSelected) -> None:
        self.query_one(SessionLog).set_filter(msg.stage)

    def on_input_submitted(self, event: Any) -> None:
        if getattr(event.input, "id", "") != "prompt-bar":
            return
        text = event.value
        event.input.value = ""
        if self.slash.run(text):
            return
        if not text.strip():
            return
        if self.pipeline is None:
            self.notify(
                "open a PDD first (/open or o); /help lists the commands", severity="warning"
            )
            return
        self.pipeline.extra_instruction = text
        self.notify("instruction queued for the next authoring/repair round")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "validation-table":
            insp = self.query_one(Inspector)
            idx = event.cursor_row
            if 0 <= idx < len(insp.validation_rows):
                d = insp.validation_rows[idx]
                self.push_screen(
                    DetailScreen(d.get("file", "") or "(project)", json.dumps(d, indent=2))
                )

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.data_table.id == "validation-table":
            self.query_one(Inspector).sort_validation(event.column_index)


def run_tui(
    config: Path | None = None, pdd: Path | None = None, run_dir: Path | None = None
) -> None:
    settings = load_settings(config)
    app = Pdd2UipathApp(settings, pdd=pdd, run_dir=run_dir, config_path=config)
    app.run()


__all__ = ["STAGES", "Pdd2UipathApp", "run_tui"]

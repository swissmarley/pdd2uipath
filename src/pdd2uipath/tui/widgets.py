"""Widgets: status header, pipeline sidebar, session stream, inspector, prompt bar."""

from __future__ import annotations

import contextlib
import csv
import difflib
import json
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import (
    Collapsible,
    DataTable,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from pdd2uipath.core.events import (
    AgentMessage,
    AgentToolResult,
    AgentToolUse,
    CommandFinished,
    CommandStarted,
    GateResolved,
    GateWaiting,
    LogEvent,
    RepairRound,
    StageFinished,
    StageStarted,
)
from pdd2uipath.core.state import STAGES
from pdd2uipath.tui.theme import Glyphs


class StatusHeader(Static):
    """Product name, PDD, project, executor pill, agent, elapsed."""

    DEFAULT_CSS = """
    StatusHeader { height: 1; background: $panel; color: $foreground; padding: 0 1; }
    """

    def __init__(self, glyphs: Glyphs) -> None:
        super().__init__("")
        self.glyphs = glyphs
        self.pdd = "-"
        self.project = "-"
        self.executor = "-"
        self.online: bool | None = None
        self.agent = "-"
        self.elapsed = ""
        self.model = ""

    def refresh_line(self) -> None:
        g = self.glyphs
        pill = ""
        if self.executor != "-":
            if self.online is None:
                pill = self.executor
            else:
                pill = f"{self.executor} {g.online if self.online else g.offline} {'online' if self.online else 'offline'}"
        if self.executor == "wsl-manual":
            pill = f"manual {g.waiting}"
        t = Text()
        t.append("pdd2uipath", style="bold")
        t.append(f"  PDD {self.pdd}", style="")
        t.append(f"  project {self.project}")
        t.append(f"  [{pill}]", style="bold" if self.online else "")
        t.append(f"  agent {self.agent}{(' ' + self.model) if self.model else ''}")
        if self.elapsed:
            t.append(f"  {self.elapsed}", style="dim")
        self.update(t)


class StageItem(ListItem):
    def __init__(self, stage: str, glyphs: Glyphs) -> None:
        super().__init__(Label(f"{glyphs.pending} {stage}", id=f"lbl-{stage}"))
        self.stage = stage
        self.status = "pending"
        self.glyphs = glyphs
        self.spin = 0

    def set_status(self, status: str, extra: str = "") -> None:
        self.status = status
        self.query_one(Label).update(
            f"{self.glyphs.for_status(status)} {self.stage}{(' ' + extra) if extra else ''}"
        )
        self.set_class(status == "running", "running")
        self.set_class(status == "failed", "failed")
        self.set_class(status in ("waiting", "stopped"), "waiting")

    def tick(self) -> None:
        if self.status == "running":
            self.spin = (self.spin + 1) % len(self.glyphs.spinner)
            self.query_one(Label).update(f"{self.glyphs.spinner[self.spin]} {self.stage}")


class PipelineSidebar(Vertical):
    """The 9 stages with status glyphs plus the repair-round counter."""

    DEFAULT_CSS = """
    PipelineSidebar { width: 26; border-right: tall $panel; padding: 0; }
    PipelineSidebar > Label.title { padding: 0 1; color: $primary; text-style: bold; }
    PipelineSidebar ListView { height: auto; }
    PipelineSidebar StageItem.running Label { color: $accent; }
    PipelineSidebar StageItem.failed Label { color: $error; }
    PipelineSidebar StageItem.waiting Label { color: $warning; }
    PipelineSidebar #repair { padding: 0 1; color: $secondary; }
    """

    class StageSelected(Message):
        def __init__(self, stage: str | None) -> None:
            super().__init__()
            self.stage = stage

    def __init__(self, glyphs: Glyphs) -> None:
        super().__init__()
        self.glyphs = glyphs

    def compose(self) -> ComposeResult:
        yield Label("Pipeline", classes="title")
        yield ListView(*[StageItem(s, self.glyphs) for s in STAGES], id="stages")
        yield Label("repair round 0", id="repair")

    def set_statuses(
        self, statuses: dict[str, str], repair_round: int = 0, max_rounds: int = 2
    ) -> None:
        for item in self.query(StageItem):
            item.set_status(statuses.get(item.stage, "pending"))
        self.query_one("#repair", Label).update(f"repair round {repair_round}/{max_rounds}")

    def set_running(self, stage: str) -> None:
        for item in self.query(StageItem):
            if item.stage == stage:
                item.set_status("running")

    def tick(self) -> None:
        for item in self.query(StageItem):
            item.tick()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        self.post_message(self.StageSelected(item.stage if isinstance(item, StageItem) else None))


class Card(Vertical):
    """One entry of the session stream, tagged with the stage it belongs to."""

    DEFAULT_CSS = """
    Card { height: auto; margin: 0 0 1 0; padding: 0 1; }
    Card.agent { border-left: thick $primary; }
    Card.tool { border-left: thick $secondary; }
    Card.error { border-left: thick $error; background: $panel; }
    Card.stage { color: $primary; text-style: bold; }
    Card.log { color: $foreground-darken-1; }
    Card.gate { border-left: thick $warning; }
    Card.system { color: $foreground-darken-2; }
    """

    def __init__(self, stage: str, kind: str, *children: Any) -> None:
        super().__init__(*children, classes=f"{kind} stage-{stage}")
        self.stage = stage


class SessionLog(VerticalScroll):
    """Chronological stream of event cards (opencode-style transcript)."""

    DEFAULT_CSS = """
    SessionLog { padding: 0 1; }
    SessionLog Collapsible { padding: 0; margin: 0; }
    SessionLog Collapsible > Contents { padding: 0 1; }
    """

    def __init__(self, glyphs: Glyphs) -> None:
        super().__init__()
        self.glyphs = glyphs
        self.current_stage = "doctor"
        self.filter: str | None = None
        self._pending_cmds: dict[str, CommandStarted] = {}
        self._partial: Static | None = None
        self._partial_text = ""

    def _add(self, card: Card) -> None:
        if self.filter is not None and card.stage != self.filter:
            card.display = False
        self.mount(card)
        self.scroll_end(animate=False)

    def set_filter(self, stage: str | None) -> None:
        self.filter = stage
        for card in self.query(Card):
            card.display = stage is None or card.stage == stage

    def handle(self, ev: Any) -> None:
        g = self.glyphs
        if isinstance(ev, StageStarted):
            self.current_stage = ev.stage
            self._partial = None
            self._add(Card(ev.stage, "stage", Static(f"── {ev.stage} ──")))
        elif isinstance(ev, StageFinished):
            mark = {
                "done": g.ok,
                "skipped": g.skipped,
                "waiting": g.waiting,
                "stopped": g.stopped,
                "failed": g.fail,
            }[ev.status]
            kind = "error" if ev.status == "failed" else "stage"
            self._add(
                Card(
                    ev.stage,
                    kind,
                    Static(
                        f"{mark} {ev.stage} {ev.status} {ev.detail[:300]} ({ev.duration_s:.1f}s)"
                    ),
                )
            )
        elif isinstance(ev, CommandStarted):
            self._pending_cmds[ev.command_id] = ev
        elif isinstance(ev, CommandFinished):
            self._pending_cmds.pop(ev.command_id, None)
            title = f"uip {' '.join(ev.argv[:5])}{' …' if len(ev.argv) > 5 else ''} {g.ok if ev.ok else g.fail} {ev.duration_s:.1f}s [{ev.side}]"
            body = ev.stdout.strip() or ev.stderr.strip() or "(no output)"
            with contextlib.suppress(ValueError):
                body = json.dumps(json.loads(body), indent=2)
            self._add(
                Card(
                    self.current_stage,
                    "tool" if ev.ok else "error",
                    Collapsible(Static(body[-6000:]), title=title, collapsed=True),
                )
            )
        elif isinstance(ev, AgentMessage):
            if ev.role in ("system",):
                self._add(Card(self.current_stage, "system", Static(ev.text[:500])))
                return
            if ev.final or ev.role == "result":
                self._partial = None
                self._add(Card(self.current_stage, "agent", Markdown(ev.text[-8000:])))
            else:
                if self._partial is None:
                    self._partial_text = ""
                    self._partial = Static("")
                    self._add(Card(self.current_stage, "agent", self._partial))
                self._partial_text += ("\n" if self._partial_text else "") + ev.text
                self._partial.update(self._partial_text[-4000:])
                self.scroll_end(animate=False)
        elif isinstance(ev, AgentToolUse):
            self._partial = None
            self._add(Card(self.current_stage, "tool", Static(f"{ev.tool}: {ev.summary[:200]}")))
        elif isinstance(ev, AgentToolResult):
            self._add(
                Card(
                    self.current_stage,
                    "error" if ev.is_error else "tool",
                    Static(f"{g.ok if not ev.is_error else g.fail} {ev.tool}: {ev.summary[:200]}"),
                )
            )
        elif isinstance(ev, LogEvent):
            kind = "error" if ev.level == "error" else "log"
            self._add(Card(self.current_stage, kind, Static(ev.message[:600])))
        elif isinstance(ev, GateWaiting):
            self._add(
                Card(
                    self.current_stage,
                    "gate",
                    Static(f"{g.waiting} waiting: {ev.gate.replace('_', ' ')}"),
                )
            )
        elif isinstance(ev, GateResolved):
            self._add(
                Card(
                    self.current_stage,
                    "gate",
                    Static(f"{g.ok} {ev.gate.replace('_', ' ')}: {ev.outcome}"),
                )
            )
        elif isinstance(ev, RepairRound):
            self._add(
                Card(
                    self.current_stage,
                    "gate",
                    Static(f"repair round {ev.round}/{ev.max_rounds} ({ev.errors} error(s))"),
                )
            )


class Inspector(TabbedContent):
    """Right-hand contextual tabs: PDD, Spec, Design, Plan, Validation, Traceability."""

    DEFAULT_CSS = """
    Inspector { width: 56; border-left: tall $panel; }
    Inspector DataTable { height: 1fr; }
    Inspector #spec-detail, Inspector #pdd-verdict { height: auto; max-height: 8; padding: 0 1; color: $accent; }
    """

    def compose(self) -> ComposeResult:
        with TabPane("PDD", id="tab-pdd"):
            yield Static("", id="pdd-verdict")
            yield Tree("PDD", id="pdd-tree")
        with TabPane("Spec", id="tab-spec"):
            yield Tree("ProcessSpec", id="spec-tree")
            yield Static("", id="spec-detail")
        with TabPane("Design", id="tab-design"):
            yield VerticalScroll(Markdown("", id="design-md"))
        with TabPane("Plan", id="tab-plan"):
            yield VerticalScroll(
                Static("", id="plan-status"), Markdown("", id="plan-md"), Static("", id="plan-diff")
            )
        with TabPane("Validation", id="tab-validation"):
            yield DataTable(id="validation-table", cursor_type="row")
        with TabPane("Traceability", id="tab-trace"):
            yield DataTable(id="trace-table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#validation-table", DataTable).add_columns(
            "file", "severity", "category", "message"
        )
        self.query_one("#trace-table", DataTable).add_columns(
            "step", "ref", "file", "activity", "status"
        )
        self.validation_rows: list[dict[str, Any]] = []
        self._loaded: dict[str, str] = {}

    def _unchanged(self, section: str, payload: Any) -> bool:
        """True when ``payload`` equals what ``section`` already shows (re-rendering Markdown
        and trees is the expensive part of a stage transition)."""
        key = json.dumps(payload, sort_keys=True, default=str)
        if self._loaded.get(section) == key:
            return True
        self._loaded[section] = key
        return False

    # -- loaders ---------------------------------------------------------
    def load_document(self, doc: dict[str, Any], verdict: dict[str, Any] | None) -> None:
        if self._unchanged("document", [doc, verdict]):
            return
        tree = self.query_one("#pdd-tree", Tree)
        tree.clear()
        tree.root.set_label(doc.get("title", "PDD"))
        stack: list[tuple[int, Any]] = [(0, tree.root)]
        for b in doc.get("blocks", []):
            if b["kind"] == "heading":
                level = max(1, int(b.get("level", 1)))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                parent = stack[-1][1] if stack else tree.root
                node = parent.add(f"{b['text']}  [{b['ref']}]", expand=level <= 1)
                stack.append((level, node))
            elif b["kind"] == "table":
                (stack[-1][1] if stack else tree.root).add_leaf(f"table {b['ref']}")
        tree.root.expand()
        if verdict:
            lines = [
                ("APPROVED" if verdict.get("approved") else "NOT APPROVED")
                + f" ({verdict.get('rule')})"
            ]
            lines += [
                f"{'ok ' if e.get('ok') else 'XX '}{e.get('text')} [{e.get('ref')}]"
                for e in verdict.get("evidence", [])
            ]
            lines += [f"reason: {r}" for r in verdict.get("reasons", [])]
            self.query_one("#pdd-verdict", Static).update("\n".join(lines))

    def load_spec(self, spec: dict[str, Any]) -> None:
        if self._unchanged("spec", spec):
            return
        tree = self.query_one("#spec-tree", Tree)
        tree.clear()
        tree.root.set_label(f"{spec['meta'].get('document_id')} {spec['meta'].get('title')}")
        steps = tree.root.add("steps", expand=True)
        for s in spec.get("steps", []):
            n = steps.add(
                f"{s['step_id']} {s['name']}{' [UI]' if s.get('is_ui_interaction') else ''}", data=s
            )
            for d in s.get("details", []):
                n.add_leaf(d[:90], data={"source_ref": s.get("source_ref"), "text": d})
        apps = tree.root.add("applications")
        for a in spec.get("applications", []):
            apps.add_leaf(f"{a['name']} ({a.get('kind')})", data=a)
        rules = tree.root.add("business rules")
        for r in spec.get("business_rules", []):
            rules.add_leaf(f"{r['id']}: {r['text'][:70]}", data=r)
        exc = tree.root.add("exceptions")
        for e in spec.get("exceptions", []):
            exc.add_leaf(f"{e['id']} ({e.get('kind')}): {e.get('description', '')[:60]}", data=e)
        oq = tree.root.add("open questions", expand=True)
        for q in spec.get("open_questions", []):
            oq.add_leaf(
                f"{'[BLOCKING] ' if q.get('blocking') else ''}{q['id']}: {q['text'][:70]}", data=q
            )
        tree.root.expand()

    def on_tree_node_selected(self, event: Tree.NodeSelected[Any]) -> None:
        data = event.node.data
        if isinstance(data, dict):
            ref = data.get("source_ref", "")
            text = (
                data.get("text")
                or data.get("action")
                or data.get("description")
                or data.get("name")
                or ""
            )
            self.query_one("#spec-detail", Static).update(f"source_ref: {ref}\n{text}")

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[Any]) -> None:
        self.on_tree_node_selected(event)  # type: ignore[arg-type]

    def load_design(self, design: dict[str, Any]) -> None:
        if self._unchanged("design", design):
            return
        md = [f"# Design: {design.get('project_name')}", ""]
        md.append(
            f"- template: **{design.get('template', {}).get('kind')}** {design.get('template', {}).get('package_id') or design.get('template', {}).get('template_id') or ''}"
        )
        md.append(
            f"- target framework: **{design.get('target_framework') or 'STOP'}**; expression language: {design.get('expression_language')}; UI capture: {design.get('ui_capture')}"
        )
        if design.get("contradiction"):
            c = design["contradiction"]
            md += [
                "",
                f"**Contradiction:** {c.get('detail')}",
                f"- Windows: {', '.join(c.get('windows_signals', []))}",
                f"- Portable: {', '.join(c.get('portable_signals', []))}",
            ]
        md += ["", "## Decisions"]
        for d in design.get("decisions", []):
            md.append(f"- **{d['topic']}:** {d['choice']} — {d['rationale']}")
        if design.get("stop_reasons"):
            md += ["", "## Stop reasons"] + [f"- {s}" for s in design["stop_reasons"]]
        md += ["", "## Workflows"]
        for w in design.get("workflows", []):
            md.append(
                f"- {w['step_id']} → `{w['file']}` ({w['role']}{', UI' if w.get('ui') else ''})"
            )
        md += ["", "## Packages"] + [
            f"- {p['id']} {p.get('version') or ''} {'(template)' if p.get('from_template') else '(install)'}"
            for p in design.get("packages", [])
        ]
        self.query_one("#design-md", Markdown).update("\n".join(md))

    def load_plan(self, text: str, status: str, approved_text: str | None) -> None:
        if self._unchanged("plan", [text, status, approved_text]):
            return
        self.query_one("#plan-status", Static).update(status)
        self.query_one("#plan-md", Markdown).update(text)
        diff = ""
        if approved_text is not None and approved_text != text:
            diff = "\n".join(
                difflib.unified_diff(
                    approved_text.splitlines(),
                    text.splitlines(),
                    "approved",
                    "current",
                    lineterm="",
                    n=2,
                )
            )
        self.query_one("#plan-diff", Static).update(
            ("Diff since approval:\n" + diff) if diff else ""
        )

    def load_validation(self, diagnostics: list[dict[str, Any]]) -> None:
        if self._unchanged("validation", diagnostics):
            return
        self.validation_rows = diagnostics
        table = self.query_one("#validation-table", DataTable)
        table.clear()
        for d in diagnostics:
            sev = d.get("severity", "")
            if d.get("expected_stub"):
                sev = f"{sev} (expected stub)"
            table.add_row(
                d.get("file", "")[-40:],
                sev,
                d.get("category", "") or d.get("source", ""),
                d.get("message", "")[:120],
                key=None,
            )

    def sort_validation(self, column_index: int) -> None:
        table = self.query_one("#validation-table", DataTable)
        keys = list(table.columns.keys())
        if 0 <= column_index < len(keys):
            table.sort(keys[column_index])

    def load_traceability(self, path: Path) -> None:
        stamp = path.stat().st_mtime_ns if path.is_file() else None
        if self._unchanged("traceability", [str(path), stamp]):
            return
        table = self.query_one("#trace-table", DataTable)
        table.clear()
        if stamp is None:
            return
        with path.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                table.add_row(
                    r["step_id"],
                    r["pdd_ref"],
                    r["file"][-30:],
                    r["activity_display_name"][:40],
                    r["status"],
                )


class PromptBar(Input):
    """Free-form instruction to the authoring agent for the next repair round."""

    DEFAULT_CSS = """
    PromptBar { dock: bottom; height: 3; margin: 0 0 1 0; }
    """

    def __init__(self) -> None:
        super().__init__(
            placeholder="instruction for the next authoring/repair round … (Enter to queue)",
            id="prompt-bar",
        )

"""Modal screens: plan approval, manual Windows round, blocking open questions, Studio lock."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, Static


class PlanApprovalScreen(ModalScreen[str | None]):
    """Shows the rendered plan, its hash and Task 0; the approver types their name."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    PlanApprovalScreen { align: center middle; }
    PlanApprovalScreen > Vertical { width: 90%; height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    PlanApprovalScreen .title { text-style: bold; color: $primary; }
    PlanApprovalScreen VerticalScroll { height: 1fr; border: tall $panel; }
    PlanApprovalScreen Horizontal { height: 3; }
    PlanApprovalScreen Input { width: 1fr; }
    """

    def __init__(
        self, plan_text: str, plan_sha: str, installs: list[str], reason: str = ""
    ) -> None:
        super().__init__()
        self.plan_text = plan_text
        self.plan_sha = plan_sha
        self.installs = installs
        self.reason = reason

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Plan approval gate", classes="title")
            yield Static(
                f"sha256 {self.plan_sha[:16]}  ·  Task 0 installs: {', '.join(self.installs) or 'none (template packages only)'}"
                + (f"\n{self.reason}" if self.reason else "")
            )
            yield VerticalScroll(Markdown(self.plan_text))
            with Horizontal():
                yield Input(placeholder="your name (approver)", id="approver")
                yield Button("Approve", id="approve", variant="success")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#approver", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve":
            self._approve()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._approve()

    def _approve(self) -> None:
        name = self.query_one("#approver", Input).value.strip()
        if not name:
            self.notify("Type your name to approve", severity="warning")
            return
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class WindowsRoundScreen(ModalScreen[str]):
    """wsl-manual: the exact command to run in Windows, copy via OSC 52, live waiting state."""

    BINDINGS = [Binding("c", "copy", "Copy command"), Binding("escape", "abort", "Abort round")]
    DEFAULT_CSS = """
    WindowsRoundScreen { align: center middle; }
    WindowsRoundScreen > Vertical { width: 90%; height: auto; max-height: 90%; border: thick $warning; background: $surface; padding: 1 2; }
    WindowsRoundScreen .title { text-style: bold; color: $warning; }
    WindowsRoundScreen #cmd { background: $panel; padding: 0 1; }
    WindowsRoundScreen Horizontal { height: 3; }
    """

    def __init__(
        self, round_id: str, command: str, ps_command: str, instructions: str, pending: int
    ) -> None:
        super().__init__()
        self.round_id = round_id
        self.command = command
        self.ps_command = ps_command
        self.instructions = instructions
        self.pending = pending
        self.waited = 0.0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"Windows round {self.round_id}: run this in your Windows terminal", classes="title"
            )
            yield Static(self.command, id="cmd")
            yield Static(f"or: {self.ps_command}", id="cmd-ps")
            yield Static(self.instructions, id="instructions")
            yield Static(f"waiting for results… {self.pending} job(s) pending", id="status")
            with Horizontal():
                yield Button("Copy command (c)", id="copy", variant="primary")
                yield Button("Abort round", id="abort", variant="error")

    def set_progress(self, pending: int, waited: float) -> None:
        self.pending = pending
        self.waited = waited
        self.query_one("#status", Static).update(
            f"waiting for results… {pending} job(s) pending, {waited:.0f}s elapsed"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "copy":
            self.action_copy()
        else:
            self.action_abort()

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.command)
        self.notify(
            "command copied (OSC 52); select the text if your terminal blocks it", timeout=4
        )

    def action_abort(self) -> None:
        self.dismiss("abort")


class OpenQuestionsScreen(ModalScreen[str]):
    """Blocking open questions with PDD refs: edit the PDD and restart, or abort."""

    BINDINGS = [Binding("escape", "abort", "Abort")]
    DEFAULT_CSS = """
    OpenQuestionsScreen { align: center middle; }
    OpenQuestionsScreen > Vertical { width: 85%; height: auto; max-height: 90%; border: thick $error; background: $surface; padding: 1 2; }
    OpenQuestionsScreen .title { text-style: bold; color: $error; }
    OpenQuestionsScreen VerticalScroll { height: auto; max-height: 20; }
    OpenQuestionsScreen Horizontal { height: 3; }
    """

    def __init__(
        self,
        questions: list[dict[str, Any]],
        stop_reasons: list[str],
        contradiction: dict[str, Any] | None,
    ) -> None:
        super().__init__()
        self.questions = questions
        self.stop_reasons = stop_reasons
        self.contradiction = contradiction

    def compose(self) -> ComposeResult:
        lines: list[str] = []
        if self.contradiction:
            lines.append(f"**Target framework contradiction:** {self.contradiction.get('detail')}")
            lines.append(
                f"- Windows-only: {', '.join(self.contradiction.get('windows_signals', []))}"
            )
            lines.append(
                f"- Cross-platform: {', '.join(self.contradiction.get('portable_signals', []))}"
            )
            lines.append("")
        if self.questions:
            lines.append("**Blocking open questions:**")
            lines += [
                f"- {q.get('id')} ({q.get('source_ref')}): {q.get('text')}"
                + (f" — owner {q.get('owner')}" if q.get("owner") else "")
                for q in self.questions
            ]
        other = [
            s for s in self.stop_reasons if "open question" not in s and "contradiction" not in s
        ]
        if other:
            lines += ["", "**Other stop reasons:**"] + [f"- {s}" for s in other]
        with Vertical():
            yield Label("Design stopped", classes="title")
            yield VerticalScroll(Markdown("\n".join(lines)))
            with Horizontal():
                yield Button("Edit PDD and restart", id="restart", variant="primary")
                yield Button("Abort", id="abort", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("restart" if event.button.id == "restart" else "abort")

    def action_abort(self) -> None:
        self.dismiss("abort")


class StudioLockScreen(ModalScreen[str]):
    DEFAULT_CSS = """
    StudioLockScreen { align: center middle; }
    StudioLockScreen > Vertical { width: 70%; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    StudioLockScreen Horizontal { height: 3; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Project is open in Studio Desktop", classes="title")
            yield Static(self.message)
            with Horizontal():
                yield Button("Retry", id="retry", variant="primary")
                yield Button("Abort", id="abort", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("retry" if event.button.id == "retry" else "abort")


class DetailScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close")]
    DEFAULT_CSS = """
    DetailScreen { align: center middle; }
    DetailScreen > Vertical { width: 80%; height: auto; max-height: 80%; border: thick $primary; background: $surface; padding: 1 2; }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, classes="title")
            yield VerticalScroll(Static(self.body))
            yield Button("Close", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class PickPddScreen(ModalScreen[str | None]):
    """Choose a PDD path (free text, with the candidates found in cwd/samples)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    PickPddScreen { align: center middle; }
    PickPddScreen > Vertical { width: 80%; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    """

    def __init__(self, candidates: list[str]) -> None:
        super().__init__()
        self.candidates = candidates

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Open a PDD (.docx / .pdf)", classes="title")
            yield Static(
                "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(self.candidates[:9]))
                or "  (no PDDs found in ./ or ./samples)"
            )
            yield Input(placeholder="path to the PDD, or the number of a candidate", id="pdd-path")

    def on_mount(self) -> None:
        self.query_one("#pdd-path", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value.isdigit() and 1 <= int(value) <= len(self.candidates):
            value = self.candidates[int(value) - 1]
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

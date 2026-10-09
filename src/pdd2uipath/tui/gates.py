"""Gate handlers that bridge the pipeline worker thread and the Textual UI thread."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Literal

from textual.worker import NoActiveWorker, get_current_worker

from pdd2uipath.executors.wsl_manual import ManualRound, RoundAborted, RoundHandler
from pdd2uipath.tui.screens import (
    OpenQuestionsScreen,
    PlanApprovalScreen,
    StudioLockScreen,
    WindowsRoundScreen,
)

if TYPE_CHECKING:
    from pdd2uipath.tui.app import Pdd2UipathApp


def worker_cancelled(app: Pdd2UipathApp) -> bool:
    """True once the app is closing or Textual cancelled the pipeline worker.

    The pipeline runs on a thread worker; threads cannot be interrupted, so every blocking
    wait in the gate handlers polls this instead of blocking forever after the app exits.
    """
    if app.closing:
        return True
    try:
        return get_current_worker().is_cancelled
    except NoActiveWorker:
        return False


class _Waiter:
    """Push a modal from the worker thread and block until it is dismissed."""

    def __init__(self, app: Pdd2UipathApp) -> None:
        self.app = app

    def ask(self, screen: Any) -> Any:
        done = threading.Event()
        box: dict[str, Any] = {}

        def _dismissed(result: Any) -> None:
            box["result"] = result
            done.set()

        def _push() -> None:
            self.app.push_screen(screen, callback=_dismissed)

        self.app.call_from_thread(_push)
        while not done.wait(0.2):
            if worker_cancelled(self.app):
                return None  # the app is gone: every gate treats this as "not approved"
        return box.get("result")


class TuiRoundHandler:
    """Shows the Windows-round modal once per round and keeps it updated while polling."""

    def __init__(self, app: Pdd2UipathApp) -> None:
        self.app = app
        self.screen: WindowsRoundScreen | None = None
        self.aborted = False

    def announce(self, round_: ManualRound) -> None:
        self.aborted = False
        screen = WindowsRoundScreen(
            round_.id,
            round_.run_cmd_windows,
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{round_.run_ps1_windows}"',
            round_.instructions(),
            len(round_.pending_ids()),
        )
        self.screen = screen

        def _on_dismiss(result: Any) -> None:
            if result == "abort":
                self.aborted = True

        self.app.call_from_thread(self.app.push_screen, screen, _on_dismiss)

    def tick(self, round_: ManualRound, waited_s: float) -> None:
        if self.aborted or worker_cancelled(self.app):
            raise RoundAborted("round aborted by the user")
        if self.screen is not None and self.screen.is_attached:
            pending = len(round_.pending_ids())
            self.app.call_from_thread(self.screen.set_progress, pending, waited_s)

    def finished(self, round_: ManualRound) -> None:
        screen = self.screen
        self.screen = None
        if screen is not None and screen.is_attached:
            self.app.call_from_thread(screen.dismiss, "done")


class TuiGateHandler:
    def __init__(self, app: Pdd2UipathApp) -> None:
        self.app = app
        self._waiter = _Waiter(app)
        self._round = TuiRoundHandler(app)

    def approve_plan(self, plan_text: str, plan_sha: str, info: dict[str, Any]) -> str | None:
        result = self._waiter.ask(
            PlanApprovalScreen(
                plan_text, plan_sha, list(info.get("installs", [])), str(info.get("reason", ""))
            )
        )
        return result if isinstance(result, str) and result.strip() else None

    def open_questions(
        self, questions: list[dict[str, Any]], stop_reasons: list[str]
    ) -> Literal["abort", "restart"]:
        contradiction = None
        if (
            self.app.pipeline is not None
            and self.app.pipeline.design is not None
            and self.app.pipeline.design.contradiction is not None
        ):
            contradiction = self.app.pipeline.design.contradiction.model_dump()
        result = self._waiter.ask(OpenQuestionsScreen(questions, stop_reasons, contradiction))
        return "restart" if result == "restart" else "abort"

    def studio_lock(self, message: str) -> Literal["retry", "abort"]:
        result = self._waiter.ask(StudioLockScreen(message))
        return "retry" if result == "retry" else "abort"

    def round_handler(self) -> RoundHandler:
        return self._round

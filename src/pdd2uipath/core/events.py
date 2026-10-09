"""Typed pipeline events and a tiny thread-safe bus shared by the CLI and the TUI."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


class _Base(BaseModel):
    ts: datetime = Field(default_factory=_now)


class StageStarted(_Base):
    kind: Literal["stage_started"] = "stage_started"
    stage: str


class StageFinished(_Base):
    kind: Literal["stage_finished"] = "stage_finished"
    stage: str
    status: Literal["done", "failed", "stopped", "skipped", "waiting"]
    detail: str = ""
    duration_s: float = 0.0


class CommandStarted(_Base):
    kind: Literal["command_started"] = "command_started"
    command_id: str
    argv: list[str]
    side: str
    cwd: str | None = None


class CommandFinished(_Base):
    kind: Literal["command_finished"] = "command_finished"
    command_id: str
    argv: list[str]
    side: str
    exit_code: int
    ok: bool
    duration_s: float
    summary: str = ""
    stdout: str = ""
    stderr: str = ""


class AgentMessage(_Base):
    kind: Literal["agent_message"] = "agent_message"
    role: Literal["assistant", "user", "system", "result"] = "assistant"
    text: str
    final: bool = True


class AgentToolUse(_Base):
    kind: Literal["agent_tool_use"] = "agent_tool_use"
    tool: str
    summary: str
    input: dict[str, Any] = Field(default_factory=dict)


class AgentToolResult(_Base):
    kind: Literal["agent_tool_result"] = "agent_tool_result"
    tool: str
    summary: str
    is_error: bool = False


class GateWaiting(_Base):
    kind: Literal["gate_waiting"] = "gate_waiting"
    gate: Literal["plan_approval", "windows_round", "open_questions", "studio_lock"]
    payload: dict[str, Any] = Field(default_factory=dict)


class GateResolved(_Base):
    kind: Literal["gate_resolved"] = "gate_resolved"
    gate: str
    outcome: str


class RepairRound(_Base):
    kind: Literal["repair_round"] = "repair_round"
    round: int
    max_rounds: int
    errors: int


class LogEvent(_Base):
    kind: Literal["log"] = "log"
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str


class ExecutorStatus(_Base):
    kind: Literal["executor_status"] = "executor_status"
    executor: str
    online: bool
    detail: str = ""


class RunStateChanged(_Base):
    kind: Literal["run_state"] = "run_state"
    stages: dict[str, str]
    repair_round: int = 0


Event = Annotated[
    StageStarted
    | StageFinished
    | CommandStarted
    | CommandFinished
    | AgentMessage
    | AgentToolUse
    | AgentToolResult
    | GateWaiting
    | GateResolved
    | RepairRound
    | LogEvent
    | ExecutorStatus
    | RunStateChanged,
    Field(discriminator="kind"),
]

Subscriber = Callable[[Any], None]


class EventBus:
    """Synchronous fan-out bus. Subscribers must be fast and never raise."""

    def __init__(self) -> None:
        self._subs: list[Subscriber] = []
        self._lock = threading.RLock()
        self.history: list[Any] = []

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def _unsub() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return _unsub

    def publish(self, event: Any) -> None:
        with self._lock:
            self.history.append(event)
            subs = list(self._subs)
        for fn in subs:
            with contextlib.suppress(Exception):  # a broken sink must not break the pipeline
                fn(event)

    def log(
        self, message: str, level: Literal["debug", "info", "warning", "error"] = "info"
    ) -> None:
        self.publish(LogEvent(level=level, message=message))


class JsonlSink:
    """Append every event as one JSON line (used for ``logs/*.jsonl``)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, event: Any) -> None:
        if not isinstance(event, BaseModel):
            return
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

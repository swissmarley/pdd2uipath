"""``AuthoringAgent`` protocol: the coding agent that executes the plan with the skill."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pdd2uipath.core.events import AgentMessage, AgentToolResult, AgentToolUse, EventBus


@dataclass
class AuthoringSession:
    project_dir: Path
    prompt: str
    env: dict[str, str]
    log_path: Path
    max_turns: int = 120
    max_budget_usd: float = 25.0
    timeout_s: float = 7200.0
    resume_session_id: str | None = None
    model: str | None = None
    bus: EventBus | None = None
    skills_root: Path | None = None


@dataclass
class AuthoringResult:
    ok: bool
    session_id: str | None = None
    turns: int = 0
    cost_usd: float | None = None
    final_text: str = ""
    error: str = ""
    raw_events: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class AuthoringAgent(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def run(self, session: AuthoringSession) -> AuthoringResult: ...


class AgentError(RuntimeError):
    pass


def jsonl_logger(path: Path) -> Callable[[dict[str, Any]], None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a", encoding="utf-8")

    def _log(obj: dict[str, Any]) -> None:
        fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        fh.flush()

    return _log


def publish_line(bus: EventBus | None, line: str, *, tool_hint: str = "") -> None:
    """Map one line of agent output (plain text or a JSON event) to bus events."""
    if bus is None:
        return
    stripped = line.strip()
    if not stripped:
        return
    if stripped.startswith("{"):
        try:
            ev = json.loads(stripped)
        except ValueError:
            bus.publish(AgentMessage(text=stripped, final=False))
            return
        if isinstance(ev, dict):
            _publish_event(bus, ev)
            return
    bus.publish(AgentMessage(text=stripped, final=False))


def _publish_event(bus: EventBus, ev: dict[str, Any]) -> None:
    etype = str(ev.get("type") or ev.get("event") or ev.get("kind") or "")
    part = ev.get("part") if isinstance(ev.get("part"), dict) else None
    tool = ev.get("tool") or ev.get("name") or (part or {}).get("tool")
    if "tool" in etype.lower() or tool:
        summary = ev.get("title") or ev.get("summary") or (part or {}).get("title") or ""
        inp = ev.get("input") or ev.get("args") or (part or {}).get("state", {}).get("input") or {}
        if not isinstance(inp, dict):
            inp = {"value": inp}
        if (
            "result" in etype.lower()
            or ev.get("output") is not None
            or (part or {}).get("state", {}).get("status") == "completed"
        ):
            out = ev.get("output") or (part or {}).get("state", {}).get("output") or summary
            bus.publish(
                AgentToolResult(
                    tool=str(tool or "tool"),
                    summary=str(out)[:400],
                    is_error=bool(ev.get("is_error") or ev.get("error")),
                )
            )
        else:
            bus.publish(
                AgentToolUse(
                    tool=str(tool or "tool"), summary=str(summary or _short(inp))[:400], input=inp
                )
            )
        return
    text = (
        ev.get("text")
        or ev.get("content")
        or ev.get("message")
        or (part or {}).get("text")
        or ev.get("result")
    )
    if isinstance(text, str) and text.strip():
        bus.publish(AgentMessage(text=text, final=etype in {"result", "final", "assistant"}))
    elif etype:
        bus.publish(AgentMessage(role="system", text=f"[{etype}] {_short(ev)}", final=False))


def _short(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str) if not isinstance(obj, str) else obj
    return s if len(s) <= 300 else s[:300] + "..."

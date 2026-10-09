"""CLI coding agents run as subprocesses: OpenCode and GitHub Copilot CLI (and a fake for tests).

Both are driven non-interactively with the project as working directory, the ``uip``
shim first on ``PATH``, and their stdout streamed line by line into the event bus and
``logs/author.jsonl``. Flags verified against ``opencode run --help`` (opencode-ai 1.18)
and ``copilot --help`` / ``copilot help permissions`` (GitHub Copilot CLI 1.0.94) on
2026-10-09.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pdd2uipath.agents.base import (
    AgentError,
    AuthoringResult,
    AuthoringSession,
    jsonl_logger,
    publish_line,
)
from pdd2uipath.core.events import AgentMessage, LogEvent
from pdd2uipath.core.exe import resolve_argv, which_cli

_SESSION_RE = re.compile(r'"(?:sessionID|session_id|sessionId)"\s*:\s*"([^"]+)"')


def _which(name: str) -> str | None:
    return which_cli(name)


class SubprocessAgent:
    """Shared machinery: build argv, stream output, collect the result."""

    name = "subprocess"
    executable = ""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        model: str | None = None,
        deny_tools: Sequence[str] = (),
    ) -> None:
        self.command = list(command) if command else []
        self.model = model
        self.deny_tools = list(deny_tools)

    @property
    def exe(self) -> str:
        return self.command[0] if self.command else self.executable

    @property
    def extra_args(self) -> list[str]:
        return list(self.command[1:]) if self.command else []

    def available(self) -> tuple[bool, str]:
        found = _which(self.exe)
        return (True, found) if found else (False, f"{self.exe} not found on PATH")

    def build_argv(self, session: AuthoringSession) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def run(self, session: AuthoringSession) -> AuthoringResult:
        ok, detail = self.available()
        if not ok:
            raise AgentError(f"{self.name}: {detail}")
        # npm .cmd shims are expanded to node + loader: no cmd.exe between us and the agent
        argv = resolve_argv(self.build_argv(session))
        log = jsonl_logger(session.log_path)
        log({"event": "start", "agent": self.name, "argv": argv, "cwd": str(session.project_dir)})
        if session.bus:
            session.bus.publish(
                LogEvent(
                    message=f"{self.name}: {' '.join(argv[:3])} ... (cwd {session.project_dir})"
                )
            )
        env = dict(os.environ)
        env.update(session.env)
        env.setdefault("PYTHONUTF8", "1")
        env["NO_COLOR"] = "1"
        env["CI"] = "1"
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(session.project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise AgentError(f"{self.name}: cannot start {argv[0]}: {exc}") from exc
        lines: list[str] = []
        err_lines: list[str] = []

        def _pump(stream: object, sink: list[str], is_err: bool) -> None:
            assert stream is not None
            for line in stream:  # type: ignore[attr-defined]
                text = str(line).rstrip("\n")
                sink.append(text)
                log({"event": "stderr" if is_err else "stdout", "line": text})
                if not is_err:
                    publish_line(session.bus, text)
                elif session.bus and text.strip():
                    session.bus.publish(AgentMessage(role="system", text=text.strip(), final=False))

        t_out = threading.Thread(target=_pump, args=(proc.stdout, lines, False), daemon=True)
        t_err = threading.Thread(target=_pump, args=(proc.stderr, err_lines, True), daemon=True)
        t_out.start()
        t_err.start()
        try:
            code = proc.wait(timeout=session.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = 124
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        duration = time.monotonic() - started
        session_id = self.find_session_id("\n".join(lines) + "\n" + "\n".join(err_lines))
        log(
            {
                "event": "end",
                "exit_code": code,
                "duration_s": round(duration, 1),
                "session_id": session_id,
            }
        )
        result = AuthoringResult(
            ok=code == 0,
            session_id=session_id,
            final_text=self.final_text(lines),
            error=""
            if code == 0
            else f"{self.name} exited {code}: {' '.join(err_lines[-5:])[-1500:]}",
            raw_events=len(lines),
            extra={"duration_s": duration, "exit_code": code},
        )
        if session.bus:
            session.bus.publish(
                AgentMessage(
                    role="result",
                    text=result.final_text[-4000:] or (result.error or "(no output)"),
                    final=True,
                )
            )
        return result

    @staticmethod
    def find_session_id(text: str) -> str | None:
        m = _SESSION_RE.search(text)
        return m.group(1) if m else None

    @staticmethod
    def final_text(lines: list[str]) -> str:
        texts: list[str] = []
        plain: list[str] = []
        for line in lines:
            s = line.strip()
            if s.startswith("{"):
                try:
                    ev = json.loads(s)
                except ValueError:
                    plain.append(line)
                    continue
                if isinstance(ev, dict):
                    part_raw = ev.get("part")
                    part: dict[str, Any] = part_raw if isinstance(part_raw, dict) else {}
                    t = ev.get("text") or ev.get("result") or ev.get("content") or part.get("text")
                    if isinstance(t, str):
                        texts.append(t)
                continue
            plain.append(line)
        return "\n".join(texts) if texts else "\n".join(plain)


class OpenCodeAgent(SubprocessAgent):
    """``opencode run`` with the local LLM configured in OpenCode (corporate default)."""

    name = "opencode"
    executable = "opencode"

    def build_argv(self, session: AuthoringSession) -> list[str]:
        argv = [self.exe, "run", "--format", "json", "--dir", str(session.project_dir), "--auto"]
        model = session.model or self.model
        if model:
            argv += ["--model", model]
        if session.resume_session_id:
            argv += ["--session", session.resume_session_id]
        argv += self.extra_args
        argv.append(session.prompt)
        return argv


class CopilotAgent(SubprocessAgent):
    """``copilot -p`` (GitHub Copilot CLI) in non-interactive mode with deny rules."""

    name = "copilot"
    executable = "copilot"

    def build_argv(self, session: AuthoringSession) -> list[str]:
        argv = [
            self.exe,
            "-p",
            session.prompt,
            "--allow-all-tools",
            "--output-format",
            "json",
            "-C",
            str(session.project_dir),
            "--add-dir",
            str(session.project_dir),
        ]
        for rule in self.deny_tools:
            argv += ["--deny-tool", rule]
        if session.skills_root is not None:
            argv += ["--add-dir", str(session.skills_root)]
        model = session.model or self.model
        if model:
            argv += ["--model", model]
        if session.resume_session_id:
            argv += ["--resume", session.resume_session_id]
        argv += self.extra_args
        return argv


class FakeAgent(SubprocessAgent):
    """Test double: runs a Python script that receives the project dir and the prompt file."""

    name = "fake"

    def __init__(self, script: Path, *, python: str | None = None) -> None:
        super().__init__([python or sys.executable, "-I", "-X", "utf8", str(script)])
        self.script = script

    def available(self) -> tuple[bool, str]:
        return (self.script.is_file(), str(self.script))

    def build_argv(self, session: AuthoringSession) -> list[str]:
        prompt_file = session.log_path.with_suffix(".prompt.md")
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(session.prompt, encoding="utf-8")
        return [
            *self.command,
            "--project",
            str(session.project_dir),
            "--prompt-file",
            str(prompt_file),
        ]


def build_agent(
    backend: str,
    *,
    command: Sequence[str] | None = None,
    model: str | None = None,
    fake_script: Path | None = None,
    deny_tools: Sequence[str] = (),
) -> SubprocessAgent:
    if backend == "auto":
        backend = (
            "opencode" if _which("opencode") else "copilot" if _which("copilot") else "opencode"
        )
    if backend == "opencode":
        return OpenCodeAgent(command, model=model, deny_tools=deny_tools)
    if backend == "copilot":
        return CopilotAgent(command, model=model, deny_tools=deny_tools)
    if backend == "fake":
        if fake_script is None:
            raise AgentError("fake agent needs a script path")
        return FakeAgent(fake_script)
    raise AgentError(f"unknown agent backend {backend!r} (opencode | copilot | fake)")

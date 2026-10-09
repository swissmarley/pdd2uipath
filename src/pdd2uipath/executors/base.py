"""The ``Executor`` interface: every ``uip`` invocation goes through it.

An executor decides *where* ``uip`` runs (native Windows, WSL interop, the file-based
bridge, or a manual Windows round) and translates path arguments. Stages never call
``subprocess`` themselves.
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pdd2uipath.core.events import CommandFinished, CommandStarted, EventBus
from pdd2uipath.core.paths import PathTranslationError, looks_like_path, to_windows
from pdd2uipath.core.platform import Platform, detect_platform

#: Flags whose value is a filesystem path that must be translated to a Windows path.
PATH_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "--project-dir",
        "--location",
        "--studio-dir",
        "--robot-dir",
        "--nuget-sources-config-path",
        "--packages-file",
        "--log-file",
        "--detailed-log-path",
        "--governance-file-path",
        "--policy-file",
        "--input-arguments-file",
        "--field-values-file",
        "--repository-path",
        "--file",
        "-f",
    }
)
#: ``rpa`` verbs that take the project directory as a positional argument.
POSITIONAL_PATH_VERBS: frozenset[str] = frozenset({"build", "restore", "analyze", "pack"})
#: ``--file-path`` stays relative (the skill mandates relative paths); absolute ones are translated.
RELATIVE_OK_FLAGS: frozenset[str] = frozenset({"--file-path"})


class ExecutorError(RuntimeError):
    """Base class for executor failures that are not ``uip`` failures."""


class WindowsSideUnavailable(ExecutorError):
    """The Windows side cannot be reached (no worker, interop disabled, ...)."""


class JobRejected(ExecutorError):
    """A job violates the allowlist or the workspace boundary."""


def parse_envelope(stdout: str) -> dict[str, Any] | None:
    """Return the last top-level JSON object printed on stdout, if any.

    ``uip`` streams log lines above the envelope (``[Information] ...``), so a plain
    ``json.loads`` is tried first and then every line that starts a JSON object,
    from the bottom up.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("{"):
            chunk = "\n".join(lines[i:])
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


@dataclass(frozen=True)
class UipResult:
    argv: tuple[str, ...]
    cwd: str | None
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    side: str
    envelope: dict[str, Any] | None = None

    @property
    def result_code(self) -> str | None:
        if self.envelope is None:
            return None
        value = self.envelope.get("Result")
        return str(value) if value is not None else None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.result_code != "Failure"

    @property
    def data(self) -> Any:
        return self.envelope.get("Data") if self.envelope else None

    @property
    def message(self) -> str:
        if self.envelope and self.envelope.get("Message"):
            return str(self.envelope["Message"])
        tail = self.stderr.strip().splitlines()
        return tail[-1] if tail else ""

    def summary(self) -> str:
        if self.ok:
            return self.result_code or "ok"
        msg = self.message
        return (
            f"{self.result_code or 'exit ' + str(self.exit_code)}: {msg}"
            if msg
            else (self.result_code or f"exit {self.exit_code}")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "ok": self.ok,
            "side": self.side,
            "duration_s": round(self.duration_s, 3),
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Job:
    argv: list[str]
    cwd: str | None = None
    timeout_s: float = 900.0
    id: str = field(default_factory=_new_job_id)
    label: str = ""


@dataclass(frozen=True)
class ExecutorState:
    kind: str
    online: bool
    detail: str = ""


def translate_argv(argv: Sequence[str], platform: Platform | None = None) -> list[str]:
    """Translate every path-valued argument to a Windows path.

    Raises :class:`PathTranslationError` when a path is not on the Windows filesystem.
    """
    platform = platform or detect_platform()
    out: list[str] = []
    args = list(argv)
    verb = args[1] if len(args) > 1 and args[0] == "rpa" else None
    positional_seen = False
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in PATH_VALUE_FLAGS and i + 1 < len(args):
            out.append(tok)
            out.append(to_windows(args[i + 1], platform))
            i += 2
            continue
        if tok in RELATIVE_OK_FLAGS and i + 1 < len(args):
            value = args[i + 1]
            out.append(tok)
            if value.startswith(("/mnt/", "\\\\", "//")) or (len(value) > 1 and value[1] == ":"):
                out.append(to_windows(value, platform))
            else:
                out.append(value)
            i += 2
            continue
        if "=" in tok and tok.split("=", 1)[0] in PATH_VALUE_FLAGS:
            flag, value = tok.split("=", 1)
            out.append(f"{flag}={to_windows(value, platform)}")
            i += 1
            continue
        if (
            verb in POSITIONAL_PATH_VERBS
            and i > 1
            and not positional_seen
            and not tok.startswith("-")
            and looks_like_path(tok)
            and (i == 0 or args[i - 1] not in PATH_VALUE_FLAGS)
        ):
            out.append(to_windows(tok, platform))
            positional_seen = True
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


@runtime_checkable
class Executor(Protocol):
    name: str

    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, timeout_s: float = 900.0
    ) -> UipResult: ...

    def run_batch(self, jobs: Sequence[Job]) -> list[UipResult]: ...

    def status(self) -> ExecutorState: ...


class BaseExecutor(ABC):
    name: str = "base"

    @abstractmethod
    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, timeout_s: float = 900.0
    ) -> UipResult: ...

    def run_batch(self, jobs: Sequence[Job]) -> list[UipResult]:
        return [self.run(j.argv, cwd=j.cwd, timeout_s=j.timeout_s) for j in jobs]

    def status(self) -> ExecutorState:
        return ExecutorState(self.name, True, "")


class TracingExecutor(BaseExecutor):
    """Decorator that publishes ``CommandStarted``/``CommandFinished`` on the event bus."""

    def __init__(self, inner: Executor, bus: EventBus) -> None:
        self.inner = inner
        self.bus = bus
        self.name = inner.name

    def run(
        self, argv: Sequence[str], *, cwd: str | None = None, timeout_s: float = 900.0
    ) -> UipResult:
        cid = _new_job_id()
        self.bus.publish(
            CommandStarted(command_id=cid, argv=list(argv), side=self.inner.name, cwd=cwd)
        )
        started = time.monotonic()
        try:
            result = self.inner.run(argv, cwd=cwd, timeout_s=timeout_s)
        except (ExecutorError, PathTranslationError) as exc:
            self.bus.publish(
                CommandFinished(
                    command_id=cid,
                    argv=list(argv),
                    side=self.inner.name,
                    exit_code=-1,
                    ok=False,
                    duration_s=time.monotonic() - started,
                    summary=f"{type(exc).__name__}: {exc}",
                    stderr=str(exc),
                )
            )
            raise
        self.bus.publish(
            CommandFinished(
                command_id=cid,
                argv=list(result.argv),
                side=result.side,
                exit_code=result.exit_code,
                ok=result.ok,
                duration_s=result.duration_s,
                summary=result.summary(),
                stdout=result.stdout[-20000:],
                stderr=result.stderr[-4000:],
            )
        )
        return result

    def run_batch(self, jobs: Sequence[Job]) -> list[UipResult]:
        if len(jobs) <= 1 or not hasattr(self.inner, "run_batch"):
            return [self.run(j.argv, cwd=j.cwd, timeout_s=j.timeout_s) for j in jobs]
        ids = [_new_job_id() for _ in jobs]
        for cid, j in zip(ids, jobs, strict=True):
            self.bus.publish(
                CommandStarted(command_id=cid, argv=list(j.argv), side=self.inner.name, cwd=j.cwd)
            )
        results = self.inner.run_batch(jobs)
        for cid, r in zip(ids, results, strict=True):
            self.bus.publish(
                CommandFinished(
                    command_id=cid,
                    argv=list(r.argv),
                    side=r.side,
                    exit_code=r.exit_code,
                    ok=r.ok,
                    duration_s=r.duration_s,
                    summary=r.summary(),
                    stdout=r.stdout[-20000:],
                    stderr=r.stderr[-4000:],
                )
            )
        return results

    def status(self) -> ExecutorState:
        return self.inner.status()

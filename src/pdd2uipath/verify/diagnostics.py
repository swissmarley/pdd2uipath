"""Parse ``uip rpa validate`` / ``uip rpa build`` results into diagnostics.

Shapes observed with @uipath/cli 1.203 (2026-10-09):

validate (clean)::   Data.message == "No diagnostics found."
validate (errors)::  Data.message == "Found 2 error(s), 1 warning(s):\\n[error] C:\\...\\File.xaml - <msg> (ERROR, Validation Error) | activity: 1.9\\n..."
build (clean)::      Result Success, Data.Success == true
build (failed)::     Result Failure, Message == "Build failed: [Framework\\\\GetTransactionData.xaml] <msg>\\r\\n[StubProbe2.xaml] <msg>\\r\\n<continuation line>\\r\\n"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pdd2uipath.executors.base import UipResult

#: In stub mode these are the *expected* errors on `TODO Indicate` activities.
EXPECTED_STUB_PATTERNS: tuple[str, ...] = (
    "Target or Input UI Element must be set",
    "activity is not yet configured",
    "Configure it by pointing to the application you are automating",
)

_VALIDATE_SPLIT = re.compile(r"(?=^\[(?:error|warning|info|verbose)\]\s)", re.M | re.I)
_VALIDATE_ITEM = re.compile(
    r"^\[(?P<sev>error|warning|info|verbose)\]\s+(?P<file>.+?)\s+-\s+(?P<msg>.*?)\s*"
    r"\((?P<SEV>ERROR|WARNING|INFO|VERBOSE),\s*(?P<cat>[^)]*)\)\s*(?:\|\s*activity:\s*(?P<act>\S+))?\s*$",
    re.S | re.I,
)
_BUILD_SPLIT = re.compile(r"(?=^\[[^\]\r\n]+\]\s)", re.M)
_BUILD_ITEM = re.compile(r"^\[(?P<file>[^\]\r\n]+)\]\s*(?P<msg>.*)$", re.S)


@dataclass
class Diagnostic:
    file: str
    severity: str
    message: str
    source: str  # validate | build | executor
    activity: str = ""
    category: str = ""
    expected_stub: bool = False

    @property
    def is_error(self) -> bool:
        return self.severity.lower() == "error"


@dataclass
class GateVerdict:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    stub_mode: bool = True

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.is_error and not d.expected_stub]

    @property
    def expected_stub_errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.is_error and d.expected_stub]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if not d.is_error]

    @property
    def passed(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"{len(self.errors)} error(s), {len(self.expected_stub_errors)} expected stub error(s) "
            f"({'stub mode' if self.stub_mode else 'live mode'}), {len(self.warnings)} warning(s)"
        )


def _relativize(path: str, project_windows: str) -> str:
    p = path.strip().replace("/", "\\")
    root = project_windows.rstrip("\\") + "\\"
    if p.lower().startswith(root.lower()):
        p = p[len(root) :]
    return p.replace("\\", "/")


def is_expected_stub(message: str) -> bool:
    low = message.lower()
    return any(pat.lower() in low for pat in EXPECTED_STUB_PATTERNS)


def parse_validate(
    result: UipResult, project_windows: str, file_rel: str | None
) -> list[Diagnostic]:
    data = result.data if isinstance(result.data, dict) else {}
    message = str(data.get("message", "")) if data else ""
    if not result.ok:
        return [
            Diagnostic(
                file=file_rel or "",
                severity="error",
                message=result.message or result.stderr[-500:] or "validate failed",
                source="validate",
            )
        ]
    if not message or "no diagnostics found" in message.lower():
        return []
    out: list[Diagnostic] = []
    for chunk in _VALIDATE_SPLIT.split(message):
        chunk = chunk.strip()
        if not chunk or not chunk.startswith("["):
            continue
        m = _VALIDATE_ITEM.match(chunk)
        if not m:
            out.append(
                Diagnostic(file=file_rel or "", severity="error", message=chunk, source="validate")
            )
            continue
        msg = re.sub(r"\s*\r?\n\s*", " ", m.group("msg")).strip()
        out.append(
            Diagnostic(
                file=_relativize(m.group("file"), project_windows) or (file_rel or ""),
                severity=m.group("sev").lower(),
                message=msg,
                source="validate",
                activity=m.group("act") or "",
                category=(m.group("cat") or "").strip(),
            )
        )
    return out


def parse_build(result: UipResult, project_windows: str) -> list[Diagnostic]:
    data = result.data if isinstance(result.data, dict) else {}
    if result.ok and (data.get("Success") is True or data.get("success") is True or not data):
        return []
    text = result.message or ""
    if text.lower().startswith("build failed:"):
        text = text[len("build failed:") :]
    text = text.strip()
    out: list[Diagnostic] = []
    for chunk in _BUILD_SPLIT.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _BUILD_ITEM.match(chunk)
        if not m:
            out.append(
                Diagnostic(
                    file="",
                    severity="error",
                    message=re.sub(r"\s*\r?\n\s*", " ", chunk),
                    source="build",
                )
            )
            continue
        out.append(
            Diagnostic(
                file=_relativize(m.group("file"), project_windows),
                severity="error",
                message=re.sub(r"\s*\r?\n\s*", " ", m.group("msg")).strip(),
                source="build",
            )
        )
    if not out:
        out.append(
            Diagnostic(
                file="",
                severity="error",
                message=result.message
                or result.stderr[-800:]
                or f"build exited {result.exit_code}",
                source="build",
            )
        )
    return out


def classify(diagnostics: list[Diagnostic], *, stub_mode: bool) -> GateVerdict:
    for d in diagnostics:
        d.expected_stub = stub_mode and d.is_error and is_expected_stub(d.message)
    return GateVerdict(diagnostics=diagnostics, stub_mode=stub_mode)


def format_for_agent(verdict: GateVerdict, extra: list[str] | None = None) -> str:
    """The exact errors, grouped by file, for a repair round."""
    lines = [
        "The Windows gate (`uip rpa validate` per file, then `uip rpa build`) reported these errors:"
    ]
    by_file: dict[str, list[Diagnostic]] = {}
    for d in verdict.errors:
        by_file.setdefault(d.file or "(project)", []).append(d)
    for file, items in by_file.items():
        lines.append(f"\n{file}:")
        for d in items:
            where = f" [activity {d.activity}]" if d.activity else ""
            lines.append(f"  - ({d.source}){where} {d.message}")
    if verdict.expected_stub_errors:
        lines.append(
            f"\n{len(verdict.expected_stub_errors)} unconfigured-target error(s) on `TODO Indicate` activities are EXPECTED in stub mode; leave those activities as they are."
        )
    for e in extra or []:
        lines.append(f"- {e}")
    lines.append(
        "\nFix one root cause at a time, re-run `uip rpa validate --file-path <file>` on each touched file, then `uip rpa build`. Do not change targetFramework/expressionLanguage or project.json dependencies."
    )
    return "\n".join(lines)

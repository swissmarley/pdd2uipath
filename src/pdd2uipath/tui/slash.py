"""Slash commands typed into the prompt bar (``/help``, ``/agent copilot``, ``/set ...``).

The prompt bar keeps its original job: plain text is queued as an instruction for the next
authoring or repair round. A line starting with ``/`` is a command. Commands either call the
app's existing actions, show a document in the detail screen, or change a setting. Settings
changes are validated by the pydantic model, applied to the session, and persisted to the
config file the session was loaded from (or the user config path when there is none).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pdd2uipath.core.config import (
    ExecutorKind,
    Settings,
    default_user_config_path,
    save_settings,
)
from pdd2uipath.core.exe import resolve_argv, which_cli
from pdd2uipath.core.state import STAGES

if TYPE_CHECKING:
    from pdd2uipath.tui.app import Pdd2UipathApp

Handler = Callable[["Pdd2UipathApp", list[str]], None]

# keys the generic /set accepts (dotted = nested section); everything else is rejected
SETTABLE_KEYS: dict[str, str] = {
    "executor": "auto | native | wsl-interop | wsl-bridge | wsl-manual",
    "workspace_root": "Windows path of the workspace (C:\\...)",
    "default_target_framework": "Windows | Portable",
    "expression_language": "VisualBasic | CSharp",
    "ui_capture": "stub | live",
    "approval_rule": "approval rule id (document-control-and-signoff)",
    "skills_root": "path of the uipath-rpa skill folder",
    "extraction.backend": "auto | openai-compatible | opencode | copilot | command | fixture",
    "extraction.model": "model id (openai-compatible) or provider/model (opencode) or id (copilot)",
    "extraction.base_url": "OpenAI-compatible endpoint, e.g. http://localhost:11434/v1",
    "extraction.api_key_env": "name of the env var holding the API key (never the key)",
    "extraction.max_tokens": "max output tokens for extraction",
    "agent.backend": "auto | opencode | copilot | fake",
    "agent.model": "github-copilot/<id> (opencode) or <id> (copilot); empty = default/auto",
    "agent.max_turns": "turn cap for the authoring session",
    "agent.timeout_s": "seconds before an authoring session is killed",
    "repair.max_rounds": "bounded repair rounds after a failed gate",
    "bridge.poll_interval_s": "seconds between bridge polls",
    "bridge.job_timeout_s": "seconds a single uip job may take",
    "packages.uia_min_version": "UIAutomation minimum version applied in live capture",
    "packages.prefer_prerelease": "true | false",
    "ui.theme": "dark | light",
    "ui.glyphs": "unicode | ascii",
}


def tokenize(text: str) -> list[str]:
    """Split a command line on whitespace; double quotes group words (no escapes, Windows paths stay intact)."""
    out: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            continue
        if ch.isspace() and not quoted:
            if cur:
                out.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def parse(text: str) -> tuple[str, list[str]] | None:
    """``"/agent copilot"`` -> ``("agent", ["copilot"])``; None when the text is not a command."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = tokenize(stripped[1:])
    if not parts:
        return ("help", [])
    return parts[0].lower(), parts[1:]


def get_setting(settings: Settings, key: str) -> Any:
    obj: Any = settings
    for part in key.split("."):
        obj = getattr(obj, part)
    return obj


def apply_setting(settings: Settings, key: str, raw: str) -> Settings:
    """Return a new validated Settings with ``key`` set from the typed ``raw`` value."""
    if key not in SETTABLE_KEYS:
        known = ", ".join(sorted(SETTABLE_KEYS))
        raise ValueError(f"unknown setting {key!r}; known: {known}")
    data = settings.model_dump(mode="json")
    node = data
    parts = key.split(".")
    for part in parts[:-1]:
        node = node[part]
    current = node[parts[-1]]
    value: Any
    lowered = raw.strip().lower()
    if isinstance(current, bool):
        if lowered not in ("true", "false", "yes", "no", "on", "off", "1", "0"):
            raise ValueError(f"{key} expects true/false")
        value = lowered in ("true", "yes", "on", "1")
    elif isinstance(current, int) and not isinstance(current, bool):
        value = int(raw)
    elif isinstance(current, float):
        value = float(raw)
    elif lowered in ("none", "null", "-", '""'):
        value = "" if isinstance(current, str) else None
    else:
        value = raw
    node[parts[-1]] = value
    new = Settings.model_validate(data)
    src = settings.__dict__.get("_config_path")
    if src is not None:
        new.__dict__["_config_path"] = src
    return new


@dataclass
class SlashCommand:
    name: str
    usage: str
    help: str
    handler: Handler
    aliases: list[str] = field(default_factory=list)


class SlashCommands:
    """Registry + dispatcher; one instance per app."""

    def __init__(self, app: Pdd2UipathApp) -> None:
        self.app = app
        self.commands: list[SlashCommand] = []
        self._register_all()

    # ------------------------------------------------------------- registry
    def add(
        self,
        name: str,
        usage: str,
        help_text: str,
        handler: Handler,
        aliases: list[str] | None = None,
    ) -> None:
        self.commands.append(SlashCommand(name, usage, help_text, handler, aliases or []))

    def find(self, name: str) -> SlashCommand | None:
        for c in self.commands:
            if c.name == name or name in c.aliases:
                return c
        return None

    def names(self) -> list[str]:
        return [f"/{c.name}" for c in self.commands]

    def help_text(self, name: str | None = None) -> str:
        if name:
            c = self.find(name.lstrip("/"))
            if c is None:
                return f"unknown command /{name}"
            alias = f" (alias: {', '.join('/' + a for a in c.aliases)})" if c.aliases else ""
            return f"/{c.name} {c.usage}{alias}\n\n{c.help}"
        width = max(len(f"/{c.name} {c.usage}") for c in self.commands)
        lines = [
            "Type a command in the prompt bar. Plain text (no slash) is queued as an instruction",
            "for the next authoring/repair round. Tab or Right accepts the inline suggestion.",
            "",
        ]
        for c in self.commands:
            lines.append(f"{('/' + c.name + ' ' + c.usage).ljust(width)}  {c.help.splitlines()[0]}")
        lines += ["", "Settings keys for /set:", ""]
        for k, d in SETTABLE_KEYS.items():
            lines.append(f"  {k.ljust(26)} {d}")
        return "\n".join(lines)

    # ------------------------------------------------------------- dispatch
    def run(self, text: str) -> bool:
        """Execute ``text`` if it is a slash command; returns False for plain instructions."""
        parsed = parse(text)
        if parsed is None:
            return False
        name, args = parsed
        cmd = self.find(name)
        if cmd is None:
            self.app.notify(f"unknown command /{name}; try /help", severity="warning")
            return True
        try:
            cmd.handler(self.app, args)
        except Exception as exc:  # never crash the UI on a typed command
            self.app.notify(f"/{name}: {exc}"[:300], severity="error", timeout=8)
        return True

    # ------------------------------------------------------------- commands
    def _register_all(self) -> None:
        self.add("help", "[command]", "List commands, or explain one.", _help, ["h", "?"])
        self.add(
            "open",
            "[pdd-path]",
            "Open a PDD (creates or resumes its run); no path opens the picker.",
            _open,
        )
        self.add(
            "run",
            "[stage]",
            f"Run all stages (pauses at the gates), or one stage: {', '.join(STAGES[1:])}.",
            _run,
        )
        self.add(
            "approve",
            "[your name]",
            "Approve the current plan (hash-bound) and continue; no name opens the modal.",
            _approve,
        )
        self.add(
            "resume", "", "wsl-manual: collect the pending Windows round and continue.", _resume
        )
        self.add(
            "status", "", "Show stage statuses, approval, pending round, repair round.", _status
        )
        self.add(
            "doctor", "", "Preflight checks (no Studio/network probes) in the detail view.", _doctor
        )
        self.add(
            "studio",
            "",
            "Open the generated project in Studio Desktop (uip rpa studio start).",
            _studio,
        )
        self.add("report", "", "Show report.md.", _show_file("report"))
        self.add("plan", "", "Show the rendered plan.", _show_file("plan"))
        self.add("spec", "", "Show the extracted ProcessSpec (spec.json).", _show_file("spec"))
        self.add("design", "", "Show the design decisions (design.json).", _show_file("design"))
        self.add(
            "logs",
            "[author|events|author-repair-N] [lines]",
            "Show the tail of a run log (default: author, 80 lines).",
            _logs,
        )
        self.add(
            "instruction",
            "<text>",
            "Queue an instruction for the next authoring/repair round (same as plain text).",
            _instruction,
            ["note"],
        )
        self.add(
            "config",
            "[save|path|show]",
            "Show the effective settings, save them, or show the file path.",
            _config,
        )
        self.add(
            "set",
            "<key> [value]",
            "Change a setting for this session and save it; without a value shows it. See keys below.",
            _set,
        )
        self.add("get", "<key>", "Show one setting.", _get)
        self.add(
            "agent",
            "[opencode|copilot|fake] [model]",
            "Show or set the authoring agent backend and model (saved).",
            _agent,
        )
        self.add(
            "model",
            "[id|none]",
            "Show or set the authoring model (saved). 'none' = backend default / auto.",
            _model,
        )
        self.add(
            "extraction",
            "[backend] [model]",
            "Show or set the extraction backend and model (saved).",
            _extraction,
        )
        self.add(
            "executor",
            "[auto|native|wsl-interop|wsl-bridge|wsl-manual]",
            "Show or set where uip runs (saved).",
            _executor,
        )
        self.add("rounds", "[n]", "Show or set the maximum repair rounds (saved).", _rounds)
        self.add(
            "ui-capture", "[stub|live]", "Show or set the UI capture mode (saved).", _ui_capture
        )
        self.add(
            "copilot",
            "login|version|models",
            "GitHub Copilot CLI: device-code login (output in the session log), version, model notes.",
            _copilot,
        )
        self.add(
            "opencode",
            "models [provider]|login|version",
            "OpenCode: list models it can use, login instructions, version.",
            _opencode,
        )
        self.add(
            "bridge",
            "install|status|stop",
            "wsl-bridge worker: generate, check heartbeat, stop.",
            _bridge,
        )
        self.add("theme", "", "Toggle dark / light.", lambda app, _a: app.action_toggle_theme())
        self.add(
            "inspector",
            "",
            "Show / hide the right pane.",
            lambda app, _a: app.action_toggle_inspector(),
        )
        self.add("clear", "", "Clear the session log.", _clear)
        self.add("quit", "", "Quit; the run state is persisted.", _quit, ["exit", "q"])


# ----------------------------------------------------------------- handlers
def _need_pipeline(app: Pdd2UipathApp) -> bool:
    if app.pipeline is None:
        app.notify("open a PDD first (/open or o)", severity="warning")
        return False
    return True


def _help(app: Pdd2UipathApp, args: list[str]) -> None:
    app.show_detail("slash commands", app.slash.help_text(args[0] if args else None))


def _open(app: Pdd2UipathApp, args: list[str]) -> None:
    if not args:
        app.action_open_pdd()
        return
    path = Path(" ".join(args)).expanduser()
    if not path.is_file():
        raise ValueError(f"no such file: {path}")
    app.open_pdd_path(path)


def _run(app: Pdd2UipathApp, args: list[str]) -> None:
    if not _need_pipeline(app):
        return
    if not args:
        app.action_run_all()
        return
    stage = args[0].lower()
    if stage not in STAGES[1:]:
        raise ValueError(f"unknown stage {stage!r}; one of {', '.join(STAGES[1:])}")
    app.action_run_stage(stage)


def _approve(app: Pdd2UipathApp, args: list[str]) -> None:
    if not _need_pipeline(app):
        return
    if not args:
        app.action_approve_plan()
        return
    app.approve_plan_as(" ".join(args))


def _resume(app: Pdd2UipathApp, args: list[str]) -> None:
    if not _need_pipeline(app):
        return
    assert app.pipeline is not None
    if not app.pipeline.state.pending_round:
        app.notify("no pending Windows round", severity="warning")
        return
    app.start_pipeline(app.pipeline.resume_round, "resume-round")


def _status(app: Pdd2UipathApp, args: list[str]) -> None:
    if not _need_pipeline(app):
        return
    p = app.pipeline
    assert p is not None
    st = p.state
    lines = [f"PDD: {p.pdd.name}", f"run dir: {p.paths.run_dir_windows}", ""]
    for name, status in st.statuses().items():
        rec = st.stage(name)
        extra = rec.error or rec.outputs.get("summary", "") if rec.outputs else rec.error
        lines.append(f"{name:9} {status:8} {extra or ''}")
    lines.append("")
    if st.approval:
        lines.append(
            f"approval: {st.approval.approver} at {st.approval.approved_at} (sha256 {st.approval.plan_sha256[:12]})"
        )
    else:
        lines.append("approval: none")
    lines.append(f"repair round: {st.repair_round} / {app.settings.repair.max_rounds}")
    lines.append(f"pending Windows round: {st.pending_round or 'none'}")
    lines.append(
        f"executor: {p.executor_kind.value}; agent: {app.settings.agent.backend} {app.settings.agent.model or '(default)'}"
    )
    lines.append(f"agent sessions with notes: {len(st.agent_notes)}")
    app.show_detail("status", "\n".join(lines))


def _doctor(app: Pdd2UipathApp, args: list[str]) -> None:
    app.action_doctor()


def _studio(app: Pdd2UipathApp, args: list[str]) -> None:
    if _need_pipeline(app):
        app.action_open_studio()


def _show_file(which: str) -> Handler:
    def handler(app: Pdd2UipathApp, args: list[str]) -> None:
        if not _need_pipeline(app):
            return
        p = app.pipeline
        assert p is not None
        paths = {
            "report": p.paths.report,
            "plan": p.paths.plan,
            "spec": p.paths.spec,
            "design": p.paths.design,
        }
        path = paths[which]
        if not path.is_file():
            app.notify(f"no {path.name} yet", severity="warning")
            return
        app.show_detail(path.name, path.read_text(encoding="utf-8"))

    return handler


def _logs(app: Pdd2UipathApp, args: list[str]) -> None:
    if not _need_pipeline(app):
        return
    p = app.pipeline
    assert p is not None
    name = args[0] if args else "author"
    n = int(args[1]) if len(args) > 1 else 80
    log_dir = p.paths.run_dir / "logs"
    path = log_dir / (name if name.endswith(".jsonl") else f"{name}.jsonl")
    if not path.is_file():
        available = (
            ", ".join(sorted(f.stem for f in log_dir.glob("*.jsonl")))
            if log_dir.is_dir()
            else "none"
        )
        raise ValueError(f"no log {path.name}; available: {available}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    app.show_detail(
        f"{path.name} (last {min(n, len(lines))} of {len(lines)} lines)", "\n".join(lines[-n:])
    )


def _instruction(app: Pdd2UipathApp, args: list[str]) -> None:
    if not _need_pipeline(app):
        return
    assert app.pipeline is not None
    app.pipeline.extra_instruction = " ".join(args)
    app.notify("instruction queued for the next authoring/repair round")


def _config(app: Pdd2UipathApp, args: list[str]) -> None:
    sub = args[0].lower() if args else "show"
    if sub == "path":
        app.notify(f"config file: {app.config_target()}", timeout=8)
    elif sub == "save":
        path = app.save_config()
        app.notify(f"saved {path}", timeout=6)
    elif sub == "show":
        import tomli_w

        from pdd2uipath.core.config import _drop_none

        text = tomli_w.dumps(_drop_none(app.settings.model_dump(mode="json")))
        app.show_detail(f"settings ({app.config_target()})", text)
    else:
        raise ValueError("usage: /config [show|save|path]")


def _set(app: Pdd2UipathApp, args: list[str]) -> None:
    if not args:
        raise ValueError("usage: /set <key> [value]")
    key = args[0]
    if len(args) == 1:
        _get(app, [key])
        return
    app.update_setting(key, " ".join(args[1:]))


def _get(app: Pdd2UipathApp, args: list[str]) -> None:
    if not args:
        raise ValueError("usage: /get <key>")
    key = args[0]
    if key not in SETTABLE_KEYS:
        raise ValueError(f"unknown setting {key!r}; see /help")
    value = get_setting(app.settings, key)
    app.notify(f"{key} = {value!r}  ({SETTABLE_KEYS[key]})", timeout=8)


def _agent(app: Pdd2UipathApp, args: list[str]) -> None:
    if not args:
        a = app.settings.agent
        app.notify(f"agent: {a.backend}, model: {a.model or '(default / auto)'}", timeout=8)
        return
    app.update_setting("agent.backend", args[0], quiet=True)
    if len(args) > 1:
        app.update_setting("agent.model", args[1], quiet=True)
    app.notify(
        f"agent: {app.settings.agent.backend}, model: {app.settings.agent.model or '(default / auto)'} (saved)",
        timeout=8,
    )


def _model(app: Pdd2UipathApp, args: list[str]) -> None:
    if not args:
        app.notify(f"agent.model = {app.settings.agent.model or '(default / auto)'}", timeout=8)
        return
    app.update_setting("agent.model", " ".join(args))


def _extraction(app: Pdd2UipathApp, args: list[str]) -> None:
    if not args:
        e = app.settings.extraction
        app.notify(
            f"extraction: {e.backend}, model: {e.model or '(default)'}, base_url: {e.base_url or '-'}",
            timeout=8,
        )
        return
    app.update_setting("extraction.backend", args[0], quiet=True)
    if len(args) > 1:
        app.update_setting("extraction.model", args[1], quiet=True)
    e = app.settings.extraction
    app.notify(f"extraction: {e.backend}, model: {e.model or '(default)'} (saved)", timeout=8)


def _executor(app: Pdd2UipathApp, args: list[str]) -> None:
    if not args:
        app.notify(f"executor = {app.settings.executor.value}", timeout=6)
        return
    kinds = [k.value for k in ExecutorKind]
    if args[0] not in kinds:
        raise ValueError(f"executor must be one of {', '.join(kinds)}")
    app.update_setting("executor", args[0])


def _rounds(app: Pdd2UipathApp, args: list[str]) -> None:
    if not args:
        app.notify(f"repair.max_rounds = {app.settings.repair.max_rounds}", timeout=6)
        return
    app.update_setting("repair.max_rounds", args[0])


def _ui_capture(app: Pdd2UipathApp, args: list[str]) -> None:
    if not args:
        app.notify(f"ui_capture = {app.settings.ui_capture}", timeout=6)
        return
    if args[0] not in ("stub", "live"):
        raise ValueError("ui_capture must be stub or live")
    app.update_setting("ui_capture", args[0])


def _copilot(app: Pdd2UipathApp, args: list[str]) -> None:
    sub = args[0].lower() if args else "version"
    exe = which_cli("copilot")
    if exe is None:
        raise ValueError("copilot CLI not on PATH (npm i -g @github/copilot)")
    if sub == "version":
        out = _capture(["copilot", "--version"])
        app.notify(out.splitlines()[0] if out else "copilot: no output", timeout=8)
    elif sub == "login":
        app.run_streaming(
            ["copilot", "login", "--device-code"],
            label="copilot login",
            hint="Open the URL shown in the session log and enter the code; the command waits for you.",
        )
    elif sub == "models":
        app.show_detail(
            "Copilot CLI models",
            "The Copilot CLI accepts only the models enabled in your plan's model picker.\n"
            "Copilot Free: no picker models, use model 'none' (= auto).\n"
            "Enterprise/Pro seats: use the ids shown by /model in an interactive `copilot` session,\n"
            "e.g. /model gpt-6.1-sol. A wrong id fails fast with 'is not available' (no request spent).\n\n"
            f"current agent model: {app.settings.agent.model or '(auto)'}",
        )
    else:
        raise ValueError("usage: /copilot login|version|models")


def _opencode(app: Pdd2UipathApp, args: list[str]) -> None:
    sub = args[0].lower() if args else "models"
    if which_cli("opencode") is None:
        raise ValueError("opencode CLI not on PATH (npm i -g opencode-ai)")
    if sub == "models":
        argv = ["opencode", "models", *args[1:2]]
        app.run_capture(argv, title="opencode models" + (f" {args[1]}" if len(args) > 1 else ""))
    elif sub == "version":
        out = _capture(["opencode", "--version"])
        app.notify(
            out.strip().splitlines()[-1] if out.strip() else "opencode: no output", timeout=8
        )
    elif sub == "login":
        app.show_detail(
            "OpenCode login",
            "OpenCode's login is interactive. In a terminal run:\n\n    opencode auth login\n\n"
            "pick the provider (GitHub Copilot: github.com or enterprise URL; local LLM: configure\n"
            "the provider in ~/.config/opencode/opencode.json), then /opencode models here.",
        )
    else:
        raise ValueError("usage: /opencode models [provider]|login|version")


def _bridge(app: Pdd2UipathApp, args: list[str]) -> None:
    sub = args[0].lower() if args else "status"
    if sub == "install":
        app.action_install_bridge()
    elif sub == "stop":
        app.action_restart_bridge()
    elif sub == "status":
        from pdd2uipath.doctor.bridge import bridge_status

        app.notify(str(bridge_status(app.settings, app.platform))[:300], timeout=8)
    else:
        raise ValueError("usage: /bridge install|status|stop")


def _clear(app: Pdd2UipathApp, args: list[str]) -> None:
    app.clear_session_log()


def _quit(app: Pdd2UipathApp, args: list[str]) -> None:
    app.closing = True
    app.exit()


def _capture(argv: list[str], timeout: float = 60.0) -> str:
    proc = subprocess.run(
        resolve_argv(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )
    return (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")


def config_target_path(settings: Settings, explicit: Path | None) -> Path:
    """Where /set and /config save: the loaded file, else the user config path."""
    if explicit is not None:
        return explicit
    src = settings.__dict__.get("_config_path")
    return src if isinstance(src, Path) else default_user_config_path()


__all__ = [
    "SETTABLE_KEYS",
    "SlashCommand",
    "SlashCommands",
    "apply_setting",
    "config_target_path",
    "get_setting",
    "parse",
    "save_settings",
    "tokenize",
]

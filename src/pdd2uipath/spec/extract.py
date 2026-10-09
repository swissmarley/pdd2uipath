"""Stage 2: turn a parsed PDD into a ``ProcessSpec`` with a strict schema.

Deterministic table facts come first; the model fills only what parsing cannot, and
every field must cite a document anchor. No Anthropic dependency: backends are

* ``openai-compatible`` - any OpenAI-style ``/chat/completions`` endpoint (local LLM via
  Ollama / vLLM / LM Studio / OpenCode's provider), stdlib HTTP only
* ``opencode``          - ``opencode run --format json`` (local LLM configured in OpenCode)
* ``copilot``           - ``copilot -p ... -s`` (GitHub Copilot CLI, non-interactive)
* ``fixture``           - JSON files on disk (tests, offline replays)
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from pdd2uipath.core.config import Settings
from pdd2uipath.core.events import EventBus
from pdd2uipath.core.exe import resolve_argv, which_cli
from pdd2uipath.core.state import slugify
from pdd2uipath.ingest.models import Document
from pdd2uipath.spec.models import (
    ExtractionInfo,
    OpenQuestion,
    ProcessSpec,
    ProcessSpecDraft,
    Step,
    strict_json_schema,
)
from pdd2uipath.spec.tables import DeterministicFacts, extract_facts

SYSTEM_PROMPT = """You extract a Process Definition Document (PDD) into a strict JSON ProcessSpec for an RPA developer.

Rules:
- Use ONLY what the PDD states. Never invent values, URLs, asset names, screen details or rules.
- Every object carries `source_ref`: copy the anchor in square brackets that precedes the text you used (e.g. "§4.3/S06" or "p.3 §4.2"). Steps use the detailed-step anchor when one exists.
- DETERMINISTIC FACTS below were parsed from the PDD's tables and are authoritative: keep every step_id, rule ID, exception ID and application exactly as listed; add detail, never contradict.
- Anything unstated, "TBD", "to be provided/clarified/defined", "[to be completed]" or contradictory goes to `open_questions` with a precise `text`, the PDD anchor, and `blocking=true` when the design cannot proceed without it (unknown target application/URL/screen for a UI step, undefined control flow or exception handling for an in-scope step, undefined template/framework or runtime environment). Also copy the PDD's own "Open points" with blocking judged the same way.
- `is_ui_interaction` is true when the robot operates an application's user interface (desktop app, SAP GUI, browser, Outlook desktop client UI, Excel UI). It is false for Orchestrator assets/queues, file system, pure calculations/validation, PDF text extraction, and email sent through mail activities.
- `applications[].credential_asset_names` contains Orchestrator asset NAMES only (e.g. "AP_SAP_Credential"), never secrets.
- `applications[].platform_hints` copies platform words verbatim ("Windows", "SAP GUI for Windows", "Linux", "serverless", "macro-enabled", "Microsoft 365 Apps", "Web", ...).
- `robot_environment.os_hints` copies the stated robot environment words verbatim.
- Keep texts short and factual; lists in document order.
- Output ONLY one JSON object that validates against the JSON schema given below. No prose, no markdown fences.
"""


class ExtractionError(RuntimeError):
    """Extraction failed after the allowed retry."""


class ExtractionBackend(Protocol):
    name: str
    model: str

    def extract(
        self, system: str, prompt: str, schema: dict[str, Any], context: dict[str, str]
    ) -> dict[str, Any]: ...


def build_prompt(doc: Document, facts: DeterministicFacts) -> str:
    return (
        "DETERMINISTIC FACTS (authoritative):\n"
        f"{facts.render()}\n\n"
        "PDD TEXT WITH ANCHORS:\n"
        f"{doc.text_with_refs()}\n\n"
        "Produce the ProcessSpec JSON now."
    )


def _loads_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ExtractionError("the model returned no JSON object")
    try:
        obj = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ExtractionError(f"the model returned invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ExtractionError("the model returned a non-object JSON value")
    return obj


def _schema_block(schema: dict[str, Any]) -> str:
    return "JSON SCHEMA (the output must validate against it):\n" + json.dumps(
        schema, ensure_ascii=False
    )


# ---------------------------------------------------------------- backends


class OpenAICompatibleBackend:
    """OpenAI-style chat completions (local LLM). Honours HTTPS_PROXY/NO_PROXY and
    SSL_CERT_FILE/REQUESTS_CA_BUNDLE through the standard library."""

    name = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key_env: str = "OPENAI_API_KEY",
        timeout_s: float = 1800.0,
        max_tokens: int = 32000,
        use_response_format: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.use_response_format = use_response_format

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        if bundle and Path(bundle).is_file():
            ctx.load_verify_locations(bundle)
        return ctx

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = os.environ.get(self.api_key_env, "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout_s, context=self._ssl_context()
            ) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise ExtractionError(f"{self.base_url}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExtractionError(f"{self.base_url}: {exc}") from exc
        try:
            obj = json.loads(body)
        except ValueError as exc:
            raise ExtractionError(f"non-JSON response from {self.base_url}: {body[:300]}") from exc
        if not isinstance(obj, dict):
            raise ExtractionError("unexpected response shape")
        return obj

    def extract(
        self, system: str, prompt: str, schema: dict[str, Any], context: dict[str, str]
    ) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": system + "\n" + _schema_block(schema)},
            {"role": "user", "content": prompt},
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        if self.use_response_format:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "ProcessSpec", "schema": schema, "strict": True},
            }
        try:
            obj = self._post(payload)
        except ExtractionError as exc:
            if self.use_response_format and "HTTP 4" in str(exc):
                payload.pop("response_format", None)
                payload["response_format"] = {"type": "json_object"}
                obj = self._post(payload)
            else:
                raise
        choices = obj.get("choices") or []
        if not choices:
            raise ExtractionError(f"no choices in response: {json.dumps(obj)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise ExtractionError("empty completion")
        return _loads_json(content)


class CommandBackend:
    """Run a CLI coding agent non-interactively and read the JSON it prints.

    The prompt is written to a temp file and referenced from the command line so very
    long PDDs do not hit argument-length limits; the agent is told to answer with the
    JSON object only.
    """

    name = "command"

    def __init__(
        self,
        command: Sequence[str],
        *,
        name: str,
        model: str = "",
        timeout_s: float = 1800.0,
        cwd: Path | None = None,
    ) -> None:
        self.command = list(command)
        self.name = name
        self.model = model
        self.timeout_s = timeout_s
        self.cwd = cwd

    def _render(self, prompt_file: Path, inline_prompt: str) -> list[str]:
        out: list[str] = []
        for tok in self.command:
            out.append(
                tok.replace("{prompt_file}", str(prompt_file))
                .replace("{prompt}", inline_prompt)
                .replace("{model}", self.model)
            )
        return out

    def extract(
        self, system: str, prompt: str, schema: dict[str, Any], context: dict[str, str]
    ) -> dict[str, Any]:
        import tempfile

        full = system + "\n" + _schema_block(schema) + "\n\n" + prompt
        with tempfile.TemporaryDirectory(prefix="pdd2uipath-extract-") as tmp:
            prompt_file = Path(tmp) / "prompt.md"
            prompt_file.write_text(full, encoding="utf-8")
            inline = (
                f"Read the file {prompt_file} in full. It contains extraction instructions, a JSON schema and a PDD. "
                "Reply with the single JSON object only - no prose, no markdown fences, no tool calls other than reading that file."
            )
            argv = resolve_argv(self._render(prompt_file, inline))
            try:
                proc = subprocess.run(
                    argv,
                    cwd=str(self.cwd) if self.cwd else None,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_s,
                    shell=False,
                )
            except FileNotFoundError as exc:
                raise ExtractionError(f"{self.name} CLI not found: {argv[0]} ({exc})") from exc
            except subprocess.TimeoutExpired as exc:
                raise ExtractionError(f"{self.name} timed out after {self.timeout_s:.0f}s") from exc
        if proc.returncode != 0 and not proc.stdout.strip():
            raise ExtractionError(
                f"{self.name} exited {proc.returncode}: {proc.stderr.strip()[-2000:]}"
            )
        return _loads_json(_final_text(proc.stdout))


def _final_text(stdout: str) -> str:
    """Return the agent's final answer from plain or JSONL output."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if lines and all(line.lstrip().startswith("{") for line in lines[:3]):
        texts: list[str] = []
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            for key in ("result", "text", "content", "message"):
                v = ev.get(key)
                if isinstance(v, str) and "{" in v:
                    texts.append(v)
            part = ev.get("part") if isinstance(ev.get("part"), dict) else None
            if part and isinstance(part.get("text"), str):
                texts.append(part["text"])
        if texts:
            return texts[-1]
    return stdout


class FixtureBackend:
    """Replay a saved extraction from ``<fixture_dir>/<document-id-slug>.json``."""

    name = "fixture"
    model = ""

    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = fixture_dir

    def extract(
        self, system: str, prompt: str, schema: dict[str, Any], context: dict[str, str]
    ) -> dict[str, Any]:
        candidates = [
            self.fixture_dir / f"{slugify(context.get('document_id', ''))}.json",
            self.fixture_dir / f"{context.get('sha256', '')}.json",
        ]
        for c in candidates:
            if c.is_file():
                data = json.loads(c.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ExtractionError(f"fixture {c} is not a JSON object")
                return data
        raise ExtractionError(
            f"no extraction fixture for {context.get('document_id')!r} in {self.fixture_dir}"
        )


def opencode_extract_command(model: str) -> list[str]:
    cmd = ["opencode", "run", "--format", "json"]
    if model:
        cmd += ["--model", model]
    return [*cmd, "{prompt}"]


def copilot_extract_command(model: str) -> list[str]:
    cmd = ["copilot", "-p", "{prompt}", "-s", "--allow-all-tools", "--allow-all-paths"]
    if model:
        cmd += ["--model", model]
    return cmd


def choose_backend(settings: Settings) -> ExtractionBackend:
    ex = settings.extraction
    kind = ex.backend
    if kind == "auto":
        if ex.base_url:
            kind = "openai-compatible"
        elif _which("opencode"):
            kind = "opencode"
        elif _which("copilot"):
            kind = "copilot"
        else:
            raise ExtractionError(
                "no extraction backend available: configure [extraction] base_url for an OpenAI-compatible "
                "local LLM, or install the opencode / copilot CLI (see docs/vdi-setup.md)"
            )
    if kind == "fixture":
        return FixtureBackend(Path(ex.fixture_dir or "tests/fixtures/extraction"))
    if kind == "openai-compatible":
        if not ex.base_url:
            raise ExtractionError(
                "[extraction] base_url is required for the openai-compatible backend"
            )
        return OpenAICompatibleBackend(
            ex.base_url,
            ex.model,
            api_key_env=ex.api_key_env,
            max_tokens=ex.max_tokens,
            use_response_format=ex.use_response_format,
        )
    if kind == "opencode":
        return CommandBackend(
            ex.command or opencode_extract_command(ex.model), name="opencode", model=ex.model
        )
    if kind == "copilot":
        return CommandBackend(
            ex.command or copilot_extract_command(ex.model), name="copilot", model=ex.model
        )
    if kind == "command":
        if not ex.command:
            raise ExtractionError("[extraction] command is required for the command backend")
        return CommandBackend(ex.command, name="command", model=ex.model)
    raise ExtractionError(f"unknown extraction backend {kind!r}")


def _which(name: str) -> str | None:
    return which_cli(name)


# ---------------------------------------------------------------- merge + validation

_UI_APP_HINTS = (
    "sap",
    "gui",
    "browser",
    "web",
    "edge",
    "chrome",
    "outlook",
    "crm",
    "desktop",
    "screen",
    "system",
)
_UI_VERBS = (
    "click",
    "enter ",
    "log in",
    "login",
    "press ",
    "select ",
    "open ",
    "navigate",
    "read from the",
    "type",
)


def _merge_facts(
    draft: ProcessSpecDraft, facts: DeterministicFacts, warnings: list[str]
) -> ProcessSpecDraft:
    by_id = {s.step_id: s for s in draft.steps}
    merged: list[Step] = []
    for fs in facts.steps:
        s = by_id.pop(fs.step_id, None)
        if s is None:
            warnings.append(f"model omitted step {fs.step_id}; added from the summary table")
            action_l = fs.action.lower()
            ui = any(v in action_l for v in _UI_VERBS) and any(
                h in fs.application.lower() for h in _UI_APP_HINTS
            )
            s = Step(
                step_id=fs.step_id,
                name=fs.action[:60],
                part=fs.part,
                application=fs.application,
                action=fs.action,
                business_rules=fs.business_rules,
                exceptions=fs.exceptions,
                is_ui_interaction=ui,
                ui_interaction_rationale="heuristic (model omitted the step)",
                source_ref=fs.source_ref,
            )
        else:
            s = s.model_copy(
                update={
                    "part": s.part or fs.part,
                    "application": s.application or fs.application,
                    "action": s.action or fs.action,
                    "business_rules": sorted(set(s.business_rules) | set(fs.business_rules)),
                    "exceptions": sorted(set(s.exceptions) | set(fs.exceptions)),
                }
            )
        merged.append(s)
    for extra in by_id.values():
        warnings.append(
            f"model produced step {extra.step_id} that is not in the summary table; kept"
        )
        merged.append(extra)
    merged.sort(key=lambda s: s.step_id)
    known_q = {q.text.strip().lower() for q in draft.open_questions}
    questions = list(draft.open_questions)
    for op in facts.open_points:
        if op.text.strip().lower() not in known_q and not any(
            op.text.lower()[:40] in q.text.lower() for q in questions
        ):
            questions.append(
                OpenQuestion(
                    id=op.id,
                    text=op.text,
                    owner=op.owner,
                    blocking=False,
                    reason="PDD open point (not classified by the model)",
                    source_ref=op.source_ref,
                )
            )
            warnings.append(f"open point {op.id} added from the PDD table")
    return draft.model_copy(update={"steps": merged, "open_questions": questions})


def extract_spec(
    doc: Document,
    settings: Settings,
    backend: ExtractionBackend | None = None,
    *,
    bus: EventBus | None = None,
) -> ProcessSpec:
    backend = backend or choose_backend(settings)
    facts = extract_facts(doc)
    schema = strict_json_schema(ProcessSpecDraft)
    prompt = build_prompt(doc, facts)
    context = {
        "document_id": facts.document_control.get("Document ID", doc.title),
        "sha256": doc.sha256,
    }
    warnings: list[str] = []
    if bus:
        bus.log(
            f"extracting ProcessSpec with backend={backend.name} ({len(prompt)} chars of PDD text)"
        )
    last_error: str | None = None
    draft: ProcessSpecDraft | None = None
    for attempt in (1, 2):
        user_prompt = (
            prompt
            if last_error is None
            else prompt
            + "\n\nYour previous output failed schema validation:\n"
            + last_error
            + "\nFix it and output the full JSON again."
        )
        try:
            raw = backend.extract(SYSTEM_PROMPT, user_prompt, schema, context)
            draft = ProcessSpecDraft.model_validate(raw)
            break
        except ValidationError as exc:
            last_error = str(exc)[:4000]
            if bus:
                bus.log(f"extraction attempt {attempt}: schema validation failed", level="warning")
        except ExtractionError as exc:
            last_error = str(exc)
            if bus:
                bus.log(f"extraction attempt {attempt} failed: {exc}", level="warning")
    if draft is None:
        raise ExtractionError(f"extraction failed twice; last error: {last_error}")
    merged = _merge_facts(draft, facts, warnings)
    return ProcessSpec(
        **merged.model_dump(),
        extraction=ExtractionInfo(
            backend=backend.name,
            model=backend.model,
            deterministic_fields=facts.fields_found(),
            warnings=warnings,
            pdd_sha256=doc.sha256,
        ),
    )

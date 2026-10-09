"""Pipeline orchestration: the nine stages, their gates, and resumable state.

The TUI and the plain CLI are thin views over this module: both subscribe to the same
``EventBus`` and call the same ``Pipeline`` methods. Gates (plan approval, blocking open
questions, Studio locks, manual Windows rounds) are delegated to a ``GateHandler``.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pdd2uipath.agents.base import AgentError, AuthoringAgent, AuthoringResult, AuthoringSession
from pdd2uipath.agents.command_agent import build_agent
from pdd2uipath.core.config import ExecutorKind, Settings, settings_source_path
from pdd2uipath.core.events import (
    EventBus,
    GateResolved,
    GateWaiting,
    JsonlSink,
    RepairRound,
    RunStateChanged,
    StageFinished,
    StageStarted,
)
from pdd2uipath.core.paths import brain_path, to_brain, to_windows, windows_join
from pdd2uipath.core.platform import Platform, detect_platform
from pdd2uipath.core.state import (
    RunState,
    RunStore,
    atomic_write_text,
    hash_inputs,
    sha256_file,
    slugify,
)
from pdd2uipath.design.models import DesignResult
from pdd2uipath.design.rules import design as design_rules
from pdd2uipath.executors.base import Executor, ExecutorError, Job
from pdd2uipath.executors.factory import build_executor, recommend_executor
from pdd2uipath.executors.shim import install_shim
from pdd2uipath.executors.surface import SNAPSHOT, UipCommands, load_surface
from pdd2uipath.executors.wsl_manual import (
    PlainRoundHandler,
    RoundAborted,
    RoundHandler,
    RoundPending,
)
from pdd2uipath.ingest.approval import ApprovalVerdict, get_rule
from pdd2uipath.ingest.models import Document
from pdd2uipath.ingest.parse import parse_document
from pdd2uipath.plan.render import PlanContext, approve, check_approval, render_plan, write_plan
from pdd2uipath.report.render import ReportContext, render_report, write_outputs
from pdd2uipath.scaffold.scaffold import git_commit_all, run_scaffold
from pdd2uipath.spec.extract import ExtractionBackend, choose_backend, extract_spec
from pdd2uipath.spec.models import ProcessSpec
from pdd2uipath.verify.checks import AuditResult, audit
from pdd2uipath.verify.diagnostics import GateVerdict, format_for_agent
from pdd2uipath.verify.gate import GateResult, check_locks, run_gate


class PipelineStop(RuntimeError):
    """A stage stopped on purpose (approval gate, blocking questions, contradiction)."""

    def __init__(
        self,
        message: str,
        *,
        status: Literal["stopped", "waiting", "failed"] = "stopped",
        waiting_for: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.waiting_for = waiting_for


class GateHandler(Protocol):
    def approve_plan(self, plan_text: str, plan_sha: str, info: dict[str, Any]) -> str | None: ...

    def open_questions(
        self, questions: list[dict[str, Any]], stop_reasons: list[str]
    ) -> Literal["abort", "restart"]: ...

    def studio_lock(self, message: str) -> Literal["retry", "abort"]: ...

    def round_handler(self) -> RoundHandler: ...


class PlainGateHandler:
    """Non-interactive gates for the scriptable CLI (prompts only on a TTY when allowed)."""

    def __init__(
        self,
        *,
        approver: str | None = None,
        interactive: bool = False,
        wait_for_rounds: bool = True,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.approver = approver
        self.interactive = interactive
        self.wait_for_rounds = wait_for_rounds
        self.echo = echo

    def approve_plan(self, plan_text: str, plan_sha: str, info: dict[str, Any]) -> str | None:
        if self.approver:
            return self.approver
        if self.interactive:
            self.echo(plan_text)
            self.echo(
                f"\nPlan sha256 {plan_sha[:16]}; Task 0 installs: {', '.join(info.get('installs', [])) or 'none'}"
            )
            name = input(
                "Approve this plan? Type your name to approve, or leave empty to stop: "
            ).strip()
            return name or None
        return None

    def open_questions(
        self, questions: list[dict[str, Any]], stop_reasons: list[str]
    ) -> Literal["abort", "restart"]:
        for q in questions:
            self.echo(f"  [BLOCKING] {q.get('id')} ({q.get('source_ref')}): {q.get('text')}")
        return "abort"

    def studio_lock(self, message: str) -> Literal["retry", "abort"]:
        if self.interactive:
            ans = input(f"{message}\nRetry? [y/N] ").strip().lower()
            return "retry" if ans.startswith("y") else "abort"
        return "abort"

    def round_handler(self) -> RoundHandler:
        return PlainRoundHandler(wait=self.wait_for_rounds, echo=self.echo)


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    run_dir_windows: str
    slug: str

    @property
    def meta(self) -> Path:
        return self.run_dir / ".pdd2uipath"

    @property
    def logs(self) -> Path:
        return self.run_dir / "logs"

    @property
    def document(self) -> Path:
        return self.run_dir / "document.json"

    @property
    def verdict(self) -> Path:
        return self.run_dir / "approval-verdict.json"

    @property
    def spec(self) -> Path:
        return self.run_dir / "spec.json"

    @property
    def design(self) -> Path:
        return self.run_dir / "design.json"

    @property
    def plan(self) -> Path:
        return self.run_dir / "docs" / "plans" / f"{self.slug}.md"

    @property
    def plan_rel(self) -> str:
        return f"docs/plans/{self.slug}.md"

    @property
    def approval(self) -> Path:
        return self.run_dir / "approval.json"

    @property
    def report(self) -> Path:
        return self.run_dir / "report.md"

    @property
    def traceability(self) -> Path:
        return self.run_dir / "traceability.csv"

    def gate_json(self, round_no: int) -> Path:
        return self.run_dir / f"gate-{round_no}.json"


def derive_slug(doc: Document) -> str:
    found = doc.lookup_key("Document ID")
    base = found[0] if found else doc.title
    return slugify(base)


def quick_slug(pdd: Path) -> str:
    """Slug without a full parse (used to locate an existing run directory)."""
    doc = parse_document(pdd)
    return derive_slug(doc)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        pdd: Path | None = None,
        run_dir: Path | None = None,
        bus: EventBus | None = None,
        platform: Platform | None = None,
        gates: GateHandler | None = None,
        executor: Executor | None = None,
        extraction_backend: ExtractionBackend | None = None,
        agent: AuthoringAgent | None = None,
        commands: UipCommands | None = None,
    ) -> None:
        self.settings = settings
        self.platform = platform or detect_platform()
        self.bus = bus or EventBus()
        self.gates: GateHandler = gates or PlainGateHandler()
        self._executor = executor
        self._extraction_backend = extraction_backend
        self._agent = agent
        self._commands = commands
        self.document: Document | None = None
        self.spec: ProcessSpec | None = None
        self.design: DesignResult | None = None
        self.last_gate: GateResult | None = None
        self.last_audit: AuditResult | None = None
        self.extra_instruction: str = ""
        if run_dir is None and pdd is None:
            raise ValueError("Pipeline needs a PDD path or an existing run directory")
        if run_dir is not None:
            self.paths = RunPaths(
                run_dir=run_dir,
                run_dir_windows=to_windows(run_dir, self.platform),
                slug=run_dir.name,
            )
            self.store = RunStore(run_dir)
            if not self.store.exists():
                raise FileNotFoundError(f"no pdd2uipath run at {run_dir}")
            self.state = self.store.load()
            self.pdd = Path(self.state.pdd_path)
        else:
            assert pdd is not None
            self.pdd = pdd.resolve()
            slug = quick_slug(self.pdd)
            root_w = to_windows(settings.workspace_root, self.platform)
            run_w = windows_join(root_w, slug)
            run_dir = to_brain(run_w, self.platform)
            self.paths = RunPaths(run_dir=run_dir, run_dir_windows=run_w, slug=slug)
            self.store = RunStore(run_dir)
            if self.store.exists():
                self.state = self.store.load()
                if self.state.pdd_sha256 != sha256_file(self.pdd):
                    self.bus.log(
                        "the PDD changed since this run was created; stages will be re-run",
                        level="warning",
                    )
            else:
                self.state = RunState(
                    run_id=uuid.uuid4().hex[:12],
                    slug=slug,
                    pdd_path=str(self.pdd),
                    pdd_sha256=sha256_file(self.pdd),
                    project_name="",
                    run_dir_windows=run_w,
                )
                self.paths.meta.mkdir(parents=True, exist_ok=True)
                self.store.save(self.state)
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        self.bus.subscribe(JsonlSink(self.paths.logs / "events.jsonl"))
        self._load_artifacts()
        self._publish_state()

    # ------------------------------------------------------------ plumbing
    @property
    def executor_kind(self) -> ExecutorKind:
        kind = self.settings.executor
        return recommend_executor(self.platform) if kind == ExecutorKind.AUTO else kind

    @property
    def manual_mode(self) -> bool:
        return self.executor_kind == ExecutorKind.WSL_MANUAL

    def executor(self) -> Executor:
        if self._executor is None:
            self._executor = build_executor(
                self.settings,
                self.platform,
                bus=self.bus,
                round_handler=self.gates.round_handler(),
                kind=self.executor_kind,
                pending_round=self.state.pending_round,
            )
            self.state.executor = self.executor_kind.value
        return self._executor

    def commands(self) -> UipCommands:
        if self._commands is None:
            cfg = settings_source_path(self.settings)
            surface = load_surface(cfg.parent / "cli-surface.json") if cfg else None
            self._commands = UipCommands(surface or SNAPSHOT, strict=surface is not None)
        return self._commands

    def extraction_backend(self) -> ExtractionBackend:
        if self._extraction_backend is None:
            self._extraction_backend = choose_backend(self.settings)
        return self._extraction_backend

    def agent(self) -> AuthoringAgent:
        if self._agent is None:
            a = self.settings.agent
            script: Path | None = None
            if a.fake_script:
                script = Path(a.fake_script)
                if not script.is_absolute():
                    cfg = settings_source_path(self.settings)
                    base = cfg.parent if cfg else Path.cwd()
                    script = (base / script).resolve()
            self._agent = build_agent(
                a.backend,
                command=a.command or None,
                model=a.model,
                fake_script=script,
                deny_tools=a.deny_tools,
            )
        return self._agent

    @property
    def project_dir(self) -> Path:
        if not self.state.project_dir_windows:
            raise PipelineStop("no project yet: run scaffold first", status="failed")
        return to_brain(self.state.project_dir_windows, self.platform)

    def _publish_state(self) -> None:
        self.bus.publish(
            RunStateChanged(stages=self.state.statuses(), repair_round=self.state.repair_round)
        )

    def _load_artifacts(self) -> None:
        if self.paths.document.is_file():
            self.document = Document.model_validate_json(
                self.paths.document.read_text(encoding="utf-8")
            )
        if self.paths.spec.is_file():
            self.spec = ProcessSpec.model_validate_json(self.paths.spec.read_text(encoding="utf-8"))
        if self.paths.design.is_file():
            self.design = DesignResult.model_validate_json(
                self.paths.design.read_text(encoding="utf-8")
            )

    def _check_pdd(self) -> None:
        current = sha256_file(self.pdd)
        if current != self.state.pdd_sha256:
            raise PipelineStop(
                f"the PDD changed since ingest (sha256 {self.state.pdd_sha256[:12]} -> {current[:12]}); re-run ingest",
                status="failed",
            )

    def _run_stage(
        self, name: str, input_hash: str, body: Callable[[], dict[str, Any]], *, force: bool = False
    ) -> bool:
        rec = self.state.stage(name)
        if rec.status == "done" and rec.input_hash == input_hash and not force:
            self.bus.publish(StageFinished(stage=name, status="skipped", detail="inputs unchanged"))
            return True
        rec.status = "running"
        rec.started = datetime.now(UTC)
        rec.finished = None
        rec.error = None
        rec.waiting_for = None
        rec.attempts += 1
        self.store.save(self.state)
        self.bus.publish(StageStarted(stage=name))
        self._publish_state()
        t0 = time.monotonic()
        try:
            outputs = body()
        except PipelineStop as stop:
            rec.status = stop.status
            rec.waiting_for = stop.waiting_for
            rec.error = str(stop)
            rec.finished = datetime.now(UTC)
            self.store.save(self.state)
            self.bus.publish(
                StageFinished(
                    stage=name,
                    status=stop.status if stop.status != "failed" else "failed",
                    detail=str(stop),
                    duration_s=time.monotonic() - t0,
                )
            )
            self._publish_state()
            raise
        except RoundPending as pending:
            rec.status = "waiting"
            rec.waiting_for = "windows_round"
            rec.error = str(pending)
            self.state.pending_round = pending.round.id
            self.store.save(self.state)
            self.bus.publish(
                GateWaiting(
                    gate="windows_round",
                    payload={
                        "round": pending.round.id,
                        "instructions": pending.round.instructions(),
                    },
                )
            )
            self.bus.publish(
                StageFinished(
                    stage=name,
                    status="waiting",
                    detail=str(pending),
                    duration_s=time.monotonic() - t0,
                )
            )
            self._publish_state()
            raise
        except Exception as exc:
            rec.status = "failed"
            rec.error = f"{type(exc).__name__}: {exc}"
            rec.finished = datetime.now(UTC)
            self.store.save(self.state)
            self.bus.publish(
                StageFinished(
                    stage=name, status="failed", detail=rec.error, duration_s=time.monotonic() - t0
                )
            )
            self._publish_state()
            raise
        rec.status = "done"
        self.state.pending_round = None
        rec.input_hash = input_hash
        rec.outputs = outputs
        rec.finished = datetime.now(UTC)
        self.store.save(self.state)
        self.bus.publish(
            StageFinished(
                stage=name,
                status="done",
                detail=str(outputs.get("summary", "")),
                duration_s=time.monotonic() - t0,
            )
        )
        self._publish_state()
        return True

    # ------------------------------------------------------------ stages
    def ingest(self, *, force: bool = False) -> ApprovalVerdict:
        verdict_holder: list[ApprovalVerdict] = []

        def body() -> dict[str, Any]:
            doc = parse_document(self.pdd)
            self.document = doc
            atomic_write_text(self.paths.document, doc.model_dump_json(indent=2))
            rule = get_rule(self.settings.approval_rule)
            verdict = rule.check(doc)
            verdict_holder.append(verdict)
            atomic_write_text(self.paths.verdict, verdict.model_dump_json(indent=2))
            self.state.pdd_sha256 = doc.sha256
            self.state.slug = self.paths.slug
            self.bus.log(
                f"PDD '{doc.title}' ({doc.format}, {len(doc.blocks)} blocks, {len(doc.tables)} tables): {verdict.summary()}",
                level="info" if verdict.approved else "error",
            )
            if not verdict.approved:
                raise PipelineStop(
                    "PDD rejected at the approval gate: " + "; ".join(verdict.reasons),
                    status="failed",
                )
            return {"summary": verdict.summary(), "title": doc.title, "approved": True}

        self._run_stage(
            "ingest", hash_inputs(self.pdd, self.settings.approval_rule), body, force=force
        )
        if verdict_holder:
            return verdict_holder[0]
        return ApprovalVerdict.model_validate_json(self.paths.verdict.read_text(encoding="utf-8"))

    def extract(self, *, force: bool = False) -> ProcessSpec:
        self._require("ingest")

        def body() -> dict[str, Any]:
            self._check_pdd()
            assert self.document is not None
            backend = self.extraction_backend()
            spec = extract_spec(self.document, self.settings, backend, bus=self.bus)
            self.spec = spec
            atomic_write_text(self.paths.spec, spec.model_dump_json(indent=2))
            summary = f"{len(spec.steps)} steps, {len(spec.applications)} applications, {len(spec.ui_steps())} UI steps, {len(spec.open_questions)} open questions ({backend.name})"
            self.bus.log("spec: " + summary)
            return {
                "summary": summary,
                "backend": backend.name,
                "warnings": spec.extraction.warnings,
            }

        backend_key = f"{self.settings.extraction.backend}:{self.settings.extraction.model}"
        self._run_stage(
            "extract", hash_inputs(self.state.pdd_sha256, backend_key), body, force=force
        )
        assert self.spec is not None
        return self.spec

    def design_stage(self, *, force: bool = False) -> DesignResult:
        self._require("extract")

        def body() -> dict[str, Any]:
            self._check_pdd()
            assert self.spec is not None
            probes = self._design_probes(self.spec)
            result = design_rules(
                self.spec, self.settings, template_search=probes[0], package_versions=probes[1]
            )
            self.design = result
            atomic_write_text(self.paths.design, result.model_dump_json(indent=2))
            self.state.project_name = result.project_name
            for d in result.decisions:
                self.bus.log(f"decision {d.topic}: {d.choice}")
            if result.stopped:
                qs = [q.model_dump() for q in result.blocking_questions]
                self.bus.publish(
                    GateWaiting(
                        gate="open_questions",
                        payload={
                            "questions": qs,
                            "stop_reasons": result.stop_reasons,
                            "contradiction": result.contradiction.model_dump()
                            if result.contradiction
                            else None,
                        },
                    )
                )
                outcome = self.gates.open_questions(qs, result.stop_reasons)
                self.bus.publish(GateResolved(gate="open_questions", outcome=outcome))
                raise PipelineStop(
                    "design stopped: " + " | ".join(result.stop_reasons), status="stopped"
                )
            summary = f"{result.template.kind} / {result.target_framework} / {len(result.workflows)} workflows / installs: {', '.join(p.id for p in result.installs()) or 'none'}"
            return {"summary": summary}

        self._run_stage(
            "design",
            hash_inputs(
                self.paths.spec,
                self.settings.default_target_framework,
                self.settings.expression_language,
                self.settings.ui_capture,
            ),
            body,
            force=force,
        )
        assert self.design is not None
        return self.design

    def _design_probes(
        self, spec: ProcessSpec
    ) -> tuple[Callable[[str], list[dict[str, Any]]] | None, Callable[[str], list[str]] | None]:
        """Run the Windows-side probes (template search + package versions) as ONE batch."""
        from pdd2uipath.design.rules import decide_template, required_packages

        kind, _ = decide_template(spec)
        needed, _, _ = required_packages(spec, kind, self.settings, None)
        package_ids = [p.id for p in needed if p.install]
        if self.settings.ui_capture == "live":
            package_ids.append("UiPath.UIAutomation.Activities")
        cmds = self.commands()
        jobs: list[Job] = []
        if kind == "reframework":
            jobs.append(
                Job(
                    cmds.templates_search("REFramework"),
                    self.paths.run_dir_windows,
                    label="templates search",
                )
            )
        for pid in package_ids:
            jobs.append(
                Job(
                    cmds.packages_versions(
                        pid, None, include_prerelease=self.settings.packages.prefer_prerelease
                    ),
                    self.paths.run_dir_windows,
                    label=f"versions {pid}",
                )
            )
        if not jobs:
            return (lambda q: []), (lambda p: [])
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            results = self.executor().run_batch(jobs)
        except (RoundPending, RoundAborted):
            raise
        except ExecutorError as exc:
            raise PipelineStop(
                f"design probes need the Windows side: {exc}",
                status="waiting",
                waiting_for="windows_side",
            ) from exc
        templates: list[dict[str, Any]] = []
        versions: dict[str, list[str]] = {}
        for job, res in zip(jobs, results, strict=True):
            if job.label == "templates search":
                if res.ok and isinstance(res.data, list):
                    templates = [x for x in res.data if isinstance(x, dict)]
                else:
                    raise PipelineStop(
                        f"templates search failed on the Windows side: {res.summary()}",
                        status="failed",
                    )
            else:
                pid = job.label.split(" ", 1)[1]
                data = res.data if isinstance(res.data, dict) else {}
                vs = data.get("versions") if res.ok else None
                versions[pid] = [str(v) for v in vs] if isinstance(vs, list) else []
                if not res.ok:
                    self.bus.log(
                        f"packages versions {pid} failed: {res.summary()}", level="warning"
                    )
        return (lambda q: templates), (lambda p: versions.get(p, []))

    def plan(self, *, force: bool = False) -> str:
        self._require("design")

        def body() -> dict[str, Any]:
            self._check_pdd()
            assert self.spec is not None and self.design is not None
            project_w = windows_join(self.paths.run_dir_windows, self.design.project_name)
            ctx = PlanContext(
                pdd_filename=self.pdd.name,
                pdd_sha256=self.state.pdd_sha256,
                project_dir_windows=project_w,
                plan_path=self.paths.plan_rel,
                executor=self.executor_kind.value,
                manual_mode=self.manual_mode,
            )
            text = render_plan(self.spec, self.design, ctx)
            previous = (
                self.paths.plan.read_text(encoding="utf-8") if self.paths.plan.is_file() else None
            )
            sha = write_plan(self.paths.plan, text)
            if previous is not None and previous != text and self.state.approval is not None:
                self.bus.log("plan changed; the previous approval is void", level="warning")
            return {"summary": f"{self.paths.plan_rel} (sha256 {sha[:12]})", "sha256": sha}

        self._run_stage(
            "plan", hash_inputs(self.paths.design, self.executor_kind.value), body, force=force
        )
        return self.paths.plan.read_text(encoding="utf-8")

    def approval_status(self) -> tuple[bool, str]:
        return check_approval(self.state, self.paths.plan)

    def approve_plan(self, approver: str) -> None:
        record = approve(self.state, self.paths.plan, approver, self.paths.approval)
        shutil.copyfile(self.paths.plan, self.paths.run_dir / "approval-plan.md")
        rec = self.state.stage("plan")
        if rec.status == "waiting":
            rec.status = "done"
            rec.waiting_for = None
        self.store.save(self.state)
        self.bus.publish(
            GateResolved(gate="plan_approval", outcome=f"approved by {record.approver}")
        )
        self.bus.log(f"plan approved by {record.approver} (sha256 {record.plan_sha256[:12]})")
        self._publish_state()

    def require_approval(self) -> None:
        ok, reason = self.approval_status()
        if ok:
            return
        assert self.design is not None
        text = self.paths.plan.read_text(encoding="utf-8")
        sha = sha256_file(self.paths.plan)
        info = {
            "installs": [f"{p.id} {p.version}" for p in self.design.installs()],
            "sha256": sha,
            "reason": reason,
        }
        self.bus.publish(GateWaiting(gate="plan_approval", payload=info))
        approver = self.gates.approve_plan(text, sha, info)
        if approver:
            self.approve_plan(approver)
            return
        rec = self.state.stage("plan")
        rec.status = "waiting"
        rec.waiting_for = "plan_approval"
        self.store.save(self.state)
        self._publish_state()
        raise PipelineStop(
            "waiting for plan approval: " + reason, status="waiting", waiting_for="plan_approval"
        )

    def scaffold(self, *, force: bool = False) -> None:
        self._require("plan")
        self.require_approval()

        def body() -> dict[str, Any]:
            self._check_pdd()
            assert self.spec is not None and self.design is not None
            plan_text = self.paths.plan.read_text(encoding="utf-8")
            resuming_round = self.state.pending_round is not None
            result = run_scaffold(
                self.executor(),
                self.commands(),
                self.design,
                run_dir_windows=self.paths.run_dir_windows,
                run_dir=self.paths.run_dir,
                description=self.spec.meta.purpose[:180] or self.spec.meta.title,
                plan_text=plan_text,
                plan_name=f"{self.paths.slug}.md",
                timeout_s=self.settings.bridge.job_timeout_s,
                bus=self.bus,
                allow_existing=resuming_round,
                spec_json=self.spec.model_dump_json(indent=2),
            )
            self.state.project_dir_windows = result.project_dir_windows
            for w in result.warnings:
                self.bus.log(w, level="warning")
            return {
                "summary": f"{result.project_dir_windows} (commit {result.commit[:10] if result.commit else 'none'})",
                "commit": result.commit,
                "installed": result.installed,
            }

        self._run_stage(
            "scaffold",
            hash_inputs(
                self.paths.plan, self.state.approval.plan_sha256 if self.state.approval else None
            ),
            body,
            force=force,
        )

    def author(self, *, force: bool = False, repair_text: str | None = None) -> AuthoringResult:
        self._require("scaffold")
        self.require_approval()
        holder: list[AuthoringResult] = []

        def body() -> dict[str, Any]:
            self._check_pdd()
            result = self._author_once(repair_text)
            holder.append(result)
            if not result.ok:
                raise PipelineStop(f"authoring agent failed: {result.error}", status="failed")
            return {
                "summary": f"{self.agent().name}: {result.turns or result.raw_events} events, session {result.session_id or 'n/a'}",
                "session_id": result.session_id,
            }

        self._run_stage(
            "author",
            hash_inputs(
                self.paths.plan,
                self.state.approval.plan_sha256 if self.state.approval else None,
                self.settings.agent.backend,
            ),
            body,
            force=force,
        )
        return holder[0] if holder else AuthoringResult(ok=True)

    def _author_once(self, repair_text: str | None) -> AuthoringResult:
        project = self.project_dir
        project_w = self.state.project_dir_windows or ""
        cfg = settings_source_path(self.settings)
        shim = install_shim(project, project_w, config_path=cfg)
        env = {"PATH": shim.path_env()}
        skills_root = self._ensure_skill(project)
        prompt = build_author_prompt(
            self.paths.plan_rel,
            manual_mode=self.manual_mode,
            repair_text=repair_text,
            deny_tools=self.settings.agent.deny_tools,
        )
        if self.extra_instruction.strip():
            prompt += (
                "\n\nAdditional instruction from the operator: " + self.extra_instruction.strip()
            )
            self.extra_instruction = ""
        round_no = self.state.repair_round
        session = AuthoringSession(
            project_dir=project,
            prompt=prompt,
            env=env,
            log_path=self.paths.logs
            / ("author.jsonl" if round_no == 0 else f"author-repair-{round_no}.jsonl"),
            max_turns=self.settings.agent.max_turns,
            max_budget_usd=self.settings.agent.max_budget_usd,
            timeout_s=self.settings.agent.timeout_s,
            resume_session_id=self.state.agent_session_id if repair_text else None,
            model=self.settings.agent.model,
            bus=self.bus,
            skills_root=skills_root,
        )
        try:
            result = self.agent().run(session)
        except AgentError as exc:
            raise PipelineStop(f"authoring agent unavailable: {exc}", status="failed") from exc
        if result.session_id:
            self.state.agent_session_id = result.session_id
        if result.final_text.strip():  # the agent's own hand-off notes go into the report
            self.state.agent_notes.append(result.final_text.strip()[-6000:])
        sha = git_commit_all(project, f"pdd2uipath: authoring round {round_no}")
        if sha:
            self.bus.log(f"authoring committed as {sha[:10]}")
        self.store.save(self.state)
        return result

    def _ensure_skill(self, project: Path) -> Path | None:
        """Make the uipath-rpa skill discoverable by the chosen agent (project-level copy)."""
        store = (
            brain_path(self.settings.skills_root, self.platform)
            if self.settings.skills_root
            else None
        )
        if store is None or not (store / "SKILL.md").is_file():
            return None
        backend = self.agent().name
        target_dirs = {
            "opencode": project / ".opencode" / "skills" / "uipath-rpa",
            "copilot": project / ".github" / "skills" / "uipath-rpa",
        }
        target = target_dirs.get(backend)
        if target is None:
            return store.parent
        if not (target / "SKILL.md").is_file():
            shutil.copytree(store, target, dirs_exist_ok=True)
            self.bus.log(
                f"copied the uipath-rpa skill into {target.relative_to(project).as_posix()} for {backend}"
            )
        return store.parent

    def verify(self, *, force: bool = False) -> GateResult:
        self._require("author")
        self.require_approval()

        def body() -> dict[str, Any]:
            self._check_pdd()
            assert self.spec is not None and self.design is not None
            project = self.project_dir
            project_w = self.state.project_dir_windows or ""
            stub = self.settings.ui_capture == "stub"
            max_rounds = self.settings.repair.max_rounds
            while True:
                self._lock_gate(project, project_w)
                gate = run_gate(
                    self.executor(),
                    self.commands(),
                    project_windows=project_w,
                    project_dir=project,
                    stub_mode=stub,
                    timeout_s=self.settings.bridge.job_timeout_s,
                    bus=self.bus,
                )
                audit_result = audit(self.spec, self.design, project)
                self.last_gate, self.last_audit = gate, audit_result
                self._save_gate(gate, audit_result)
                problems = audit_result.problems()
                if gate.verdict.passed and not problems:
                    return {
                        "summary": gate.verdict.summary() + "; audit passed",
                        "round": self.state.repair_round,
                    }
                if self.state.repair_round >= max_rounds:
                    raise PipelineStop(
                        f"gate failed after {self.state.repair_round} repair round(s): {gate.verdict.summary()}"
                        + ("; " + "; ".join(problems) if problems else ""),
                        status="failed",
                    )
                self.state.repair_round += 1
                self.store.save(self.state)
                self.bus.publish(
                    RepairRound(
                        round=self.state.repair_round,
                        max_rounds=max_rounds,
                        errors=len(gate.verdict.errors) + len(problems),
                    )
                )
                repair_text = format_for_agent(gate.verdict, problems)
                result = self._author_once(repair_text)
                if not result.ok:
                    raise PipelineStop(
                        f"repair round {self.state.repair_round} failed: {result.error}",
                        status="failed",
                    )

        self._run_stage("verify", hash_inputs(str(time.time())), body, force=True)
        assert self.last_gate is not None
        return self.last_gate

    def _lock_gate(self, project: Path, project_w: str) -> None:
        while True:
            # The instances probe is informational; in wsl-manual it would cost the user a
            # whole Windows round before every gate, so only the file-lock check runs there.
            manual = self.settings.executor == ExecutorKind.WSL_MANUAL
            report = check_locks(
                project,
                project_w,
                executor=None if manual else self.executor(),
                commands=None if manual else self.commands(),
            )
            if not report.blocked:
                return
            self.bus.publish(GateWaiting(gate="studio_lock", payload={"message": report.message()}))
            outcome = self.gates.studio_lock(report.message())
            self.bus.publish(GateResolved(gate="studio_lock", outcome=outcome))
            if outcome != "retry":
                raise PipelineStop(
                    "project is open in Studio Desktop: " + report.message(),
                    status="waiting",
                    waiting_for="studio_lock",
                )

    def _save_gate(self, gate: GateResult, audit_result: AuditResult) -> None:
        payload = {
            "round": self.state.repair_round,
            "verdict": {
                "passed": gate.verdict.passed,
                "summary": gate.verdict.summary(),
                "diagnostics": [d.__dict__ for d in gate.verdict.diagnostics],
            },
            "audit": {
                "passed": audit_result.passed,
                "problems": audit_result.problems(),
                "todo_indicate_total": audit_result.todo_indicate_total,
                "rows": [r.__dict__ for r in audit_result.rows],
            },
            "files": gate.files,
            "results": [
                r.to_dict()
                for r in [
                    *gate.validate_results,
                    *([gate.build_result] if gate.build_result else []),
                ]
            ],
        }
        atomic_write_text(
            self.paths.gate_json(self.state.repair_round),
            json.dumps(payload, indent=2, default=str),
        )

    def report(self, *, force: bool = False) -> Path:
        self._require("scaffold")

        def body() -> dict[str, Any]:
            assert self.spec is not None and self.design is not None
            project = self.project_dir
            audit_result = self.last_audit or audit(self.spec, self.design, project)
            verdict: GateVerdict | None = (
                self.last_gate.verdict if self.last_gate else self._load_last_verdict()
            )
            ctx = ReportContext(
                run_dir_windows=self.paths.run_dir_windows,
                project_dir_windows=self.state.project_dir_windows or "",
                plan_rel=self.paths.plan_rel,
                executor=self.executor_kind.value,
                agent_backend=self.settings.agent.backend,
                approver=self.state.approval.approver if self.state.approval else "",
                repair_rounds=self.state.repair_round,
                max_rounds=self.settings.repair.max_rounds,
                pdd_filename=self.pdd.name,
                pdd_sha256=self.state.pdd_sha256,
                gate_passed=bool(verdict and verdict.passed),
                scaffold_commit=self.state.stage("scaffold").outputs.get("commit"),
                final_commit=None,
                agent_notes=list(self.state.agent_notes),
            )
            text = render_report(self.spec, self.design, audit_result, verdict, ctx)
            report_path, csv_path = write_outputs(self.paths.run_dir, text, audit_result)
            for target in (project / "report.md", project / "traceability.csv"):
                shutil.copyfile(self.paths.run_dir / target.name, target)
            sha = git_commit_all(project, "pdd2uipath: hand-off report and traceability")
            return {
                "summary": f"{report_path.name}, {csv_path.name}"
                + (f" (commit {sha[:10]})" if sha else ""),
                "commit": sha,
            }

        self._run_stage("report", hash_inputs(str(time.time())), body, force=True)
        return self.paths.report

    def _load_last_verdict(self) -> GateVerdict | None:
        from pdd2uipath.verify.diagnostics import Diagnostic

        path = self.paths.gate_json(self.state.repair_round)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        diags = [Diagnostic(**d) for d in data.get("verdict", {}).get("diagnostics", [])]
        return GateVerdict(diagnostics=diags, stub_mode=self.settings.ui_capture == "stub")

    def open_studio(self) -> str:
        """Open the project in Studio Desktop (Windows side), with a direct-launch fallback."""
        project_w = self.state.project_dir_windows
        if not project_w:
            raise PipelineStop("no project to open", status="failed")
        res = self.executor().run(
            self.commands().studio_start(project_w), cwd=project_w, timeout_s=300
        )
        if res.ok:
            return f"Studio Desktop opened {project_w} (uip rpa studio start)"
        hint = f'Open Studio Desktop manually: File > Open > "{project_w}\\project.json"'
        if self.platform.is_windows:
            exe = find_studio_exe()
            if exe:
                import subprocess

                subprocess.Popen([exe, str(Path(project_w) / "project.json")], shell=False)
                return f"launched {exe} with {project_w}\\project.json (studio start failed: {res.summary()})"
        return f"uip rpa studio start failed ({res.summary()}). {hint}"

    # ------------------------------------------------------------ orchestration
    def _require(self, stage: str) -> None:
        rec = self.state.stage(stage)
        if rec.status != "done":
            raise PipelineStop(
                f"stage '{stage}' has not completed (status: {rec.status}); run it first",
                status="failed",
            )

    def run_all(self, *, stop_after: str | None = None, force: bool = False) -> None:
        order: list[tuple[str, Callable[[], Any]]] = [
            ("ingest", lambda: self.ingest(force=force)),
            ("extract", lambda: self.extract(force=force)),
            ("design", lambda: self.design_stage(force=force)),
            ("plan", lambda: self.plan(force=force)),
            ("scaffold", lambda: self.scaffold(force=force)),
            ("author", lambda: self.author(force=force)),
            ("verify", lambda: self.verify(force=force)),
            ("report", lambda: self.report(force=force)),
        ]
        for name, fn in order:
            fn()
            if stop_after == name:
                return

    def resume_round(self) -> None:
        """wsl-manual: continue after the user ran the pending Windows round.

        The waiting stage is re-run; the manual executor reuses the pending round (same
        jobs) and collects its results instead of generating a new script.
        """
        if not self.state.pending_round:
            raise PipelineStop("no pending Windows round", status="failed")
        self.run_all()


def find_studio_exe() -> str | None:
    candidates: list[Path] = []
    for base in (
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if not base:
            continue
        b = Path(base)
        candidates += [
            b / "UiPath" / "Studio" / "UiPath.Studio.exe",
            b / "Programs" / "UiPath" / "Studio" / "UiPath.Studio.exe",
        ]
        plat = b / "UiPathPlatform" / "Studio"
        if plat.is_dir():
            candidates += sorted(plat.glob("*/UiPath.Studio.exe"), reverse=True)
        candidates += (
            sorted((b / "UiPath").glob("app-*/UiPath.Studio.exe"), reverse=True)
            if (b / "UiPath").is_dir()
            else []
        )
    for c in candidates:
        if c.is_file():
            return str(c)
    found = shutil.which("UiPath.Studio.exe")
    return found


def build_author_prompt(
    plan_rel: str, *, manual_mode: bool, repair_text: str | None, deny_tools: list[str]
) -> str:
    lines = [
        f"Use the uipath-rpa skill to execute the plan at {plan_rel} in this project (the current working directory contains project.json).",
        "Read the plan first and follow it task by task; its header says `Execution autonomy: autonomous`, so continue until every checkbox is done or a listed Stop condition is hit, then report per the skill's Completion Output.",
        "Rules that override anything else:",
        "- Annotate every workflow with its PDD step ID(s): root Sequence DisplayName `<StepId> - <Name>` and AnnotationText `PDD step <StepId> (<ref>)`.",
        '- UI steps use the Placeholder-Selector Stub Pattern: real UIA activities from `uip rpa activities get-default-xaml` with unconfigured targets and `DisplayName="TODO Indicate - ..."`; never a Log Message instead of a UI action; never hand-written selectors.',
        "- Install only the packages named in Task T0 (`uip rpa packages install`); never edit project.json dependencies, targetFramework or expressionLanguage by hand.",
        "- Never run " + ", ".join(deny_tools) + "; no Orchestrator publish, pack, or job runs.",
        "- Never write credential values; only the Orchestrator asset names from the plan.",
        "- Stop conditions are task-scoped: a blocked task (missing required value, validation errors after 5 attempts) is recorded under that task and skipped; continue with every other task, since the step workflows are independent. Only package, framework or expression-language problems end the run.",
        "- A missing technique (how to parse, locate, compare or name something) is not a stop condition: implement a reasonable approach from the plan's Reference data, annotate it `TODO Confirm - <what>` and mention it in your final report. Only missing values (queue, asset, path, URL, mailbox, transaction code, field name) stop a task.",
        '- The plan is self-contained: the PDD is NOT in the project. Its data items (any "appendix" or "field list" a task mentions), business rules, exceptions and applications are in the plan\'s *Reference data* section and in `docs/process-spec.json`. Use them; never invent values that are not there.',
    ]
    if manual_mode:
        lines.append(
            "- This session has NO access to `uip rpa validate`/`uip rpa build` (executor wsl-manual): author only, mark Validate boxes as 'deferred to the Windows gate'; pdd2uipath validates afterwards and comes back with the errors."
        )
    else:
        lines.append(
            '- `uip` on PATH forwards to the Windows session; use `uip rpa validate --file-path <file> --project-dir "$(pwd)" --output json` after each file and `uip rpa build "$(pwd)" --output json` at the end, exactly as the skill prescribes.'
        )
    if repair_text:
        lines.append("")
        lines.append("REPAIR ROUND. " + repair_text)
    return "\n".join(lines)

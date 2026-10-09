"""Stage 3: deterministic design rules, each with a written rationale.

Nothing here calls a model. The only external inputs are two optional probes that run
on the Windows side through the executor: ``templates search`` and ``packages versions``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pdd2uipath.core.config import Settings
from pdd2uipath.core.state import pascal_case
from pdd2uipath.design.models import (
    Contradiction,
    Decision,
    DesignResult,
    PackageRequirement,
    TemplateChoice,
    TemplateKind,
    WorkflowMapping,
    WorkflowRole,
)
from pdd2uipath.spec.models import OpenQuestion, ProcessSpec, Step

TemplateSearch = Callable[[str], list[dict[str, Any]]]
PackageVersions = Callable[[str], list[str]]

#: Signals that the automation needs the Windows runtime (case-insensitive substrings).
WINDOWS_SIGNALS: tuple[str, ...] = (
    "sap gui",
    "windows",
    "classic desktop",
    "microsoft 365 apps",
    "macro-enabled",
    ".xlsm",
    "vba",
    "macro ",
    "desktop (sap)",
    "citrix",
    "vdi",
    "outlook desktop",
    "excel com",
)
#: Signals that the automation must run cross-platform.
PORTABLE_SIGNALS: tuple[str, ...] = (
    "linux",
    "serverless",
    "cross-platform",
    "crossplatform",
    "container",
    "docker",
    "studio web",
    "macos",
    "ubuntu",
)
_TBD_RE = re.compile(
    r"\b(tbd|to be (provided|clarified|defined|completed|confirmed)|\[to be)", re.I
)

_APP_INIT_RE = re.compile(
    r"\b(log ?in|logon|log on|sign in|start sap|open (the )?(sap|browser|application|app)\b)", re.I
)
_SETTINGS_RE = re.compile(
    r"\b(load configuration|configuration and credential|read orchestrator assets|init(ial)? settings)\b",
    re.I,
)
_END_RE = re.compile(
    r"\b(daily summary|end of (the )?run|summary email|summary report|at the end)\b", re.I
)
_CLOSE_RE = re.compile(r"\b(close all|log ?out|close (the )?(sap|browser|application))\b", re.I)


# ---------------------------------------------------------------- framework


def framework_signals(spec: ProcessSpec) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Collect ``(signal text, source_ref)`` pairs for Windows-only and cross-platform needs."""
    windows: list[tuple[str, str]] = []
    portable: list[tuple[str, str]] = []
    texts: list[tuple[str, str]] = []
    for a in spec.applications:
        texts.append(
            (f"{a.name}: {a.kind} / {a.access} / {' / '.join(a.platform_hints)}", a.source_ref)
        )
    texts.append(
        (
            spec.robot_environment.description
            + " / "
            + " / ".join(spec.robot_environment.os_hints),
            spec.robot_environment.source_ref,
        )
    )
    for s in spec.steps:
        texts.append((s.application + " / " + s.action + " / " + " ".join(s.details), s.source_ref))
    for text, ref in texts:
        low = text.lower()
        for sig in WINDOWS_SIGNALS:
            if sig in low:
                windows.append((sig.strip(), ref))
        for sig in PORTABLE_SIGNALS:
            if sig in low:
                portable.append((sig, ref))
    return _dedupe(windows), _dedupe(portable)


def _dedupe(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def decide_framework(spec: ProcessSpec, default: str) -> tuple[str, Contradiction | None, Decision]:
    windows, portable = framework_signals(spec)
    w_txt = [f"'{s}' ({r})" for s, r in windows]
    p_txt = [f"'{s}' ({r})" for s, r in portable]
    if windows and portable:
        contradiction = Contradiction(
            topic="target framework",
            detail=(
                "The PDD requires a cross-platform runtime AND Windows-only capabilities. "
                "Rule 2a: never silently pick one, never infer Legacy."
            ),
            windows_signals=[f"{s} ({r})" for s, r in windows],
            portable_signals=[f"{s} ({r})" for s, r in portable],
            source_refs=sorted({r for _, r in windows + portable}),
        )
        decision = Decision(
            id="D-framework",
            topic="target framework",
            choice="STOP (contradiction)",
            rationale=f"Windows-only signals {w_txt} conflict with cross-platform signals {p_txt}.",
            source_refs=contradiction.source_refs,
        )
        return "", contradiction, decision
    if windows:
        return (
            "Windows",
            None,
            Decision(
                id="D-framework",
                topic="target framework",
                choice="Windows",
                rationale=f"Windows-only applications/capabilities: {', '.join(w_txt)}.",
                source_refs=sorted({r for _, r in windows}),
            ),
        )
    if portable:
        return (
            "Portable",
            None,
            Decision(
                id="D-framework",
                topic="target framework",
                choice="Portable",
                rationale=f"Cross-platform runtime signals: {', '.join(p_txt)}; no Windows-only capability found.",
                source_refs=sorted({r for _, r in portable}),
            ),
        )
    return (
        default,
        None,
        Decision(
            id="D-framework",
            topic="target framework",
            choice=default,
            rationale=f"The PDD gives no platform signal; configured default '{default}' applies.",
        ),
    )


# ---------------------------------------------------------------- template


def decide_template(spec: ProcessSpec) -> tuple[TemplateKind, Decision]:
    purpose = (spec.meta.purpose + " " + spec.meta.title).lower()
    if re.search(r"\b(test automation|test cases?|test spec)\b", purpose):
        return "test", Decision(
            id="D-template",
            topic="project template",
            choice="Test Automation",
            rationale="The PDD describes a test specification.",
            source_refs=[spec.meta.source_ref],
        )
    if re.search(r"\b(reusable (component|library)|library project)\b", purpose):
        return "library", Decision(
            id="D-template",
            topic="project template",
            choice="Library",
            rationale="The PDD describes a reusable component.",
            source_refs=[spec.meta.source_ref],
        )
    t = spec.transaction
    evidence: list[str] = []
    if t.dispatcher_performer:
        evidence.append("Dispatcher/Performer split")
    if t.queue_name:
        evidence.append(f"Orchestrator queue '{t.queue_name}'")
    unit = t.unit.lower()
    if (
        unit
        and not re.search(r"\b(one|single|whole) (daily )?run\b", unit)
        and re.search(r"\b(one|each|per)\b", unit)
    ):
        evidence.append(f"transaction unit '{t.unit}'")
    if re.search(r"(?<!no )(?<!without )\bqueue items?\b", t.retry_policy.lower() + " " + unit):
        evidence.append("queue-item retry policy")
    if evidence:
        return "reframework", Decision(
            id="D-template",
            topic="project template",
            choice="REFramework (Official template)",
            rationale="Queue/transaction-based process: " + ", ".join(evidence) + ".",
            source_refs=[t.source_ref] if t.source_ref else [],
        )
    return "blank", Decision(
        id="D-template",
        topic="project template",
        choice="Blank (single linear run)",
        rationale=f"No queue or per-item transaction: '{t.unit or 'single run'}'. REFramework's four-state ceremony is not justified.",
        source_refs=[t.source_ref] if t.source_ref else [],
    )


def resolve_template(
    kind: TemplateKind, search: TemplateSearch | None
) -> tuple[TemplateChoice, Decision | None, str | None]:
    """Apply the skill's template-selection rule to a live ``templates search`` result."""
    if kind == "blank":
        return TemplateChoice(kind=kind, template_id="BlankTemplate", status="resolved"), None, None
    if kind == "library":
        return (
            TemplateChoice(kind=kind, template_id="LibraryProcessTemplate", status="resolved"),
            None,
            None,
        )
    if kind == "test":
        return (
            TemplateChoice(
                kind=kind, template_id="TestAutomationProjectTemplate", status="resolved"
            ),
            None,
            None,
        )
    query = "REFramework"
    if search is None:
        return (
            TemplateChoice(
                kind=kind,
                query=query,
                status="unresolved",
                detail="no Windows-side template search available",
            ),
            None,
            "REFramework template not resolved: the Windows side was not available for `uip rpa templates search`",
        )
    results = search(query)
    official = [r for r in results if str(r.get("source", "")).lower() == "official"]
    marketplace = [r for r in results if str(r.get("source", "")).lower() != "official"]
    if len(official) == 1:
        pick = official[0]
        choice = TemplateChoice(
            kind=kind,
            query=query,
            package_id=str(pick.get("packageId")),
            package_version=str(pick.get("version")) if pick.get("version") else None,
            candidates=results,
            status="resolved",
            detail=f"single Official result among {len(results)}",
        )
        return (
            choice,
            Decision(
                id="D-template-package",
                topic="template package",
                choice=f"{choice.package_id} {choice.package_version or ''}".strip(),
                rationale=f"`uip rpa templates search --query {query}` returned exactly one Official template ({len(marketplace)} Marketplace items ignored per the skill rule).",
            ),
            None,
        )
    if len(official) > 1:
        names = ", ".join(f"{r.get('packageId')} {r.get('version')}" for r in official)
        return (
            TemplateChoice(
                kind=kind,
                query=query,
                candidates=results,
                status="ambiguous",
                detail=f"{len(official)} Official templates: {names}",
            ),
            None,
            f"ambiguous template search: {len(official)} Official REFramework templates ({names}); choose one",
        )
    if marketplace:
        names = ", ".join(f"{r.get('packageId')} {r.get('version')}" for r in marketplace)
        return (
            TemplateChoice(
                kind=kind,
                query=query,
                candidates=results,
                status="ambiguous",
                detail=f"only Marketplace templates: {names}",
            ),
            None,
            f"no Official REFramework template; only Marketplace candidates ({names}) - never picked silently",
        )
    return (
        TemplateChoice(
            kind=kind, query=query, candidates=[], status="ambiguous", detail="no results"
        ),
        None,
        "templates search returned no REFramework template; confirm BlankTemplate fallback explicitly",
    )


# ---------------------------------------------------------------- workflow mapping


def classify_step(step: Step, last_step_id: str) -> WorkflowRole:
    text = f"{step.name} {step.action} {' '.join(step.details[:2])}"
    if _SETTINGS_RE.search(text) or (
        step.application.lower() == "orchestrator"
        and "asset" in text.lower()
        and "queue" not in text.lower()
    ):
        return "settings"
    if step.part.lower() == "dispatcher":
        return "dispatcher"
    if _CLOSE_RE.search(step.name) or _CLOSE_RE.search(step.action):
        return "app-close"
    if _APP_INIT_RE.search(step.name) or _APP_INIT_RE.search(step.action):
        return "app-init"
    if _END_RE.search(text) and step.step_id == last_step_id:
        return "end-of-run"
    return "process-step"


def _file_stem(step: Step) -> str:
    return f"{step.step_id}_{pascal_case(step.name, 40)}"


def map_workflows(spec: ProcessSpec, kind: TemplateKind) -> list[WorkflowMapping]:
    out: list[WorkflowMapping] = []
    last_id = spec.steps[-1].step_id if spec.steps else ""
    for step in spec.steps:
        role = classify_step(step, last_id) if kind == "reframework" else "process-step"
        file: str
        location: str
        invoked_from: str
        notes: list[str] = []
        if kind == "reframework":
            if role == "settings":
                file = "Framework/InitAllSettings.xaml"
                location = "REFramework Init state (template workflow, customized)"
                invoked_from = "Main.xaml (Init)"
                notes.append(
                    "Keep the template file; add the asset names to the Config Assets section and read them there."
                )
            elif role == "dispatcher":
                file = f"Dispatcher/{_file_stem(step)}.xaml"
                location = "Dispatcher entry point (linear Sequence, no REFramework states)"
                invoked_from = "Dispatcher.xaml (second entry point)"
            elif role == "app-init":
                file = f"Performer/{_file_stem(step)}.xaml"
                location = "REFramework Init state -> InitAllApplications"
                invoked_from = "Framework/InitAllApplications.xaml"
                notes.append("Also re-run on retry after a system exception (template behaviour).")
            elif role == "app-close":
                file = f"Performer/{_file_stem(step)}.xaml"
                location = "REFramework End Process state -> CloseAllApplications"
                invoked_from = "Framework/CloseAllApplications.xaml"
            elif role == "end-of-run":
                file = f"Performer/{_file_stem(step)}.xaml"
                location = "REFramework End Process state (runs once per job, on every exit)"
                invoked_from = "Framework/CloseAllApplications.xaml"
                notes.append(
                    "End-of-run step: invoked from CloseAllApplications so it runs on every exit; guard with the run counters."
                )
            else:
                file = f"Performer/{_file_stem(step)}.xaml"
                location = "REFramework Process Transaction state -> Process.xaml"
                invoked_from = "Framework/Process.xaml (in step order)"
        else:
            file = f"Steps/{_file_stem(step)}.xaml"
            location = "linear Main.xaml sequence"
            invoked_from = "Main.xaml (in step order)"
        if step.is_ui_interaction:
            notes.append(
                "UI step: real UIA activities with placeholder selectors and 'TODO Indicate' markers (stub pattern)."
            )
        out.append(
            WorkflowMapping(
                step_id=step.step_id,
                step_name=step.name,
                file=file,
                role=role,
                location=location,
                invoked_from=invoked_from,
                ui=step.is_ui_interaction,
                application=step.application,
                pdd_ref=step.source_ref,
                business_rules=step.business_rules,
                exceptions=step.exceptions,
                notes=notes,
            )
        )
    return out


# ---------------------------------------------------------------- packages


def _latest(versions: list[str], *, prerelease: bool) -> str | None:
    pool = versions if prerelease else [v for v in versions if "-" not in v]
    return pool[0] if pool else (versions[0] if versions else None)


def required_packages(
    spec: ProcessSpec, kind: TemplateKind, settings: Settings, versions: PackageVersions | None
) -> tuple[list[PackageRequirement], list[Decision], list[str]]:
    """Decide activity packages. Template-provided ones are recorded but not re-installed."""
    template_provided: dict[str, str] = {}
    if kind == "reframework":
        template_provided = {
            "UiPath.System.Activities": "(template)",
            "UiPath.UIAutomation.Activities": "(template)",
            "UiPath.Excel.Activities": "(template)",
            "UiPath.Testing.Activities": "(template)",
        }
    else:
        template_provided = {"UiPath.System.Activities": "(template)"}
    needed: dict[str, str] = {}
    apps_text = " ".join(
        f"{a.name} {a.kind} {a.used_for} {' '.join(a.platform_hints)}" for a in spec.applications
    ).lower()
    steps_text = " ".join(
        f"{s.application} {s.action} {' '.join(s.details)}" for s in spec.steps
    ).lower()
    if spec.ui_steps():
        needed["UiPath.UIAutomation.Activities"] = (
            f"UI interaction steps: {', '.join(s.step_id for s in spec.ui_steps())}"
        )
    if "excel" in apps_text or ".xls" in apps_text or "workbook" in steps_text:
        needed["UiPath.Excel.Activities"] = "Excel workbooks are read/written"
    if (
        any(a.kind == "email" for a in spec.applications)
        or "outlook" in apps_text
        or "email" in steps_text
    ):
        needed["UiPath.Mail.Activities"] = "email is read or sent (Outlook / mail activities)"
    if "pdf" in apps_text or "pdf" in steps_text:
        needed["UiPath.PDF.Activities"] = "PDF text extraction"
    if "word" in apps_text and any(a.kind == "word" for a in spec.applications):
        needed["UiPath.Word.Activities"] = "Word documents"
    packages: list[PackageRequirement] = []
    decisions: list[Decision] = []
    stops: list[str] = []
    for pid, _reason in template_provided.items():
        packages.append(
            PackageRequirement(
                id=pid,
                version=None,
                reason=needed.get(pid, "shipped by the template"),
                from_template=True,
                install=False,
            )
        )
    for pid, reason in needed.items():
        if pid in template_provided:
            continue
        version: str | None = None
        if versions is not None:
            version = _latest(versions(pid), prerelease=settings.packages.prefer_prerelease)
            if version is None:
                stops.append(
                    f"no version found for {pid} (`uip rpa packages versions` returned nothing)"
                )
        else:
            stops.append(
                f"version of {pid} not resolved (Windows side unavailable for `uip rpa packages versions`)"
            )
        packages.append(
            PackageRequirement(
                id=pid, version=version, reason=reason, from_template=False, install=True
            )
        )
    uia = next((p for p in packages if p.id == "UiPath.UIAutomation.Activities"), None)
    if uia is not None:
        if settings.ui_capture == "live":
            uia.min_version = settings.packages.uia_min_version
            if uia.from_template:
                uia.install = True
                uia.version = (
                    _latest(versions("UiPath.UIAutomation.Activities"), prerelease=False)
                    if versions
                    else None
                ) or settings.packages.uia_min_version
            decisions.append(
                Decision(
                    id="D-uia",
                    topic="UIAutomation package",
                    choice=f">= {settings.packages.uia_min_version}",
                    rationale="ui_capture=live needs the `uip rpa uia` CLI, which requires the skill's minimum UIA version; the upgrade is listed in Task 0 so plan approval is the consent.",
                )
            )
        else:
            decisions.append(
                Decision(
                    id="D-uia",
                    topic="UIAutomation package",
                    choice="keep the template/installed version",
                    rationale="ui_capture=stub: the Placeholder-Selector Stub Pattern needs no UIA CLI, so no upgrade is forced (keeps Studio 2025.10-era templates working).",
                )
            )
    installs = [p for p in packages if p.install]
    decisions.append(
        Decision(
            id="D-packages",
            topic="activity packages",
            choice=", ".join(f"{p.id} {p.version or '?'}" for p in installs)
            or "none beyond the template",
            rationale="; ".join(f"{p.id}: {p.reason}" for p in packages),
        )
    )
    return packages, decisions, stops


# ---------------------------------------------------------------- open questions


def derive_questions(spec: ProcessSpec) -> list[OpenQuestion]:
    """Blocking questions the rules can infer themselves (UI step with unknown target)."""
    derived: list[OpenQuestion] = []
    known = {q.text.lower()[:50] for q in spec.open_questions}
    flagged = [q for q in spec.open_questions if q.blocking]
    for step in spec.ui_steps():
        if any(step.step_id in q.text for q in flagged):
            continue
        app = next(
            (
                a
                for a in spec.applications
                if a.name.lower() in step.application.lower()
                or step.application.lower() in a.name.lower()
            ),
            None,
        )
        access = app.access if app else ""
        details = " ".join(step.details)
        if (
            _TBD_RE.search(access)
            or _TBD_RE.search(details)
            or (app is None and not step.application)
        ):
            text = f"UI step {step.step_id} ({step.name}) has an undefined target application/URL/screen ({access or step.application or 'no application named'})."
            if text.lower()[:50] not in known:
                derived.append(
                    OpenQuestion(
                        id=f"DQ-{step.step_id}",
                        text=text,
                        owner="",
                        blocking=True,
                        reason="UI step target unknown",
                        source_ref=step.source_ref,
                    )
                )
    return derived


# ---------------------------------------------------------------- entry point


def design(
    spec: ProcessSpec,
    settings: Settings,
    *,
    template_search: TemplateSearch | None = None,
    package_versions: PackageVersions | None = None,
) -> DesignResult:
    decisions: list[Decision] = []
    stops: list[str] = []
    framework, contradiction, d_fw = decide_framework(spec, settings.default_target_framework)
    decisions.append(d_fw)
    if contradiction is not None:
        stops.append("target framework contradiction: " + contradiction.detail)
    kind, d_t = decide_template(spec)
    decisions.append(d_t)
    template, d_pkg, t_stop = resolve_template(kind, template_search)
    if d_pkg:
        decisions.append(d_pkg)
    if t_stop:
        stops.append(t_stop)
    decisions.append(
        Decision(
            id="D-language",
            topic="expression language",
            choice=settings.expression_language,
            rationale="From configuration; XAML workflows by default, coded files only for transforms/DTOs/helpers.",
        )
    )
    decisions.append(
        Decision(
            id="D-capture",
            topic="UI capture",
            choice=settings.ui_capture,
            rationale="stub: target apps are not reachable from the Windows session; developers run Indicate in Studio."
            if settings.ui_capture == "stub"
            else "live: config says the apps are reachable from the Windows session.",
        )
    )
    workflows = map_workflows(spec, kind)
    packages, d_pk, p_stops = required_packages(spec, kind, settings, package_versions)
    decisions.extend(d_pk)
    stops.extend(p_stops)
    blocking = list(spec.blocking_questions()) + derive_questions(spec)
    nonblocking = [q for q in spec.open_questions if not q.blocking]
    if blocking:
        stops.append(
            f"{len(blocking)} blocking open question(s): "
            + "; ".join(f"{q.id}: {q.text}" for q in blocking)
        )
    windows, portable = framework_signals(spec)
    title = re.sub(r"\(.*?\)", "", spec.meta.title)
    config_values: dict[str, Any] = {
        "queue_name": spec.transaction.queue_name,
        "credential_assets": spec.credential_assets(),
        "assets": sorted(
            {n for a in spec.applications for n in a.credential_asset_names} | _asset_names(spec)
        ),
        "max_retry_number": _max_retries(spec),
    }
    decisions.append(
        Decision(
            id="D-structure",
            topic="project structure",
            choice=f"{len(workflows)} workflows for {len(spec.steps)} steps",
            rationale="One workflow per PDD step, named <StepId>_<Name>.xaml, grouped by REFramework role; template files are customized, never replaced."
            if kind == "reframework"
            else "One workflow per PDD step under Steps/, invoked in order from Main.xaml.",
        )
    )
    return DesignResult(
        project_name=pascal_case(title),
        template=template,
        target_framework=framework,
        expression_language=settings.expression_language,
        ui_capture=settings.ui_capture,
        contradiction=contradiction,
        workflows=workflows,
        packages=packages,
        decisions=decisions,
        blocking_questions=blocking,
        nonblocking_questions=nonblocking,
        stop_reasons=stops,
        config_values=config_values,
        windows_signals=[f"{s} ({r})" for s, r in windows],
        portable_signals=[f"{s} ({r})" for s, r in portable],
    )


def _asset_names(spec: ProcessSpec) -> set[str]:
    names: set[str] = set()
    for s in spec.steps:
        for m in re.finditer(
            r"\b([A-Z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b", " ".join([s.action, *s.details])
        ):
            if "asset" in (s.action + " " + " ".join(s.details)).lower():
                names.add(m.group(1))
    return names


def _max_retries(spec: ProcessSpec) -> int | None:
    m = re.search(r"max\.?\s*(\d+)\s*retr", spec.transaction.retry_policy, re.I)
    return int(m.group(1)) if m else None

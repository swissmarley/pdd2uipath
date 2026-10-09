"""Stage 5: create the project with the official CLI (never hand-written scaffolding)."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pdd2uipath.core.events import EventBus
from pdd2uipath.core.paths import windows_join
from pdd2uipath.design.models import DesignResult
from pdd2uipath.executors.base import Executor, Job, UipResult
from pdd2uipath.executors.surface import UipCommands

GITATTRIBUTES = """# Keep UiPath artefacts byte-stable across WSL and Windows git (added by pdd2uipath).
*.xaml  -text
*.json  -text
*.cs    -text
*.xlsx  binary
*.nupkg binary
*.md    text
"""

GITIGNORE = """# pdd2uipath
.local/
.pdd2uipath/
*.tmp
.tmh/
"""


class ScaffoldError(RuntimeError):
    pass


@dataclass
class ScaffoldResult:
    project_dir_windows: str
    project_dir: Path
    results: list[UipResult] = field(default_factory=list)
    commit: str | None = None
    warnings: list[str] = field(default_factory=list)
    installed: list[str] = field(default_factory=list)


def _sanitize_description(text: str) -> str:
    bad = set('&|<>^"%!\r\n')
    return "".join(c for c in text if c not in bad)[:200]


def scaffold_jobs(
    commands: UipCommands,
    design: DesignResult,
    *,
    run_dir_windows: str,
    description: str,
    timeout_s: float = 900.0,
) -> tuple[list[Job], str]:
    """Build the Windows-side jobs: init -> restore -> package installs. Returns (jobs, project_dir_windows)."""
    project_windows = windows_join(run_dir_windows, design.project_name)
    t = design.template
    init = commands.init(
        name=design.project_name,
        location_windows=run_dir_windows,
        target_framework=design.target_framework,
        expression_language=design.expression_language,
        description=_sanitize_description(description),
        template_id=t.template_id if t.package_id is None else None,
        template_package_id=t.package_id,
        template_package_version=t.package_version,
    )
    jobs = [
        Job(init, run_dir_windows, timeout_s, label="init"),
        Job(commands.restore(project_windows), project_windows, timeout_s, label="restore"),
    ]
    installs = [(p.id, p.version) for p in design.installs()]
    if installs:
        jobs.append(
            Job(
                commands.packages_install(installs, project_windows),
                project_windows,
                timeout_s,
                label="packages install",
            )
        )
    return jobs, project_windows


def check_scaffold_results(
    results: list[UipResult], project_dir: Path, jobs: list[Job]
) -> list[str]:
    """Return a list of hard errors (empty when the scaffold is usable)."""
    errors: list[str] = []
    for job, res in zip(jobs, results, strict=True):
        data = res.data if isinstance(res.data, dict) else {}
        if job.label == "init":
            success = bool(data.get("success", res.ok))
            if not res.ok or not success:
                if (project_dir / "project.json").is_file():
                    continue  # partial success: project.json exists
                errors.append(f"init failed: {res.message or res.stderr[-500:]}")
        elif job.label == "restore":
            if not res.ok or data.get("Success") is False:
                errors.append(f"restore failed: {res.message or res.stderr[-500:]}")
        elif job.label == "packages install":
            failed = data.get("failedPackages") or data.get("failed") or []
            if not res.ok or data.get("success") is False or failed:
                errors.append(
                    f"packages install failed: {failed or res.message or res.stderr[-500:]}"
                )
    if not (project_dir / "project.json").is_file():
        errors.append(f"project.json not found at {project_dir} after init")
    return errors


def finalize_project(
    project_dir: Path,
    *,
    plan_text: str,
    plan_name: str,
    bus: EventBus | None = None,
    spec_json: str | None = None,
) -> tuple[str | None, list[str]]:
    """Copy the plan in, add .gitattributes/.gitignore, git init + commit the pristine scaffold."""
    warnings: list[str] = []
    plans_dir = project_dir / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / plan_name).write_text(plan_text, encoding="utf-8", newline="\n")
    if spec_json is not None:  # the agent never sees the PDD; the spec is its reference
        (project_dir / "docs" / "process-spec.json").write_text(
            spec_json, encoding="utf-8", newline="\n"
        )
    ga = project_dir / ".gitattributes"
    if not ga.exists():
        ga.write_text(GITATTRIBUTES, encoding="utf-8", newline="\n")
    gi = project_dir / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if ".pdd2uipath/" not in existing:
        gi.write_text(
            existing.rstrip("\n") + ("\n" if existing else "") + GITIGNORE,
            encoding="utf-8",
            newline="\n",
        )
    git = shutil.which("git")
    if git is None:
        warnings.append("git not found on the brain side; scaffold not committed")
        return None, warnings
    try:
        if not (project_dir / ".git").exists():
            subprocess.run(
                [git, "init", "-q"],
                cwd=project_dir,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        subprocess.run(
            [git, "add", "-A"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        subprocess.run(
            [
                git,
                "-c",
                "user.name=pdd2uipath",
                "-c",
                "user.email=pdd2uipath@local",
                "commit",
                "-q",
                "-m",
                "pdd2uipath: pristine scaffold from uip rpa init",
                "--allow-empty",
            ],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        sha = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        warnings.append(f"git commit failed: {detail[-300:]}")
        return None, warnings
    if bus:
        bus.log(f"scaffold committed as {sha[:10]}")
    return sha, warnings


def git_commit_all(project_dir: Path, message: str) -> str | None:
    git = shutil.which("git")
    if git is None or not (project_dir / ".git").exists():
        return None
    try:
        subprocess.run(
            [git, "add", "-A"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        subprocess.run(
            [
                git,
                "-c",
                "user.name=pdd2uipath",
                "-c",
                "user.email=pdd2uipath@local",
                "commit",
                "-q",
                "-m",
                message,
                "--allow-empty",
            ],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def run_scaffold(
    executor: Executor,
    commands: UipCommands,
    design: DesignResult,
    *,
    run_dir_windows: str,
    run_dir: Path,
    description: str,
    plan_text: str,
    plan_name: str,
    timeout_s: float = 900.0,
    bus: EventBus | None = None,
    allow_existing: bool = False,
    spec_json: str | None = None,
) -> ScaffoldResult:
    jobs, project_windows = scaffold_jobs(
        commands,
        design,
        run_dir_windows=run_dir_windows,
        description=description,
        timeout_s=timeout_s,
    )
    project_dir = run_dir / design.project_name
    # ``allow_existing``: a pending wsl-manual round already ran ``init`` on Windows; the
    # executor reuses that round and only collects its results.
    if not allow_existing and (project_dir / "project.json").is_file():
        raise ScaffoldError(
            f"a project already exists at {project_windows}; remove it or use a new run directory"
        )
    results = executor.run_batch(jobs)
    errors = check_scaffold_results(results, project_dir, jobs)
    if errors:
        raise ScaffoldError("; ".join(errors))
    commit, warnings = finalize_project(
        project_dir, plan_text=plan_text, plan_name=plan_name, bus=bus, spec_json=spec_json
    )
    return ScaffoldResult(
        project_dir_windows=project_windows,
        project_dir=project_dir,
        results=results,
        commit=commit,
        warnings=warnings,
        installed=[p.id for p in design.installs()],
    )

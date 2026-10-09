"""Stage 0: ``pdd2uipath doctor`` - preflight checks with fix hints, executor
recommendation, and the live CLI-surface probe (``docs/cli-surface.md``)."""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pdd2uipath.core.config import (
    ExecutorKind,
    Settings,
    default_user_config_path,
    settings_source_path,
)
from pdd2uipath.core.paths import PathTranslationError, brain_path, to_brain, to_windows
from pdd2uipath.core.platform import Platform, detect_platform
from pdd2uipath.executors.base import Executor, ExecutorError, UipResult
from pdd2uipath.executors.bridge_protocol import heartbeat_age
from pdd2uipath.executors.factory import (
    brain_executor,
    bridge_layout,
    build_executor,
    discover_windows_uip,
    recommend_executor,
)
from pdd2uipath.executors.surface import CliSurface, probe_surface, save_surface

Status = Literal["ok", "warn", "fail", "skip"]

NPM_REGISTRY = "https://registry.npmjs.org/"
GITHUB_NPM = "https://npm.pkg.github.com/"
UIPATH_FEED = (
    "https://pkgs.dev.azure.com/uipath/Public.Feeds/_packaging/UiPath-Official/nuget/v3/index.json"
)
GITHUB_API = "https://api.github.com/"
PROXY_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "PIP_INDEX_URL",
    "NPM_CONFIG_REGISTRY",
)


@dataclass
class Check:
    id: str
    title: str
    status: Status
    detail: str = ""
    fix: str = ""


@dataclass
class DoctorReport:
    platform: Platform
    checks: list[Check] = field(default_factory=list)
    recommended_executor: ExecutorKind = ExecutorKind.WSL_MANUAL
    windows_uip_command: list[str] = field(default_factory=list)
    brain_uip_command: list[str] = field(default_factory=list)
    skills_root: str | None = None
    surface: CliSurface | None = None
    surface_md: Path | None = None
    agent_backend: str = ""

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    def add(self, id_: str, title: str, status: Status, detail: str = "", fix: str = "") -> Check:
        c = Check(id_, title, status, detail, fix)
        self.checks.append(c)
        return c


def _run(cmd: list[str], timeout: float = 60.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def _reachable(url: str, timeout: float = 6.0) -> tuple[bool, str]:
    ctx = ssl.create_default_context()
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if bundle and Path(bundle).is_file():
        try:
            ctx.load_verify_locations(bundle)
        except ssl.SSLError as exc:
            return False, f"CA bundle {bundle}: {exc}"
    req = urllib.request.Request(url, headers={"User-Agent": "pdd2uipath-doctor"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code} (reachable)"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"{reason}"


def _version_line(result: UipResult) -> str:
    text = (result.stdout or result.stderr).strip().splitlines()
    return text[-1] if text else result.message


def find_skill_root(settings: Settings, home: Path | None = None) -> tuple[Path | None, str]:
    """Locate the uipath-rpa skill directory (never assume one path)."""
    home = home or Path.home()
    candidates: list[tuple[Path, str]] = []
    if settings.skills_root:
        candidates.append((brain_path(settings.skills_root), "config skills_root"))
    if os.name != "nt" and Path("/mnt/c/Users").is_dir():
        for user_dir in sorted(Path("/mnt/c/Users").glob("*")):
            candidates.append(
                (
                    user_dir / ".uipath" / ".skills" / "skills" / "uipath-rpa",
                    "uip skills store (Windows user)",
                )
            )
    candidates += [
        (home / ".uipath" / ".skills" / "skills" / "uipath-rpa", "uip skills store"),
        (home / ".config" / "opencode" / "skills" / "uipath-rpa", "opencode global skills"),
        (home / ".copilot" / "skills" / "uipath-rpa", "copilot global skills"),
        (Path.cwd() / ".opencode" / "skills" / "uipath-rpa", "project .opencode/skills"),
        (Path.cwd() / ".github" / "skills" / "uipath-rpa", "project .github/skills"),
        (home / ".claude" / "skills" / "uipath-rpa", "claude skills"),
    ]
    for path, where in candidates:
        if (path / "SKILL.md").is_file():
            return path, where
    return None, "not found"


def opencode_model_status(code: int, output: str, model: str) -> tuple[Status, str, str]:
    """Judge a configured ``provider/model`` against ``opencode models <provider>`` output."""
    provider = model.split("/", 1)[0]
    listed = [
        line.strip() for line in output.splitlines() if line.strip().startswith(provider + "/")
    ]
    if model in listed:
        return "ok", f"listed by `opencode models {provider}`", ""
    if code != 0 or not listed:
        return (
            "fail",
            f"provider {provider!r} is not available to OpenCode ({output.strip().splitlines()[-1] if output.strip() else 'no output'})",
            "`opencode auth login` for that provider. GitHub Copilot: OpenCode keeps only the models the "
            "Copilot API flags model_picker_enabled for your plan (Copilot Free has none and rejects "
            "explicit models with HTTP 400): use an Enterprise/Pro seat, or the Copilot CLI with model auto",
        )
    return (
        "fail",
        f"{model} is not listed; available: {', '.join(listed[:8])}{' ...' if len(listed) > 8 else ''}",
        "Pick one of the listed ids for [agent]/[extraction] model",
    )


def _model_checks(
    rep: DoctorReport,
    settings: Settings,
    *,
    opencode: str | None,
    agent_backend: str,
    extraction_backend: str,
) -> None:
    for scope, backend, model in (
        ("agent", agent_backend, settings.agent.model),
        ("extraction", extraction_backend, settings.extraction.model),
    ):
        if not model:
            continue
        if backend == "opencode" and opencode:
            if "/" not in model:
                rep.add(
                    f"{scope}-model",
                    f"{scope}: OpenCode model {model}",
                    "fail",
                    "OpenCode models are written provider/model",
                    "e.g. github-copilot/gpt-5-mini or ollama/qwen2.5-coder:32b (see `opencode models`)",
                )
                continue
            code, out = _run([opencode, "models", model.split("/", 1)[0]], 120)
            status, detail, fix = opencode_model_status(code, out, model)
            rep.add(f"{scope}-model", f"{scope}: OpenCode model {model}", status, detail, fix)
        elif backend == "copilot" and model != "auto":
            rep.add(
                f"{scope}-model",
                f"{scope}: Copilot CLI model {model}",
                "warn",
                "not verified: the CLI accepts only models enabled in your plan's model picker",
                f'check with `copilot -p "Reply OK" --model {model}` ("not available" costs no request); '
                "Copilot Free offers only `auto`",
            )


def run_doctor(
    settings: Settings,
    *,
    platform: Platform | None = None,
    probe_studio: bool = True,
    probe_network: bool = True,
    probe_surface_flag: bool = True,
    surface_md_path: Path | None = None,
) -> DoctorReport:
    platform = platform or detect_platform()
    rep = DoctorReport(platform=platform)

    # 1. platform + interop
    if platform.is_windows:
        rep.add(
            "platform",
            "Brain runs on native Windows",
            "ok",
            f"Python {sys.version.split()[0]} on win32",
        )
    elif platform.is_wsl:
        rep.add(
            "platform",
            f"Brain runs in WSL ({platform.distro or 'unknown distro'})",
            "ok",
            f"Python {sys.version.split()[0]}",
        )
        if platform.interop_enabled:
            rep.add(
                "interop",
                "WSL interop (binfmt) enabled",
                "ok",
                "Windows executables can be started from WSL",
                'If your VDI policy forbids interop, set executor = "wsl-bridge" in pdd2uipath.toml',
            )
        else:
            rep.add(
                "interop",
                "WSL interop disabled",
                "warn",
                "/proc/sys/fs/binfmt_misc/WSLInterop missing or disabled",
                "Use the wsl-bridge executor (pdd2uipath bridge install, then start worker.cmd in Windows)",
            )
    else:
        rep.add(
            "platform",
            "Brain runs on plain Linux/macOS",
            "fail",
            "no Windows side reachable from here",
            "Run inside WSL (or on Windows); the uip rpa verbs need the Windows session",
        )

    # 2. workspace root
    try:
        root_w = to_windows(settings.workspace_root, platform)
        root_b = to_brain(root_w, platform)
        try:
            root_b.mkdir(parents=True, exist_ok=True)
            probe = root_b / ".pdd2uipath-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            rep.add(
                "workspace",
                "Workspace root on the Windows filesystem",
                "ok",
                f"{root_w} (brain path {root_b})",
            )
        except OSError as exc:
            rep.add(
                "workspace",
                "Workspace root not writable",
                "fail",
                f"{root_w}: {exc}",
                "Set workspace_root in pdd2uipath.toml to a writable C:\\ path",
            )
    except PathTranslationError as exc:
        rep.add(
            "workspace",
            "Workspace root invalid",
            "fail",
            str(exc),
            'Set workspace_root = "C:\\\\Users\\\\<you>\\\\RPA\\\\pdd2uipath-projects" in pdd2uipath.toml',
        )

    # 3. python / uv / git
    rep.add("python", "Python >= 3.12", "ok", sys.version.split()[0])
    uv = shutil.which("uv")
    rep.add(
        "uv",
        "uv available",
        "ok" if uv else "warn",
        uv or "not on PATH",
        ""
        if uv
        else "Install uv (pip install uv, or the standalone installer via your Artifactory mirror)",
    )
    git = shutil.which("git")
    rep.add(
        "git",
        "git available",
        "ok" if git else "warn",
        git or "not on PATH",
        "" if git else "Install git so scaffolds and authoring rounds are committed",
    )

    # 4. brain-side uip
    brain = brain_executor(settings, platform)
    if brain is None:
        rep.add(
            "uip-brain",
            "uip on the brain side",
            "warn",
            "not found (only needed for `uip skills` here)",
            "npm i -g @uipath/cli (through your registry mirror)",
        )
    else:
        res = brain.run(["--version"], timeout_s=60)
        rep.brain_uip_command = brain.command
        rep.add(
            "uip-brain",
            "uip on the brain side",
            "ok" if res.ok else "warn",
            f"{' '.join(brain.command)} -> {_version_line(res)}",
        )

    # 5. Windows-side uip + rpa tool (+ .NET)
    win_cmd = list(settings.uip_windows_command) or discover_windows_uip(platform)
    rep.windows_uip_command = win_cmd
    windows_exec: Executor | None = None
    kind = recommend_executor(platform)
    if win_cmd and (platform.is_windows or (platform.is_wsl and platform.interop_enabled)):
        try:
            windows_exec = build_executor(
                settings,
                platform,
                kind=ExecutorKind.NATIVE if platform.is_windows else ExecutorKind.WSL_INTEROP,
            )
            res = windows_exec.run(["--version"], timeout_s=120)
            if res.ok:
                rep.add(
                    "uip-windows",
                    "uip on the Windows side",
                    "ok",
                    f"{' '.join(win_cmd)} -> {_version_line(res)}",
                )
            else:
                rep.add(
                    "uip-windows",
                    "uip on the Windows side failed",
                    "fail",
                    _version_line(res),
                    "Install @uipath/cli on Windows: npm i -g @uipath/cli",
                )
                windows_exec = None
        except ExecutorError as exc:
            rep.add("uip-windows", "uip on the Windows side unreachable", "fail", str(exc))
    elif win_cmd:
        rep.add(
            "uip-windows",
            "Windows uip located (not probed: no interop)",
            "warn",
            " ".join(win_cmd),
            "The bridge worker will run it in your Windows session",
        )
    else:
        rep.add(
            "uip-windows",
            "Windows uip not found",
            "fail",
            "no uip.cmd under %APPDATA%\\npm",
            "In Windows: npm i -g @uipath/cli, then uip tools install @uipath/rpa-tool",
        )
    if windows_exec is not None:
        res = windows_exec.run(["rpa", "--help"], timeout_s=180)
        text = res.stdout + res.stderr
        if "init" in text and "validate" in text:
            rep.add(
                "rpa-tool",
                "uip rpa tool installed on Windows",
                "ok",
                "verbs: init, validate, build, packages, templates ...",
            )
        else:
            rep.add(
                "rpa-tool",
                "uip rpa tool missing on Windows",
                "fail",
                res.message[:200],
                "npm config set @uipath:registry https://npm.pkg.github.com && uip tools install @uipath/rpa-tool (GitHub npm registry; needs a GitHub token with read:packages)",
            )
        res = windows_exec.run(["login", "status"], timeout_s=120)
        data = res.data if isinstance(res.data, dict) else {}
        status = str(data.get("Status", res.message or "unknown"))
        rep.add(
            "login-windows",
            "uip login (Windows side)",
            "ok" if status.lower() == "logged in" or "Logged in" in status else "warn",
            status,
            ""
            if "Logged in" in status
            else "uip login (Windows) - needed for Marketplace/tenant feeds; Official templates and packages work without it",
        )
        dotnet_cmd = (
            ["dotnet", "--list-runtimes"]
            if platform.is_windows
            else ["/mnt/c/Program Files/dotnet/dotnet.exe", "--list-runtimes"]
        )
        code, out = _run(dotnet_cmd, 60)
        if code == 0 and "Microsoft.NETCore.App" in out:
            rep.add(
                "dotnet",
                ".NET runtime on Windows",
                "ok",
                ", ".join(
                    sorted(
                        {
                            line.split()[1]
                            for line in out.splitlines()
                            if line.startswith("Microsoft.NETCore.App")
                        }
                    )
                ),
            )
        else:
            rep.add(
                "dotnet",
                ".NET runtime on Windows",
                "warn",
                "not detected",
                "Install the .NET 8 runtime (required by the headless Studio host)",
            )
        if brain is not None:
            code, out = (
                _run([*brain.command, "login", "status", "--output", "json"], 60)
                if brain.name == "brain"
                else (1, "")
            )
            if code == 0 and "Logged in" in out:
                rep.add("login-brain", "uip login (brain side)", "ok", "logged in")
            else:
                rep.add(
                    "login-brain",
                    "uip login (brain side)",
                    "warn",
                    "not logged in (separate from the Windows login)",
                    "uip login (brain) only if you need cloud features from WSL",
                )

    # 6. skill
    skill_root, where = find_skill_root(settings)
    if skill_root is not None:
        rep.skills_root = str(skill_root)
        rep.add("skill", "uipath-rpa skill found", "ok", f"{skill_root} ({where})")
    else:
        rep.add(
            "skill",
            "uipath-rpa skill not found",
            "fail",
            "no SKILL.md in the known locations",
            "uip skills install --agent opencode   (or --agent copilot); pdd2uipath copies it into the project for the chosen agent",
        )

    # 7. coding agents + extraction backend
    opencode = shutil.which("opencode") or shutil.which("opencode.cmd")
    copilot = shutil.which("copilot") or shutil.which("copilot.cmd")
    backend = settings.agent.backend
    if backend == "auto":
        backend = "opencode" if opencode else "copilot" if copilot else ""
    rep.agent_backend = backend
    if opencode:
        code, out = _run([opencode, "--version"], 60)
        rep.add(
            "agent-opencode",
            "OpenCode CLI",
            "ok",
            f"{opencode} ({out.splitlines()[-1] if out else '?'})",
        )
    else:
        rep.add(
            "agent-opencode",
            "OpenCode CLI",
            "warn" if backend != "opencode" else "fail",
            "not on PATH",
            "npm i -g opencode-ai (through your registry mirror) and configure the local provider in opencode.json",
        )
    if copilot:
        code, out = _run([copilot, "--version"], 60)
        rep.add(
            "agent-copilot",
            "GitHub Copilot CLI",
            "ok",
            f"{copilot} ({out.splitlines()[0] if out else '?'})",
            "Run `copilot` once interactively and /login if you have not authenticated yet",
        )
    else:
        rep.add(
            "agent-copilot",
            "GitHub Copilot CLI",
            "warn" if backend != "copilot" else "fail",
            "not on PATH",
            "npm i -g @github/copilot, then `copilot` and /login",
        )
    if not backend:
        rep.add(
            "agent",
            "No authoring agent available",
            "fail",
            "neither opencode nor copilot found",
            "Install one of them; set [agent] backend in pdd2uipath.toml",
        )
    else:
        rep.add("agent", "Authoring agent", "ok", backend)
    ex = settings.extraction
    ex_kind = ex.backend
    if ex_kind == "auto":
        ex_kind = (
            "openai-compatible"
            if ex.base_url
            else "opencode"
            if opencode
            else "copilot"
            if copilot
            else ""
        )
    if ex_kind == "openai-compatible":
        if probe_network:
            okr, detail = _reachable(ex.base_url.rstrip("/") + "/models")
            rep.add(
                "extraction",
                f"Extraction backend: openai-compatible ({ex.model or 'model?'})",
                "ok" if okr else "fail",
                f"{ex.base_url}: {detail}",
                ""
                if okr
                else "Start the local LLM server (e.g. Ollama/vLLM) or fix [extraction] base_url",
            )
        else:
            rep.add("extraction", "Extraction backend: openai-compatible", "ok", ex.base_url)
    elif ex_kind:
        rep.add("extraction", f"Extraction backend: {ex_kind}", "ok", ex.model or "default model")
    else:
        rep.add(
            "extraction",
            "No extraction backend",
            "fail",
            "",
            "Set [extraction] base_url (local LLM) or install opencode/copilot",
        )

    _model_checks(
        rep, settings, opencode=opencode, agent_backend=backend, extraction_backend=ex_kind
    )

    # 8. Studio Desktop / Helm
    from pdd2uipath.core.runner import find_studio_exe

    if platform.is_windows:
        exe = find_studio_exe()
        rep.add(
            "studio",
            "Studio Desktop (optional)",
            "ok" if exe else "warn",
            exe or "not found",
            "" if exe else "Only needed for the human hand-off; uip rpa uses the headless host",
        )
        helm = Path(os.environ.get("LOCALAPPDATA", "")) / "UiPath" / "Helm"
        rep.add(
            "helm",
            "Headless Studio host cache",
            "ok" if helm.is_dir() else "warn",
            str(helm)
            if helm.is_dir()
            else "not downloaded yet (first uip rpa call restores it, 30-90 s)",
        )
    if probe_studio and windows_exec is not None:
        res = windows_exec.run(
            [
                "rpa",
                "templates",
                "search",
                "--query",
                "REFramework",
                "--limit",
                "1",
                "--output",
                "json",
            ],
            timeout_s=300,
        )
        if res.ok:
            rep.add(
                "studio-host",
                "Headless Studio host responds (licensed)",
                "ok",
                f"templates search ok in {res.duration_s:.0f}s",
            )
        elif "licence" in res.message.lower() or "license" in res.message.lower():
            rep.add(
                "studio-host",
                "Headless Studio host has no licence",
                "fail",
                res.message[:200],
                "Open Studio Desktop and sign in with a Studio-licensed account (keep it open), or `uip login` as a licensed user; then retry",
            )
        else:
            rep.add(
                "studio-host",
                "Headless Studio host probe failed",
                "warn",
                res.message[:200],
                "First call restores NuGet packages (30-90 s); re-run doctor, raise --timeout, check proxy/CA for the UiPath feed",
            )

    # 9. proxy / CA / reachability
    env_set = {k: os.environ[k] for k in PROXY_VARS if os.environ.get(k)}
    rep.add(
        "proxy",
        "Proxy / CA environment",
        "ok",
        ", ".join(f"{k}={'<set>' if 'PROXY' not in k else v}" for k, v in env_set.items())
        or "none set (direct connection)",
    )
    if probe_network:
        for cid, title, url, fix in (
            (
                "net-npm",
                "npm registry",
                os.environ.get("NPM_CONFIG_REGISTRY") or NPM_REGISTRY,
                "Point npm at your Artifactory npm remote: npm config set registry <url>",
            ),
            (
                "net-ghnpm",
                "GitHub npm registry (@uipath/rpa-tool)",
                GITHUB_NPM,
                "Needed once to install @uipath/rpa-tool; mirror it in Artifactory or install from an allowed host",
            ),
            (
                "net-nuget",
                "UiPath Official NuGet feed",
                UIPATH_FEED,
                "The headless Studio host and packages install need this feed (or a NuGet mirror configured in Studio settings)",
            ),
            (
                "net-github",
                "GitHub API (Copilot CLI)",
                GITHUB_API,
                "Only needed when the Copilot CLI backend is used",
            ),
        ):
            okr, detail = _reachable(url)
            rep.add(cid, title, "ok" if okr else "warn", f"{url} -> {detail}", "" if okr else fix)

    # 9b. MCP servers and recipes
    from pdd2uipath.mcp.recipes import resolve_servers

    try:
        mcp_servers = resolve_servers(settings)
    except Exception as exc:
        mcp_servers = {}
        rep.add("mcp", "MCP servers", "fail", f"config error: {exc}", "fix [mcp] in the config")
    if not mcp_servers:
        rep.add(
            "mcp",
            "MCP servers",
            "skip",
            "none configured",
            "optional: [mcp.servers.<name>] in the config, or mcp.import_opencode = true",
        )
    for mcp_name, mcp_cfg in mcp_servers.items():
        target = " ".join(mcp_cfg.command) if mcp_cfg.transport == "stdio" else mcp_cfg.url
        if not probe_network:
            rep.add(
                f"mcp-{mcp_name}",
                f"MCP server {mcp_name}",
                "skip",
                f"{mcp_cfg.transport}: {target}",
                "",
            )
            continue
        try:
            from pdd2uipath.mcp.client import McpClient

            probe_cfg = mcp_cfg.model_copy(update={"timeout_s": min(mcp_cfg.timeout_s, 20.0)})
            with McpClient(mcp_name, probe_cfg) as client:
                tool_count = len(client.list_tools())
                server_name = client.server_info.get("name", "?")
            rep.add(
                f"mcp-{mcp_name}",
                f"MCP server {mcp_name}",
                "ok",
                f"{server_name}: {tool_count} tool(s) via {mcp_cfg.transport}",
            )
        except Exception as exc:
            rep.add(
                f"mcp-{mcp_name}",
                f"MCP server {mcp_name}",
                "fail",
                str(exc)[:200],
                "check the command/url, the ${VAR} environment variables and that the server runs",
            )
    if settings.mcp.recipes:
        bad = [k for k, r in settings.mcp.recipes.items() if r.server not in mcp_servers]
        rep.add(
            "mcp-recipes",
            "MCP recipes",
            "warn" if bad else "ok",
            ", ".join(f"{k} -> {r.server}" for k, r in settings.mcp.recipes.items()),
            f"recipes without a configured server: {', '.join(bad)}" if bad else "",
        )

    # 10. bridge status
    if platform.is_wsl or settings.executor in (ExecutorKind.WSL_BRIDGE, ExecutorKind.WSL_MANUAL):
        try:
            layout = bridge_layout(settings, platform)
            age = heartbeat_age(layout)
            installed = layout.worker_cmd.exists()
            if age is not None and age <= settings.bridge.heartbeat_timeout_s:
                rep.add(
                    "bridge",
                    "Bridge worker online",
                    "ok",
                    f"heartbeat {age:.0f}s ago at {layout.root_windows}",
                )
            else:
                rep.add(
                    "bridge",
                    "Bridge worker",
                    "warn",
                    "installed, not running" if installed else "not installed",
                    "pdd2uipath bridge install; then start worker.cmd in your Windows session"
                    if kind != ExecutorKind.WSL_INTEROP
                    else "",
                )
        except PathTranslationError:
            pass

    # 11. executor recommendation
    if platform.is_wsl and platform.interop_enabled and windows_exec is not None:
        kind = ExecutorKind.WSL_INTEROP
    elif platform.is_wsl:
        kind = ExecutorKind.WSL_BRIDGE
    rep.recommended_executor = kind
    rep.add(
        "executor",
        "Recommended executor",
        "ok",
        kind.value,
        'Override with executor = "..." in pdd2uipath.toml (wsl-bridge is the VDI default when interop is blocked; wsl-manual when no long-running worker is possible)',
    )

    # 12. CLI surface probe
    if probe_surface_flag and windows_exec is not None:
        surface = probe_surface(windows_exec, timeout_s=180)
        rep.surface = surface
        cfg = settings_source_path(settings) or default_user_config_path()
        json_path = cfg.parent / "cli-surface.json"
        md_path = surface_md_path or (
            Path.cwd() / "docs" / "cli-surface.md"
            if (Path.cwd() / "docs").is_dir()
            else cfg.parent / "cli-surface.md"
        )
        save_surface(surface, json_path, md_path)
        rep.surface_md = md_path
        missing = [
            op
            for op in (
                "init",
                "validate",
                "build",
                "templates_search",
                "packages_install",
                "packages_versions",
                "restore",
            )
            if not any(
                surface.has(p)
                for p in __import__(
                    "pdd2uipath.executors.surface", fromlist=["CANONICAL_OPS"]
                ).CANONICAL_OPS[op]
            )
        ]
        rep.add(
            "cli-surface",
            "Live CLI surface recorded",
            "ok" if not missing else "fail",
            f"{md_path} ({len(surface.verbs)} verbs)"
            + (f"; missing operations: {missing}" if missing else ""),
            ""
            if not missing
            else "Update @uipath/cli / @uipath/rpa-tool; the adapter refuses to guess verb names",
        )
    return rep


def apply_recommendation(settings: Settings, report: DoctorReport, path: Path) -> Path:
    """Write the recommended executor and discovered paths into the config file."""
    from pdd2uipath.core.config import save_settings

    settings.executor = report.recommended_executor
    if report.windows_uip_command:
        settings.uip_windows_command = report.windows_uip_command
    if report.brain_uip_command:
        settings.uip_brain_command = report.brain_uip_command
    if report.skills_root:
        settings.skills_root = report.skills_root
    if report.agent_backend and settings.agent.backend == "auto":
        settings.agent.backend = report.agent_backend
    return save_settings(settings, path)

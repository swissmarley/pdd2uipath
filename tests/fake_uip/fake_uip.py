"""A fake ``uip`` CLI for tests.

Records every invocation to ``$FAKE_UIP_LOG`` (JSON lines) and returns response
envelopes shaped like the real @uipath/cli 1.203 ``rpa`` tool (captured 2026-10-09).
Scenarios are selected with ``$FAKE_UIP_SCENARIO`` (comma-separated flags):

  templates_zero | templates_multi_official | templates_marketplace_only
  init_fail | init_partial | install_fail | validate_errors | build_fail
  stub_clean (treat TODO-Indicate stubs as clean) | exit_nonzero | slow | logged_in
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

VERSION = "9.9.9-fake"
STUB_ERROR = "Target or Input UI Element must be set."

HELP_TOP = """  UiPath CLI 9.9.9-fake
┌─ Commands ───────────────────────────────────────────────────────────────────┐
│ login      Login to UiPath Cloud                                             │
│ logout     Logout                                                            │
│ config     View or update CLI configuration preferences                      │
│ tools      Manage CLI tools                                                  │
│ skills     Install and manage UiPath skills for AI coding agents.            │
└──────────────────────────────────────────────────────────────────────────────┘
"""

HELP_RPA = """                                    uip rpa
┌─ Usage ──────────────────────────────────────────────────────────────────────┐
│ uip rpa [options] [command]                                                  │
└──────────────────────────────────────────────────────────────────────────────┘
┌─ Commands ───────────────────────────────────────────────────────────────────┐
│ activities                     Discover and inspect activities               │
│ analyze                        Validate and analyze a UiPath project         │
│ build                          Build (compile) a UiPath project              │
│ init                           Initialize a new UiPath project               │
│ instances                      Discover running Studio instances             │
│ packages                       Manage NuGet packages used by the project     │
│ restore                        Restore NuGet dependencies                    │
│ studio                         Control the Studio process                    │
│ templates                      Discover project templates                    │
│ validate                       Validate a single file or the entire project  │
└──────────────────────────────────────────────────────────────────────────────┘
"""

HELP_VERBS: dict[str, str] = {
    "templates search": "│ uip rpa templates search [--query <string>] [--limit <integer>] [--include-prerelease] │",
    "init": "│ uip rpa init --name <string> [--template-id <string>] [--location <string>] [--description <string>] [--expression-language { VisualBasic | CSharp }] [--target-framework { Legacy | Windows | Portable }] [--template-package-id <string>] [--template-package-version <string>] │",
    "restore": "│ uip rpa restore [options] [project-dir] [output-path]                        │",
    "packages versions": "│ uip rpa packages versions --package-id <string> [--include-prerelease]       │",
    "packages install": "│ uip rpa packages install --packages <key=value> [--packages-file <path>]     │",
    "activities find": "│ uip rpa activities find --query <string> [--tags <string>] [--limit <integer>] │",
    "validate": "│ uip rpa validate [--file-path <string>] [--skip-validation] [--min-severity { error | warning | info | verbose }] │",
    "build": "│ uip rpa build [options] [project-dir]                                        │",
    "analyze": "│ uip rpa analyze [options] [project-dir]                                      │",
    "studio start": "│ uip rpa studio start [options]                                               │",
    "instances list": "│ uip rpa instances list [options]                                             │",
}

COMMON_FLAGS = (
    "│ --project-dir <path>  │ --timeout <seconds>  │ --output <table|json|yaml|plain|markdown> │"
)


def _help_for(verb: str) -> str:
    usage = HELP_VERBS[verb]
    box = (
        f"                                  uip rpa {verb}\n"
        "┌─ Usage ──────────────────────────────────────────────────────────────────────┐\n"
        f"{usage}\n"
        "└──────────────────────────────────────────────────────────────────────────────┘\n"
    )
    if verb in ("build", "restore", "analyze"):
        box += (
            "┌─ Arguments ──────────────────────────────────────────────────────────────────┐\n"
            "│ project-dir   Path to the UiPath project directory.                          │\n"
            "└──────────────────────────────────────────────────────────────────────────────┘\n"
        )
    box += (
        "┌─ Options ────────────────────────────────────────────────────────────────────┐\n"
        f"{COMMON_FLAGS}\n"
        "└──────────────────────────────────────────────────────────────────────────────┘\n"
    )
    return box


MINIMAL_XAML = """<Activity mc:Ignorable="sap sap2010" x:Class="{cls}" xmlns="http://schemas.microsoft.com/netfx/2009/xaml/activities" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:sap="http://schemas.microsoft.com/netfx/2009/xaml/activities/presentation" xmlns:sap2010="http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation" xmlns:ui="http://schemas.uipath.com/workflow/activities" xmlns:uix="http://schemas.uipath.com/workflow/activities/uix" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
  <Sequence DisplayName="{cls}" sap2010:WorkflowViewState.IdRef="Sequence_1">
    <ui:LogMessage DisplayName="Log Message" Level="Info" Message="[&quot;{cls}&quot;]" />
  </Sequence>
</Activity>
"""

REF_FILES = [
    "Framework/InitAllSettings.xaml",
    "Framework/InitAllApplications.xaml",
    "Framework/GetTransactionData.xaml",
    "Framework/Process.xaml",
    "Framework/SetTransactionStatus.xaml",
    "Framework/RetryCurrentTransaction.xaml",
    "Framework/CloseAllApplications.xaml",
    "Framework/KillAllProcesses.xaml",
    "Framework/TakeScreenshot.xaml",
    "Tests/MainTestCase.xaml",
]

TEMPLATES_OFFICIAL = {
    "packageId": "UiPath.Template.REFramework",
    "version": "26.10.1",
    "title": "NewBusinessProcessProjectTemplateTitle",
    "description": "NewBusinessProcessProjectTemplateDescription",
    "authors": "UiPath",
    "source": "Official",
    "tags": ["UiPathStudioTemplate", "UiPathStudioProcess"],
}
TEMPLATES_MARKET = [
    {
        "packageId": "Enhanced.REFrameWork",
        "version": "1.0.1",
        "title": "Enhanced REFrameWork",
        "description": "A UiPath Studio template ...",
        "authors": "Internal Labs",
        "source": "Marketplace",
        "tags": ["VisualBasic", "VB"],
    },
    {
        "packageId": "Lite.ReFrameWork",
        "version": "1.1.1",
        "title": "Lite ReFrameWork",
        "description": "A lighter version of REFramework",
        "authors": "Internal Labs",
        "source": "Marketplace",
        "tags": ["VisualBasic", "VB"],
    },
]


def scenario() -> set[str]:
    return {s.strip() for s in os.environ.get("FAKE_UIP_SCENARIO", "").split(",") if s.strip()}


def log_call(argv: list[str]) -> None:
    path = os.environ.get("FAKE_UIP_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "ts": time.time()}) + "\n")


def emit(obj: dict, code: int = 0, stderr: str = "") -> int:
    if stderr:
        sys.stderr.write(stderr)
    sys.stdout.write(json.dumps(obj, indent=2) + "\n")
    return code


def failure(message: str, code: str = "unknown_error", instructions: str = "") -> int:
    return emit(
        {
            "Result": "Failure",
            "ErrorCode": code,
            "Message": message,
            "Instructions": instructions
            or "If this is not the expected result check command arguments.",
            "Retry": "RetryWillNotFix",
        },
        1,
    )


def opt(argv: list[str], flag: str) -> str | None:
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


def opts(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == flag and i + 1 < len(argv)]


def positional(argv: list[str], start: int) -> str | None:
    skip = False
    for tok in argv[start:]:
        if skip:
            skip = False
            continue
        if tok.startswith("--"):
            skip = tok not in ("--include-prerelease", "--skip-validation", "--skip-analyze")
            continue
        return tok
    return None


def project_dir(argv: list[str], verb_index: int) -> Path:
    p = opt(argv, "--project-dir") or positional(argv, verb_index + 1) or os.getcwd()
    return Path(p)


def xaml_files(project: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(project.rglob("*.xaml")):
        rel = p.relative_to(project).as_posix()
        if rel.startswith((".local/", ".templates/", ".tmh/", ".screenshots/")):
            continue
        out.append(p)
    return out


def diagnostics_for(project: Path, file: Path, sc: set[str]) -> list[str]:
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [f"Could not load {file} file: 'file not found'."]
    msgs: list[str] = []
    if "BROKEN" in text or "validate_errors" in sc:
        msgs.append(
            f"Could not load {file} file: 'Cannot set unknown member "
            "'UiPath.UIAutomationNext.Activities.NApplicationCard.ApplicationWindow'.' "
            "Line number '19' and line position '27'."
        )
    if "TODO Indicate" in text and re.search(r"<uix:N\w+", text) and "stub_clean" not in sc:
        n = len(re.findall(r"<uix:N(?:Click|TypeInto|GetText|SelectItem|Check|Hover)\b", text))
        msgs.extend([STUB_ERROR] * max(n, 1))
    return msgs


def main(argv: list[str]) -> int:
    log_call(argv)
    sc = scenario()
    if "slow" in sc:
        time.sleep(float(os.environ.get("FAKE_UIP_SLOW_S", "5")))
    if "exit_nonzero" in sc:
        sys.stdout.write("garbage output\n")
        sys.stderr.write("fatal: simulated crash\n")
        return 3
    if not argv or argv[0] in ("--help", "-h"):
        sys.stdout.write(HELP_TOP)
        return 0
    if argv[0] in ("--version", "-v"):
        sys.stdout.write(VERSION + "\n")
        return 0
    if argv[0] == "login":
        if len(argv) > 1 and argv[1] == "status":
            logged = "logged_in" in sc
            return emit(
                {
                    "Result": "Success",
                    "Code": "LoginStatus",
                    "Data": {
                        "Status": "Logged in" if logged else "Not logged in",
                        "Organization": "acme" if logged else "N/A",
                        "Tenant": "DefaultTenant" if logged else "N/A",
                    },
                }
            )
        return failure("error: unknown command", "invalid_argument")
    if argv[0] != "rpa":
        return failure(f"error: unknown command '{argv[0]}'", "invalid_argument")
    rest = argv[1:]
    if not rest or rest[0] in ("--help", "-h"):
        sys.stdout.write(HELP_RPA)
        return 0
    verb = rest[0]
    sub = rest[1] if len(rest) > 1 and not rest[1].startswith("-") else ""
    key = f"{verb} {sub}".strip()
    if "--help" in rest or "-h" in rest:
        if key in HELP_VERBS:
            sys.stdout.write(_help_for(key))
            return 0
        if verb in HELP_VERBS:
            sys.stdout.write(_help_for(verb))
            return 0
        return failure(f"error: unknown command '{verb}'", "invalid_argument")

    if key == "templates search":
        data: list[dict]
        if "templates_zero" in sc:
            data = []
        elif "templates_multi_official" in sc:
            data = [
                TEMPLATES_OFFICIAL,
                {
                    **TEMPLATES_OFFICIAL,
                    "packageId": "UiPath.Template.REFramework.CSharp",
                    "title": "REFramework C#",
                },
            ]
        elif "templates_marketplace_only" in sc:
            data = list(TEMPLATES_MARKET)
        else:
            data = [TEMPLATES_OFFICIAL, *TEMPLATES_MARKET]
        return emit({"Result": "Success", "Code": "ToolResult", "Data": data})

    if verb == "init":
        name = opt(rest, "--name") or "Project"
        location = Path(opt(rest, "--location") or os.getcwd())
        tf = opt(rest, "--target-framework") or "Windows"
        lang = opt(rest, "--expression-language") or "VisualBasic"
        pkg = opt(rest, "--template-package-id")
        if "init_fail" in sc:
            return failure("IInteropProjectService.CreateProject threw: simulated failure")
        proj = location / name
        proj.mkdir(parents=True, exist_ok=True)
        deps = {"UiPath.System.Activities": "[26.8.2]"}
        if pkg and "REFramework" in pkg:
            deps.update(
                {
                    "UiPath.Excel.Activities": "[3.2.1]",
                    "UiPath.Testing.Activities": "[25.10.3]",
                    "UiPath.UIAutomation.Activities": "[26.10.5]",
                }
            )
            for rel in REF_FILES:
                f = proj / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(MINIMAL_XAML.format(cls=Path(rel).stem), encoding="utf-8")
            (proj / "Data").mkdir(exist_ok=True)
            (proj / "Data" / "Config.json").write_text(
                json.dumps(
                    {
                        "Settings": {"logF_BusinessProcessName": "Framework"},
                        "Constants": {"MaxRetryNumber": 0},
                        "Assets": {},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (proj / "Framework" / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
            (proj / "Framework" / "AGENTS.md").write_text(
                "# REFramework agent guide (fake)\n", encoding="utf-8"
            )
        (proj / "Main.xaml").write_text(MINIMAL_XAML.format(cls="Main"), encoding="utf-8")
        (proj / "project.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "projectId": str(uuid.uuid4()),
                    "description": opt(rest, "--description") or "",
                    "main": "Main.xaml",
                    "dependencies": deps,
                    "schemaVersion": "4.1",
                    "studioVersion": "26.0.203.0",
                    "projectVersion": "1.0.0",
                    "designOptions": {"outputType": "Process", "fileInfoCollection": []},
                    "expressionLanguage": lang,
                    "entryPoints": [
                        {
                            "filePath": "Main.xaml",
                            "uniqueId": str(uuid.uuid4()),
                            "input": [],
                            "output": [],
                        }
                    ],
                    "targetFramework": tf,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return emit(
            {
                "Result": "Success",
                "Code": "ToolResult",
                "Data": {
                    "success": "init_partial" not in sc,
                    "projectDirectory": str(proj),
                    "errorMessage": "partial" if "init_partial" in sc else None,
                    "templatePackageVersion": opt(rest, "--template-package-version")
                    if pkg
                    else None,
                    "solutionRegistration": {"Status": "NotInSolution"},
                },
            }
        )

    if verb == "restore":
        proj = project_dir(rest, 0)
        if not (proj / "project.json").exists():
            return failure(f"project not found: {proj}")
        return emit({"Result": "Success", "Code": "Restore", "Data": {"Success": True}})

    if key == "packages versions":
        pid = opt(rest, "--package-id") or ""
        versions = {
            "UiPath.UIAutomation.Activities": [
                "26.10.5",
                "26.10.4",
                "26.10.2",
                "26.4.4-preview",
                "25.10.42",
            ],
            "UiPath.Mail.Activities": ["2.12.10", "2.12.9-preview", "2.11.8"],
            "UiPath.PDF.Activities": ["3.22.3", "3.22.0", "3.20.1-preview"],
            "UiPath.Excel.Activities": ["3.2.1", "3.1.0", "2.24.4"],
            "UiPath.System.Activities": ["26.8.2", "26.8.1"],
            "UiPath.Word.Activities": ["1.26.1", "1.25.0"],
        }.get(pid, ["1.0.0"])
        if "--include-prerelease" not in rest:
            versions = [v for v in versions if "-" not in v]
        return emit(
            {
                "Result": "Success",
                "Code": "ToolResult",
                "Data": {
                    "packageId": pid,
                    "includePrerelease": "--include-prerelease" in rest,
                    "versions": versions,
                },
            }
        )

    if key == "packages install":
        proj = project_dir(rest, 1)
        pj = proj / "project.json"
        if not pj.exists():
            return failure(f"project not found: {proj}")
        if "install_fail" in sc:
            return emit(
                {
                    "Result": "Success",
                    "Code": "ToolResult",
                    "Data": {"success": False, "failedPackages": opts(rest, "--packages")},
                }
            )
        data = json.loads(pj.read_text(encoding="utf-8"))
        for spec in opts(rest, "--packages"):
            fields = dict(kv.split("=", 1) for kv in spec.split(",") if "=" in kv)
            data["dependencies"][fields["id"]] = fields.get("version", "1.0.0")
        pj.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return emit({"Result": "Success", "Code": "ToolResult", "Data": {"success": True}})

    if key == "activities find":
        return emit(
            {
                "Result": "Success",
                "Code": "ToolResult",
                "Data": [
                    {
                        "className": "UiPath.UIAutomationNext.Activities.NClick, UiPath.UIAutomationNext.Activities, Version=26.10.5.0",
                        "packageName": "UiPath.UIAutomation.Activities",
                        "packageVersion": "26.10.5",
                        "namespace": "UiPath.UIAutomationNext.Activities",
                        "description": "Clicks a specified UI element.",
                    }
                ],
            }
        )

    if verb == "validate":
        proj = project_dir(rest, 0)
        rel = opt(rest, "--file-path")
        files = [proj / rel] if rel else xaml_files(proj)
        msgs: list[str] = []
        for f in files:
            if not f.exists():
                return failure(f"File {f} is not in the project folder")
            msgs.extend(
                f"[error] {f} - {m} (ERROR, Validation Error)" for m in diagnostics_for(proj, f, sc)
            )
        if not msgs:
            return emit(
                {
                    "Result": "Success",
                    "Code": "ToolResult",
                    "Data": {"message": "No diagnostics found."},
                }
            )
        return emit(
            {
                "Result": "Success",
                "Code": "ToolResult",
                "Data": {"message": f"Found {len(msgs)} error(s):\n" + "\n".join(msgs)},
            }
        )

    if verb == "build":
        proj = project_dir(rest, 0)
        if not (proj / "project.json").exists():
            return failure(
                f"project not found: {proj}",
                instructions="Check project path and build configuration.",
            )
        warn = f"[WARN] [Process] [{proj.name}] Your organization requires your project to have an Automation Hub URL defined.\n"
        errors: list[str] = []
        if "build_fail" in sc:
            errors.append("[Main.xaml] Simulated build failure.")
        for f in xaml_files(proj):
            rel = f.relative_to(proj).as_posix().replace("/", "\\")
            errors.extend(f"[{rel}] {m}" for m in diagnostics_for(proj, f, sc))
        if errors:
            body = "\r\n".join(errors) + "\r\n"
            stderr = warn + "".join(f"[ERROR] [Process] [{proj.name}] {e}\n" for e in errors)
            return emit(
                {
                    "Result": "Failure",
                    "ErrorCode": "unknown_error",
                    "Message": "Build failed: " + body,
                    "Instructions": "Check project path and build configuration.",
                    "Retry": "RetryWillNotFix",
                },
                1,
                stderr,
            )
        return emit({"Result": "Success", "Code": "Build", "Data": {"Success": True}}, 0, warn)

    if key == "studio start":
        return emit(
            {
                "Result": "Success",
                "Code": "ToolResult",
                "Data": {"ProcessId": 4242, "Status": "Ready"},
            }
        )
    if key == "instances list":
        return emit({"Result": "Success", "Code": "InstanceList", "Data": []})
    if verb == "analyze":
        return emit({"Result": "Success", "Code": "Analyze", "Data": {"Success": True}})
    return failure(f"error: unknown command '{verb}'", "invalid_argument")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

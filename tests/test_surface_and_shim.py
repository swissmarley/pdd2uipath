from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pdd2uipath.core.config import ExecutorKind, Settings, save_settings
from pdd2uipath.core.platform import WINDOWS
from pdd2uipath.executors.native import NativeExecutor
from pdd2uipath.executors.shim import install_shim
from pdd2uipath.executors.surface import (
    SNAPSHOT,
    CliSurface,
    SurfaceMismatch,
    UipCommands,
    VerbSpec,
    parse_help,
    probe_surface,
    render_markdown,
)
from tests.conftest import IS_WINDOWS
from tests.helpers import read_log

REAL_INIT_HELP = """                                  uip rpa init
                Initialize a new UiPath project from a template
┌─ Usage ──────────────────────────────────────────────────────────────────────┐
│ uip rpa init --name <string> [--template-id <string>] [--location <string>] [--description <string>] [--expression-language { VisualBasic | CSharp }] [--target-framework { Legacy | Windows | Portable }] [--allow-prerelease-packages] [--template-package-id <string>] [--template-package-version <string>] [--skip-solution-registration] │
└──────────────────────────────────────────────────────────────────────────────┘

┌─ Options ────────────────────────────────────────────────────────────────────┐
│ --template-id <string>         Template identifier (e.g. 'BlankTemplate',    │
│ --name <string>                Name of the project to create                 │
│ --project-dir <path>           Project directory to match against running    │
│ -h, --help                     display help for command                      │
│ --output                       Output format (default: json)                 │
└──────────────────────────────────────────────────────────────────────────────┘
"""

REAL_RPA_HELP = """┌─ Commands ───────────────────────────────────────────────────────────────────┐
│ activities                     Discover and inspect activities               │
│ build                          Build (compile) a UiPath project              │
│ get-library-object-repositor   Get the Object Repository out of the given    │
│ y                              library .nupkg file(s) and return the         │
│ init                           Initialize a new UiPath project from a        │
│                                template                                      │
│ validate                       Validate a single file or the entire project  │
└──────────────────────────────────────────────────────────────────────────────┘
┌─ Arguments ──────────────────────────────────────────────────────────────────┐
│ project-dir   Path to the UiPath project directory. Defaults to              │
└──────────────────────────────────────────────────────────────────────────────┘
"""


def test_parse_help_real_text() -> None:
    parsed = parse_help(REAL_INIT_HELP)
    assert parsed.usage.startswith("uip rpa init --name <string>")
    for f in (
        "--name",
        "--template-package-id",
        "--target-framework",
        "--expression-language",
        "--output",
        "--project-dir",
    ):
        assert f in parsed.flags
    cmds = parse_help(REAL_RPA_HELP)
    assert cmds.subcommands == [
        "activities",
        "build",
        "get-library-object-repositor",
        "init",
        "validate",
    ]
    assert cmds.positional == ["project-dir"]


def test_probe_surface_against_fake(fake_uip_command: list[str], fake_uip_log: Path) -> None:
    ex = NativeExecutor(fake_uip_command, platform=WINDOWS, translate=False)
    surface = probe_surface(ex, timeout_s=60)
    assert surface.uip_version == "9.9.9-fake"
    assert "validate" in surface.rpa_commands
    assert (
        surface.has(["rpa", "init"])
        and surface.has(["rpa", "validate"])
        and surface.has(["rpa", "build"])
    )
    assert not surface.has(["rpa", "create-project"])
    assert "--template-package-id" in surface.flags_for(["rpa", "init"])
    md = render_markdown(surface)
    assert "| `validate` | `uip rpa validate` |" in md and "## `uip rpa build`" in md


def test_commands_adapter_builds_argv() -> None:
    c = UipCommands(SNAPSHOT)
    assert c.templates_search("REFramework") == [
        "rpa",
        "templates",
        "search",
        "--query",
        "REFramework",
        "--output",
        "json",
    ]
    argv = c.init(
        name="Proj",
        location_windows="C:\\ws\\run",
        target_framework="Windows",
        expression_language="VisualBasic",
        template_package_id="UiPath.Template.REFramework",
        template_package_version="26.10.1",
        description="d",
    )
    assert (
        "--template-id" not in argv
        and argv[argv.index("--template-package-id") + 1] == "UiPath.Template.REFramework"
    )
    assert c.init(
        name="P",
        location_windows="C:\\ws",
        target_framework="Windows",
        expression_language="VisualBasic",
    )[2:6] == ["--name", "P", "--location", "C:\\ws"]
    assert "BlankTemplate" in c.init(
        name="P",
        location_windows="C:\\ws",
        target_framework="Windows",
        expression_language="VisualBasic",
    )
    assert c.validate("C:\\ws\\p", "Main.xaml") == [
        "rpa",
        "validate",
        "--file-path",
        "Main.xaml",
        "--min-severity",
        "error",
        "--project-dir",
        "C:\\ws\\p",
        "--output",
        "json",
    ]
    assert c.build("C:\\ws\\p") == ["rpa", "build", "C:\\ws\\p", "--output", "json"]
    assert c.packages_install(
        [("UiPath.Mail.Activities", "2.12.10"), ("UiPath.PDF.Activities", None)], "C:\\ws\\p"
    ) == [
        "rpa",
        "packages",
        "install",
        "--packages",
        "id=UiPath.Mail.Activities,version=2.12.10",
        "--packages",
        "id=UiPath.PDF.Activities",
        "--project-dir",
        "C:\\ws\\p",
        "--output",
        "json",
    ]
    assert (
        c.packages_versions("UiPath.Mail.Activities", "C:\\ws\\p", include_prerelease=True)[-3]
        == "--include-prerelease"
    )


def test_commands_adapter_resolves_alternate_verbs_and_flags() -> None:
    old = CliSurface(
        uip_version="1.100.0",
        rpa_commands=["create-project", "get-errors", "build"],
        verbs={
            "rpa create-project": VerbSpec(
                path=["rpa", "create-project"],
                flags=[
                    "--name",
                    "--location",
                    "--target-framework",
                    "--expression-language",
                    "--output",
                    "--template-id",
                    "--template-package-id",
                    "--template-package-version",
                    "--description",
                ],
            ),
            "rpa get-errors": VerbSpec(
                path=["rpa", "get-errors"], flags=["--file-path", "--project-dir", "--output"]
            ),
            "rpa build": VerbSpec(path=["rpa", "build"], flags=["--output"]),
        },
    )
    c = UipCommands(old)
    assert c.resolve("init") == ["rpa", "create-project"]
    assert c.resolve("validate") == ["rpa", "get-errors"]
    with pytest.raises(SurfaceMismatch, match="min-severity"):
        c.validate("C:\\ws\\p", "Main.xaml")
    with pytest.raises(SurfaceMismatch, match="exposes none"):
        c.resolve("templates_search")
    lax = UipCommands(old, strict=False)
    assert lax.resolve("templates_search") == ["rpa", "templates", "search"]


def test_shim_forwards_to_executor(
    tmp_path: Path, fake_uip_command: list[str], fake_uip_log: Path, win_root: str
) -> None:
    project = Path(win_root) / "run" / "Proj" if IS_WINDOWS else tmp_path / "Proj"
    project.mkdir(parents=True)
    project_w = str(project) if IS_WINDOWS else "C:\\ws\\run\\Proj"
    cfg = tmp_path / "pdd2uipath.toml"
    settings = Settings(
        workspace_root=win_root, executor=ExecutorKind.NATIVE, uip_windows_command=fake_uip_command
    )
    save_settings(settings, cfg)
    info = install_shim(project, project_w, config_path=cfg, python=sys.executable)
    assert info.sh_path.exists() and info.cmd_path.exists()
    assert info.path_env("X").startswith(str(info.bin_dir))
    if not IS_WINDOWS:
        pytest.skip("native executor forwarding needs a Windows host; shim text verified")
    env = dict(os.environ, PATH=info.path_env())
    exe = shutil.which("uip.cmd", path=env["PATH"]) if IS_WINDOWS else "uip"
    assert exe, "shim not found on PATH"
    proc = subprocess.run(
        [exe, "rpa", "templates", "search", "--query", "REFramework", "--output", "json"],
        cwd=str(project),
        capture_output=True,
        text=True,
        env=env,
        shell=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert '"source": "Official"' in proc.stdout
    assert read_log(fake_uip_log)[-1]["argv"][:3] == ["rpa", "templates", "search"]

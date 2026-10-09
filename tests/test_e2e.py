"""Opt-in end-to-end tests against the real Windows ``uip`` through the configured executor.

Run with ``uv run python -m pytest -m e2e``. Needs ``pdd2uipath.toml`` (doctor --write-config),
the Windows side reachable, and - for the full run - a licensed headless Studio host.
Set ``PDD2UIPATH_E2E_FULL=1`` to also run the whole PDD-01 pipeline into a temporary
workspace folder (several minutes).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pdd2uipath.core.config import discover_config_path, load_settings
from pdd2uipath.core.paths import windows_join
from pdd2uipath.core.runner import Pipeline, PlainGateHandler
from pdd2uipath.executors.factory import build_executor
from pdd2uipath.executors.surface import UipCommands

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def real_settings():
    cfg = REPO / "pdd2uipath.toml"
    if not cfg.is_file() and discover_config_path() is None:
        pytest.skip("no pdd2uipath.toml; run `pdd2uipath doctor --write-config` first")
    return load_settings(cfg if cfg.is_file() else None)


def test_real_uip_version_and_template_search(real_settings) -> None:
    ex = build_executor(real_settings)
    res = ex.run(["--version"], cwd=real_settings.workspace_root, timeout_s=120)
    assert res.ok, res.stderr
    assert res.stdout.strip()
    cmds = UipCommands()
    res = ex.run(
        cmds.templates_search("REFramework"), cwd=real_settings.workspace_root, timeout_s=300
    )
    assert res.ok, res.message
    official = [t for t in res.data if t.get("source") == "Official"]
    assert len(official) == 1 and official[0]["packageId"] == "UiPath.Template.REFramework"


def test_real_full_run_pdd01(real_settings, tmp_path_factory: pytest.TempPathFactory) -> None:
    if not os.environ.get("PDD2UIPATH_E2E_FULL"):
        pytest.skip("set PDD2UIPATH_E2E_FULL=1 to run the whole pipeline against the real uip")
    settings = real_settings.model_copy(deep=True)
    settings.workspace_root = windows_join(real_settings.workspace_root, "e2e")
    settings.extraction.backend = "fixture"
    settings.extraction.fixture_dir = str(REPO / "tests" / "fixtures" / "extraction")
    settings.agent.backend = "fake"
    settings.agent.fake_script = str(REPO / "tests" / "fake_agent" / "fake_agent.py")
    pdd = REPO / "samples" / "PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx"
    p = Pipeline(settings, pdd=pdd, gates=PlainGateHandler(approver="e2e"))
    p.run_all()
    assert p.state.statuses()["report"] == "done"
    assert p.last_gate is not None and p.last_gate.verdict.passed, p.last_gate.verdict.summary()
    assert (p.project_dir / "project.json").is_file()

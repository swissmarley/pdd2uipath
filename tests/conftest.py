from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
FAKE_UIP = ROOT / "fake_uip" / "fake_uip.py"
SAMPLES = ROOT.parent / "samples"
FIXTURES = ROOT / "fixtures"

IS_WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="needs a Windows host")


@pytest.fixture
def fake_uip_command() -> list[str]:
    return [sys.executable, "-I", "-X", "utf8", str(FAKE_UIP)]


@pytest.fixture
def fake_uip_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "fake-uip.log"
    monkeypatch.setenv("FAKE_UIP_LOG", str(log))
    monkeypatch.delenv("FAKE_UIP_SCENARIO", raising=False)
    return log


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch):
    def _set(*flags: str) -> None:
        monkeypatch.setenv("FAKE_UIP_SCENARIO", ",".join(flags))

    return _set


@pytest.fixture
def win_root(tmp_path: Path) -> str:
    """A Windows-style workspace root string that is also a real directory on Windows."""
    if IS_WINDOWS:
        root = tmp_path / "ws"
        root.mkdir()
        return str(root)
    return "C:\\ws"


@pytest.fixture
def samples_dir() -> Path:
    return SAMPLES


@pytest.fixture
def pdd01() -> Path:
    return SAMPLES / "PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx"


@pytest.fixture
def pdd02() -> Path:
    return SAMPLES / "PDD-02_Daily_FX_Rate_Update_v1.0_APPROVED.pdf"


@pytest.fixture
def pdd03() -> Path:
    return SAMPLES / "PDD-03_Customer_Address_Change_v0.3_DRAFT.docx"


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("PDD2UIPATH_CONFIG", raising=False)
    for k in list(os.environ):
        if k.startswith("PDD2UIPATH_"):
            monkeypatch.delenv(k, raising=False)
    yield

"""Where the "brain" runs: native Windows, WSL, or plain Linux."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class BrainOS(StrEnum):
    WINDOWS = "windows"
    WSL = "wsl"
    LINUX = "linux"


@dataclass(frozen=True)
class Platform:
    brain: BrainOS
    interop_enabled: bool
    distro: str | None = None

    @property
    def is_windows(self) -> bool:
        return self.brain is BrainOS.WINDOWS

    @property
    def is_wsl(self) -> bool:
        return self.brain is BrainOS.WSL

    @property
    def windows_reachable(self) -> bool:
        return self.is_windows or self.is_wsl


WINDOWS = Platform(BrainOS.WINDOWS, True)
WSL_INTEROP = Platform(BrainOS.WSL, True, "Ubuntu")
WSL_NO_INTEROP = Platform(BrainOS.WSL, False, "Ubuntu")
LINUX = Platform(BrainOS.LINUX, False)


def _proc_version_mentions_microsoft() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower()
    except OSError:
        return False


def interop_enabled() -> bool:
    p = Path("/proc/sys/fs/binfmt_misc/WSLInterop")
    try:
        return p.exists() and "enabled" in p.read_text(errors="ignore")
    except OSError:
        return False


def detect_platform() -> Platform:
    if os.name == "nt":
        return WINDOWS
    distro = os.environ.get("WSL_DISTRO_NAME")
    if distro or _proc_version_mentions_microsoft():
        return Platform(BrainOS.WSL, interop_enabled(), distro)
    return LINUX

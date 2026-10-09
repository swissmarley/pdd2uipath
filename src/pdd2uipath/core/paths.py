"""Path translation between the brain side (WSL/Windows) and the Windows side.

Generated projects always live on the Windows filesystem. Paths are translated purely
by string rules so the behaviour is identical on every host and fully testable;
``wslpath`` is only used by ``doctor`` as a cross-check.
"""

from __future__ import annotations

import re
from os import PathLike
from pathlib import Path, PurePosixPath, PureWindowsPath

from pdd2uipath.core.platform import Platform, detect_platform

_DRIVE_RE = re.compile(r"^([A-Za-z]):([\\/].*)?$")
_MNT_RE = re.compile(r"^/mnt/([A-Za-z])(/.*)?$")
_FORBIDDEN = ("\\\\wsl$", "\\\\wsl.localhost", "//wsl$", "//wsl.localhost")


class PathTranslationError(ValueError):
    """Raised when a path cannot be expressed as a Windows-filesystem path."""


def _normalize_windows(s: str) -> str:
    s = s.replace("/", "\\")
    s = re.sub(r"(?<!^)\\{2,}", "\\\\", s)
    drive, rest = s[0].upper(), s[1:]
    if rest == ":":
        rest = ":\\"
    s = drive + rest
    if len(s) > 3 and s.endswith("\\"):
        s = s.rstrip("\\")
    return s


def to_windows(path: str | PathLike[str], platform: Platform | None = None) -> str:
    """Return ``path`` as an absolute Windows path (``C:\\...``) or raise."""
    s = str(path).strip()
    if not s:
        raise PathTranslationError("empty path")
    low = s.lower()
    if any(low.startswith(p) for p in _FORBIDDEN):
        raise PathTranslationError(
            f"{s!r} is a \\\\wsl$ path. Generated projects must live on the Windows "
            "filesystem (C:\\...), because Studio, NuGet and file locking misbehave there."
        )
    if s.startswith(("\\\\", "//")):
        raise PathTranslationError(f"{s!r} is a UNC path, not a local Windows path")
    if _DRIVE_RE.match(s):
        return _normalize_windows(s)
    m = _MNT_RE.match(s)
    if m:
        drive = m.group(1).upper()
        rest = (m.group(2) or "/").replace("/", "\\")
        return _normalize_windows(f"{drive}:{rest}")
    platform = platform or detect_platform()
    if platform.is_windows and PureWindowsPath(s).is_absolute():
        return _normalize_windows(str(PureWindowsPath(s)))
    raise PathTranslationError(
        f"{s!r} is not on the Windows filesystem. Use a C:\\... path (or /mnt/c/... from WSL)."
    )


def to_brain(windows_path: str | PathLike[str], platform: Platform | None = None) -> Path:
    """Return the brain-side path for a Windows path (``/mnt/c/...`` under WSL)."""
    platform = platform or detect_platform()
    w = to_windows(windows_path, platform)
    if platform.is_windows:
        return Path(w)
    drive = w[0].lower()
    rest = w[2:].replace("\\", "/")
    return Path(f"/mnt/{drive}{rest or '/'}")


def windows_join(base: str, *parts: str) -> str:
    """Join Windows path parts without touching the filesystem."""
    p = PureWindowsPath(to_windows(base))
    for part in parts:
        p = p / part
    return _normalize_windows(str(p))


def is_inside(root: str | PathLike[str], path: str | PathLike[str]) -> bool:
    """True when ``path`` equals ``root`` or is below it (case-insensitive, Windows rules)."""
    r = to_windows(root).lower().rstrip("\\")
    p = to_windows(path).lower().rstrip("\\")
    return p == r or p.startswith(r + "\\")


def looks_like_path(token: str) -> bool:
    """Heuristic used for positional project-dir arguments."""
    if not token or token.startswith("-"):
        return False
    return bool(_DRIVE_RE.match(token) or _MNT_RE.match(token) or "\\" in token or "/" in token)


def posix_relative(path: str | PathLike[str], start: str | PathLike[str]) -> str:
    """Relative path with forward slashes (what ``--file-path`` wants)."""
    return PurePosixPath(Path(path).relative_to(Path(start)).as_posix()).as_posix()


def brain_path(value: str | PathLike[str], platform: Platform | None = None) -> Path:
    """Interpret a configured path on the brain side (Windows drive paths map to /mnt/<d> in WSL)."""
    s = str(value)
    platform = platform or detect_platform()
    if _DRIVE_RE.match(s) and not platform.is_windows:
        return to_brain(s, platform)
    return Path(s)

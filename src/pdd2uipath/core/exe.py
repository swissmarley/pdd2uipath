"""Resolve CLI tools installed through npm (``copilot``, ``opencode``, ``uip``) into argv prefixes.

On Windows an npm global install puts a ``<name>.cmd`` batch shim on PATH. ``subprocess`` with
``shell=False`` cannot start ``copilot`` by its bare name (WinError 2), and starting the ``.cmd``
itself routes every argument through ``cmd.exe``, which mangles ``%``, ``^``, ``&`` and line
breaks - fatal for a multi-kilobyte agent prompt passed as one argument. So the shim is read and
replaced by ``[node.exe, <loader.js>]``, exactly like ``uip`` in ``executors/factory.py``.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

_DP0_JS = re.compile(r'"%dp0%\\([^"]+?\.(?:js|mjs|cjs))"', re.IGNORECASE)


def which_cli(name: str, path: str | None = None) -> str | None:
    """``shutil.which`` that also tries the Windows ``.cmd`` / ``.exe`` spellings."""
    for candidate in (name, name + ".cmd", name + ".exe"):
        found = shutil.which(candidate, path=path)
        if found:
            return found
    return None


def resolve_npm_cmd_shim(shim: Path) -> list[str] | None:
    """``[node.exe, loader.js]`` for an npm ``.cmd`` shim, or None when it is not one."""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _DP0_JS.search(text)
    if not match:
        return None
    entry = shim.parent / match.group(1)
    node = shim.parent / "node.exe"
    if not node.exists():
        found = shutil.which("node.exe") or shutil.which("node")
        if not found:
            return None
        node = Path(found)
    if not entry.exists():
        return None
    return [str(node), str(entry)]


def resolve_cli(name: str, path: str | None = None) -> list[str]:
    """argv prefix that starts ``name`` without a shell.

    A bare name is looked up on PATH (``.cmd``/``.exe`` included); an npm ``.cmd`` shim is
    expanded to ``node.exe`` + its loader script; anything else is returned as found. A name
    that already carries a directory is returned unchanged, and so is a name that cannot be
    found (the caller reports the FileNotFoundError with the original spelling).
    """
    if os.sep in name or (os.altsep and os.altsep in name):
        return [name]
    found = which_cli(name, path=path)
    if not found:
        return [name]
    found_path = Path(found)
    if found_path.suffix.lower() in (".cmd", ".bat"):
        expanded = resolve_npm_cmd_shim(found_path)
        if expanded:
            return expanded
    return [str(found_path)]


def resolve_argv(argv: list[str], path: str | None = None) -> list[str]:
    """Apply :func:`resolve_cli` to ``argv[0]``; the rest is passed through untouched."""
    if not argv:
        return argv
    return [*resolve_cli(argv[0], path=path), *argv[1:]]

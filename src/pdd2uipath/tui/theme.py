"""Palette, glyph sets, and the two Textual themes."""

from __future__ import annotations

import os
from dataclasses import dataclass

from textual.theme import Theme

DARK = Theme(
    name="pdd2uipath-dark",
    primary="#5fa8d3",
    secondary="#7fb069",
    accent="#e9c46a",
    warning="#f4a261",
    error="#e76f51",
    success="#7fb069",
    foreground="#e6e6e6",
    background="#111417",
    surface="#181c21",
    panel="#1f242b",
    dark=True,
    variables={"block-cursor-foreground": "#111417", "footer-key-foreground": "#e9c46a"},
)

LIGHT = Theme(
    name="pdd2uipath-light",
    primary="#1f6f9f",
    secondary="#3a7d44",
    accent="#b8860b",
    warning="#c76b1a",
    error="#b23a2a",
    success="#3a7d44",
    foreground="#1b1f23",
    background="#fbfaf7",
    surface="#f2f0ea",
    panel="#e8e5dd",
    dark=False,
    variables={"block-cursor-foreground": "#fbfaf7", "footer-key-foreground": "#1f6f9f"},
)


@dataclass(frozen=True)
class Glyphs:
    pending: str
    running: str
    done: str
    failed: str
    waiting: str
    stopped: str
    skipped: str
    online: str
    offline: str
    ok: str
    fail: str
    spinner: tuple[str, ...]

    def for_status(self, status: str) -> str:
        return {
            "pending": self.pending,
            "running": self.running,
            "done": self.done,
            "failed": self.failed,
            "waiting": self.waiting,
            "stopped": self.stopped,
            "skipped": self.skipped,
        }.get(status, self.pending)


UNICODE = Glyphs(
    pending="○",
    running="◐",
    done="●",
    failed="✗",
    waiting="⏸",
    stopped="■",
    skipped="◌",
    online="●",
    offline="○",
    ok="✓",
    fail="✗",
    spinner=("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
)
ASCII = Glyphs(
    pending=".",
    running=">",
    done="*",
    failed="X",
    waiting="?",
    stopped="!",
    skipped="-",
    online="*",
    offline="o",
    ok="OK",
    fail="X",
    spinner=("|", "/", "-", "\\"),
)


def pick_glyphs(setting: str) -> Glyphs:
    if setting == "ascii" or os.environ.get("PDD2UIPATH_ASCII"):
        return ASCII
    return UNICODE


def no_color() -> bool:
    return bool(os.environ.get("NO_COLOR"))

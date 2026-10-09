from __future__ import annotations

import pytest

from pdd2uipath.core.paths import (
    PathTranslationError,
    is_inside,
    looks_like_path,
    to_brain,
    to_windows,
    windows_join,
)
from pdd2uipath.core.platform import LINUX, WINDOWS, WSL_INTEROP


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("/mnt/c/Users/me/RPA", "C:\\Users\\me\\RPA"),
        ("/mnt/d/", "D:\\"),
        ("/mnt/c", "C:\\"),
        ("C:\\Users\\me", "C:\\Users\\me"),
        ("c:/Users/me/dir with spaces/", "C:\\Users\\me\\dir with spaces"),
        ("/mnt/c/Users/me/dir with spaces/x y", "C:\\Users\\me\\dir with spaces\\x y"),
        ("C:\\\\double\\\\slashes", "C:\\double\\slashes"),
    ],
)
def test_to_windows(given: str, expected: str) -> None:
    assert to_windows(given, WSL_INTEROP) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "\\\\wsl$\\Ubuntu\\home\\me\\proj",
        "\\\\wsl.localhost\\Ubuntu\\home",
        "//wsl$/Ubuntu/x",
        "\\\\fs01\\share",
        "/home/me/proj",
        "relative/path",
        "",
    ],
)
def test_to_windows_refuses(bad: str) -> None:
    with pytest.raises(PathTranslationError):
        to_windows(bad, WSL_INTEROP)


def test_to_windows_linux_home_refused() -> None:
    with pytest.raises(PathTranslationError):
        to_windows("/home/me/x", LINUX)


def test_to_brain() -> None:
    assert to_brain("C:\\Users\\me\\RPA", WSL_INTEROP).as_posix() == "/mnt/c/Users/me/RPA"
    assert to_brain("D:\\", WSL_INTEROP).as_posix() == "/mnt/d"
    assert str(to_brain("C:\\Users\\me", WINDOWS)) == "C:\\Users\\me"


def test_windows_join_and_inside() -> None:
    root = windows_join("/mnt/c/ws", "run-1", "project")
    assert root == "C:\\ws\\run-1\\project"
    assert is_inside("C:\\ws", root)
    assert is_inside("c:\\WS\\", "C:\\ws")
    assert not is_inside("C:\\ws", "C:\\ws2\\x")
    assert not is_inside("C:\\ws", "D:\\ws\\x")


def test_looks_like_path() -> None:
    assert looks_like_path("C:\\x")
    assert looks_like_path("/mnt/c/x")
    assert looks_like_path("sub/dir")
    assert not looks_like_path("--flag")
    assert not looks_like_path("Main.xaml")

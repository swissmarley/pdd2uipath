"""Configuration: ``pdd2uipath.toml`` + ``PDD2UIPATH_*`` environment variables."""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_FILENAME = "pdd2uipath.toml"


class ExecutorKind(StrEnum):
    AUTO = "auto"
    NATIVE = "native"
    WSL_INTEROP = "wsl-interop"
    WSL_BRIDGE = "wsl-bridge"
    WSL_MANUAL = "wsl-manual"


class Side(StrEnum):
    WINDOWS = "windows"
    BRAIN = "brain"


class ExtractionSettings(BaseModel):
    """Stage 2 model backend. No Anthropic dependency anywhere."""

    backend: str = "auto"  # auto | openai-compatible | opencode | copilot | command | fixture
    model: str = ""  # openai-compatible: model name; opencode: provider/model; copilot: model id
    base_url: str = ""  # openai-compatible: e.g. http://localhost:11434/v1
    api_key_env: str = "OPENAI_API_KEY"
    use_response_format: bool = True
    command: list[str] = Field(default_factory=list)  # command backend / override
    fixture_dir: str | None = None
    max_tokens: int = 32000


class AgentSettings(BaseModel):
    """Stage 6 coding agent: opencode (local LLM) or GitHub Copilot CLI."""

    backend: str = "auto"  # auto | opencode | copilot | fake
    model: str | None = None
    command: list[str] = Field(default_factory=list)  # override executable (+ extra flags)
    max_turns: int = 120
    max_budget_usd: float = 25.0
    timeout_s: float = 7200.0
    deny_tools: list[str] = Field(
        default_factory=lambda: [
            "shell(uip solution:*)",
            "shell(uip or:*)",
            "shell(uip rpa publish:*)",
            "shell(uip rpa pack:*)",
            "shell(uip rpa run:*)",
            "shell(uip rpa debug:*)",
            "shell(uip rpa remote:*)",
            "shell(git push:*)",
        ]
    )
    fake_script: str | None = None


class RepairSettings(BaseModel):
    max_rounds: int = 2


class BridgeSettings(BaseModel):
    poll_interval_s: float = 1.0
    heartbeat_timeout_s: float = 15.0
    job_timeout_s: float = 900.0
    dirname: str = ".pdd2uipath-bridge"


class UiSettings(BaseModel):
    theme: str = "dark"  # dark | light
    glyphs: str = "unicode"  # unicode | ascii


class UiPathSettings(BaseModel):
    org: str = ""
    tenant: str = ""


class PackagePolicy(BaseModel):
    uia_min_version: str = "26.10.2"
    prefer_prerelease: bool = False


def _default_workspace_root() -> str:
    if os.name == "nt":
        return str(Path.home() / "RPA" / "pdd2uipath-projects")
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PDD2UIPATH_", env_nested_delimiter="__", extra="ignore"
    )

    workspace_root: str = Field(default_factory=_default_workspace_root)
    executor: ExecutorKind = ExecutorKind.AUTO
    uip_windows_command: list[str] = Field(default_factory=list)
    uip_brain_command: list[str] = Field(default_factory=list)
    skills_root: str | None = None
    skill_installed_for_claude: bool = False
    default_target_framework: str = "Windows"
    expression_language: str = "VisualBasic"
    ui_capture: str = "stub"  # stub | live
    approval_rule: str = "document-control-and-signoff"
    side_table: dict[str, Side] = Field(
        default_factory=lambda: {
            "rpa": Side.WINDOWS,
            "skills": Side.BRAIN,
            "login": Side.WINDOWS,
            "--version": Side.WINDOWS,
        }
    )
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    repair: RepairSettings = Field(default_factory=RepairSettings)
    bridge: BridgeSettings = Field(default_factory=BridgeSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    uipath: UiPathSettings = Field(default_factory=UiPathSettings)
    packages: PackagePolicy = Field(default_factory=PackagePolicy)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls))

    def side_for(self, argv: list[str]) -> Side:
        """Which side a ``uip`` invocation runs on (config table, not scattered ifs)."""
        head = argv[0] if argv else ""
        return self.side_table.get(head, Side.WINDOWS)


def default_user_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "pdd2uipath" / "config.toml"


def discover_config_path(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    env = os.environ.get("PDD2UIPATH_CONFIG")
    if env:
        return Path(env)
    for c in (Path.cwd() / CONFIG_FILENAME, default_user_config_path()):
        if c.is_file():
            return c
    return None


def load_settings(path: Path | None = None, **overrides: Any) -> Settings:
    cfg = discover_config_path(path)

    class _Loaded(Settings):
        model_config = SettingsConfigDict(
            env_prefix="PDD2UIPATH_",
            env_nested_delimiter="__",
            extra="ignore",
            toml_file=str(cfg) if cfg else None,
        )

    settings = _Loaded(**overrides)
    settings.__dict__["_config_path"] = cfg
    return settings


def settings_source_path(settings: Settings) -> Path | None:
    value = settings.__dict__.get("_config_path")
    return value if isinstance(value, Path) else None


def _drop_none(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj


def save_settings(settings: Settings, path: Path) -> Path:
    data = _drop_none(settings.model_dump(mode="json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")
    return path


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)

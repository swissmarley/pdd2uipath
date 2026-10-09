"""Design stage output: explainable, deterministic decisions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from pdd2uipath.spec.models import OpenQuestion

TemplateKind = Literal["reframework", "blank", "library", "test"]
WorkflowRole = Literal[
    "settings", "app-init", "app-close", "dispatcher", "process-step", "end-of-run"
]


class Decision(BaseModel):
    id: str
    topic: str
    choice: str
    rationale: str
    source_refs: list[str] = Field(default_factory=list)


class Contradiction(BaseModel):
    topic: str
    detail: str
    windows_signals: list[str] = Field(default_factory=list)
    portable_signals: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class TemplateChoice(BaseModel):
    kind: TemplateKind
    query: str | None = None
    template_id: str | None = None
    package_id: str | None = None
    package_version: str | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["resolved", "ambiguous", "unresolved"] = "unresolved"
    detail: str = ""


class WorkflowMapping(BaseModel):
    step_id: str
    step_name: str
    file: str
    kind: Literal["xaml", "cs"] = "xaml"
    role: WorkflowRole
    location: str
    invoked_from: str = ""
    ui: bool = False
    application: str = ""
    pdd_ref: str = ""
    business_rules: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PackageRequirement(BaseModel):
    id: str
    version: str | None = None
    min_version: str | None = None
    reason: str = ""
    from_template: bool = False
    install: bool = True


class DesignResult(BaseModel):
    project_name: str
    template: TemplateChoice
    target_framework: str
    expression_language: str
    ui_capture: str
    contradiction: Contradiction | None = None
    workflows: list[WorkflowMapping] = Field(default_factory=list)
    packages: list[PackageRequirement] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    blocking_questions: list[OpenQuestion] = Field(default_factory=list)
    nonblocking_questions: list[OpenQuestion] = Field(default_factory=list)
    stop_reasons: list[str] = Field(default_factory=list)
    config_values: dict[str, Any] = Field(default_factory=dict)
    windows_signals: list[str] = Field(default_factory=list)
    portable_signals: list[str] = Field(default_factory=list)

    @property
    def stopped(self) -> bool:
        return bool(self.stop_reasons)

    def installs(self) -> list[PackageRequirement]:
        return [p for p in self.packages if p.install]

    def by_file(self) -> dict[str, list[WorkflowMapping]]:
        out: dict[str, list[WorkflowMapping]] = {}
        for w in self.workflows:
            out.setdefault(w.file, []).append(w)
        return out

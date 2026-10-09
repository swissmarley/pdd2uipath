"""``ProcessSpec``: the structured, source-anchored representation of a PDD.

Every item carries a ``source_ref`` (a document anchor such as ``§4.3/S06`` or
``p.3 §4.2``). Anything the PDD does not state goes to ``open_questions``; nothing is
invented.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProcessMeta(BaseModel):
    document_id: str = Field(description="Document ID from Document Control, e.g. PDD-FIN-AP-014")
    title: str
    version: str = ""
    status: str = ""
    date: str = ""
    business_unit: str = ""
    prepared_by: str = ""
    purpose: str = Field(default="", description="One-paragraph purpose of the automation")
    source_ref: str


class Step(BaseModel):
    step_id: str = Field(description="Step ID exactly as in the PDD, e.g. S01")
    name: str = Field(description="Short step name")
    part: str = Field(
        default="", description="Dispatcher | Performer | Both | '' when the PDD has no split"
    )
    actor: str = Field(default="", description="Who/what performs it: robot, human, system")
    application: str = Field(
        default="", description="Application the step uses, as named in the PDD"
    )
    action: str = Field(description="What the step does (summary row text)")
    details: list[str] = Field(default_factory=list, description="Detailed sub-steps in order")
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list, description="Rule IDs, e.g. BR-01")
    exceptions: list[str] = Field(
        default_factory=list, description="Exception IDs, e.g. BE-01, SE-02"
    )
    decision_points: list[str] = Field(default_factory=list)
    is_ui_interaction: bool = Field(
        description="True when the robot drives an application's UI (click, type, read screen)"
    )
    ui_interaction_rationale: str = ""
    source_ref: str


class Application(BaseModel):
    name: str
    kind: str = Field(
        default="other",
        description="desktop | web | email | excel | word | pdf | file-share | orchestrator | other",
    )
    access: str = Field(default="", description="URL, executable, version or access notes")
    credential_asset_names: list[str] = Field(
        default_factory=list, description="Orchestrator credential ASSET NAMES only, never values"
    )
    used_for: str = ""
    platform_hints: list[str] = Field(
        default_factory=list,
        description="Platform signals verbatim: 'Windows', 'SAP GUI for Windows', 'Linux', 'macro-enabled', ...",
    )
    source_ref: str


class DataItem(BaseModel):
    name: str
    direction: str = Field(default="input", description="input | output")
    format: str = ""
    mandatory: bool = False
    target: str = ""
    source_ref: str


class BusinessRule(BaseModel):
    id: str
    text: str
    step_ids: list[str] = Field(default_factory=list)
    source_ref: str


class ExceptionDef(BaseModel):
    id: str
    kind: str = Field(description="business | system")
    description: str
    handling: str = ""
    step_ids: list[str] = Field(default_factory=list)
    retryable: str = Field(default="unknown", description="yes | no | unknown")
    source_ref: str


class TransactionInfo(BaseModel):
    unit: str = Field(default="", description="What one transaction is, e.g. 'one invoice'")
    queue_name: str = Field(default="", description="Orchestrator queue name if stated")
    dispatcher_performer: bool = False
    volumes: str = ""
    retry_policy: str = ""
    source_ref: str = ""


class Schedule(BaseModel):
    schedule: str = ""
    sla: str = ""
    source_ref: str = ""


class RobotEnvironment(BaseModel):
    description: str = ""
    os_hints: list[str] = Field(
        default_factory=list, description="e.g. 'Windows 11 VDI', 'Linux serverless'"
    )
    robot_type: str = Field(
        default="unknown", description="unattended | attended | serverless | unknown"
    )
    source_ref: str = ""


class Note(BaseModel):
    text: str
    source_ref: str


class OpenQuestion(BaseModel):
    id: str
    text: str
    owner: str = ""
    blocking: bool = Field(
        description="True when the design cannot proceed without the answer (unknown target app/URL for a "
        "UI step, undefined control flow, template/framework choice)"
    )
    reason: str = ""
    source_ref: str


class ProcessSpecDraft(BaseModel):
    """What the model fills in (the deterministic facts are merged in afterwards)."""

    meta: ProcessMeta
    steps: list[Step]
    applications: list[Application]
    data_items: list[DataItem] = Field(default_factory=list)
    business_rules: list[BusinessRule] = Field(default_factory=list)
    exceptions: list[ExceptionDef] = Field(default_factory=list)
    transaction: TransactionInfo = Field(default_factory=TransactionInfo)
    schedule: Schedule = Field(default_factory=Schedule)
    robot_environment: RobotEnvironment = Field(default_factory=RobotEnvironment)
    assumptions: list[Note] = Field(default_factory=list)
    out_of_scope: list[Note] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)


class ExtractionInfo(BaseModel):
    backend: str
    model: str = ""
    deterministic_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    pdd_sha256: str = ""


class ProcessSpec(ProcessSpecDraft):
    extraction: ExtractionInfo = Field(default_factory=lambda: ExtractionInfo(backend="unknown"))

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def ui_steps(self) -> list[Step]:
        return [s for s in self.steps if s.is_ui_interaction]

    def blocking_questions(self) -> list[OpenQuestion]:
        return [q for q in self.open_questions if q.blocking]

    def credential_assets(self) -> list[str]:
        names: list[str] = []
        for a in self.applications:
            for n in a.credential_asset_names:
                if n not in names:
                    names.append(n)
        return names


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema with every property required and no additional properties.

    Both Anthropic structured outputs and the Agent SDK accept this shape; defaults
    are preserved so Python-side validation stays lenient.
    """
    schema = model.model_json_schema()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for key in ("default",):
                node.pop(key, None)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(schema)
    return schema

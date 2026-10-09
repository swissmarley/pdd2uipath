"""A deterministic stand-in for the coding agent.

Reads the plan in ``<project>/docs/plans/*.md`` and materializes every task's workflow:
one XAML per task file with the step annotation, real UIA activities with
``TODO Indicate`` placeholders for UI tasks (the Placeholder-Selector Stub Pattern),
and the queue name configured where the plan says. It prints JSON event lines like a
real agent would. Set ``FAKE_AGENT_MODE=log-only`` to produce the forbidden Log-only
stubs, ``FAKE_AGENT_MODE=skip:S08`` to omit a step, ``FAKE_AGENT_MODE=broken`` to
write a file the gate rejects, or ``FAKE_AGENT_MODE=fail`` to exit non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

TASK_RE = re.compile(
    r"^## Task T(?P<n>\d+) — uipath-rpa — `(?P<file>[^`]+)` \(steps? (?P<steps>[^)]+)\)", re.M
)
ROOT_RE = re.compile(
    r'Root `Sequence`: `DisplayName="(?P<display>[^"]+)"`, `AnnotationText="(?P<annotation>[^"]+)"`'
)
UI_RE = re.compile(r'This is a UI step on "(?P<app>[^"]+)"')
DETAIL_RE = re.compile(r"^- \[ \] (?P<sid>S\d{2}): (?P<text>.+)$", re.M)
QUEUE_RE = re.compile(r"Set the Orchestrator queue name to `(?P<q>[^`]+)`")

HEADER = (
    '<Activity mc:Ignorable="sap sap2010" x:Class="{cls}" '
    'xmlns="http://schemas.microsoft.com/netfx/2009/xaml/activities" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:sap="http://schemas.microsoft.com/netfx/2009/xaml/activities/presentation" '
    'xmlns:sap2010="http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation" '
    'xmlns:scg="clr-namespace:System.Collections.Generic;assembly=System.Private.CoreLib" '
    'xmlns:ui="http://schemas.uipath.com/workflow/activities" '
    'xmlns:uix="http://schemas.uipath.com/workflow/activities/uix" '
    'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">\n'
    "  <TextExpression.NamespacesForImplementation>\n"
    '    <scg:List x:TypeArguments="x:String" Capacity="5">\n'
    "      <x:String>System</x:String>\n"
    "      <x:String>System.Activities</x:String>\n"
    "      <x:String>System.Activities.Statements</x:String>\n"
    "      <x:String>System.Activities.Expressions</x:String>\n"
    "      <x:String>UiPath.Core.Activities</x:String>\n"
    "    </scg:List>\n"
    "  </TextExpression.NamespacesForImplementation>\n"
    "  <TextExpression.ReferencesForImplementation>\n"
    '    <scg:List x:TypeArguments="AssemblyReference" Capacity="4">\n'
    "      <AssemblyReference>System.Activities</AssemblyReference>\n"
    "      <AssemblyReference>UiPath.System.Activities</AssemblyReference>\n"
    "      <AssemblyReference>UiPath.UIAutomationNext.Activities</AssemblyReference>\n"
    "      <AssemblyReference>UiPath.UiAutomation.Activities</AssemblyReference>\n"
    "    </scg:List>\n"
    "  </TextExpression.ReferencesForImplementation>\n"
)

APP_CARD = (
    '    <uix:NApplicationCard AttachMode="ByInstance" DisplayName="TODO Indicate - Use Application/Browser ({app})" HealingAgentBehavior="Job" Version="V2" sap2010:WorkflowViewState.IdRef="NApplicationCard_1">\n'
    "      <uix:NApplicationCard.Body>\n"
    '        <ActivityAction x:TypeArguments="x:Object">\n'
    "          <ActivityAction.Argument>\n"
    '            <DelegateInArgument x:TypeArguments="x:Object" Name="WSSessionData" />\n'
    "          </ActivityAction.Argument>\n"
    '          <Sequence DisplayName="Do" sap2010:WorkflowViewState.IdRef="Sequence_2">\n'
    "{body}"
    "          </Sequence>\n"
    "        </ActivityAction>\n"
    "      </uix:NApplicationCard.Body>\n"
    "      <uix:NApplicationCard.TargetApp>\n"
    '        <uix:TargetApp Area="0, 0, 0, 0" Version="V3" />\n'
    "      </uix:NApplicationCard.TargetApp>\n"
    "    </uix:NApplicationCard>\n"
)


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def ui_body(details: list[str], mode: str) -> str:
    lines: list[str] = []
    n = 1
    for d in details[:6]:
        low = d.lower()
        label = escape(re.sub(r"[^\w\s./()-]", "", d)[:60])
        if mode == "log-only":
            lines.append(
                f'            <ui:LogMessage DisplayName="Log {label}" Level="Info" Message="[&quot;{label}&quot;]" />\n'
            )
            continue
        if "enter" in low or "type" in low:
            lines.append(
                f'            <uix:NTypeInto DisplayName="TODO Indicate - Type {label}" Text="[&quot;TODO&quot;]" Version="V5" sap2010:WorkflowViewState.IdRef="NTypeInto_{n}" />\n'
            )
        elif "read" in low or "verify" in low:
            lines.append(
                f'            <uix:NGetText DisplayName="TODO Indicate - Read {label}" Version="V5" sap2010:WorkflowViewState.IdRef="NGetText_{n}" />\n'
            )
        else:
            lines.append(
                f'            <uix:NClick ActivateBefore="True" ClickType="Single" DisplayName="TODO Indicate - Click {label}" HealingAgentBehavior="SameAsCard" KeyModifiers="None" MouseButton="Left" Version="V5" sap2010:WorkflowViewState.IdRef="NClick_{n}" />\n'
            )
        n += 1
    if not lines:
        lines.append(
            '            <uix:NClick ActivateBefore="True" ClickType="Single" DisplayName="TODO Indicate - Click target" HealingAgentBehavior="SameAsCard" KeyModifiers="None" MouseButton="Left" Version="V5" sap2010:WorkflowViewState.IdRef="NClick_1" />\n'
        )
    return "".join(lines)


def workflow_xaml(
    cls: str, display: str, annotation: str, details: list[str], ui_app: str | None, mode: str
) -> str:
    body = ""
    for i, d in enumerate(details[:8]):
        label = escape(re.sub(r"[^\w\s./()-]", "", d)[:80])
        # the first non-UI detail carries an agent assumption, like a real agent would
        note = (
            f' sap2010:Annotation.AnnotationText="TODO Confirm - assumption for {label[:30]}"'
            if i == 0 and not ui_app
            else ""
        )
        body += f'    <ui:LogMessage DisplayName="{label[:40]}"{note} Level="Info" Message="[&quot;{label}&quot;]" />\n'
    if ui_app and mode == "log-only":
        body += ui_body(details, mode)
    elif ui_app:
        body += APP_CARD.format(app=escape(ui_app), body=ui_body(details, mode))
    if mode == "broken":
        body += '    <uix:NApplicationCard ApplicationWindow="{x:Null}" DisplayName="broken" />\n'
    return (
        HEADER.format(cls=cls)
        + f'  <Sequence DisplayName="{escape(display)}" sap2010:Annotation.AnnotationText="{escape(annotation)}" sap2010:WorkflowViewState.IdRef="Sequence_1">\n'
        + body
        + "  </Sequence>\n</Activity>\n"
    )


def annotate_existing(path: Path, display: str, annotation: str) -> None:
    text = path.read_text(encoding="utf-8-sig")
    m = re.search(r"<(Sequence|Flowchart|StateMachine)\b([^>]*)>", text)
    if not m:
        return
    tag, attrs = m.group(1), m.group(2)
    if "sap2010:Annotation.AnnotationText=" in attrs:
        attrs2 = re.sub(
            r'sap2010:Annotation\.AnnotationText="[^"]*"',
            f'sap2010:Annotation.AnnotationText="{escape(annotation)}"',
            attrs,
        )
    else:
        attrs2 = f' sap2010:Annotation.AnnotationText="{escape(annotation)}"' + attrs
    text = text[: m.start()] + f"<{tag}{attrs2}>" + text[m.end() :]
    path.write_text(text, encoding="utf-8")


def set_queue_name(project: Path, queue: str) -> list[str]:
    touched: list[str] = []
    cfg = project / "Data" / "Config.json"
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8-sig"))
        data.setdefault("Settings", {})["OrchestratorQueueName"] = queue
        cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
        touched.append("Data/Config.json")
    gtd = project / "Framework" / "GetTransactionData.xaml"
    if gtd.exists():
        text = gtd.read_text(encoding="utf-8-sig")
        # UiPath.System.Activities 26.x: the queue is the `QueueType` InArgument (display name
        # "Queue name"), given as a literal attribute per the activity doc's XAML example.
        new = re.sub(r'QueueType="[^"]*"', f'QueueType="{queue}"', text, count=1)
        if new == text and "QueueType=" not in text:
            new = re.sub(
                r"<ui:GetQueueItem\b",
                f'<ui:GetQueueItem QueueType="{queue}"',
                text,
                count=1,
            )
        if new != text:
            gtd.write_text(new, encoding="utf-8")
            touched.append("Framework/GetTransactionData.xaml")
    return touched


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--prompt-file", required=True)
    args = ap.parse_args()
    mode = os.environ.get("FAKE_AGENT_MODE", "stub")
    if mode == "fail":
        sys.stderr.write("simulated agent failure\n")
        return 7
    project = Path(args.project)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    emit({"type": "message", "text": f"fake agent: {prompt[:80]}..."})
    plans = sorted((project / "docs" / "plans").glob("*.md"))
    if not plans:
        emit({"type": "result", "text": "no plan found"})
        return 1
    plan = plans[0].read_text(encoding="utf-8")
    skip = {s.split(":")[1] for s in mode.split(",") if s.startswith("skip:")}
    written: list[str] = []
    q = QUEUE_RE.search(plan)
    if q:
        written += set_queue_name(project, q.group("q"))
    sections = list(TASK_RE.finditer(plan))
    for i, m in enumerate(sections):
        start = m.end()
        end = sections[i + 1].start() if i + 1 < len(sections) else len(plan)
        body = plan[start:end]
        file = m.group("file")
        step_ids = [s.strip() for s in m.group("steps").split(",")]
        if any(s in skip for s in step_ids):
            continue
        root = ROOT_RE.search(body)
        display = root.group("display") if root else f"{step_ids[0]} - step"
        annotation = root.group("annotation") if root else f"PDD step {step_ids[0]}"
        ui = UI_RE.search(body)
        details = [d.group("text") for d in DETAIL_RE.finditer(body)]
        target = project / file
        emit({"type": "tool", "name": "write", "title": file})
        if target.exists() and file.startswith("Framework/"):
            annotate_existing(target, display, annotation)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                workflow_xaml(
                    target.stem, display, annotation, details, ui.group("app") if ui else None, mode
                ),
                encoding="utf-8",
            )
        written.append(file)
        emit({"type": "tool_result", "name": "write", "output": f"wrote {file}"})
    emit(
        {
            "type": "result",
            "text": f"done: {len(written)} files ({', '.join(written)})",
            "sessionID": "fake-session-1",
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

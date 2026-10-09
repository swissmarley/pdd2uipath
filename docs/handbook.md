# pdd2uipath handbook

The complete manual: what the tool does, how to start it, every screen, key, command, stage,
setting and artefact. Installation is covered in the [setup guide](setup-guide.md); design
rationale in [decisions.md](decisions.md).

## 1. What pdd2uipath does

pdd2uipath turns an **approved Process Definition Document** into a **UiPath Studio project**
that a developer finishes in Studio. It does not run robots, publish packages or touch
Orchestrator. The output is a project that scaffolds cleanly, mirrors the PDD one step per
workflow, has real UI Automation activities with placeholder targets (`TODO Indicate`) where
the PDD describes screen interactions, a hand-off report and a traceability table.

**Brain and hands.** Everything deterministic runs in Python (the brain) on Windows or in WSL.
Every `uip rpa` operation runs on Windows (the hands) through one **executor**. The coding
agent (GitHub Copilot CLI or OpenCode) runs on the brain side and reaches `uip` through a
per-project shim that forwards to the executor.

**Nine stages.** doctor, ingest, extract, design, plan, scaffold, author, verify, report. Two
human gates: the PDD must be approved (ingest) and a human must approve the plan (hash-bound,
before scaffold). The verify stage runs the Windows gate (validate every workflow, build the
project) plus a Python audit, and feeds errors back to the agent for a bounded number of repair
rounds.

**Guardrails** are enforced in code, not by convention: never past ingest on an unapproved
PDD; never past plan without a matching approval hash; `project.json`, selectors and scaffold
files are never hand-written; only packages named in the approved plan's Task 0 are installed;
target framework and expression language never change after init; UI steps are real UIA
activities, never Log-only stubs; the bridge worker runs only allowlisted `uip` verbs inside the
workspace; no Orchestrator publish, pack or job run; credential values never appear in specs,
plans, projects, logs or reports.

## 2. Starting the tool

### The TUI

```bash
uv run pdd2uipath
uv run pdd2uipath -c path\to\pdd2uipath.toml
```

With no arguments in a terminal the Textual user interface starts. It opens the PDD picker if no
run is given; `o` or `/open <path>` opens a PDD. A run that was quit earlier resumes at the exact
gate it was waiting at.

### The scriptable CLI

```bash
uv run pdd2uipath --plain <command> [options]
```

`--plain` prints plain text and never opens the TUI; use it in scripts and CI. Exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | error (details on stderr) |
| 3 | waiting for plan approval |
| 4 | a Windows round is pending (`wsl-manual` with `--no-wait`) |
| 5 | stopped on purpose: rejected PDD, design contradiction, blocking open questions |

### Configuration discovery

The config file is found in this order: `-c/--config`, the `PDD2UIPATH_CONFIG` environment
variable, `./pdd2uipath.toml` in the current directory, then the user file
`~/.config/pdd2uipath/config.toml` (`XDG_CONFIG_HOME` is honoured). Any key can be overridden by
an environment variable with the `PDD2UIPATH_` prefix; nested sections use `__`:

```bash
PDD2UIPATH_WORKSPACE_ROOT=C:/Users/me/RPA/other uv run pdd2uipath --plain status --pdd my.docx
PDD2UIPATH_AGENT__BACKEND=copilot PDD2UIPATH_EXECUTOR=wsl-manual uv run pdd2uipath --plain run-all my.docx
```

## 3. The TUI

### Layout

- **Header**: product name, PDD, project path, executor pill with its online state, agent
  backend and model, elapsed time of the running stage.
- **Pipeline sidebar** (left): the nine stages with their status glyphs. Selecting a stage
  filters the session log to that stage.
- **Session log** (centre): one card per event in order: stage start and finish, every `uip`
  command with duration and result, agent messages and tool calls, gate events, log lines.
- **Inspector** (right, `i` toggles): tabs PDD (document tree and approval verdict), Spec
  (ProcessSpec tree with source references), Design (decisions with rationale), Plan (rendered
  plan, approval status, diff since approval), Validation (gate diagnostics; click a header to
  sort, a row for details), Traceability (step to workflow table).
- **Prompt bar** (bottom): slash commands, or plain text that is queued as an instruction for the
  next authoring or repair round.
- **Footer**: the key bindings.

### Keys

| Key | Action |
|---|---|
| `o` | open a PDD (picker lists `.docx`/`.pdf` in the current directory and `samples/`) |
| `r` | run all stages; pauses at the gates |
| `a` | open the plan approval modal |
| `i` | show or hide the inspector |
| `t` | dark or light theme |
| `ctrl+p` | command palette |
| `q` | quit; the run state is persisted and resumes later |

### Command palette (`ctrl+p`)

Open PDD, Run all stages, Run stage: <name> for each stage, Run doctor, Slash commands help,
Approve plan, Switch executor: <kind>, Install bridge worker, Restart bridge worker, Open in
Studio, Open report, Toggle inspector, Toggle theme, Quit. Type to filter.

### Slash commands (prompt bar)

Type `/` and the command; Tab or Right accepts the inline suggestion; Enter runs it. `/help`
shows this list inside the TUI, `/help <command>` explains one. Commands that change a setting
validate it, apply it to the session, and save it to the config file in use (or the user config
file when none was loaded).

| Command | What it does |
|---|---|
| `/help [command]` | list commands or explain one |
| `/open [pdd-path]` | open a PDD; without a path the picker opens |
| `/run [stage]` | run all stages, or one of ingest, extract, design, plan, scaffold, author, verify, report |
| `/approve [name]` | approve the current plan as `name` and continue; without a name the modal opens |
| `/resume` | wsl-manual: collect the pending Windows round and continue |
| `/status` | stage statuses, approval, repair round, pending round, agent sessions |
| `/doctor` | preflight checks in the detail view (no Studio or network probes) |
| `/studio` | open the generated project in Studio Desktop |
| `/report`, `/plan`, `/spec`, `/design` | show report.md, the plan, spec.json, design.json |
| `/logs [name] [lines]` | tail of a run log: `author`, `author-repair-1`, `events`, ... |
| `/instruction <text>` | queue an instruction for the next authoring or repair round (plain text does the same) |
| `/config [show|save|path]` | show the effective settings as TOML, save them, show the file path |
| `/set <key> [value]` | change a setting (saved); without a value shows it |
| `/get <key>` | show one setting |
| `/agent [backend] [model]` | authoring agent: `opencode`, `copilot` or `fake`, optionally with a model |
| `/model [id|none]` | authoring model; `none` means the backend default, which is `auto` for Copilot |
| `/extraction [backend] [model]` | extraction backend and model |
| `/executor [kind]` | `auto`, `native`, `wsl-interop`, `wsl-bridge`, `wsl-manual` |
| `/rounds [n]` | maximum repair rounds |
| `/ui-capture [stub|live]` | UI capture mode |
| `/copilot login|version|models` | Copilot CLI: device-code login streamed into the session log, version, model notes |
| `/opencode models [provider]|login|version` | OpenCode: the models it can use, login instructions, version |
| `/bridge install|status|stop` | wsl-bridge worker |
| `/theme`, `/inspector`, `/clear`, `/quit` | theme, inspector, clear the session log, quit |

Settings keys accepted by `/set` and `/get`: `executor`, `workspace_root`,
`default_target_framework`, `expression_language`, `ui_capture`, `approval_rule`, `skills_root`,
`extraction.backend`, `extraction.model`, `extraction.base_url`, `extraction.api_key_env`,
`extraction.max_tokens`, `agent.backend`, `agent.model`, `agent.max_turns`, `agent.timeout_s`,
`repair.max_rounds`, `bridge.poll_interval_s`, `bridge.job_timeout_s`,
`packages.uia_min_version`, `packages.prefer_prerelease`, `ui.theme`, `ui.glyphs`.
A new `workspace_root` applies to the next `/open`.

### Modals

- **Plan approval**: the rendered plan with its sha256 and the Task 0 installs. Type your name
  and Approve, or Escape to leave the run waiting. Approval is bound to the plan hash: editing
  the plan afterwards invalidates it and the TUI shows the diff since approval.
- **Windows round** (wsl-manual): the `run.cmd` and `run.ps1` to execute in a Windows terminal,
  the job count and a progress line while it polls; `c` copies the command, Escape aborts.
- **Open questions**: blocking questions found during design, with the contradiction when one
  exists. Both buttons stop the run; "Edit PDD and restart" reminds you that pressing `r`
  after editing the PDD re-runs ingest, extract and design, because the PDD hash changed.
- **Studio lock**: XAML or `project.json` files are locked by another process; Retry or Abort.
- **Detail**: any document or output; Escape closes.

### Resuming

Every stage writes its state to `<run>/.pdd2uipath/state.json`. Quit at any time; opening the
same PDD or run directory resumes at the gate it was waiting at, and stages whose inputs did not
change are skipped. `--force` on a CLI stage re-runs it anyway.

## 4. CLI reference

All stage commands take `--pdd <file>` (derives the run directory from the PDD's document ID) or
`--run <dir>`. `run-all` and `ingest` take the PDD as a positional argument.

| Command | Options | Purpose |
|---|---|---|
| `version` | | print the version |
| `doctor` | `--write-config`, `--probe-studio/--no-probe-studio`, `--network/--no-network`, `--json` | Stage 0: preflight, executor recommendation, `docs/cli-surface.md` |
| `ingest <pdd>` | `--force` | Stage 1: parse and apply the approval rule |
| `extract` | `--pdd/--run`, `--force` | Stage 2: ProcessSpec |
| `design` | `--pdd/--run`, `--force`, `--no-wait` | Stage 3: design rules and Windows probes |
| `plan` | `--pdd/--run`, `--force` | Stage 4: render the plan |
| `approve-plan` | `--approver <name>` (required), `--pdd/--run` | record the hash-bound approval |
| `scaffold` | `--pdd/--run`, `--force`, `--no-wait` | Stage 5: init, restore, package installs |
| `author` | `--pdd/--run`, `--force` | Stage 6: the coding agent on the plan |
| `verify` | `--pdd/--run`, `--no-wait` | Stage 7: Windows gate, audit, repair rounds |
| `report` | `--pdd/--run` | Stage 8: report.md and traceability.csv |
| `run-all <pdd>` | `--approver`, `--stop-after <stage>`, `--force`, `--no-wait` | every stage; pauses at the gates |
| `resume-round` | `--pdd/--run`, `--no-wait` | wsl-manual: collect the pending round and continue |
| `open-studio` | `--pdd/--run` | `uip rpa studio start` on the project, with a direct launch fallback |
| `status` | `--pdd/--run` | stage table, approval, repair round, pending round |
| `bridge install|status|stop` | | the wsl-bridge worker scripts, heartbeat, STOP file |
| `forward -- <uip args>` | `--project` | target of the per-project `uip` shim |

Typical sequences:

```bash
# one shot, approving as you
uv run pdd2uipath --plain run-all my.docx --approver "Jane Doe"

# stage by stage
uv run pdd2uipath --plain run-all my.docx --stop-after plan
uv run pdd2uipath --plain approve-plan --approver "Jane Doe" --pdd my.docx
uv run pdd2uipath --plain run-all my.docx

# wsl-manual without waiting
uv run pdd2uipath --plain run-all my.docx --approver "Jane Doe" --no-wait   # exit 4, run the round
uv run pdd2uipath --plain resume-round --pdd my.docx --no-wait              # repeat until exit 0
```

## 5. The stages

### Stage 0: doctor

Checks, in order: platform and WSL interop, workspace root, Python, uv, git, `uip` on the
brain and on Windows, the `rpa` tool, `uip login` on both sides, .NET, the `uipath-rpa` skill,
OpenCode and Copilot CLIs, the configured agent and extraction models, the extraction backend,
Studio Desktop, the headless host cache and a licence probe (`templates search`), proxy and CA
settings, registry reachability, the bridge worker, and finally the executor recommendation.
It probes the live `uip` command surface and writes `docs/cli-surface.md`; the pipeline's
command adapter resolves canonical operations against that surface and refuses to guess verbs.
`--write-config` saves the recommendation and discovered launcher paths.

### Stage 1: ingest

Parses DOCX (python-docx, body order) or PDF (pdfplumber with table anchoring) into a
`Document` with headings, paragraphs and tables, each carrying a reference such as `§4.3/S06`
or `p.3 §4.2`. The approval rule `document-control-and-signoff` requires the Document Control
table to say `Approved` and every row of the decision table to carry `Approved` with a parseable
date. A draft PDD stops here with exit 5 and the verdict in `approval-verdict.json`.

### Stage 2: extract

Deterministic table facts come first: steps, business rules, exceptions, applications,
operational data, open points, field mappings. The model then fills the strict JSON schema
(every property required, no additional properties); deterministic facts win on conflict,
steps the model omitted are added with a warning, and a validation failure gets one retry.
Backends: `openai-compatible` (local server, stdlib HTTP, response-format JSON schema),
`opencode` (`opencode run --format json`), `copilot` (`copilot -p ... -s`), `command` (your own
CLI, `{prompt_file}` and `{model}` placeholders), `fixture` (JSON files for tests). The prompt is
passed through a temporary file so long PDDs never hit argument limits.

### Stage 3: design

Explainable rules with rationale and PDD references:

- **Template**: Dispatcher/Performer split, a queue name, a per-item transaction unit or a
  queue-item retry policy means REFramework; the Windows probe `templates search` must return
  exactly one Official template, otherwise the run stops and asks. Library and test wording map
  to those templates; otherwise Blank.
- **Target framework**: Windows-only signals (SAP GUI, classic Office, Excel macros, Windows
  VDI) give `Windows`; cross-platform signals (Linux robots, containers, Studio Web) give
  `Portable`; both at once is a contradiction and the run stops with both lists. Legacy is never
  inferred.
- **Workflows**: one file per step, named `<StepId>_<Name>.xaml`, grouped by REFramework role:
  settings into `InitAllSettings`, application login into `InitAllApplications`, Dispatcher steps
  under `Dispatcher/`, Performer steps under `Performer/` invoked from `Process.xaml`, end-of-run
  steps from `CloseAllApplications`.
- **Packages**: Mail, PDF, Excel, Word and UI Automation from the applications and step kinds,
  with versions from `packages versions`; in stub mode no UIA upgrade is forced.
- **Questions**: blocking open questions stop the run; non-blocking ones go into the plan and
  the report.

### Stage 4: plan

Renders `docs/plans/<slug>.md` in the skill's plan format: header (goal, source hash, project
type, framework, expression language, UI capture, executor), understanding, decisions, stop
conditions, open questions, Orchestrator resource names, the **Reference data** section (data
items, business rules, exceptions, applications, transaction and schedule, so the agent never
needs the PDD), Task T0 (package installs; approving the plan is the install consent), Task T1
(REFramework configuration), one task per workflow file with the step details and data items,
and the build gate task. Approval writes `approval.json` with the plan's sha256 and the
approver; `approval-plan.md` keeps the approved text. Any later edit changes the hash and the
pipeline refuses to continue until the plan is approved again.

### Stage 5: scaffold

`uip rpa init` with the chosen template, `restore`, and `packages install` for exactly the Task
0 packages. Then the plan and `docs/process-spec.json` are copied into the project,
`.gitattributes` and `.gitignore` are added, and the pristine scaffold is committed. A project
that already exists in the run directory stops the stage unless a pending manual round created
it.

### Stage 6: author

Installs the per-project `uip` shim (`<project>/.pdd2uipath/bin/uip`), makes the skill
discoverable (`.github/skills/uipath-rpa` for Copilot, `.opencode/skills/uipath-rpa` for
OpenCode, copied when the global install is missing), and runs the agent with the prompt:
execute the plan task by task with the skill, annotate every workflow with its step ID,
validate after each file, stop conditions are task-scoped, a missing technique is implemented
and marked `TODO Confirm`, no credential values, no publish, pack, run or `git push` (enforced
with deny rules). Copilot runs `copilot -p ... --allow-all-tools --output-format json` with
`--deny-tool` rules; OpenCode runs `opencode run --format json --auto`. The session is logged to
`logs/author.jsonl`; a queued instruction from the prompt bar is appended to the prompt. Each
round is committed.

### Stage 7: verify

The Windows gate validates every workflow file and builds the project. In stub mode the only
acceptable errors are the unconfigured-target errors on `TODO Indicate` activities ("Target or
Input UI Element must be set", "The Use Application/Browser activity is not yet configured");
they are counted as expected. The Python audit checks that every PDD step has a workflow, that
UI steps are not Log-only stubs, that no credential literal appears, and counts the markers. On
failure the exact diagnostics and audit problems go back to the agent as a repair prompt,
resuming its session where the CLI supports it, up to `repair.max_rounds` times. Each round's
verdict is `gate-<n>.json`. A lock check reports Studio instances and blocks only on real file
locks.

### Stage 8: report

`report.md` with outcome, decisions, open questions, `TODO Indicate` per file, `TODO Confirm`
assumptions per file, the agent's hand-off notes per session, Orchestrator resources to create,
validation summary, traceability, how to run, and what the developer still does in Studio.
`traceability.csv` has one row per step and activity: `step_id`, `pdd_ref`, `file`,
`activity_display_name`, `status` (implemented, stub, log-only, missing). Both are committed.

## 6. Artefacts

Run directory `<workspace_root>\<document-id>\`:

| Path | Content |
|---|---|
| `.pdd2uipath\state.json` | stage records, approval, repair round, pending round, agent notes |
| `document.json`, `approval-verdict.json` | parsed PDD and approval verdict |
| `spec.json`, `design.json` | ProcessSpec and design result |
| `docs\plans\<slug>.md`, `approval.json`, `approval-plan.md` | plan, approval record, approved text |
| `gate-<n>.json` | verdict and audit of each gate run |
| `report.md`, `traceability.csv` | hand-off |
| `logs\events.jsonl`, `logs\author*.jsonl` | event bus and agent sessions |
| `<ProjectName>\` | the Studio project (git repository) |

Inside the project pdd2uipath adds `docs\plans\<slug>.md`, `docs\process-spec.json`,
`.pdd2uipath\bin\uip`, `.gitattributes`, `.gitignore` and, when needed, the skill copy.

## 7. Configuration reference

`pdd2uipath.toml`; defaults in brackets.

| Key | Meaning |
|---|---|
| `workspace_root` | Windows path for generated runs (required) |
| `executor` [auto] | `auto`, `native`, `wsl-interop`, `wsl-bridge`, `wsl-manual` |
| `uip_windows_command`, `uip_brain_command` [[]] | launcher argv on each side; the doctor fills them (`node.exe` + the CLI's `index.js`) |
| `skills_root` [none] | folder of the `uipath-rpa` skill |
| `default_target_framework` [Windows] | used when the PDD gives no signal |
| `expression_language` [VisualBasic] | `VisualBasic` or `CSharp` |
| `ui_capture` [stub] | `stub` (placeholder targets) or `live` (applies `packages.uia_min_version`) |
| `approval_rule` [document-control-and-signoff] | approval rule id |
| `side_table` | which side runs `rpa`, `skills`, `login`, `--version` |
| `extraction.backend` [auto] | `auto`, `openai-compatible`, `opencode`, `copilot`, `command`, `fixture` |
| `extraction.model` [""] | model id or provider/model |
| `extraction.base_url` [""] | OpenAI-compatible endpoint |
| `extraction.api_key_env` [OPENAI_API_KEY] | env var holding the key; the key itself is never stored |
| `extraction.use_response_format` [true] | send the JSON schema as response format |
| `extraction.command` [[]] | custom extraction command with `{prompt_file}` / `{model}` |
| `extraction.fixture_dir` [none] | fixture JSON directory |
| `extraction.max_tokens` [32000] | output cap |
| `agent.backend` [auto] | `auto`, `opencode`, `copilot`, `fake` |
| `agent.model` [none] | `github-copilot/<id>` for OpenCode, `<id>` for Copilot, empty = default or auto |
| `agent.command` [[]] | override the agent launcher |
| `agent.max_turns` [120], `agent.max_budget_usd` [25.0], `agent.timeout_s` [7200] | session limits |
| `agent.deny_tools` | tools the agent may never call (publish, pack, run, debug, remote, Orchestrator, `git push`) |
| `agent.fake_script` [none] | the deterministic test agent |
| `repair.max_rounds` [2] | repair rounds after a failed gate |
| `bridge.poll_interval_s` [1.0], `bridge.heartbeat_timeout_s` [15], `bridge.job_timeout_s` [900], `bridge.dirname` [.pdd2uipath-bridge] | bridge protocol |
| `ui.theme` [dark], `ui.glyphs` [unicode] | TUI appearance; `ascii` for terminals without Unicode |
| `uipath.org`, `uipath.tenant` [""] | informational |
| `packages.uia_min_version` [26.10.2], `packages.prefer_prerelease` [false] | package policy |

## 8. Executors

| Executor | How `uip` runs | Notes |
|---|---|---|
| `native` | directly, brain and hands on the same Windows machine | default on Windows |
| `wsl-interop` | `node.exe` or `cmd.exe /c` started from WSL through binfmt interop | fastest WSL option; WSLENV forwards proxy variables |
| `wsl-bridge` | jobs written as files under `<workspace>\.pdd2uipath-bridge\jobs`, served by `worker.cmd` or `worker.ps1` running in a Windows terminal | heartbeat by file mtime; allowlisted verbs only; paths must be inside the workspace |
| `wsl-manual` | one generated `rounds\<id>\run.cmd` (and `run.ps1`) per batch; you run it by hand | the TUI shows the command and polls; the CLI waits, or exits with code 4 under `--no-wait`; `resume-round` reuses the same round, so each script runs exactly once |

Design probes, scaffold and each gate form one round each; a 12-step PDD needs three manual
rounds for a clean run plus one per repair round.

The bridge allowlist: `rpa templates|init|restore|packages|activities|validate|build|analyze|analyzer-rules|studio|instances|get-object-repository|workflow-examples`, `login status|which`, `--version`, `--help`. Tokens containing `& | < > ^ " % !` or line breaks are refused.

## 9. Agents and models

**GitHub Copilot CLI.** Login with `copilot login` (or `/copilot login` in the TUI, device-code
flow). The CLI only accepts models that your plan's model picker enables; Copilot Free has
none, so leave the model empty (auto). Enterprise seats expose what the organisation policy
enables, for example `gpt-6.1-sol`; a wrong id fails fast with "is not available" without
spending a request. Each authoring session costs one premium request regardless of its length.

**OpenCode.** `opencode auth login` for GitHub Copilot or a local provider; `opencode models`
lists what it can use. The GitHub Copilot provider only exists when at least one model is
picker-enabled for the seat. Model ids are `provider/model`.

**Local LLM.** Any OpenAI-compatible server for extraction (`extraction.base_url`), and through
OpenCode for authoring.

**Fake agent.** `agent.backend = "fake"` with `tests/fake_agent/fake_agent.py` writes stub
workflows from the plan deterministically; it is what the test suite and the demos use.

**Model availability checks.** `doctor` verifies a configured OpenCode model against
`opencode models <provider>`; for Copilot it tells you the dry-run command, since the CLI cannot
list models non-interactively.

## 10. The skill and the uip shim

The `uipath-rpa` skill (installed with `uip skills install --agent ...`) is the source of truth
for how workflows are authored; pdd2uipath only tells the agent to execute the plan with it.
When the skill and this documentation disagree, the skill wins. The per-project shim
`<project>/.pdd2uipath/bin/uip` is placed first on the agent's PATH so the skill's own
`uip rpa validate` and `uip rpa build` calls go through the active executor, which matters in
WSL. `pdd2uipath forward -- <args>` is its target.

## 11. Security notes

Secrets come only from environment variables or the OS keyring; the audit scans generated
workflows for credential literals. Proxy and CA settings (`HTTPS_PROXY`, `NO_PROXY`,
`SSL_CERT_FILE`, `UV_NATIVE_TLS`) are honoured and reported by the doctor. `shell=True` is never
used; npm-installed CLIs are started as `node.exe <loader.js>` so no `cmd.exe` parses the
prompts. Logs never contain credential values, and the bridge worker refuses everything outside
its allowlist and the workspace root.

## 12. Troubleshooting and FAQ

| Question | Answer |
|---|---|
| The run stopped with exit 5 after ingest | the PDD is not approved per the rule; `approval-verdict.json` lists what failed |
| Design stopped with a contradiction | the PDD mixes Windows-only and cross-platform signals; fix the PDD and `/run design` or restart |
| `templates search` returned several Official templates | the run asks; pin the template id in the PDD or wait for a rule update |
| The plan hash no longer matches | the plan was edited after approval; approve again |
| The agent stopped early | read `/logs author` and the hand-off notes in the report; task-level stop conditions now skip a task instead of ending the run |
| `No Studio licence` | open Studio Desktop and sign in |
| OpenCode: `Provider not found: github-copilot` | no picker-enabled models on this seat; use the Copilot CLI with auto, or an enterprise seat |
| Copilot: `Model "x" ... is not available` | use `/model none` or a listed id |
| The gate fails only with "Target or Input UI Element must be set" | expected in stub mode; the gate passes when nothing else is wrong |
| Where is the project? | `<workspace_root>\<document-id>\<ProjectName>\project.json`; `/studio` opens it |

## 13. Development

```bash
uv run python -m pytest                     # fake uip, fixtures, fake agent, TUI pilot + snapshots
uv run python -m pytest -m e2e              # real uip; PDD2UIPATH_E2E_FULL=1 for the whole PDD-01 run
uv run python -m ruff check src tests && uv run python -m ruff format --check src tests
uv run python -m mypy
UPDATE_GOLDEN=1 uv run python -m pytest tests/test_plan.py     # accept a changed plan rendering
uv run python -m pytest tests/test_tui.py --snapshot-update    # accept changed TUI snapshots
```

Layout: `core/` (config, events, state, paths, runner, exe), `ingest/`, `spec/`, `design/`,
`plan/`, `scaffold/`, `agents/`, `verify/`, `report/`, `executors/`, `doctor/`, `tui/`
(app, widgets, screens, gates, commands, slash). Design decisions and lessons from real runs are
recorded in [decisions.md](decisions.md).

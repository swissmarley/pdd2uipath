# Decisions and defaults

Recorded while building pdd2uipath (2026-10-09). Anything not asked of the user is a default here.

## Environment and tooling

- **No Anthropic packages.** The corporate VDI has no Anthropic access, so `anthropic` and
  `claude-agent-sdk` are not dependencies at all. LLM-facing work is pluggable:
  extraction backends `openai-compatible` (local LLM, stdlib HTTP), `opencode`, `copilot`,
  `command`, `fixture`; authoring agents `opencode` (default when installed), `copilot`, `fake`.
  OpenCode flags verified with `opencode run --help` (opencode-ai 1.18.35); Copilot flags with
  `copilot --help` / `copilot help permissions` (GitHub Copilot CLI 1.0.94).
- **Packages via Artifactory.** Dependencies are mainstream PyPI packages only (typer, textual,
  pydantic, pydantic-settings, jinja2, rich, pdfplumber, python-docx, keyring, tomli-w). See
  `docs/vdi-setup.md` for `UV_INDEX_URL` / `npm config set registry` / CA bundle settings.
- **AppLocker-safe invocations.** `.venv\Scripts\pytest.exe` was blocked by an application-control
  policy (os error 4551) on the development machine; docs and CI use `python -m pytest|ruff|mypy`.
- **Studio 2025.10.x compatibility.** The REFramework mapping is role-based and resolved against
  the files the template actually ships (`Data/Config.json` in 26.10, `Config.xlsx` in 25.10). In
  stub mode no UIA upgrade is forced (the Placeholder-Selector Stub Pattern needs no `uip rpa uia`
  CLI); `ui_capture = "live"` applies the skill's minimum version (26.10.2). Alternate verb
  spellings (`create-project`, `search-templates`, `get-errors`, `start-studio`) are resolved from
  the live surface probe. Studio Desktop is searched under `Program Files\UiPath\Studio`,
  `Program Files\UiPathPlatform\Studio\<version>`, `%LOCALAPPDATA%\Programs\UiPath\Studio` and
  `%LOCALAPPDATA%\UiPath\app-*`.
- **Headless host licensing.** On this machine `uip rpa` returned
  `HelmFeatureNotAvailableException: No Studio licence` while Studio Desktop was closed and worked
  once Studio Desktop was open and signed in. `doctor` probes `templates search` and reports this
  with the fix.
- **`exec/` is `executors/`.** Shadowing the `exec` builtin trips linters; same content.

## Pipeline semantics

- **Approval rule** (`document-control-and-signoff`): Document Control `Status == "Approved"`
  and every row of the Decision/Date table has `Approved` plus a parseable date. Pluggable via
  `approval_rule` in config.
- **Extraction** parses tables deterministically first (steps, rules, exceptions, applications,
  operational, open points, field mapping); the model fills the rest under a strict JSON schema
  (every property required, no additional properties). Deterministic facts win on conflict; steps
  the model omitted are added from the table with a heuristic UI flag and a warning. One retry on
  validation failure, then stop.
- **Template rule**: Dispatcher/Performer split, queue name, per-item transaction unit, or a
  queue-item retry policy -> REFramework (exactly one *Official* `templates search` result;
  several Official, Marketplace-only, or none -> stop and ask). Library/test-spec wording ->
  Library / Test Automation. Otherwise Blank.
- **Framework rule**: Windows-only signals (SAP GUI, classic Outlook/Office desktop, Excel macros
  / `.xlsm` / VBA, Windows VDI) -> `Windows`; cross-platform signals (Linux, serverless,
  container, Studio Web) -> `Portable`; both -> stop with the contradiction (PDD-02). Legacy is
  never inferred. No signal -> configured default (`Windows`).
- **Dispatcher in the same project.** PDD-01's Dispatcher steps become `Dispatcher/*.xaml`
  invoked from a second entry point `Dispatcher.xaml`; Performer steps go to `Performer/*.xaml`
  invoked from `Framework/Process.xaml`; app login -> `InitAllApplications`; end-of-run ->
  `CloseAllApplications`; config load -> `InitAllSettings` (customized, never replaced). One
  project keeps the pipeline (and the hand-off) simple; a separate Dispatcher project remains a
  one-line change in `design/rules.py`.
- **Stub-mode acceptance** (skill contradicts the prompt, skill wins): a stub-mode deliverable
  does *not* reach a fully clean build — the only acceptable remaining errors are the
  unconfigured-target ones on `TODO Indicate` activities (`Target or Input UI Element must be
  set`, `The Use Application/Browser activity is not yet configured`). The gate classifies those
  as *expected* and passes when nothing else is wrong; the report lists them.
- **Pristine REFramework builds fail** until the queue name is set
  (`[Framework\GetTransactionData.xaml] Value for a required activity argument 'Queue name' was not
  supplied`), so Task T1 of every REFramework plan configures the queue, assets and retry
  constants before any step workflow.
- **Repair rounds**: default 2 (`repair.max_rounds`); each feeds the exact validate/build errors
  and audit problems back to the agent (`resume` on the same session where the CLI supports it).
- **Design probes** (`templates search`, `packages versions`) run as one Windows round *in
  Stage 3*, so Stage 4 stays deterministic and Task 0 carries exact versions (newest stable unless
  `packages.prefer_prerelease`).
- **Run directory**: `<workspace_root>\<slug>\` where slug = Document ID; the project is created
  inside it; state in `.pdd2uipath/state.json`; artefacts `document.json`, `spec.json`,
  `design.json`, `docs/plans/<slug>.md`, `approval.json`, `gate-<n>.json`, `report.md`,
  `traceability.csv`, `logs/*.jsonl`.

## Bridge protocol

- `jobs/<id>.json` (canonical, `job.schema.json`) + `jobs/<id>.args` sidecar (one field per
  line) because cmd.exe cannot parse JSON. Results: `results/<id>.json` written as `.tmp` then
  renamed, with stdout/stderr in `<id>.out` / `<id>.err`. Heartbeat: `heartbeat.json` mtime (no
  inotify over `/mnt/c`; polling only).
- Allowlist: `rpa {templates, init, restore, packages, activities, validate, build, analyze,
  analyzer-rules, studio, instances, get-object-repository, workflow-examples}`, `login
  {status, which}`, `--version`, `--help`. Tokens with `& | < > ^ " % !` or line breaks are
  refused; every path flag and the cwd must be inside the workspace root. `worker.ps1` enforces
  all of it; `worker.cmd` enforces verb, cwd and metacharacters (reduced, documented) — the Python
  side validates everything before writing a job.
- Manual rounds are explicit scripts (`rounds/<id>/run.cmd` + `run.ps1`) with the arguments baked
  in; the TUI shows the command once per round and polls. In plain mode `--no-wait` exits with
  code 4 and records the pending round in the run state; the next `run-all` / `resume-round`
  re-runs the waiting stage and the manual executor *reuses* that round (same jobs) instead of
  generating a new script, so the user runs each round script exactly once. The round's job
  list is kept in `rounds/<id>/manifest.json` (job files may be consumed by a worker); a
  pending round that does not match the first batch of the resumed stage is kept for the next
  matching batch, and a matching batch consumes it (an identical later batch - e.g. the gate
  after a repair round - gets a fresh round). Stage code must let `RoundPending` /
  `RoundAborted` propagate; the scaffold stage tolerates the project the pending round
  already created, and the gate skips the informational `instances list` probe in
  `wsl-manual` (it would cost the user an extra round before every gate).

## Agent sessions

- Prompt: "execute `docs/plans/<slug>.md` with the uipath-rpa skill" plus the guardrails;
  `--allow-all-tools` (required for non-interactive Copilot) is paired with explicit
  `--deny-tool` rules for publish/pack/run/solution/orchestrator verbs and `git push`; OpenCode
  runs with `--auto`. The uipath-rpa skill is copied into `.opencode/skills/` or
  `.github/skills/` of the project when the agent cannot see the global store.
- Sessions are logged to `logs/author*.jsonl`; stdout lines (plain or JSONL) are mapped to
  `AgentMessage` / `AgentToolUse` / `AgentToolResult` events leniently (event shapes differ per
  CLI version).

## Learned from the real runs (2026-10-09)

- `GetQueueItem` (UiPath.System.Activities 26.8) takes the queue as the `QueueType` InArgument
  (display name "Queue name"); a `QueueName` attribute fails to load the XAML. The activity doc
  under `<project>/.local/docs/packages/UiPath.System.Activities/activities/GetQueueItem.md` is
  the authority, which is exactly why the plan tells the agent to read `.local/docs` first.
- The headless host reports validate errors in the OS language (Italian here) and build errors
  in English; diagnostics are deduplicated per file/severity/message, so the same defect can
  appear twice when the texts differ. The gate still counts it once per file in the report.
- When Studio Desktop is running, `uip rpa` routes the project's calls through that instance
  and the project then shows up in `uip rpa instances list` as *open*. That is not a lock: the
  gate blocks only on real file locks (`PermissionError` on open) and reports the instance list.
- Config written by a Windows `doctor` run contains Windows launcher paths; under WSL the brain
  ignores a Windows-path `uip_brain_command` and translates `skills_root` with `brain_path()`.
- GitHub Copilot model entitlement is per seat: the Copilot API's `model_picker_enabled` flag
  decides what the Copilot CLI (`--model`) and OpenCode (`github-copilot/*`) can use. Copilot
  Free exposes no picker models (CLI: `auto` only; OpenCode drops the provider and explicit
  models get HTTP 400), Enterprise seats expose what the org policy enables. `doctor` checks the
  configured OpenCode model; Copilot CLI ids can only be checked by a dry call.

## Learned from the real Copilot CLI runs (2026-10-09, model `auto`)

- The agent never sees the PDD, so the plan must be self-contained. The first real run stopped
  at S04 because the task said "the fields listed in Appendix A" and the plan did not reproduce
  that list. The plan now has a *Reference data* section (data items, business rules,
  exceptions, applications, transaction, schedule), spells out each step's data items, and the
  scaffold ships `docs/process-spec.json` next to the plan.
- Stop conditions must be task-scoped, otherwise one blocked step ends the run with most
  workflows missing (the second real run stopped at T2 on a validation error and later at S04).
  Project-level conditions (packages, framework, expression language, a `uip` command that
  fails identically twice) end the run; task-level ones skip the task and the run continues.
- A missing *technique* (how to parse PDF text, which label precedes a value) is normal in a PDD
  and is not a stop condition: the agent implements a reasonable approach, annotates it
  `TODO Confirm - <what>` and reports it; a missing *value* (queue, asset, path, URL, mailbox,
  transaction code, field name) still stops that task.
- npm-installed CLIs (`copilot`, `opencode`) are started as `node.exe <loader.js>` resolved from
  their `.cmd` shim (`core/exe.py`): a bare name fails with WinError 2 under `shell=False`, and
  running the `.cmd` would pass the multi-kilobyte prompt through `cmd.exe`.
- Copilot CLI `--output-format json` emits mostly `*_delta` and ephemeral events (19 MB for one
  session); `premiumRequests` in its final `result` event is cumulative per resumed session.

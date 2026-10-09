# pdd2uipath

Approved Process Definition Document (PDD) -> UiPath Studio project, built around the official
`uip` CLI (`@uipath/cli`, `rpa` tool) and the `uipath-rpa` skill, with a Textual TUI and a
scriptable CLI. Python owns everything deterministic; a coding agent (OpenCode with a local LLM,
or GitHub Copilot CLI) authors the workflows through the skill; an `Executor` decides where `uip`
runs.

## Brain / hands split (the central design decision)

- **Brain** (WSL or Windows): Python core, TUI, PDD parsing, design rules, plan rendering, the
  coding agent.
- **Hands** (always Windows): every `uip rpa` verb (init, restore, packages, validate, build,
  studio start). They go through one `Executor` (`src/pdd2uipath/executors/`):
  `native` (Windows Python), `wsl-interop` (cmd.exe / node.exe via binfmt), `wsl-bridge`
  (file-based job queue served by `worker.cmd` / `worker.ps1` in the Windows session; the VDI
  default when interop is blocked), `wsl-manual` (one generated `run.cmd` per gate round that the
  user runs by hand). Generated projects always live under the Windows workspace root; `\\wsl$`
  paths are refused.
- The per-project `uip` shim (`<project>/.pdd2uipath/bin/uip`) forwards the skill's own
  `uip rpa validate/build` calls to the active executor.

## Source of truth for UiPath specifics

The installed `uipath-rpa` skill (`uip skills list` shows the store) and the live `uip --help` /
`uip rpa <verb> --help` are the source of truth. `pdd2uipath doctor` records the probed surface in
`docs/cli-surface.md`; the command adapter (`executors/surface.py`) refuses to guess verbs or
flags. When the skill contradicts this repo's docs, the skill wins — update `docs/decisions.md`.

## Commands

```bash
uv sync                                   # deps (no Anthropic packages; Artifactory-friendly)
uv run python -m pytest                   # unit tests (fake uip, fixtures, fake agent)
uv run python -m pytest -m e2e            # opt-in: real Windows uip through the executor
uv run python -m ruff check src tests && uv run python -m ruff format --check src tests
uv run python -m mypy
uv run pdd2uipath doctor --write-config   # preflight + executor recommendation + cli-surface.md
uv run pdd2uipath                         # TUI (no args in a TTY); `pdd2uipath <cmd> --plain` for CI
uv run pdd2uipath run-all samples/PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx
```

Use `python -m <tool>` rather than the `.venv\Scripts\*.exe` launchers: application-control
policies (AppLocker / Smart App Control) block the generated launcher executables.

## Layout

`core/` (config, events, state, paths, runner) · `ingest/` (DOCX/PDF -> Document, approval rule)
· `spec/` (ProcessSpec, deterministic tables, extraction backends) · `design/` (rules with
rationale) · `plan/` (skill-format plan + approval) · `scaffold/` · `agents/` (opencode, copilot,
fake) · `verify/` (gate parsing, XAML audit) · `report/` · `executors/` · `doctor/` · `tui/`.

## Guardrails (enforced in code)

Never past Stage 1 on an unapproved PDD; never past Stage 4 without a hash-bound plan approval;
never hand-written `project.json`/selectors; no package outside Task 0; no target-framework or
expression-language change after init; UI steps are real UIA activities with `TODO Indicate`
placeholders; the bridge worker runs only allowlisted `uip` verbs inside the workspace; no
Orchestrator publish, pack, or job runs.

# Build `pdd2uipath`: an approved-PDD → UiPath Studio project pipeline (WSL + Windows, with TUI)

> Paste this whole file into Claude Code (start in **plan mode**) from an empty repo folder.
> Fill in `<my_inputs>` first. Everything else is written for the model.

<my_inputs>
- Sample PDDs (in `./samples/`):
  - `PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx`: complete and approved. Expected: REFramework, Windows, full run.
  - `PDD-02_Daily_FX_Rate_Update_v1.0_APPROVED.pdf`: approved, but contains gaps and a framework contradiction (Linux robots + Excel `.xlsm` macro). Expected: open questions, and a stop at the design stage.
  - `PDD-03_Customer_Address_Change_v0.3_DRAFT.docx`: draft. Expected: rejected at the approval gate.
- PDD formats we receive: `{{PDF and DOCX}}`
- Approval rule: `{{Document Control → Status = "Approved" AND every row of the "Approval and Sign-off" table has Decision = "Approved" with a date}}`
- Default target framework when the PDD gives no signal: `{{Windows}}`
- Expression language: `{{VisualBasic}}`
- UiPath Cloud org / tenant: `{{org/tenant}}`
- Workspace root on the Windows filesystem where generated projects live: `{{C:\Users\<me>\RPA\pdd2uipath-projects}}`
- Coding agent I also use: `{{opencode (runs inside WSL)}}`
</my_inputs>

<context>
Our RPA team writes a Process Definition Document (PDD) for every automation. Once business and IT sign it off, a developer spends days turning it into a Studio project skeleton. We want the first 60–80% done automatically and consistently. The developer should start from a project that already builds, mirrors the PDD 1:1, and only needs selectors indicated and business logic refined in Studio.

UiPath ships tooling designed for this:
- **UiPath CLI (`uip`)**: the cross-platform, agent-friendly CLI (npm `@uipath/cli`, GA since mid-2026). The RPA verbs live in the `rpa` tool (`@uipath/rpa-tool`), which drives a headless Studio host.
- **UiPath Skills**: installed with `uip skills install --agent claude` (or `--agent opencode`). The relevant skill is **`uipath-rpa`**. It creates projects, authors XAML and coded workflows, and validates and builds them with `uip rpa`.

**The environment makes this harder than it sounds.** The pipeline runs inside our corporate VDI, which is heavily restricted:
- Coding agents (Claude Code, opencode) and Python tooling run in **WSL**.
- `uip rpa` validation, build and run only work in the **Windows session**. They need the Windows Studio host, .NET Windows components, and the desktop apps.
- Today a developer edits in WSL, then manually switches to a Windows terminal to validate and build, then switches back. The pipeline must turn that switch into a deliberate, well-supported part of the design, not an afterthought.
- Expect: no admin rights, a corporate proxy with TLS inspection (custom CA), AppLocker / PowerShell execution-policy restrictions, possibly disabled WSL↔Windows interop, restricted package registries, and no Docker.

Your job is to build the **Python** orchestration and a polished **TUI** around these tools. Do not reimplement what the skill already does.
</context>

<objective>
Build a Python package named `pdd2uipath` with a TUI and a scriptable CLI. Given one approved PDD, it produces a UiPath Studio project that:
1. was created with the official CLI (never a hand-written `project.json`),
2. contains one workflow (or REFramework state or sub-workflow) per PDD process step, annotated with the PDD step ID,
3. passes per-file `validate` and project-level `build` with zero errors, **executed in the Windows session**,
4. uses real UI Automation activities with placeholder selectors and `TODO Indicate` markers wherever the PDD describes UI interaction (never `Log`-only stubs),
5. ships with a traceability matrix (PDD step → file → activity) and a hand-off report,
6. opens cleanly in UiPath Studio Desktop,
7. works the same whether the pipeline runs natively on Windows or in WSL with any of the bridge modes below.
</objective>

<verified_environment_facts>
These were true in October 2026, but the surface moves fast. **Treat them as hypotheses and confirm each one against the live tools before relying on it.** Use `uip --help`, `uip rpa --help`, `uip rpa <verb> --help`, and the installed skill files.

- Install: `npm i -g @uipath/cli`. `@uipath/rpa-tool` is hosted on the **GitHub npm registry**, so the `@uipath` npm scope must point at `https://npm.pkg.github.com` before `uip tools install @uipath/rpa-tool` works. `uip rpa` needs a .NET runtime.
- Auth: `uip login` (interactive OAuth) or env-var auth. Session credentials are resolved from a `.uipath/.auth` file by walking up the directory tree. **The Windows and WSL installs of `uip` are separate and each needs its own login.**
- Skills: `uip skills install --agent claude` (global for Claude Code). OpenCode is also a supported agent.
- The `uipath-rpa` SKILL.md currently uses these commands:
  - `uip rpa templates search`
  - `uip rpa init`, with explicit `--target-framework` and `--expression-language`
  - `uip rpa packages versions|install`
  - `uip rpa activities find|get-default-xaml`
  - `uip rpa validate --file-path`
  - `uip rpa build`, `uip rpa run`, `uip rpa pack`
  The public docs also list alternate verbs (`create-project`, `search-templates`, `get-errors`, `start-studio`). **The installed skill and live `--help` win.**
- `uip rpa` auto-launches a headless Studio. Studio Desktop is needed only for a few operations and for the human hand-off.
- The skill reads a plan file at `docs/plans/*.md`:
  - Header fields `Execution autonomy: autonomous|interactive`, a `Stop conditions` section, task checkboxes, and an optional `UI capture:` line control how far it runs.
  - **Approval of a plan whose Task 0 names package installs (ID + version) counts as consent** for those installs.
- Without live app access, the skill's **Placeholder-Selector Stub Pattern** is the required way to author UI steps.
- Claude Code asks for approval on every `uip` call by default. A realistic RPA build runs 25+ subcommands.
- WSL specifics, to verify on the target machine:
  - Windows interop is available only if `/proc/sys/fs/binfmt_misc/WSLInterop` exists and is enabled. It can be disabled by `/etc/wsl.conf` or by organizational policy.
  - npm-installed Windows CLIs are `.cmd` shims and must be invoked via `cmd.exe /c` or `powershell.exe`.
  - `wslpath -w|-u` converts paths.
  - Windows-side writes to `/mnt/c` do **not** reliably raise inotify events in WSL, so watch with polling.
</verified_environment_facts>

<execution_model>
This is the most important design decision in the project. Get it right before writing pipeline stages.

**Two halves:**
- **Brain (WSL or Windows):** the Python core, the TUI, PDD parsing, design, plan generation, and the coding agent (Claude Code headless; optionally opencode).
- **Hands (always Windows):** every `uip rpa` operation that touches Studio, plus opening Studio Desktop.

**Rules that follow from this:**
1. **Generated projects always live on the Windows filesystem**, under the configured workspace root. WSL accesses them via `/mnt/<drive>/...`. Never put a project under `\\wsl$` / `\\wsl.localhost`, because Studio, NuGet and file locking misbehave there. The repo of `pdd2uipath` itself may live anywhere.
2. **All `uip` invocations go through one `Executor` interface:** `run(argv, cwd) -> UipResult(exit_code, stdout_json, stderr, duration)`. Path arguments are translated automatically (`--project-dir`, `--file-path`, positional project paths, `cwd`). If a path is not on the Windows filesystem, refuse with a clear error.
3. **Executors**, auto-detected by `doctor` and overridable in config:
   - `native`: the pipeline runs in Windows Python and calls `uip` directly.
   - `wsl-interop`: WSL calls Windows `uip` through `cmd.exe /c` or `powershell.exe -NoProfile`, translating paths with `wslpath -w`. Use this only if interop is enabled and allowed.
   - `wsl-bridge` (**the default for our VDI when interop is blocked**): a file-based job queue on the Windows filesystem, served by a **Windows worker** the user starts once per session in the Windows session.
     - The worker is a small script generated by `pdd2uipath bridge install`. Provide both `.ps1` and `.cmd` variants, because PowerShell may be blocked by execution policy or AppLocker. It needs no Python on Windows.
     - It polls `<workspace>/.pdd2uipath-bridge/jobs/`, executes the job, writes `results/<job-id>.json` atomically (write a temp file, then rename), and writes a heartbeat file every few seconds.
     - **The worker executes only an allowlist of `uip` subcommands**, with arguments validated against the job schema and project paths that must be inside the workspace root. It is a command channel, so treat it like one: no arbitrary command execution, no shell interpolation.
   - `wsl-manual`: the fallback when even a long-running worker is not possible.
     - The pipeline batches all pending Windows operations into one generated script and shows the user the exact command to run in their Windows terminal.
     - It then polls for results and continues automatically.
     - The validate → repair loop becomes rounds (author in WSL → one Windows gate round → repair in WSL), bounded by config.
4. **A `uip` shim for the coding agent.** Generate `<project>/.pdd2uipath/bin/uip` and prepend it to `PATH` for the authoring session. It forwards to the active executor, so the `uipath-rpa` skill's own `uip rpa validate/build` calls work unchanged from WSL. In `wsl-manual` mode the shim fails fast with a clear message, and the authoring prompt switches to "author only; validation happens in the Windows gate".
5. **Line endings, encoding and locks.**
   - Preserve each file's existing encoding and line endings. Never rewrite XAML or `project.json` just to normalize them.
   - Add a `.gitattributes` that keeps `*.xaml`, `*.json` and `*.cs` stable across WSL and Windows git.
   - Before a Windows gate, detect files locked by an open Studio Desktop and ask the user to close the project instead of failing obscurely.
6. **Corporate network.** Honour `HTTPS_PROXY`/`NO_PROXY` and custom CA bundles (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`) on both sides. `doctor` reports clearly when the GitHub npm registry, the UiPath NuGet feeds or the Anthropic API are unreachable.
7. **Which side runs what is a config table, not scattered `if` statements.** Default: every `uip rpa` verb runs on Windows; `uip skills` and `uip login` run on the side that needs them. `doctor` may probe whether a Linux-side `uip rpa` can validate a `Portable` project and offer that as an option, but never assume it.
</execution_model>

<architecture>
Python owns everything deterministic. The coding agent (via the skill) owns workflow authoring. The executor owns where `uip` runs. The TUI and CLI are thin views over one event stream.

```
PDD (pdf/docx)
  │  1 ingest + approval gate          (deterministic)
  ▼
ProcessSpec JSON  ◄── 2 extraction     (Claude API, structured output, every field cites PDD page/section)
  │  3 design rules                    (deterministic, explainable)
  ▼
design.json → 4 plan → docs/plans/<slug>.md   ── HUMAN APPROVAL GATE (TUI modal) ──
  │  5 scaffold                        (uip via Executor → Windows)
  ▼
  │  6 author                          (coding agent in WSL + uipath-rpa skill; uip shim → Executor)
  ▼
  │  7 Windows gate + verification     (validate/build via Executor; traceability + stub audit in Python)
  │     ↺ bounded repair rounds
  ▼
report.md + traceability.csv → open in Studio Desktop (Windows)
```

Suggested layout (adjust if you have a better reason):
```
src/pdd2uipath/
  core/
    events.py        # typed events (StageStarted, CommandRun, AgentMessage, GateWaiting, ...) + async bus
    state.py         # resumable run state: <run_dir>/.pdd2uipath/state.json (stage, input hashes, outputs)
    config.py        # pydantic-settings; pdd2uipath.toml + env
  ingest/  spec/  design/  plan/  verify/  report/
  exec/
    base.py          # Executor protocol, UipResult, path translation
    native.py  wsl_interop.py  wsl_bridge.py  wsl_manual.py
    bridge_worker/   # templates for worker.ps1 / worker.cmd + job JSON schema
    shim.py          # generates the per-project `uip` shim
  agents/
    base.py          # AuthoringAgent protocol
    claude_code.py   # Claude Agent SDK for Python (default)
    opencode.py      # optional: `opencode run` non-interactive (stretch goal)
  cli.py             # Typer: doctor, bridge install|status, ingest, design, plan, approve-plan,
                     #        scaffold, author, verify, run-all, tui
  tui/               # Textual app (see <tui>)
tests/
```
</architecture>

<pipeline_stages>

**Stage 0: `doctor` (preflight).** Check each item and report it with a fix hint:
- WSL or native, interop status, Windows filesystem access to the workspace root
- Python, `uv`
- WSL `uip` and Windows `uip`: presence and versions on each side
- `rpa` tool on Windows, including GitHub-registry scope and .NET runtime
- `uip` login state on each side
- `uipath-rpa` skill installed for the chosen agent (locate its SKILL.md; don't assume a path)
- Claude Code / Agent SDK
- Studio Desktop on Windows (warn only)
- Proxy and CA configuration; reachability of the npm, NuGet and Anthropic endpoints

Then:
- Recommend an executor and write it to config.
- Probe the live CLI surface on the Windows side and record it in `docs/cli-surface.md`. The command adapter must be driven by this probe, not by hard-coded guesses.

**Stage 1: `ingest` + approval gate.**
- Parse PDF (pdfplumber) and DOCX (python-docx), keeping page/section anchors and tables.
- Decide approval with the rule in `<my_inputs>`, implemented as a pluggable check.
- Refuse unapproved, draft or ambiguous-approval PDDs and say exactly what is missing.
- Pin the PDD's SHA-256 in run state and re-check it at every later stage.

**Stage 2: extract `ProcessSpec`.**
- Use the Anthropic Python SDK with a strict schema derived from Pydantic v2 models. Model:
  - process metadata
  - ordered steps (`step_id`, actor, application, action, inputs, outputs, business rules, decision points, `is_ui_interaction`)
  - application inventory (name, kind, URL or executable, **credential asset names only**)
  - input/output data
  - business vs system exceptions
  - transaction unit and volumes
  - schedule/SLA
  - robot environment
  - assumptions, out-of-scope
- Every field carries a `source_ref`. Anything unstated or marked TBD goes to `open_questions`; never invent it.
- Parse tables deterministically first; let the model fill only what parsing cannot.
- Retry once on schema failure, then stop.

**Stage 3: `design`** (deterministic rules, each with a written rationale).
- Template:
  - Queue/transaction-based → REFramework (Official template only, discovered via the CLI).
  - Single linear run → Blank.
  - Reusable component → Library.
  - Test spec → Test Automation.
  - Ambiguous template search result → stop and ask.
- Target framework:
  - Windows-only apps or capabilities → `Windows`.
  - Cross-platform runtime → `Portable`.
  - Both required → **stop and surface the contradiction** (PDD-02 must trigger this). Never infer Legacy.
- Expression language comes from config. XAML is the default. Use coded only for transforms, DTOs and helpers.
- UI capture: `stub` by default; `live` only if config says the apps are reachable from the Windows session.
- Map each `step_id` to a file and to its REFramework location. Blocking `open_questions` stop the stage; non-blocking ones flow into the plan and the report.

**Stage 4: `plan` + human approval.**
- Render `docs/plans/<slug>.md` in the skill's format:
  - `Execution autonomy: autonomous`
  - `UI capture:` line
  - concrete `Stop conditions`
  - **Task 0 listing every package install with exact ID and version**
  - one checkbox task per workflow, citing `step_id`s and PDD refs
- Approval happens in a TUI modal or with `pdd2uipath approve-plan`. It records the approver and the plan hash; any later edit invalidates it.

**Stage 5: `scaffold`** (via Executor, so it runs on Windows).
- Run template search → init with explicit name, Windows-side location, target framework, expression language and template package → restore → Task 0 package installs.
- Never write `project.json` or scaffold files yourself.
- Copy the plan into the project, add `.gitattributes`, `git init`, and commit the pristine scaffold.

**Stage 6: `author`** (coding agent in WSL or Windows; default backend is Claude Code via the Claude Agent SDK for Python).
- Session setup:
  - `cwd` = the project (`/mnt/...` path in WSL)
  - user/project settings loaded so the globally installed skill is discoverable
  - Skill, file and Bash tools allowed
  - the `uip` shim first on `PATH`
  - turn and budget limits from config
- The session prompt is short: use the `uipath-rpa` skill to execute `docs/plans/<slug>.md`, annotate every workflow with its `step_id`s, and follow the Placeholder-Selector Stub Pattern.
  - In `wsl-manual` mode, add: "author only, do not run `uip rpa validate/build`; a Windows gate validates afterwards."
- Stream every agent event into the event bus and into `<run_dir>/logs/author.jsonl`.

**Stage 7: Windows gate + independent verification.** Do not trust the agent's claim of success.
- Through the Executor: per-file validate on every workflow, then a project-level build. Parse the JSON results and exit codes.
- In Python, check that:
  - every `step_id` appears in a workflow
  - every UI step contains real UIA activities (not just `Log`/`WriteLine`)
  - `TODO Indicate` markers are counted
  - no credential-like literals appear
- On failure, run a bounded repair round: feed the exact errors back to the agent, then run the Windows gate again. The maximum number of rounds is configurable (default 2). After that, stop with a precise report.

**Stage 8: hand-off.**
- Write `report.md` with:
  - decisions and rationale
  - open questions
  - `TODO Indicate` list per file
  - Orchestrator assets, queues and credentials to create
  - how to run
- Write `traceability.csv` with columns `step_id, pdd_ref, file, activity_display_name, status`.
- Offer "Open in Studio" (through the Executor, on Windows). Commit.

`run-all` chains the stages and pauses at the plan gate. When the Windows side is needed in `wsl-manual` mode, it also pauses for the Windows round.
</pipeline_stages>

<tui>
An elegant, keyboard-first TUI in the spirit of **opencode**. Build it with **Textual** (latest).

`pdd2uipath` with no arguments in a TTY opens the TUI. `pdd2uipath <command> --plain` gives a scriptable, CI-friendly mode. **The TUI contains no business logic.** It subscribes to the same event bus as the plain CLI and calls the same core functions inside Textual workers, so the UI never blocks.

**Layout:**
- **Header:** product name, current PDD, project path, active executor with a live status pill (e.g. `bridge ● online` from the worker heartbeat, `interop`, `manual ⏸`), model name, elapsed time.
- **Left sidebar, "Pipeline":** the 9 stages with status glyphs (pending, running with spinner, done, failed, waiting for approval, waiting for Windows), plus a repair-round counter. Selecting a stage filters the session view.
- **Center, "Session":** a chronological stream of event cards, styled like opencode's transcript:
  - streamed agent messages rendered as Markdown
  - tool calls as compact one-liners (`uip rpa validate Main.xaml ✓ 2.1s`), expandable to full JSON output
  - stage boundaries as subtle rules
  - errors highlighted, never as walls of red text
- **Right, "Inspector"** (toggleable): contextual tabs:
  - **PDD:** sections, approval verdict and the evidence for it
  - **Spec:** a tree, with `source_ref` shown on hover or select
  - **Design:** decisions and rationale
  - **Plan:** rendered Markdown, with a diff since approval
  - **Validation:** a DataTable of file / severity / rule / message, sortable; Enter opens details
  - **Traceability**
- **Footer:** context-aware key hints, plus a prompt bar for sending a free-form instruction to the authoring agent during a repair round.

**Interactions:**
- Command palette (`ctrl+p`) for every command: run or resume a stage, approve plan, switch executor, install or restart the bridge, open in Studio, open report, change theme.
- Modals:
  - **Plan approval:** rendered plan, its hash, Task 0 package list. The approver types their name.
  - **Windows round** (manual mode):
    - shows the exact command to run in the Windows terminal, with copy to clipboard via OSC 52 (works in Windows Terminal) and a fallback to selectable text
    - shows a live "waiting for results…" state
    - continues automatically when results appear
  - **Blocking open questions:** listed with their PDD references, with the options *edit PDD and restart* / *abort*.
- Quitting is always safe: state is persisted, and reopening resumes at the exact gate.

**Look and feel:**
- A restrained, consistent palette with a dark default and a light theme. Respect `NO_COLOR`.
- No reliance on Nerd Fonts; ASCII glyph fallback via config.
- Must look right in Windows Terminal running WSL, at a minimum of 100×30, and degrade gracefully at 80×24.
- Smooth but minimal animation (spinners only).

**Testing:** Textual's `run_test()` pilot for interaction flows (approve plan, manual Windows round, resume after quit) and `pytest-textual-snapshot` for screens.
</tui>

<python_standards>
- Python 3.12+, `uv`, Typer, Textual, Pydantic v2 + pydantic-settings, Jinja2, `rich`/`structlog`. Everything installable **without admin rights** and through a proxy or internal mirror.
- `ruff` and `mypy --strict` clean. `pytest` with coverage on `design/`, `plan/`, `exec/`, `verify/`.
- No `shell=True`. Use argument lists, explicit timeouts, and log every command with its stdout/stderr.
- Stages are idempotent and resumable. Re-running with unchanged inputs is a no-op.
- Secrets come only from env vars or the OS keyring. Never write credential values into specs, plans, projects, logs, bridge jobs, or reports.
</python_standards>

<claude_code_setup_for_this_repo>
- Create a concise `CLAUDE.md` covering:
  - purpose
  - the brain/hands split
  - how to run tests, lint and the TUI
  - "the installed `uipath-rpa` skill and live `uip --help` are the source of truth for UiPath specifics"
- Add `.claude/settings.json` with a permission allow-rule scoped to `uip`. Check the current Claude Code permission-rule syntax first. The shim is also named `uip`, so the same rule covers it.
- The Stage 6 authoring session uses the same scoped permissions. No blanket bypass mode.
</claude_code_setup_for_this_repo>

<guardrails>
- Never proceed past Stage 1 on an unapproved PDD, or past Stage 4 without a recorded plan approval matching the current plan hash.
- Never hand-write selectors, `project.json` or scaffolding. Never change target framework or expression language after init.
- Never install packages not named in the approved plan's Task 0.
- UI steps use UiPath UI Automation only: no Selenium, Playwright, PowerShell or HTTP form posts.
- The bridge worker executes only allowlisted `uip` subcommands on paths inside the workspace root.
- No Orchestrator deployment, publishing or job runs. The pipeline stops at a local, building project.
- If the live CLI or skill contradicts this prompt, follow them, update `docs/cli-surface.md`, and tell me what changed.
</guardrails>

<testing>
- **Fake `uip`:** a Python script placed first on `PATH` that records invocations and returns fixture JSON envelopes, including failure cases (non-zero exit, validation errors, template search with zero / multiple Official / Marketplace-only results).
- **Executor tests:**
  - path translation (`/mnt/c/...` ↔ `C:\...`, spaces, refusal of `\\wsl$` paths)
  - bridge job protocol (atomic results, heartbeat timeout, allowlist rejection, malformed job)
  - manual-mode round trip, simulated by a test helper that plays the Windows side
- **Fixture PDDs:** the three samples in `./samples/`, each with its expected outcome asserted.
- **Golden tests** for design rules and plan rendering.
- **TUI pilot and snapshot tests** (see `<tui>`).
- **Opt-in end-to-end test** (`pytest -m e2e`) against the real Windows `uip` through the configured executor.
</testing>

<acceptance_criteria>
Done means all of the following, demonstrated in your final summary:
1. `pdd2uipath doctor` runs in WSL and in native Windows. It correctly identifies the executor to use, or lists exactly what I must install or change.
2. From WSL in `wsl-bridge` mode: I start the worker once in my Windows session; then `run-all` on PDD-01 completes with a clean Windows-side validate + build, without me switching windows again.
3. From WSL in `wsl-manual` mode: the same run completes, and the TUI tells me exactly when and what to run in Windows, at most once per gate round.
4. PDD-02 stops at Stage 3 with the framework contradiction and its open questions. PDD-03 is rejected at Stage 1 with a clear reason. An edited plan after approval is rejected at Stage 5.
5. Every PDD-01 step appears in `traceability.csv`, and no UI step is a `Log`-only stub.
6. The TUI shows the full run (stages, agent stream, validation table, approval and Windows-round modals) and resumes correctly after quit.
7. `ruff`, `mypy --strict`, and the non-e2e test suite pass.
8. The generated project opens in Studio Desktop without errors. Tell me the exact command or click path to check this myself.
</acceptance_criteria>

<how_to_work>
- Start in plan mode. First read the installed `uipath-rpa` SKILL.md in full, plus the references it marks as required for creating projects, CLI usage and execution maps. Then probe `uip --help` and `uip rpa --help` **on both sides** if you can reach them. Base the design on what you find.
- Design and test the `exec/` layer before anything else. Every later stage depends on it.
- Ask me only about things you cannot discover:
  - whether WSL interop is allowed in our VDI
  - whether `.ps1` or only `.cmd` may run
  - tenant details
  - a framework contradiction
  Record everything else as a default in `docs/decisions.md`.
- Build in vertical slices, with tests passing at each step:

  exec + doctor → ingest/approval → spec → design → plan → scaffold → author → gate/verify → report → TUI polish

  Wire a minimal TUI early (pipeline sidebar + session stream) so every later slice shows up in it.
- Run independent tool calls in parallel.
- Keep it as simple as the job allows: no web UI, database, or plugin framework.
- When you finish, report:
  - what was built
  - real command-output excerpts from the verification
  - assumptions
  - the one-time setup steps for my VDI (Windows side and WSL side)
  - what a developer still has to do in Studio
</how_to_work>

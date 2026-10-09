# pdd2uipath

Approved Process Definition Document (PDD) → UiPath Studio project, automatically and consistently,
so a developer starts from a project that already scaffolds, mirrors the PDD 1:1, and only needs
selectors indicated and business logic refined in Studio.

Built around the official **UiPath CLI** (`@uipath/cli` + the `rpa` tool, headless Studio host) and
the **`uipath-rpa` skill**; Python owns everything deterministic, a coding agent (OpenCode with a
local LLM, or GitHub Copilot CLI) authors the workflows, and an *executor* decides where `uip` runs
(native Windows, WSL interop, a file-based Windows bridge worker, or manual Windows rounds).
No Anthropic dependency; packages come from PyPI (or an Artifactory mirror).

```
PDD (pdf/docx)
  │  1 ingest + approval gate          (deterministic)
  ▼
ProcessSpec JSON  ◄── 2 extraction     (model, strict schema, every field cites PDD page/section)
  │  3 design rules                    (deterministic, explainable; template + framework probes on Windows)
  ▼
design.json → 4 plan → docs/plans/<slug>.md   ── HUMAN APPROVAL GATE (TUI modal / approve-plan) ──
  │  5 scaffold                        (uip rpa init/restore/packages install via Executor → Windows)
  ▼
  │  6 author                          (coding agent + uipath-rpa skill; uip shim → Executor)
  ▼
  │  7 Windows gate + verification     (validate per file + build; traceability + stub audit in Python)
  │     ↺ bounded repair rounds
  ▼
report.md + traceability.csv → open in Studio Desktop (Windows)
```

## Quick start

```bash
uv sync
cp pdd2uipath.example.toml pdd2uipath.toml     # set workspace_root (a C:\ path)
uv run pdd2uipath doctor --write-config        # preflight, executor recommendation, docs/cli-surface.md
uv run pdd2uipath                              # TUI (ctrl+p = commands, o = open PDD, r = run all)
uv run pdd2uipath --plain run-all samples/PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx
uv run pdd2uipath --plain approve-plan --approver "Your Name" --pdd samples/PDD-01_...docx
uv run pdd2uipath --plain run-all samples/PDD-01_...docx   # resumes after the gate
uv run pdd2uipath --plain open-studio --pdd samples/PDD-01_...docx
```

Exit codes in plain mode: `0` ok, `1` error, `3` waiting for plan approval, `4` a Windows round is
pending (`wsl-manual`), `5` stopped on purpose (rejected PDD, design contradiction, blocking questions).

## WSL + Windows

The brain can run in WSL; every `uip rpa` call runs in the Windows session through the configured
executor. `wsl-interop` needs binfmt interop; `wsl-bridge` needs the worker started once per session
(`pdd2uipath bridge install`, then `<workspace>\.pdd2uipath-bridge\worker.cmd` in Windows);
`wsl-manual` prints one `run.cmd` per gate round and continues when its results appear; with
`--no-wait` the command exits with code 4 instead, and `pdd2uipath --plain resume-round --pdd ...
[--no-wait]` continues after you ran the script (each round script runs exactly once).
See `docs/vdi-setup.md` and `docs/decisions.md`.

## Documentation

- [docs/setup-guide.md](docs/setup-guide.md): step-by-step installation (Windows, agent CLIs, Python, doctor, first run, WSL, VDI transfer).
- [docs/handbook.md](docs/handbook.md): the full manual: TUI keys and slash commands, CLI reference, every stage, artefacts, configuration, executors, agents, troubleshooting.
- [docs/vdi-setup.md](docs/vdi-setup.md): corporate specifics (Artifactory, proxy, CA, offline wheels).
- [docs/decisions.md](docs/decisions.md): design decisions and lessons from the real runs.

## Development

```bash
uv run python -m pytest            # unit + integration tests (fake uip, fixture extraction, fake agent)
uv run python -m pytest -m e2e     # opt-in end-to-end with the real Windows uip
uv run python -m ruff check src tests; uv run python -m ruff format --check src tests
uv run python -m mypy
```

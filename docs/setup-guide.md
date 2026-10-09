# pdd2uipath setup guide

Step-by-step installation on a private Windows machine, with the optional WSL and corporate
VDI variants. The companion [handbook](handbook.md) explains every function once the tool is
installed; [vdi-setup.md](vdi-setup.md) has the Artifactory, proxy and CA details for the
corporate environment.

## What you need

| Component | Where | Why |
|---|---|---|
| Windows 10/11 with Node.js LTS | Windows | the UiPath CLI (`uip`) is an npm package |
| `@uipath/cli` + the `rpa` tool | Windows | every project operation: init, restore, packages, validate, build, Studio start |
| .NET 8 runtime | Windows | the headless Studio host (`UiPath.Studio.Helm`) that `uip rpa` downloads on first use |
| UiPath Studio Desktop (2025.10.x or 26.x), signed in | Windows | licence for the headless host, and the developer's hand-off |
| Git | Windows (and WSL if used) | generated projects are git repositories with one commit per stage |
| Python 3.12+ and `uv` | Windows or WSL ("brain") | pdd2uipath itself |
| A coding agent CLI: GitHub Copilot CLI or OpenCode | brain side | authors the workflows through the `uipath-rpa` skill |
| A model for extraction | brain side | Copilot CLI, OpenCode, or a local OpenAI-compatible server |

No Anthropic account or package is involved anywhere.

## Step 1: the Windows side (the "hands")

1. Install Node.js (user install is enough), then the UiPath CLI and its RPA tool:

   ```powershell
   npm i -g @uipath/cli
   uip tools install @uipath/rpa-tool
   uip --version
   uip rpa --help
   ```

   At work the registry is an Artifactory mirror; see [vdi-setup.md](vdi-setup.md) for the
   `npm config set registry ...` and `@uipath:registry` lines.
2. Install the .NET 8 runtime if `dotnet --list-runtimes` shows no 8.x entry.
3. Install UiPath Studio Desktop and sign in. Keep Studio open while pdd2uipath runs: the
   headless host borrows its licence. Without it `uip rpa` answers
   `HelmFeatureNotAvailableException: No Studio licence`.
4. The first `uip rpa` call downloads the headless host (30 to 90 seconds). Run one command once
   to warm it up:

   ```powershell
   uip rpa templates search --query REFramework --output json
   ```

5. Install the `uipath-rpa` skill for the agent you will use (either or both):

   ```powershell
   uip skills install --agent copilot
   uip skills install --agent opencode
   ```

   pdd2uipath also copies the skill into each generated project, so a global install is a
   convenience, not a requirement.

## Step 2: the coding agent and the extraction model

Choose at least one authoring agent. The authoring stage runs it non-interactively on the
generated project with the skill and the approved plan.

**GitHub Copilot CLI**

```powershell
npm i -g @github/copilot
copilot login
copilot --version
```

- Copilot Free: the CLI works only with the automatic model. Leave `agent.model` empty.
- Enterprise, Pro or Business seats: explicit models are allowed if your organisation's model
  policy enables them. Find the ids with `/model` inside an interactive `copilot` session, then
  set them in pdd2uipath with `/agent copilot gpt-6.1-sol` (TUI) or `[agent] model` in the config.
- Data-residency tenants: `copilot login --host https://<tenant>.ghe.com`.

**OpenCode**

```powershell
npm i -g opencode-ai
opencode auth login        # pick GitHub Copilot (github.com or enterprise URL) or a local provider
opencode models            # must list the provider/model you intend to use
```

- With GitHub Copilot, OpenCode lists only the models your seat has in the model picker. Copilot
  Free shows none and rejects explicit models, so use the Copilot CLI there.
- With a local LLM, configure the provider in `~/.config/opencode/opencode.json` and use ids like
  `ollama/qwen2.5-coder:32b`.

**Extraction model**

- Simplest: reuse the agent CLI, `[extraction] backend = "copilot"` or `"opencode"`.
- Local LLM: `[extraction] backend = "openai-compatible"`, `base_url = "http://localhost:11434/v1"`,
  `model = "qwen2.5-coder:32b"`; the API key, if any, comes from the env var named in
  `api_key_env` (default `OPENAI_API_KEY`).
- Tests and demos: `backend = "fixture"` with `fixture_dir = "tests/fixtures/extraction"`.

## Step 3: the Python side (the "brain")

1. Install Python 3.12 or newer and `uv` (`pip install uv`, or the standalone binary).
2. Get the repository and install the dependencies:

   ```bash
   cd C:\Users\<you>\pdd2uipath
   uv sync
   uv run pdd2uipath version
   ```

   Always start tools with `uv run python -m ...` or `uv run pdd2uipath`; application-control
   policies often block the `.venv\Scripts\*.exe` launchers.
3. Create the config file:

   ```bash
   cp pdd2uipath.example.toml pdd2uipath.toml
   ```

   Set `workspace_root` to a Windows path where generated projects may live, for example
   `C:/Users/<you>/RPA/pdd2uipath-projects`, and the `[agent]` and `[extraction]` backends from
   Step 2. Everything else can stay at its default; the doctor fills in the rest.

## Step 4: run the doctor

```bash
uv run pdd2uipath doctor --write-config
```

The doctor checks the platform, the workspace, Python, uv, git, the `uip` launchers on both
sides, the `rpa` tool, logins, .NET, the skill, the agent CLIs, the configured models, Studio,
the headless host and its licence, proxies, registry reachability, and the bridge. It then
recommends an executor and, with `--write-config`, stores the recommendation and the discovered
`uip` paths in the config file. It also writes `docs/cli-surface.md`, the live command surface
that the pipeline validates its verbs against.

Every line is `ok`, `warn`, `fail` or `skip` with a fix hint. Fix the `fail` lines before the
first run. Typical first-time failures:

| Doctor line | Fix |
|---|---|
| uip on the Windows side unreachable | `npm i -g @uipath/cli`, then rerun with `--write-config` |
| rpa tool | `uip tools install @uipath/rpa-tool` |
| Headless Studio host responds (licensed) | open Studio Desktop and sign in, or `uip login` with a licensed account |
| uipath-rpa skill not found | `uip skills install --agent copilot` (or opencode) |
| agent: OpenCode model ... not listed | pick an id from `opencode models <provider>` |
| Extraction backend | install an agent CLI or set `[extraction] base_url` |

## Step 5: the first run

Put an approved PDD (`.docx` or `.pdf`) somewhere; the repository ships three samples under
`samples/`. Then either:

**TUI**

```bash
uv run pdd2uipath
```

Press `o` and pick `samples/PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx`, press `r`.
The pipeline stops at the plan approval modal: read the plan, type your name, Approve. It then
scaffolds, authors, verifies and writes the report. `/studio` opens the result in Studio
Desktop. `/help` lists every command.

**CLI**

```bash
uv run pdd2uipath --plain run-all samples/PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx
uv run pdd2uipath --plain approve-plan --approver "Your Name" --pdd samples/PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx
uv run pdd2uipath --plain run-all samples/PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx
uv run pdd2uipath --plain open-studio --pdd samples/PDD-01_Vendor_Invoice_Processing_v1.2_APPROVED.docx
```

The first `run-all` exits with code 3 at the approval gate; the second resumes and finishes.
`--approver "Your Name"` on `run-all` approves non-interactively in one go.

Expect 5 to 15 minutes for the deterministic stages and the gate, plus the agent's authoring
time: the Copilot CLI on the automatic model took about 40 minutes for the 12-step sample.

## Step 6 (optional): brain in WSL

The pipeline can run in WSL while `uip` stays on Windows. Install Python and uv in WSL, run
`uv sync` with a separate environment (`UV_PROJECT_ENVIRONMENT=.venv-wsl`) and let the doctor pick
the executor:

| Executor | When | One-time action |
|---|---|---|
| `wsl-interop` | `cmd.exe` / `node.exe` can be started from WSL (binfmt interop) | none |
| `wsl-bridge` | interop is blocked, a Windows terminal is available | `pdd2uipath bridge install`, then start `<workspace>\.pdd2uipath-bridge\worker.cmd` in Windows once per session |
| `wsl-manual` | nothing can be started across the boundary | run each generated `rounds\<id>\run.cmd` by hand when the TUI or CLI asks; `resume-round` continues |

Generated projects always live under the Windows workspace root; `\\wsl$` paths are refused.
Details and the per-round workflow are in the handbook.

## Step 6b (optional): MCP servers for Confluence and GitHub

If your company runs MCP servers (the same ones OpenCode uses), pdd2uipath can fetch PDDs from
Confluence and drive GitHub through them. Either reuse OpenCode's list or declare the servers:

```toml
[mcp]
import_opencode = true          # take the servers from ~/.config/opencode/opencode.json

[mcp.servers.confluence]        # or declare them here (stdio or http)
transport = "stdio"
command = ["npx", "-y", "@acme/confluence-mcp"]
env = { CONFLUENCE_TOKEN = "${CONFLUENCE_TOKEN}" }

[mcp.recipes.fetch-pdd]         # map the PDD fetch onto your server's tool
server = "confluence"
tool = "get_attachment"
args = { page_id = "{ref}", filename = "PDD.docx" }
```

Check it: `uv run pdd2uipath mcp servers`, `uv run pdd2uipath mcp tools confluence` shows the
real tool names and arguments to put into the recipe, and `uv run pdd2uipath doctor` connects to
every server. Then `uv run pdd2uipath mcp fetch-pdd <page id>` downloads the PDD into
`pdd-inbox/` and `run-all` takes it from there; in the TUI `/open mcp:<page id>` does both.
Markdown pages work as PDDs too, as long as they keep the Document Control and sign-off tables.

## Step 7: moving to the corporate VDI

Everything is plain PyPI and npm packages, so the transfer is a matter of registries, proxies
and certificates:

1. Point `uv` at the Artifactory PyPI remote (`UV_INDEX_URL`) and npm at the npm remote; set
   `UV_NATIVE_TLS=1`, `SSL_CERT_FILE`, `HTTPS_PROXY` and `NO_PROXY` as described in
   [vdi-setup.md](vdi-setup.md).
2. If the VDI is offline for PyPI, build a wheel set on the private machine
   (`uv export`, `uv pip download`) and install with `--no-index --find-links`.
3. Copy the repository without `.venv`, `pdd2uipath.toml` and `cli-surface.json`; run
   `uv sync` and `doctor --write-config` again on the VDI.
4. Log the agent CLI into the enterprise GitHub account; the enterprise model policy decides
   which model ids work (`gpt-6.1-sol` and similar). Set them with `/agent` or `/extraction`.
5. Keep Studio Desktop open and signed in during runs for the licence.

## Verify the installation

```bash
uv run python -m pytest            # unit tests with the fake uip and fake agent
uv run python -m pytest -m e2e     # real uip through the configured executor (needs the licence)
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `HelmFeatureNotAvailableException: No Studio licence` | Studio Desktop closed or signed out; open and sign in |
| `copilot CLI not found` or `WinError 2` | the CLI is not on PATH; `npm i -g @github/copilot`; pdd2uipath starts npm CLIs through `node.exe` itself |
| `Model "x" from --model flag is not available` | the id is not in your plan's picker; use `/model none` (auto) or a listed id |
| `Provider not found: github-copilot` in OpenCode | no picker-enabled models on this seat; see the handbook's model section |
| `a project already exists at ...` | the run directory already has a project; use a new `workspace_root` or delete the run folder |
| `.venv\Scripts\pytest.exe` blocked | application control; use `uv run python -m pytest` |
| TUI shows no agent output for minutes | normal; the agent session runs 5 to 40 minutes, `/logs author` shows its live log |

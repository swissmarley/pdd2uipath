# One-time setup (corporate VDI and private machine)

Everything installs without admin rights. Replace `<artifactory>` with your JFrog host.

## Windows side (the "hands")

1. Node.js (user install) and the UiPath CLI through your npm remote:
   ```powershell
   npm config set registry https://<artifactory>/artifactory/api/npm/npm-remote/
   npm config set cafile "C:\path\to\corporate-ca.pem"     # proxy with TLS inspection
   npm i -g @uipath/cli
   npm config set @uipath:registry https://<artifactory>/artifactory/api/npm/github-npm-remote/  # proxy of npm.pkg.github.com
   uip tools install @uipath/rpa-tool
   uip --version; uip rpa --help
   ```
   If `@uipath/rpa-tool` cannot be mirrored, install it once from a machine that can reach
   `https://npm.pkg.github.com` (needs a GitHub token with `read:packages`).
2. .NET 8 runtime (user-scope installer) and, for the human hand-off, Studio Desktop
   (2025.10.x or 26.x). The headless host (`UiPath.Studio.Helm`) is downloaded by `uip rpa` on
   first use (30–90 s) from the UiPath NuGet feed — mirror
   `https://pkgs.dev.azure.com/uipath/Public.Feeds/_packaging/UiPath-Official/nuget/v3/index.json`
   in Artifactory and register the mirror in Studio's package sources if the feed is blocked.
3. **Licence:** the headless host needs a Studio licence. If `pdd2uipath doctor` reports
   `No Studio licence`, open Studio Desktop and sign in (keep it open) or `uip login` with a
   licensed account on the Windows side.
4. `uip login` on Windows only if you need Marketplace templates or tenant feeds.
5. Bridge worker (only for the `wsl-bridge` executor): after `pdd2uipath bridge install` from
   WSL, start once per session in a Windows terminal:
   `<workspace>\.pdd2uipath-bridge\worker.cmd`
   (or `powershell -NoProfile -ExecutionPolicy Bypass -File ...\worker.ps1` when cmd scripts are
   blocked). It runs only allowlisted `uip` verbs inside the workspace root.

## WSL side (the "brain")

1. Python 3.12+ and uv from Artifactory:
   ```bash
   export UV_INDEX_URL=https://<artifactory>/artifactory/api/pypi/pypi-remote/simple
   export UV_NATIVE_TLS=1                 # use the OS trust store (corporate CA)
   export SSL_CERT_FILE=/etc/ssl/certs/corporate-ca.pem
   export HTTPS_PROXY=http://proxy:8080 NO_PROXY=localhost,127.0.0.1,.corp
   pip install --index-url "$UV_INDEX_URL" uv       # or the standalone uv binary from Artifactory
   ```
2. The project (lockfile included; `uv.lock` resolves against your index):
   ```bash
   cd /mnt/c/.../pdd2uipath            # repo may live anywhere; generated projects never under \\wsl$
   UV_PROJECT_ENVIRONMENT=.venv-wsl uv sync      # separate venv from the Windows one
   uv run pdd2uipath doctor --write-config
   ```
   Offline transfer: on the private machine `uv export --format requirements-txt > requirements.txt`
   and `uv pip download -r requirements.txt -d wheels/`; at work `uv pip install --no-index
   --find-links wheels/ -r requirements.txt`.
3. Coding agent (one of):
   - **OpenCode + local LLM:** `npm i -g opencode-ai`; configure the local provider
     (OpenAI-compatible endpoint) in `~/.config/opencode/opencode.json`; `opencode models` to
     verify; `uip skills install --agent opencode` (global `~/.config/opencode/skills/`).
   - **GitHub Copilot CLI:** `npm i -g @github/copilot`; run `copilot` once and `/login`;
     `uip skills install --agent copilot`. Copilot can also use a BYOK local model
     (`COPILOT_PROVIDER_BASE_URL`, `COPILOT_PROVIDER_TYPE=openai`).
   - **Model availability (GitHub Copilot):** both CLIs can only use the models that the Copilot
     API flags as *model-picker enabled* for your seat (org/enterprise model policy). On
     Copilot Free nothing is picker-enabled: the Copilot CLI accepts only `--model auto`
     (`Model "x" from --model flag is not available` otherwise, no request spent) and OpenCode
     drops the `github-copilot` provider entirely (`opencode models` lists no `github-copilot/*`,
     explicit models are rejected with HTTP 400). With an Enterprise/Pro seat the enabled models
     (e.g. `gpt-6.1-sol`) appear in `opencode models github-copilot` and in the CLI's `/model`
     picker; set `[agent] model = "github-copilot/<id>"` (OpenCode) or `"<id>"` (Copilot CLI).
     Data-residency tenants: `copilot login --host https://<tenant>.ghe.com`; in OpenCode's
     `/connect` choose GitHub Copilot -> enterprise and enter that URL. `pdd2uipath doctor`
     verifies a configured OpenCode model against `opencode models <provider>`.
   pdd2uipath copies the `uipath-rpa` skill into the generated project for the chosen agent when
   the global install is missing.
4. Extraction model: set `[extraction] base_url` to the OpenAI-compatible endpoint of the local
   LLM (e.g. `http://localhost:11434/v1`) and `model`; or leave `backend = "auto"` to use the
   agent CLI.
5. `uip` inside WSL is optional (only `uip skills ...`); the Windows `uip` does the RPA work.

## Config

Copy `pdd2uipath.example.toml` to `pdd2uipath.toml` (repo root) or `~/.config/pdd2uipath/config.toml`
and set at least `workspace_root`. `pdd2uipath doctor --write-config` fills in the executor and
the discovered `uip` paths.

## Proxy / CA summary

| Where | Variables |
|---|---|
| WSL brain | `HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`, `UV_INDEX_URL`, `UV_NATIVE_TLS` |
| Windows hands | `HTTPS_PROXY`, `NO_PROXY`, `NODE_EXTRA_CA_CERTS` (node/uip), npm `cafile`, Studio NuGet mirror |
| interop executor | forwards `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY`/`NODE_EXTRA_CA_CERTS` via `WSLENV` |

`pdd2uipath doctor` reports reachability of the npm registry, the GitHub npm registry, the
UiPath NuGet feed, the GitHub API (Copilot) and the configured LLM endpoint.

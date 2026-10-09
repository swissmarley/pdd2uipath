# uip CLI surface (probed)

- uip version: `1.203.0`
- probed at: 2026-10-09T20:01:45+00:00 on `native`
- top-level commands: `login`, `logout`, `config`, `tools`, `skills`, `mcp`, `feedback`
- `uip rpa` commands: `activities`, `analyze`, `analyzer-rules`, `build`, `cleanup`, `data-fabric-entities`, `debug`, `execution`, `files`, `get-library-object-repositor`, `get-object-repository`, `help`, `init`, `instances`, `pack`, `packages`, `publish`, `remote`, `restore`, `run`, `studio`, `templates`, `test-data`, `tm`, `uia`, `validate`, `workflow-examples`

The command adapter (`pdd2uipath.executors.surface.UipCommands`) resolves canonical
operations against this table; a missing flag or verb raises `SurfaceMismatch`
instead of guessing.

| canonical op | resolved verb |
|---|---|
| `templates_search` | `uip rpa templates search` |
| `init` | `uip rpa init` |
| `restore` | `uip rpa restore` |
| `packages_versions` | `uip rpa packages versions` |
| `packages_install` | `uip rpa packages install` |
| `activities_find` | `uip rpa activities find` |
| `validate` | `uip rpa validate` |
| `build` | `uip rpa build` |
| `analyze` | `uip rpa analyze` |
| `studio_start` | `uip rpa studio start` |
| `instances_list` | `uip rpa instances list` |
| `login_status` | `uip login status` |

## `uip rpa templates search`

Usage: `uip rpa templates search [--query <string>] [--limit <integer>] [--include-prerelease]`

Flags: `--help`, `--include-prerelease`, `--limit`, `--log-file`, `--log-level`, `--output`, `--output-filter`, `--profile`, `--project-dir`, `--query`, `--robot-dir`, `--studio-dir`, `--timeout`, `--verbose`, `-h`

## `uip rpa init`

Usage: `uip rpa init --name <string> [--template-id <string>] [--location <string>] [--description <string>] [--expression-language { VisualBasic | CSharp }] [--target-framework { Legacy | Windows | Portable }] [--allow-prerelease-packages] [--template-package-id <string>] [--template-package-version <string>] [--skip-solution-registration]`

Flags: `--allow-prerelease-packages`, `--description`, `--expression-language`, `--help`, `--location`, `--log-file`, `--log-level`, `--name`, `--output`, `--output-filter`, `--profile`, `--project-dir`, `--robot-dir`, `--skip-solution-registration`, `--studio-dir`, `--target-framework`, `--template-id`, `--template-package-id`, `--template-package-version`, `--timeout`, `--verbose`, `-h`

## `uip rpa restore`

Usage: `uip rpa restore [options] [project-dir] [output-path]`

Positional: `project-dir`, `output-path`

Flags: `--exclude-configured-sources`, `--feed-folder`, `--format`, `--help`, `--log-file`, `--log-level`, `--nuget-sources-config-path`, `--output`, `--output-filter`, `--profile`, `--project-dir`, `--robot-dir`, `--studio-dir`, `--timeout`, `--verbose`, `-h`

## `uip rpa packages versions`

Usage: `uip rpa packages versions --package-id <string> [--include-prerelease]`

Flags: `--help`, `--include-prerelease`, `--log-file`, `--log-level`, `--output`, `--output-filter`, `--package-id`, `--profile`, `--project-dir`, `--robot-dir`, `--studio-dir`, `--timeout`, `--verbose`, `-h`

## `uip rpa packages install`

Usage: `uip rpa packages install --packages <key=value> [--packages-file <path>] [--nuget-sources-config-path <string>]`

Flags: `--help`, `--log-file`, `--log-level`, `--nuget-sources-config-path`, `--output`, `--output-filter`, `--packages`, `--packages-file`, `--profile`, `--project-dir`, `--robot-dir`, `--studio-dir`, `--timeout`, `--verbose`, `-h`

## `uip rpa activities find`

Usage: `uip rpa activities find --query <string> [--tags <string>] [--limit <integer>]`

Flags: `--help`, `--limit`, `--log-file`, `--log-level`, `--output`, `--output-filter`, `--profile`, `--project-dir`, `--query`, `--robot-dir`, `--studio-dir`, `--tags`, `--timeout`, `--verbose`, `-h`

## `uip rpa validate`

Usage: `uip rpa validate [--file-path <string>] [--skip-validation] [--min-severity { error | warning | info | verbose }]`

Flags: `--file-path`, `--help`, `--log-file`, `--log-level`, `--min-severity`, `--output`, `--output-filter`, `--profile`, `--project-dir`, `--robot-dir`, `--skip-validation`, `--studio-dir`, `--timeout`, `--verbose`, `-h`

## `uip rpa build`

Usage: `uip rpa build [options] [project-dir]`

Positional: `project-dir`

Flags: `--detailed-log-path`, `--exclude-configured-sources`, `--feed-folder`, `--format`, `--governance-file-path`, `--governance-file-type`, `--help`, `--log-file`, `--log-level`, `--nuget-sources-config-path`, `--output`, `--output-filter`, `--profile`, `--project-dir`, `--robot-dir`, `--skip-analyze`, `--studio-dir`, `--timeout`, `--verbose`, `-h`

## `uip rpa analyze`

Usage: `uip rpa analyze [options] [project-dir]`

Positional: `project-dir`

Flags: `--default-severity`, `--detailed-log-path`, `--exclude-configured-sources`, `--feed-folder`, `--format`, `--governance-file-path`, `--governance-file-type`, `--help`, `--log-file`, `--log-level`, `--nuget-sources-config-path`, `--output`, `--output-filter`, `--policy-file`, `--policy-file-type`, `--profile`, `--project-dir`, `--repository-path`, `--robot-dir`, `--skip-analyze`, `--studio-dir`, `--timeout`, `--verbose`, `-h`

## `uip rpa studio start`

Usage: `uip rpa studio start [options]`

Flags: `--help`, `--log-file`, `--log-level`, `--output`, `--output-filter`, `--profile`, `--project-dir`, `--robot-dir`, `--studio-dir`, `--timeout`, `--verbose`, `-h`

## `uip rpa instances list`

Usage: `uip rpa instances list [options]`

Flags: `--help`, `--log-file`, `--log-level`, `--output`, `--output-filter`, `--profile`, `--project-dir`, `--robot-dir`, `--studio-dir`, `--timeout`, `--verbose`, `-h`

## `uip login status`

Usage: `uip login status [options]`

Flags: `--help`, `--log-file`, `--log-level`, `--output`, `--output-filter`, `--profile`, `-h`

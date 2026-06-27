# Commands

Reference for every `baird` subcommand. Run `baird <cmd> --help` for current flags; this doc gives the orienting picture.

Bare `baird` (no args) prints a context-aware hint based on cwd, then help.

## Project

| Command | What it does |
|---|---|
| `baird project init <id> [--name --github --force]` | Write a starter `.baird/project.yaml` in cwd |
| `baird project push` | Upsert the local `project.yaml` into the hub |
| `baird project pull <id> [--out <dir>]` | Materialise a project's record from the hub |
| `baird project list` | All projects known to the hub |

## Coding

| Command | What it does |
|---|---|
| `baird code [--show-context] [-f <file>] [--budget N]` | Multi-turn REPL with diff approval. `--show-context` prints the per-turn context and exits. |
| `baird chat` | Free-form chat (no project context). _Not yet implemented — use `baird code` outside a project for now._ |
| `baird diff apply <patch> -m <msg> [--repo --action-id]` | Apply a unified diff file as a BAIRD-trailered git commit |
| `baird undo [--repo]` | Revert the last BAIRD commit via `git revert` |

REPL slash-commands inside `baird code`: `/exit`, `/quit`, `/context`, `/reset`, `/cost`, `/model [id]`, `/sessions`, `/no-diff`, `/help`. `/model` with no argument prints the current model and a numbered list of popular OpenRouter models; `/model <id-or-index>` switches mid-session. `/sessions` lists the project's prior sessions so you can resume one with `baird code --session <id>`. A bare `"""` line opens a heredoc-style multi-line input block; another `"""` closes it.

## Tasks

| Command | What it does |
|---|---|
| `baird task add <id> [--force]` | Write a starter `~/.baird/tasks/<id>.yaml` |
| `baird task list` | All tasks under `~/.baird/tasks/` |
| `baird task run <id>` | Fire one task now, ignoring its schedule |
| `baird task history <id> [--limit N]` | Recent firings of one task |

## Orchestrator

| Command | What it does |
|---|---|
| `baird orchestrator serve [--tick S --max-workers N]` | Run the scheduler. Long-lived; supervise with systemd. |

## Hub / daemon

| Command | What it does |
|---|---|
| `baird up` | Spawn the hub in the background if it isn't already running |
| `baird stop` | Stop the supervised background hub |
| `baird hub serve [--host --port]` | Run the FastAPI hub in the foreground (defaults from `config.yaml` `listen:`) |
| `baird daemon` | Run the satellite-side daemon (watchdog + executor) |

`baird code`, `baird project push`, `baird status` and friends call `baird up` automatically when the hub URL is local — you almost never need to start it by hand.

## Satellites

| Command | What it does |
|---|---|
| `baird satellite enroll <ssh-host> [--host-id --git-ref --port --watch-root --no-use-hub-for-models]` | One-shot: pick a hub-side port, write the systemd-user tunnel, SSH out, install BAIRD via uv, write `host.yaml` with the hub's auth token already filled in, verify round-trip |
| `baird satellite list` | Enrolled satellites + live tunnel status |
| `baird satellite remove <host-id>` | Tear down the hub-side tunnel for a satellite (leaves the remote install in place) |

## Observability

| Command | What it does |
|---|---|
| `baird status [--watch --interval S]` | One-shot dashboard or live refresh |
| `baird ps [--limit N]` | Currently-running actions |
| `baird logs <action_id>` | One action's full record (cost, tokens, command, summary) |
| `baird registry actions [--project --task --since-hours --unfinished --limit]` | List actions with filters |
| `baird inbox [--unresolved --limit N]` | Notification inbox |
| `baird inbox resolve <id> [<resolution>]` | Mark a notification resolved |

## Sessions (tmux/screen)

| Command | What it does |
|---|---|
| `baird session list` | List multiplexer sessions on this host |
| `baird session attach <name>` | Print the attach command (use as `$(baird session attach foo)`) |
| `baird session kill <name>` | Kill a session |

## Pipelines

| Command | What it does |
|---|---|
| `baird snakemake <Snakefile> [--cwd --project --live] [extra args...]` | Run Snakemake, post-parse the report into a summary on the hub. `--live` streams stdout and posts a `logged` inbox row every 10% of progress. |
| `baird nextflow <main.nf> [--cwd --project] [extra args...]` | Same for Nextflow (parses `trace.txt`) |

## Research and self-improvement

| Command | What it does |
|---|---|
| `baird research "<query>" [--project --model]` | Plan → web search → synthesize → inbox row |
| `baird improve [--since-hours N --model M]` | Review recent activity, propose harness improvements (prompt edits / new rules / task tuning) as inbox `proposal` rows |

## Events + lineage

| Command | What it does |
|---|---|
| `baird emit <name> [--payload JSON]` | Publish a reactive event. Stored in the hub's events table; the scheduler polls each tick and republishes onto its in-process bus so reactive triggers fire. |
| `baird files lineage <file_id>` | Show the actions that produced or modified a file, in order. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | command-level failure (file not found, model error, refused diff, etc.) |
| 2 | invalid arguments (Typer / config validation) |

Pipeline wrappers (`baird snakemake` / `baird nextflow`) propagate the underlying tool's exit code.

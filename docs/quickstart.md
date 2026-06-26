# Quickstart

End-to-end walkthrough of standing up BAIRD on one machine, then a satellite.

## 1. Install

Python 3.11+. The repo includes a pinned `pyproject.toml`.

```bash
git clone git@github.com:ethanlewisbaird/BAIRD.git
cd BAIRD
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
baird --version
```

Optional but recommended:

- `tmux` (or `screen`) for persistent multiplexed sessions.
- `mamba` (faster than `conda`) if you use conda environments.

## 2. Configure the host

Each machine running a BAIRD daemon needs `~/.baird/host.yaml`. On the hub itself, this also tells the CLI where the hub is.

```bash
mkdir -p ~/.baird
cat > ~/.baird/host.yaml <<EOF
host_id: $(hostname)
hub_url: http://127.0.0.1:8000
session_multiplexer: auto    # auto | tmux | screen | none
auth_token: null             # set for satellites; null means "deny remote calls"

volumes:
  - id: $(hostname):/home
    mount: $HOME
    shared: false

watch:
  roots:
    - $HOME/baird-sandbox
  deny:
    - "**/.git/**"
    - "**/__pycache__/**"
    - "**/.snakemake/**"
EOF
mkdir -p ~/baird-sandbox
```

Hub-side defaults live under `~/.baird/config.yaml` (auto-created on first hub run with sensible defaults: `127.0.0.1:8000`, `~/.baird/registry.sqlite`, `~/.baird/memory.sqlite`, `daily_total_usd: 5.0`).

See [configuration.md](configuration.md) for every field.

## 3. (You don't need to do anything here)

The hub auto-starts in the background the first time you run a command that
needs it (like `baird code` or `baird project push`). Logs go to
`<baird_home>/hub.log`; PID to `<baird_home>/hub.pid`. Stop it with `baird
stop`. Start it explicitly without entering the REPL with `baird up`.

The filesystem watchdog (`baird daemon`) is optional and not needed for the
REPL. Start it in a terminal when you want it; skip otherwise.

On a satellite (different machine), set `hub_url: http://hub.tailnet:8000` and `auth_token: <shared-secret>` in `host.yaml`, then run `baird daemon` there. The hub stays on one machine.

## 4. Set your model key

```bash
export OPENROUTER_API_KEY=sk-or-...
```

For Telegram push notifications (optional):

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
```

## 5. Enrol a project

A project is any git repo with a `.baird/project.yaml` file.

```bash
cd ~/baird-sandbox
git init
echo "# Sandbox" > README.md
git add . && git commit -m "init"

baird project init sandbox --name "Sandbox" --github me/sandbox
baird project push          # upsert it into the hub
baird project list          # confirm it shows up
```

`project init` writes a starter `.baird/project.yaml` with default rules (`seeds-set`, `env-pinned`, `readme-present`, `ai-friendly-outputs`). Edit it freely — it's just YAML.

## 6. Code with the agent

```bash
cd ~/baird-sandbox
baird code
```

You'll get a REPL. Each line is a turn; the model sees the project context (tree, recent commits, decisions, rules). Slash-commands:

- `/context` — print the rendered context block
- `/reset` — start a fresh session
- `/cost` — show this session's cost so far
- `/no-diff` — disable diff-block approval prompts
- `/exit` — leave

If the model replies with a ` ```diff ` block, BAIRD prompts you `apply? [y/N/q]` per block. Accepted blocks become real git commits with a `Baird-Action-Id` trailer; revert with `baird undo`.

## 7. Schedule a background task

```bash
baird task add daily-poke
$EDITOR ~/.baird/tasks/daily-poke.yaml      # set the prompt, model, schedule
baird task list
baird task run daily-poke                   # fire once, ignoring the schedule
baird orchestrator serve                    # run all scheduled tasks (long-lived)
```

The orchestrator process supervises the scheduler. Run it once on the hub (under systemd if you want it auto-restart). See [workflows.md](workflows.md#scheduled-tasks) for example task YAMLs.

## 8. See what's going on

```bash
baird status                  # one-shot dashboard (health, budget, inbox, recent activity, tasks)
baird status --watch          # live refresh
baird ps                      # currently running actions
baird inbox                   # unresolved notifications
baird logs <action_id>        # one action's full record
baird task history daily-poke # firings of one task
baird registry actions --since-hours 24
```

## 9. Try one feature

A few things to verify everything's wired:

```bash
baird research "rapid scRNA-seq integration benchmarks 2026"
baird improve --since-hours 24             # propose harness self-improvements
baird snakemake path/to/Snakefile          # wrap a pipeline run with provenance
```

That's it. The rest is in [commands.md](commands.md) and [workflows.md](workflows.md).

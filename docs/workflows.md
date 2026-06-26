# Workflows

End-to-end recipes for common things. Assumes hub + daemon are running and `OPENROUTER_API_KEY` is set. See [quickstart.md](quickstart.md) for that.

## Setting up a new project

```bash
cd ~/projects/my-new-thing
git init && echo "# my-new-thing" > README.md && git add . && git commit -m "init"

baird project init my-new-thing --name "My New Thing" --github me/my-new-thing
$EDITOR .baird/project.yaml          # fill in context, goals, env
git add .baird && git commit -m "baird: enrol project"

baird project push                    # mirror into the hub
```

## Interactive coding session

```bash
cd ~/projects/my-new-thing
baird code
```

Watch the conversation, accept diffs as they're proposed, type `/cost` to peek at spend, `/exit` to leave.

Useful flags:

- `--show-context` dumps the rendered context block to stdout and exits. Run it before starting a session to sanity-check what the model will see.
- `-f <path>` adds extra files to the always-include set for this session (otherwise: `.baird/project.yaml`, `environment.yml`, `README.md`, `CLAUDE.md`, `pyproject.toml`).
- `--budget N` adjusts the context's token budget (default 6000).

Recording a decision the model proposed:

```bash
# manually, via the hub
curl -X POST localhost:8000/projects/my-new-thing/decisions \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"my-new-thing","text":"use harmony for batch integration","author":"ai"}'

# or via Python in a quick `baird code` turn — the model can write decisions
# on the user's behalf (per design) once you give it a tool, which is a follow-up.
```

`baird code` already loads the last 5 decisions into the context block, so they survive across sessions.

## Reverting an applied diff

The REPL marks every diff it applies with a `Baird-Action-Id` trailer.

```bash
git log --format='%h %s' -3
baird undo                            # revert the last BAIRD commit (via git revert)
```

`baird undo` refuses if HEAD isn't a BAIRD commit, so a hand-typed commit between yours is safe.

## Scheduled tasks

Drop a task YAML in `~/.baird/tasks/`:

```yaml
# ~/.baird/tasks/morning-pulse.yaml
id: morning-pulse
description: Morning summary of last 24h activity
enabled: true

trigger:
  type: cron
  cron: "0 9 * * *"

runnable:
  prompt: "Give me a 3-sentence summary of what changed in the scrna-2026 repo in the last 24h, and flag anything that looks off."
  model: anthropic/claude-3-haiku
  project_id: scrna-2026

budget:
  max_runtime_s: 60
  max_cost_usd: 0.05
```

Fire it once to test:

```bash
baird task run morning-pulse
baird task history morning-pulse
```

Then start the long-lived scheduler:

```bash
baird orchestrator serve
```

A bare-bones systemd unit (Linux):

```ini
# /etc/systemd/system/baird-orchestrator.service
[Unit]
Description=BAIRD orchestrator
After=network.target

[Service]
Type=simple
User=ethan
Environment=OPENROUTER_API_KEY=sk-or-...
Environment=TELEGRAM_BOT_TOKEN=...
Environment=TELEGRAM_CHAT_ID=...
ExecStart=/home/ethan/BAIRD/.venv/bin/baird orchestrator serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Same shape for `baird hub serve` and `baird daemon`.

## File-watched task

```yaml
trigger:
  type: watch
  path: /home/ethan/projects/scrna-2026/results
  events: [created, modified]
```

Useful for "when a new output file lands, summarise it" — debounced 2s so editor save bursts don't fire repeatedly.

## Reactive task

For "when another task fails 3x, do X" or "when a long pipeline finishes, run a QC summary":

```yaml
trigger:
  type: reactive
  event: pipeline.done
```

Emit the event from anywhere in-process:

```python
from baird.event_bus import default_bus
default_bus.publish("pipeline.done", {"workflow": "qc.smk"})
```

(There's no `baird emit <event>` CLI yet — emit from your code or from a `baird task run`-style script.)

## Running a Snakemake/Nextflow pipeline with provenance

```bash
cd ~/projects/scrna-2026

baird snakemake Snakefile --cores 8 --use-conda
baird nextflow main.nf -profile slurm
```

What you get back:

- one parent **Action** row carrying the full command, with cost/runtime and a parsed summary
- the rule/process completion count (parsed from Snakemake's stdout or, if you also pass `--report report.json`, from the JSON)
- a **`result`** inbox row, pushed to Telegram if configured

To pass extra args without `--` ambiguity:

```bash
baird snakemake Snakefile -- --cores 8 --use-conda --rerun-incomplete
```

## Doing research

```bash
export TAVILY_API_KEY=tvly-...
baird research "rapid scRNA-seq batch integration benchmarks 2026" --project scrna-2026
```

The result lands as an inbox `result` row with the markdown brief — read it with `baird inbox` or `baird logs <action_id>`.

Without `TAVILY_API_KEY`, the loop runs but produces "no results returned" — useful for testing the wiring, not the synthesis.

As a standing watch task (weekly):

```yaml
# ~/.baird/tasks/weekly-scrna.yaml
id: weekly-scrna
trigger: { type: cron, cron: "0 8 * * 0" }
runnable:
  prompt: "Find any new scRNA-seq integration papers since last week."
  model: anthropic/claude-3-haiku
budget: { max_cost_usd: 0.10 }
```

Then point its prompt at `baird research` from inside a custom runnable, or call `run_research()` directly from a small Python wrapper task — full integration of `baird research` as a task `runnable.kind` is a follow-up.

## Self-improvement loop

Run on demand (one-off, ~$0.01-0.10):

```bash
baird improve --since-hours 168       # last week
```

Proposals land in the inbox with kind `proposal`:

```bash
baird inbox
baird inbox resolve <id> accept       # or 'reject'
```

`accept` here just marks the proposal closed in the inbox; applying its diff is manual (read the body, save to a `.patch`, `baird diff apply`). Auto-apply isn't implemented — by design, per the spec.

As a weekly cron:

```yaml
# ~/.baird/tasks/weekly-improve.yaml
id: weekly-improve
trigger: { type: cron, cron: "0 6 * * 1" }
runnable:
  prompt: "(unused — baird improve has its own prompt)"
  model: anthropic/claude-3.5-sonnet
budget: { max_cost_usd: 0.30 }
```

(Today this still calls the model with the task's prompt — wiring `baird improve` to a task `runnable.kind: self_improve` is a follow-up. For now, run on demand or shell out from a wrapper task.)

## Inspecting one action in detail

```bash
baird ps                              # find an action_id
baird logs <action_id>                # full record
baird registry actions --task <task_id> --since-hours 24
```

For lineage (what produced this file?):

```bash
curl localhost:8000/files/<file_id>/lineage | jq
```

`baird files lineage` as a CLI wrapper is a small follow-up.

## Recall: searching memory

```bash
# Search across action summaries, decisions, and inbox bodies:
curl 'localhost:8000/recall?query=harmony&project_id=scrna-2026&k=10' | jq
```

Backed by SQL `LIKE` for now. The `/recall` shape is locked, so once the LanceDB swap lands you keep the same calls.

## Adding a satellite machine

### One command (the smooth path)

Prereqs on the hub:

- `~/.ssh/config` has an entry for the satellite hostname (uses your normal SSH config; no password prompts — keys preferred).
- The hub's `~/.baird/config.yaml` has `auth_token:` set (any random string; `openssl rand -hex 32` is fine). The enrol command pulls it from there automatically.

Then, from the hub:

```bash
baird satellite enroll <ssh-host>            # e.g. `baird satellite enroll hibu`
```

What it does, in order:

1. Picks an unused hub-side forward port (8766+).
2. Writes `~/.config/systemd/user/baird-tunnel@.service` (if absent) and `~/.config/baird/tunnel-<ssh-host>.env`, then `systemctl --user enable --now baird-tunnel@<ssh-host>`. The tunnel is `ssh -R 8000:localhost:8000 -L <port>:localhost:8765`.
3. SSHes out and runs a bootstrap script: `git clone` BAIRD into `~/code/BAIRD`, install via `uv` (provisions a managed Python 3.11), add a `baird` alias to `~/.bashrc`.
4. Renders `host.yaml` on the satellite with the hub's `auth_token` already filled in as `hub_auth_token`, sets `use_hub_for_models: true` so the satellite routes OpenRouter calls through the hub's proxy, and `executor_listen: 127.0.0.1:8765`.
5. Probes `baird project list` through the tunnel to prove the round-trip works.
6. Records the satellite in `~/.baird/satellites.json`.

Flags:

```
--host-id NAME              # BAIRD host_id; defaults to ssh-host
--git-ref REF               # tag or branch to install (default: main)
--port N                    # hub-side forward port (default: next free)
--watch-root PATH           # watch root on the satellite (default: ~/projects)
--no-use-hub-for-models     # let the satellite call OpenRouter directly with its own key
```

After enrolment, from the satellite:

```bash
baird project list                  # works (auth via tunnel)
baird code                          # REPL; model calls go via hub proxy
```

No `OPENROUTER_API_KEY` on the satellite — it lives in `~/.baird/secrets.env` on the hub.

### Manage satellites

```bash
baird satellite list                     # show enrolled satellites + tunnel status
baird satellite remove <host-id>         # tear down hub-side tunnel (leaves remote install)
systemctl --user status baird-tunnel@<ssh-host>
journalctl --user -u baird-tunnel@<ssh-host> -f
```

### When the satellite can't SSH to the hub

If the satellite can't initiate SSH to the hub (e.g. the hub is on Tailscale at `100.x.y.z` and the satellite doesn't have Tailscale), the tunnel direction is reversed: the **hub** SSHes out to the satellite, which the systemd-user unit does by default (`ssh -N -R 8000:localhost:8000 ...`). The hub needs SSH key auth set up *to* the satellite. The enrol command handles this for you.

### Manual install (if you don't want the one-command flow)

Only useful when the satellite blocks the hub's outbound SSH. Mirror the steps `baird satellite enroll` would have done by hand:

```bash
# on the satellite
git clone https://github.com/ethanlewisbaird/BAIRD.git ~/code/BAIRD
cd ~/code/BAIRD && uv venv --python 3.11 && uv pip install -e .

mkdir -p ~/.baird
cat > ~/.baird/host.yaml <<EOF
host_id: $(hostname)
hub_url: http://127.0.0.1:8000        # via SSH tunnel; change if reachable directly
hub_auth_token: "<same as hub's config.yaml auth_token>"
use_hub_for_models: true
executor_listen: 127.0.0.1:8765
volumes:
  - id: $(hostname):/home
    mount: $HOME
    shared: false
watch:
  roots: [$HOME/projects]
  deny: ["**/.git/**"]
EOF

echo "alias baird='BAIRD_HOME=\$HOME/.baird \$HOME/code/BAIRD/.venv/bin/baird'" >> ~/.bashrc

# from the hub, stand up the tunnel manually (see `baird satellite enroll`'s systemd unit
# in ~/.config/systemd/user/baird-tunnel@.service for the canonical ssh args).
```

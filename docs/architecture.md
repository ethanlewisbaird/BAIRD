# Architecture

## Processes

```
┌──────────────────────────── HUB (Linux server) ────────────────────────────┐
│                                                                            │
│   baird hub serve  ──►  FastAPI on :8000  (bearer-auth, deny-by-default)  │
│     │                    routes: /files, /actions, /projects, /decisions, │
│     │                            /sessions, /messages, /notifications,    │
│     │                            /budgets/usage, /stats, /recall,         │
│     │                            /v1/proxy/chat/completions               │
│     │                    backed by ~/.baird/registry.sqlite + memory.sqlite│
│     │                    loads ~/.baird/secrets.env on startup            │
│     │                                                                      │
│   baird orchestrator serve  ──►  scheduler (threaded) + notifier          │
│     │                              loads ~/.baird/tasks/*.yaml             │
│     │                              cron / interval / watch / reactive      │
│     │                                                                      │
│   baird code  ──►  per-user REPL (transient; auto-starts hub if missing)  │
│   baird research / improve / snakemake / nextflow / status / ...          │
│                                                                            │
└─────────────────────────────────┬──────────────────────────────────────────┘
                                  │  HTTP over Tailscale OR SSH tunnels
                                  │  (Bearer hub_auth_token + X-Baird-Action-Id)
        ┌─────────────────────────┴─────────────────────────┐
        │                         │                         │
┌───────▼───────────┐  ┌──────────▼──────────┐  ┌──────────▼──────────┐
│ satellite A       │  │ satellite B          │  │ satellite C          │
│ (workstation)     │  │ (HPC login node)     │  │ (laptop)             │
│                   │  │                      │  │                      │
│ baird daemon      │  │ baird daemon         │  │ baird code (REPL)    │
│  └─ watchdog      │  │  └─ watchdog         │  │                      │
│  └─ executor      │  │  └─ executor         │  │  no OPENROUTER key   │
│     (bearer-auth) │  │     (bearer-auth)    │  │  routes via hub      │
│ use_hub_for_models│  │ use_hub_for_models   │  │  /v1/proxy/...       │
│   = true          │  │   = true             │  │                      │
└───────────────────┘  └──────────────────────┘  └──────────────────────┘
```

- **Hub**: one always-on process. Owns the truth: two SQLite databases (registry + memory) plus the OpenRouter credential. Bearer-token middleware gates every route except `/health`.
- **Model proxy**: `/v1/proxy/chat/completions` lets satellites call OpenRouter without holding the key. Records cost + tokens against the caller's action via `X-Baird-Action-Id`.
- **Daemon**: one per machine. Combines a watchdog (registers new/changed files) and the executor (`read_file` / `write_file` / `run_command` / `apply_diff` — bearer-token auth, path-scoped to declared volumes). Co-located so a hub-driven write and a watchdog event can't race into duplicate provenance rows.
- **Orchestrator**: one process on the hub. Runs the task scheduler. Same code path as `baird code` — "background" means no human in the loop, not a different runtime.
- **CLI commands** like `baird code`, `baird research`, `baird snakemake`: transient processes; they hit the hub via HTTP and (when `use_hub_for_models: true`) call OpenRouter through the hub's proxy.
- **`baird satellite enroll`**: from the hub, drives the full remote install + tunnel setup in one command. Writes `host.yaml` with the correct `hub_auth_token` automatically.

## Storage model

> Every file lives canonically per host. Movement between hosts is explicit and logged.

- **User code** → GitHub. `baird project pull X --to <host>` is `git clone` + register a checkout. (Phase 3+ ergonomics, see [workflows.md](workflows.md).)
- **User data** (BAMs, FASTQs, intermediates) → stays on the producing machine. The registry tracks `(storage_volume, relative_path)`.
- **Metadata** (provenance, decisions, conversations, inbox) → hub only, in SQLite. Single writer → no multi-master pain.
- **Harness state** (`~/.baird/tasks/*.yaml`, `~/.baird/config.yaml`) → hub only. Satellites just run the daemon.

Storage volumes are modelled, not hosts. `cluster:/work` is one volume regardless of which login or compute node touches it; `cluster-node17:/scratch` is a different volume because it's per-node.

## File identity

Each file in the registry carries:

- a **fast fingerprint** — `(size, mtime_ns, head_hash, tail_hash)` where head/tail are sha256s of the first/last 4MB. Recorded on every write.
- a **lazy `sha256`** — full hash, computed by a background worker. Field starts `pending`, becomes `computed` (or `skipped` for >1TB files).

Identity rule: two records refer to the same file when their `sha256`s match (if both computed), OR all four fingerprint fields match (otherwise).

## Two databases, one service

- `~/.baird/registry.sqlite` — `files`, `actions`, `file_actions` (M:N).
- `~/.baird/memory.sqlite` — `projects`, `decisions`, `sessions`, `messages`, `notifications`.

Both are served by the one FastAPI app. Splitting the files keeps each engine's writers cheap (the watchdog hits registry hard; the conversation side does fewer larger writes). Cross-DB joins are done in the app layer.

## Three-tier safe/destructive classifier

Every command the executor runs is classified:

- **safe** — read-only (`ls`, `git status`, `samtools view -H` …) — auto-run in interactive mode.
- **project** — writes scoped *inside* the active project root, or scoped tools (`pytest`, `make`, `snakemake`) — auto with warning in interactive mode; prompt in background mode.
- **destructive** — everything else, plus an always-destructive allowlist that even project overrides can't override (`pip install`, `conda install`, `apt`, `sudo`, `git push --force`, `rm -rf /`, …).

The executor rejects destructive calls server-side. Elevating one is the orchestrator's job (prompt the user, get approval, re-issue).

## Persistent multiplexed sessions

Long jobs run inside a deterministically-named tmux or screen session on the satellite, so SSH disconnects don't kill them and you can `baird session attach <name>` to watch. `host.yaml`'s `session_multiplexer` picks the backend: `auto` prefers tmux, falls back to screen, falls back to `none` (subprocess-only).

## Persistent conversation threads

Each `task_id` (and each `project_id` for REPL sessions) has one Session row reused across firings. Prior messages are pre-loaded each turn so the model sees real conversation continuity. Capped at the last 20 messages — a context-compressor + rolling-summary path is on the deferred list.

## Budgets

Per-task and global. Backed by summing `cost_usd` on completed Actions over a rolling window (`/budgets/usage?since_hours=24[&task_id=...]`).

- Per task: `task.budget.max_cost_usd` (or `hub_cfg.daily_per_task_default_usd` fallback)
- Global: `hub_cfg.daily_total_usd`

Hitting either ceiling skips the firing — already-running ones complete. The skip writes a `logged` inbox row so it's visible in `baird status`.

## Notifications

Every notification writes an inbox row (universal backstop). On top of that, four tiers route differently:

| Kind | Push to Telegram | Inbox row |
|---|---|---|
| `approval` | yes | yes |
| `failure` | yes | yes |
| `result` | yes | yes |
| `logged` | no | yes |
| `proposal` | no (inbox only) | yes |

Telegram is best-effort — a network failure logs an error but never crashes the firing.

## What's NOT enforced at the daemon layer

- Concurrency between multiple daemons on the same volume. The first POST wins on dedup; subsequent writes update `last_seen_at`. There's no row-level lock.
- Cross-host conflict resolution for the same path. The hub treats `(storage_volume, relative_path)` as unique-per-volume; the same file on two volumes is two rows by design.
- Data sync. BAIRD intentionally doesn't move bulk data — that's a separate concern (rsync, S3, etc.).

## Security boundary

Three independent layers:

- **Hub auth.** When `config.yaml` has `auth_token:` set, the hub middleware requires `Authorization: Bearer <token>` on every route except `/health`. Satellites send the matching token from `host.yaml` → `hub_auth_token`. With no token configured, the hub is open (single-user / single-machine convenience).
- **Executor auth.** The satellite executor binds to `host.yaml` → `executor_listen` and requires a bearer token (`auth_token` on that satellite). With no token configured, it refuses *every* call (deny-by-default).
- **Transport.** Two common deployments: Tailscale (bind to the tailnet iface; only tailnet peers can reach the hub or executor) or SSH-tunneled (hub stays on `127.0.0.1`; each satellite gets reverse-tunneled access via persistent `ssh -R`).

`baird satellite enroll` defaults to the SSH-tunneled pattern, writes the matching `hub_auth_token` automatically, and stands up a `systemd --user` unit per satellite so the tunnel survives disconnects.

No multi-user model — BAIRD assumes one user.

## Central model proxy

`POST /v1/proxy/chat/completions` on the hub forwards to OpenRouter using the **hub's** `OPENROUTER_API_KEY` (loaded from `<baird_home>/secrets.env` on startup). Satellites that set `use_hub_for_models: true` in `host.yaml` route their `baird code` / `task run` / `research` / `improve` calls through this endpoint:

- Key lives on one machine (the hub).
- One unified cost/token ledger: clients pass `X-Baird-Action-Id` and the proxy enriches the action row directly.
- Provider swaps are one config change (`config.yaml` → `openrouter_url:`).
- HPC satellites with restricted outbound networking still work — they only need to reach the hub.

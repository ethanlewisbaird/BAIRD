# Design

The full design — every sub-decision across 5 phases, with rationale — lives in project memory:

```
~/.claude/projects/-home-ethan/memory/project_ai_harness.md
```

This document captures what's *shipped* against that design and what's deliberately left for later.

## Shipped

### Phase 1 — storage & provenance

- FastAPI hub with two SQLite databases (registry + memory).
- Satellite daemon: watchdog + sha256 backfill worker.
- Hybrid file identity: fast fingerprint `(size, mtime_ns, head_hash, tail_hash)` + lazy full sha256.
- Volumes modelled as `host:path`; longest-prefix wins on resolve.
- Watch-roots + gitignore-style denylist.
- Dedup-on-write: same fingerprint → update `last_seen_at`; different → soft-delete + insert.

### Phase 2 — shared memory core

- `.baird/project.yaml` Pydantic schema with `goals`, `state`, `data_aliases`, `rules`, `env`, `permissions`.
- Rules engine with four built-in checkers: `seeds_set`, `env_pinned`, `readme_present`, `ai_friendly_outputs`.
- Decisions (append-only, `user`/`ai` author), sessions, messages, notifications (inbox).
- `recall(query, sources, project_id, k)` — currently SQL-LIKE backed; API shape locked.
- `start_action()` context manager that accumulates cost + tokens, finishes cleanly on exit.

### Phase 3 — coding-mode substrate

- Three-tier safe/destructive classifier (`safe` / `project` / `destructive`) with regex layer + path scoping + per-project overrides + non-overridable always-destructive list.
- Satellite executor (`read_file` / `write_file` / `run_command` / `apply_diff`) with bearer-auth + volume-mount path-scoping + tier enforcement.
- Repo context loader: project header, memory, tree with auto-collapse, git log/status, decisions, rules, action summaries.
- `diff_apply.apply_diff_to_repo()` + `undo_last_baird_commit()` (refuses unless HEAD is BAIRD-trailered).

### Phase 4 — background-agent mode

- `OpenRouterClient` with pluggable transport, cost estimation from a static price table when the API doesn't return one.
- Task schema with `cron` / `interval` / `watch` / `reactive` triggers, `Runnable`, `Budget`, `concurrency_group`.
- Threaded `Scheduler` with concurrency groups, per-task + global budget gate, signal-driven shutdown.
- `Notifier` with tiered routing (`approval` / `failure` / `result` / `logged` / `proposal`) — Telegram push + universal inbox row.
- `runner.run_task_once()` with persistent per-task Session and prior-history pre-load.

### Phase 4b / 5 polish + features

- tmux/screen/noop multiplexer abstraction with deterministic naming.
- Conda/mamba/Docker/Singularity activation prefix builder.
- `EventBus` for reactive triggers; watchdog Observer per watch-triggered task with debounce.
- Multi-turn REPL for `baird code` with fenced-diff detection + per-block approval, `/model` slash command (live-fetched OpenRouter catalog), per-session model switch.
- Snakemake/Nextflow wrappers (parent action, report parsing, summary).
- Self-improvement loop (`baird improve`) — generates inbox `proposal` rows.
- Research loop (`baird research`) — plan → search → synthesize → inbox.
- `baird status` (one-shot + `--watch`), `baird logs / ps / registry actions / task history / session list-attach-kill`.
- Mode auto-detection hints on bare `baird`.

### Production-readiness pass

- **Hub bearer-token auth**: `auth_token` in `config.yaml` gates every route except `/health`. Satellites send the matching `hub_auth_token` from `host.yaml`.
- **Central model proxy**: `POST /v1/proxy/chat/completions` forwards to OpenRouter using the hub's key. Satellites set `use_hub_for_models: true` and never hold credentials. Cost + tokens enrich the caller's action via `X-Baird-Action-Id`. Upstream URL overridable via `openrouter_url:`.
- **`<baird_home>/secrets.env`**: `KEY=value` file loaded into `os.environ` on hub startup. Replaces shell-rc-based credential plumbing.
- **`baird up` / `baird stop`**: hub supervisor — `baird code` and friends auto-spawn the hub in the background if it isn't running. Honest about which side is local: refuses to auto-spawn when `hub_url` is remote.
- **`baird satellite enroll/list/remove`**: one-shot satellite setup from the hub. Picks a forward port, writes the `systemd --user` tunnel, SSHes out to install BAIRD via uv, writes `host.yaml` with the auth token already filled in, verifies the round-trip.
- **Auto schema migration**: `metadata.create_all` + `ALTER TABLE ADD COLUMN` for any declared column not on the existing table. Phase-1 SQLite + Phase-4 code now works seamlessly.
- **`$BAIRD_HOME`**: state directory override so a dev install and a prod install can run side by side without colliding.

### Gap-finishing pass

- **`baird emit <event>`** publishes a reactive event by POSTing to `/events/{name}`. The scheduler polls the events table every tick and republishes onto its in-process bus so reactive triggers fire across processes.
- **`baird files lineage <file_id>`** CLI wrapper for the existing `/files/{id}/lineage` route.
- **REPL multi-line input**: `"""` opens a heredoc-style block, a second `"""` closes it. Body sent as one user message.
- **REPL session resume**: `baird code --session <id>` attaches to a specific prior session; `/sessions` lists them in-REPL.
- **`runnable.kind`**: free-form `model` (default), `self_improve`, `research`, and `command`. Each `kind` has its own dispatcher in `runner.py`.
- **Context compressor**: rolling-summary above N turns, in-process cache keyed by `(session_id, older_count)`. Compressor failure falls back to the old "drop older silently" behaviour.
- **Snakemake `--live`**: streaming runner pumps stdout/stderr line-by-line; parses `"X of Y steps (Z%) done"` and posts a `logged` inbox row at 10% boundaries (throttled).
- **Permissions → executor**: `baird/executor_client.py` is a typed HTTP wrapper around the satellite executor's four routes; `run_command(project_root=X)` reads `X/.baird/project.yaml` `permissions:` automatically and packages them into the request body. `ProjectYaml` gained the `env:` and `permissions:` fields that were already documented.
- **Orchestrator → satellite executor dispatch**: `baird/dispatcher.py` runs a `kind=command` task locally if `host_id` is None / matches the hub, otherwise routes through the satellite's executor via the SSH forward tunnel. `baird satellite enroll` now generates a per-satellite `executor_auth_token` (32 hex) on enrolment, writes it into the satellite's `host.yaml` `auth_token:`, AND records it in `satellites.json`.

## Deferred (each its own meaningful slice)

These are real new work, not just substrate plumbing — flagged here so they don't get forgotten:

### LanceDB swap-in for `/recall` + tier-3 promoted-fragment ingestion

The `/recall` API shape is locked and SQL-backed today. Swapping to LanceDB needs:

- An embedding model (OpenRouter doesn't expose embeddings cleanly — likely OpenAI / Voyage / a local model). **Decision required.**
- A `fragments` table with `(source, source_id, project_id, text, vector, created_at, metadata)`.
- `baird flag <action_id> --range L100-L150` (user flag), `baird resolve <action_id>` (error→fix pair), the always-promoted paths for first-time-success runs of finicky tools.
- A migration path for the existing SQL-backed call sites — they keep working unchanged.

Deferred by design: best done once you've used SQL-recall enough to know what filters you actually want.

### Full Rich `Live + Layout` TUI for `baird code`

The design pinned a specific layout:

- header (project / host / branch)
- conversation panel (~70%) + tool-call sidebar
- live status bar (tokens, cost, inbox count, budget)
- input line at the bottom with `/`-prefixed commands inside the panel
- diff approval renders in the conversation panel with `y/n/e/q` key handling

Deferred by design: the line-by-line REPL works. The full TUI is UX iteration that benefits from real use first.

### bioRxiv / PubMed MCP integration as research backends

The `web_search` callable is the seam. Wiring MCP clients depends on which MCP-client library settles in for the broader harness — external dependency choice.

### Streaming responses through `/v1/proxy/chat/completions`

Today the proxy waits for the full upstream response before returning; the REPL doesn't render partial tokens anyway. The right time to wire SSE end-to-end is alongside the full TUI work.

### Smaller follow-ups still open

- `apply_diff` dispatch through the satellite executor (substrate in `ExecutorClient` is there, the runner's `kind=command` doesn't use it yet).
- Status fan-in across satellites: a satellite executor failure currently shows up under its own action row; surfacing it on the originating task is real work.
- Retries on transient SSH-tunnel hiccups in `dispatcher.py`.
- Pagination for sessions over 1000 messages (the compressor caps at the hub's max limit).

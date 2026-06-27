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

### Smaller-open sweep

- **apply_diff dispatch**: `dispatcher.apply_diff_anywhere()` routes a unified diff to either local `apply_diff_to_repo` or `ExecutorClient.apply_diff` on a named satellite.
- **Dispatcher retries**: `httpx.ConnectError` / `ReadTimeout` / `RemoteProtocolError` / `TransportError` retried 3× with 1s/3s backoff.
- **Status fan-in**: dispatcher posts a `failure` inbox row tagged with the originating task_id when a satellite-dispatched command errors out.
- **Session-message pagination**: `/sessions/{id}/messages?offset=` + the compressor walks pages of 1000 until exhausted.

### Final deferred slices (all shipped 2026-06-27)

- **LanceDB semantic recall** with bge-small CPU embedder. `<baird_home>/lance/fragments.lance`, lazy table wrapper to keep hub startup fast, auto-population on action/decision/notification create, hybrid SQL+vector `/recall`, `baird flag` + `baird resolve` for tier-3 promotion. Embedder is configurable via `embedder_model:` — swap to `bge-large` after fixing the GPU driver.
- **Rich Live + Layout TUI** for `baird code` (default). Header (project/host/branch/model), conversation panel with streaming-aware buffers, status bar, modal diff approval with `y/n/e/q` single-key reading via `tui_keys.read_key`. `--no-tui` falls back to the line REPL.
- **SSE streaming** end-to-end: `OpenRouterClient.stream_complete()` drives chunked SSE; `/v1/proxy/chat/completions` switches to `StreamingResponse` when `stream:true`, forwards SSE verbatim, watches for a `usage:` chunk to enrich the action; TUI panel updates per-token.
- **MCP integration**: official `mcp` SDK, sync wrapper, `~/.baird/mcp_servers.yaml` config, planner picks per-sub-query between web and configured MCP tools, `baird mcp list/tools/call/ping` for management.

## Still-deferred (intentionally)

These items remain open by choice, not by oversight:

- **Tool-call sidebar in the TUI** — was in the original design but speculative; deferred until BAIRD actually emits tool calls inside conversations.
- **Tier-3 auto-promotion rules** — `baird flag` and `baird resolve` are manual; the automatic "first-time-success on a finicky tool" detection from the original design is deferred until recall has enough volume to evaluate it.
- **Vector index compaction** — LanceDB grows monotonically; periodic compact + dedup of `(source, source_id)` pairs is deferred until the fragments table actually gets large.
- **Cluster-aware executor dispatch** — currently one satellite per `host_id`. HPC-cluster-style "any node" dispatch with a job queue is deferred.
- **Long-running tool-call loops in `baird research`** — today the planner picks tools up front; an iterative "based on what I found, search for X next" loop is deferred.

Everything else from the original 5-phase plan is shipped.

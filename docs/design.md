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
- Multi-turn REPL for `baird code` with fenced-diff detection + per-block approval.
- Snakemake/Nextflow wrappers (parent action, report parsing, summary).
- Self-improvement loop (`baird improve`) — generates inbox `proposal` rows.
- Research loop (`baird research`) — plan → search → synthesize → inbox.
- `baird status` (one-shot + `--watch`), `baird logs / ps / registry actions / task history / session list-attach-kill`.
- Mode auto-detection hints on bare `baird`.

## Deferred (each its own meaningful slice)

These are real new work, not just substrate plumbing — flagged here so they don't get forgotten:

### LanceDB swap-in for `/recall` + tier-3 promoted-fragment ingestion

The `/recall` API shape is locked and SQL-backed today. Swapping to LanceDB needs:

- An embedding model (OpenRouter doesn't expose embeddings cleanly — likely OpenAI / Voyage / a local model).
- A `fragments` table with `(source, source_id, project_id, text, vector, created_at, metadata)`.
- `baird flag <action_id> --range L100-L150` (user flag), `baird resolve <action_id>` (error→fix pair), the always-promoted paths for first-time-success runs of finicky tools.
- A migration path for the existing SQL-backed call sites — they keep working unchanged.

Best done once you've used SQL-recall enough to know what filters you actually want.

### Full Rich `Live + Layout` TUI for `baird code`

The design pinned a specific layout:

- header (project / host / branch)
- conversation panel (~70%) + tool-call sidebar
- live status bar (tokens, cost, inbox count, budget)
- input line at the bottom with `/`-prefixed commands inside the panel
- diff approval renders in the conversation panel with `y/n/e/q` key handling

The current line-by-line REPL works. The full TUI is UX iteration that benefits from real use first.

### Wiring `baird improve` / `baird research` as task `runnable.kind` values

Today, scheduling these means writing a small Python wrapper task or running them on a cron via shell. A first-class `runnable.kind: self_improve` (and `: research`) variant would skip the wrapper, but it needs a small extension of the task schema and runner dispatch — not hard, just deferred until you actually want to schedule them.

### bioRxiv / PubMed MCP integration as research backends

The `web_search` callable is the seam. Wiring MCP clients depends on which MCP-client library settles in for the broader harness.

### Other smaller follow-ups

- `baird emit <event>` CLI to publish reactive events from the shell.
- `baird files lineage <file_id>` CLI wrapper (the `/files/{id}/lineage` route exists).
- Context compressor with rolling summarisation (currently the runner just caps history at 20 turns).
- Snakemake `--live` mode parsing.
- Multi-line input in the REPL.
- A "session continuity" command — pick up a prior REPL session and continue.
- Proper integration of `permissions:` from `project.yaml` into the executor (the schema exists, the loader exists; piping the loaded overrides into every `run_command` call is the missing link).

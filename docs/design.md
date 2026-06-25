# BAIRD design

The complete design — five phases, all sub-decisions resolved — is captured in
the project memory at:

```
/home/ethan/.claude/projects/-home-ethan/memory/project_ai_harness.md
```

Future sessions that pick this project up can read that file for the full context.

## Phases (build order)

1. **Storage & provenance foundation** — provenance registry on the hub (SQLite + FastAPI), satellite watchdog daemons posting fast fingerprints.
2. **Shared memory core** — conversation DB, project memory YAML schema (with enforceable rules), LanceDB vector store, narrow client library.
3. **Interactive coding mode** — hub orchestrator + satellite executor (JSON-RPC), repo context loader, hunk-level diff approval, test/lint feedback, conda env activation, safe/destructive gate.
4. **Background agent mode** — harness-internal scheduler (cron / watch / reactive triggers), persistent task threads, Telegram + inbox notifications, Snakemake/Nextflow DAG wrappers, self-improvement loop, research loop.
5. **Orchestration & UX polish** — CLI surface (minimum viable first), mode auto-detection, Rich TUI for `baird code`, `baird status` dashboard.

## Cross-cutting requirements

- Persistent sessions via tmux **or** screen, abstracted by the executor.
- AI-friendly outputs: every visualization has a machine-readable sibling (CSV/JSON/Parquet).
- Best-practices rules in `.baird/project.yaml` enforced at pre/post/review stages.

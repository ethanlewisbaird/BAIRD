"""Auto-add missing columns on engine creation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from baird.db import create_registry_engine


def _table_columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_adds_phase4_columns_to_old_actions_table(tmp_path: Path) -> None:
    db = tmp_path / "registry.sqlite"
    # Pre-populate with a Phase-1-era schema (no Phase-4 columns).
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE actions (
            id VARCHAR PRIMARY KEY,
            project_id VARCHAR,
            parent_action_id VARCHAR,
            tool_name VARCHAR,
            tool_version VARCHAR,
            command TEXT,
            host VARCHAR,
            conda_env VARCHAR,
            env_hash VARCHAR,
            started_at DATETIME,
            finished_at DATETIME,
            exit_code INTEGER,
            slurm_job_id VARCHAR,
            summary TEXT
        )"""
    )
    conn.commit()
    conn.close()

    cols_before = _table_columns(db, "actions")
    assert "cost_usd" not in cols_before
    assert "input_tokens" not in cols_before

    create_registry_engine(str(db))

    cols_after = _table_columns(db, "actions")
    for needed in ("cost_usd", "input_tokens", "output_tokens", "model_name", "task_id"):
        assert needed in cols_after, f"{needed} missing after migration"


def test_idempotent_on_already_migrated_db(tmp_path: Path) -> None:
    """Second call must not fail."""
    db = tmp_path / "r2.sqlite"
    create_registry_engine(str(db))
    create_registry_engine(str(db))
    # If we got here without an OperationalError, success.

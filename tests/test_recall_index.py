"""LanceDB-backed semantic recall index.

These tests need the optional `recall` extras (lancedb + sentence-transformers)
installed. We skip cleanly if they're not available.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_skip_no_recall = pytest.mark.skipif(
    pytest.importorskip("lancedb", reason="recall extras not installed") is None,
    reason="recall extras not installed",
)


@pytest.fixture
def cfg(tmp_path: Path):
    from baird.config import HubConfig

    return HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
        recall_enabled=True,
        embedder_model="BAAI/bge-small-en-v1.5",
        embedder_device="cpu",
    )


@_skip_no_recall
def test_ensure_index_idempotent(cfg, tmp_path: Path):
    from baird import recall_index

    a = recall_index.ensure_index(cfg, lance_dir=tmp_path / "lance")
    b = recall_index.ensure_index(cfg, lance_dir=tmp_path / "lance")
    assert a is not None and b is not None


@_skip_no_recall
def test_upsert_and_search_finds_nearest(cfg, tmp_path: Path):
    from baird import recall_index

    table = recall_index.ensure_index(cfg, lance_dir=tmp_path / "lance")
    recall_index.upsert_fragment(
        table,
        source="action", source_id="a1", project_id="p",
        text="Snakemake QC pipeline completed successfully on the scrna dataset",
    )
    recall_index.upsert_fragment(
        table,
        source="action", source_id="a2", project_id="p",
        text="Refactored the React dashboard to use new color palette",
    )
    recall_index.upsert_fragment(
        table,
        source="decision", source_id="d1", project_id="p",
        text="Chose harmony over scVI for single-cell batch integration",
    )

    hits = recall_index.search(
        table, query="scRNA batch correction algorithm choice", k=2, project_id="p"
    )
    assert len(hits) >= 1
    # Decision about harmony should rank above the React refactor.
    top = hits[0]
    assert top["source"] in ("decision", "action")
    assert "harmony" in top["text"] or "scrna" in top["text"].lower()


@_skip_no_recall
def test_search_filters_by_source(cfg, tmp_path: Path):
    from baird import recall_index

    table = recall_index.ensure_index(cfg, lance_dir=tmp_path / "lance")
    recall_index.upsert_fragment(
        table, source="action", source_id="a", project_id="p", text="alpha"
    )
    recall_index.upsert_fragment(
        table, source="decision", source_id="d", project_id="p", text="alpha"
    )
    only_decisions = recall_index.search(
        table, query="alpha", k=10, project_id="p", sources=["decision"]
    )
    assert all(r["source"] == "decision" for r in only_decisions)


@_skip_no_recall
def test_promote_action_writes_tier_3(cfg, tmp_path: Path):
    from baird import recall_index

    table = recall_index.ensure_index(cfg, lance_dir=tmp_path / "lance")
    fid = recall_index.promote_action(
        table,
        action_id="a1", text="memorable line worth flagging",
        project_id="p", kind="flag",
    )
    assert fid is not None
    rows = table.table.to_arrow().to_pylist()
    assert any(r["tier"] == 3 for r in rows)
    assert any(r["source"] == "flag" for r in rows)


def test_disabled_when_recall_enabled_false(tmp_path: Path):
    from baird import recall_index
    from baird.config import HubConfig

    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
        recall_enabled=False,
    )
    assert recall_index.ensure_index(cfg, lance_dir=tmp_path / "lance") is None


@_skip_no_recall
def test_hub_recall_returns_vector_hits_end_to_end(tmp_path: Path) -> None:
    """Boot a real hub, create a decision, hit /recall. The vector path
    should surface the decision even when SQL LIKE wouldn't."""
    from fastapi.testclient import TestClient

    from baird.config import HubConfig
    from baird.hub import create_app

    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
        recall_enabled=True,
        embedder_model="BAAI/bge-small-en-v1.5",
        embedder_device="cpu",
    )
    # Redirect the lance dir to tmp_path so we don't pollute the user's state.
    import os
    os.environ["BAIRD_HOME"] = str(tmp_path)

    client = TestClient(create_app(cfg))

    client.post("/projects", json={"id": "p1", "name": "P1"})
    client.post(
        "/projects/p1/decisions",
        json={"project_id": "p1", "text": "Use harmony for batch integration", "author": "user"},
    )
    # SQL LIKE for "batch correction" would NOT match the decision text;
    # vector should still find it because the meaning is close.
    r = client.get("/recall", params={"query": "batch correction", "k": 5})
    assert r.status_code == 200
    hits = r.json()["hits"]
    assert any("harmony" in h["text"].lower() for h in hits)


@_skip_no_recall
def test_flag_and_resolve_routes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from baird.config import HubConfig
    from baird.hub import create_app

    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
        recall_enabled=True,
        embedder_model="BAAI/bge-small-en-v1.5",
        embedder_device="cpu",
    )
    import os
    os.environ["BAIRD_HOME"] = str(tmp_path)
    client = TestClient(create_app(cfg))

    flag_r = client.post(
        "/recall/flag",
        json={"action_id": "a1", "text": "memorable line", "project_id": "p1"},
    )
    assert flag_r.status_code == 200
    assert flag_r.json()["id"] is not None

    res_r = client.post(
        "/recall/resolve",
        json={
            "error_action_id": "e1", "fix_action_id": "f1",
            "text": "broken thing → fixed by edit", "project_id": "p1",
        },
    )
    assert res_r.status_code == 200
    assert res_r.json()["id"] is not None

"""End-to-end verification for governed synthesis and proposal isolation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from plastic_promise.core.context_engine import ContextEngine, ContextPack
from plastic_promise.core.memory_proposals import (
    MemoryProposalStore,
    ProposalCandidate,
)
from plastic_promise.core.synthesis import SynthesisStore
from plastic_promise.core.synthesis_maintenance import (
    replay_synthesis_index_jobs,
    scan_synthesis_integrity,
)

PROJECT_ID = "project:governed-e2e"
SOURCE_A = "e2e-source-alpha"
SOURCE_B = "e2e-source-beta"
SYNTHESIS_QUERY = "review the quasarlock governance conclusion"


class _ObservableEmbedder:
    model_name = "e2e-observable-embedder"
    dim = 1024

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        return [0.25] * self.dim


class _ObservableLanceDB:
    def __init__(self) -> None:
        self.inserted: list[dict[str, object]] = []
        self.deleted: list[str] = []
        self.rows: dict[str, dict[str, object]] = {}

    def insert_checked(self, **kwargs: object) -> None:
        row = dict(kwargs)
        memory_id = str(row["memory_id"])
        if memory_id in self.rows:
            return
        self.inserted.append(row)
        self.rows[memory_id] = row

    def replace_checked(self, **kwargs: object) -> None:
        row = dict(kwargs)
        memory_id = str(row["memory_id"])
        if self.rows.get(memory_id) == row:
            return
        self.inserted.append(row)
        self.rows[memory_id] = row

    def delete_checked(self, memory_id: str) -> None:
        self.deleted.append(memory_id)
        self.rows.pop(memory_id, None)

    def search(self, **_kwargs: object) -> list[tuple[str, float]]:
        return []

    def fts_search(self, *_args: object, **_kwargs: object) -> list[tuple[str, float]]:
        return []

    def list_memory_ids(self) -> list[str]:
        return list(self.rows)

    def count_rows(self) -> int:
        return len(self.rows)


def _supply(engine: ContextEngine, query: str = SYNTHESIS_QUERY) -> ContextPack:
    return engine.supply(
        query,
        task_vector=[0.0] * 1024,
        task_type="code_review",
        scope="global",
        debug=True,
        project_id=PROJECT_ID,
        project_policy="strict",
        retrieval_mode="code",
    )


def _layer_ids(pack: ContextPack) -> list[str]:
    return [item.id for item in (*pack.core, *pack.related, *pack.divergent)]


def _raw_ids(pack: ContextPack) -> list[str]:
    return [str(item["id"]) for item in pack.audit_metadata["raw_evidence"]]


def _assert_proposals_are_isolated(
    engine: ContextEngine,
    lancedb: _ObservableLanceDB,
    proposal_ids: set[str],
    proposal_contents: set[str],
) -> None:
    conn = engine._sqlite._conn
    memory_rows = conn.execute("SELECT id, content FROM memories").fetchall()
    memory_ids = {str(row[0]) for row in memory_rows}
    memory_contents = {str(row[1]) for row in memory_rows}
    assert proposal_ids.isdisjoint(memory_ids)
    assert proposal_contents.isdisjoint(memory_contents)

    for proposal_content in proposal_contents:
        pack = _supply(engine, proposal_content)
        public_items = [*pack.core, *pack.related, *pack.divergent]
        assert proposal_ids.isdisjoint(item.id for item in public_items)
        assert proposal_ids.isdisjoint(_raw_ids(pack))
        assert proposal_content not in {item.content for item in public_items}
        assert proposal_content not in {
            str(item.get("content") or "")
            for item in pack.audit_metadata["raw_evidence"]
        }

    indexed_payload = json.dumps(lancedb.inserted, ensure_ascii=False)
    assert not any(proposal_id in indexed_payload for proposal_id in proposal_ids)
    assert not any(content in indexed_payload for content in proposal_contents)


def test_governed_synthesis_lifecycle_and_proposal_isolation_e2e(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "governed-synthesis-e2e.db"))
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "1")
    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "0")
    monkeypatch.setenv("PP_CODE_MEMORY_ENABLED", "0")
    monkeypatch.setenv("PP_QUERY_EXPANSION", "0")
    monkeypatch.setenv("PP_RERANK_DISABLED", "1")
    monkeypatch.setenv("PP_FTS_DISABLED", "1")
    monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
    monkeypatch.setenv("PP_DECAY_IN_RANKING", "0")
    monkeypatch.setenv("PP_TIER_AUTO_PROMOTE", "0")

    engine = ContextEngine(use_sqlite=True)
    embedder = _ObservableEmbedder()
    lancedb = _ObservableLanceDB()
    engine._embedder = embedder
    engine._ldb = lancedb
    engine._heavy_init_done = True
    conn = engine._sqlite._conn

    source_records = (
        {
            "id": SOURCE_A,
            "content": (
                "Alpha independently records the durable SQLite behavior and "
                "supports the controlled conclusion with direct evidence. "
            )
            * 3,
            "memory_type": "experience",
            "source": "user",
            "source_class": "user_fact",
            "project_id": PROJECT_ID,
            "visibility": "project",
            "origin_kind": "document",
            "origin_uri": "file:///e2e-alpha.md",
            "origin_ref": "e2e-alpha",
            "origin_hash": "origin:e2e-alpha:v1",
            "metadata_json": {"status": "current"},
        },
        {
            "id": SOURCE_B,
            "content": (
                "Beta independently confirms the same controlled conclusion "
                "through a separate operational observation. "
            )
            * 3,
            "memory_type": "experience",
            "source": "agent",
            "source_class": "experience",
            "project_id": PROJECT_ID,
            "visibility": "project",
            "origin_kind": "document",
            "origin_uri": "file:///e2e-beta.md",
            "origin_ref": "e2e-beta",
            "origin_hash": "origin:e2e-beta:v1",
            "metadata_json": {"quality_status": "verified"},
        },
    )
    for source_record in source_records:
        engine.register_memory(source_record)

    proposal_store = MemoryProposalStore(conn)
    proposal_contents = {
        "pending": "E2E-PENDING-PROPOSAL must remain outside recall.",
        "rejected": "E2E-REJECTED-PROPOSAL must remain outside recall.",
        "expired": "E2E-EXPIRED-PROPOSAL must remain outside recall.",
    }

    def proposal_candidate(state: str) -> ProposalCandidate:
        return ProposalCandidate(
            content=proposal_contents[state],
            category="fact",
            project_id=PROJECT_ID,
            visibility="project",
            origin_role="user",
            origin_turn_hash=f"sha256:e2e-proposal-{state}",
            origin_call_id=f"call:e2e-proposal-{state}",
            origin_visibility="project",
        )

    pending = proposal_store.create_many([proposal_candidate("pending")])[0]
    rejected = proposal_store.create_many([proposal_candidate("rejected")])[0]
    expired = proposal_store.create_many(
        [proposal_candidate("expired")],
        now=datetime.now(timezone.utc) - timedelta(days=8),
    )[0]
    proposal_store.reject(
        rejected["proposal_id"],
        actor="reviewer",
        call_id="call:e2e-reject",
        reason="reviewer_rejected",
    )
    assert proposal_store.expire_and_redact(now=datetime.now(timezone.utc)) == 1
    assert proposal_store.get(pending["proposal_id"])["status"] == "pending"
    assert proposal_store.get(rejected["proposal_id"])["status"] == "rejected"
    assert proposal_store.get(expired["proposal_id"])["status"] == "expired"
    proposal_ids = {
        pending["proposal_id"],
        rejected["proposal_id"],
        expired["proposal_id"],
    }
    _assert_proposals_are_isolated(
        engine,
        lancedb,
        proposal_ids,
        set(proposal_contents.values()),
    )

    synthesis_store = SynthesisStore(conn, engine=engine)
    draft = synthesis_store.create_draft(
        (
            "The quasarlock governance conclusion is supported by two independent "
            "operational sources and remains suitable for audited reuse."
        ),
        [SOURCE_A, SOURCE_B],
        synthesis_key="e2e:quasarlock-governance",
        validity_scope=PROJECT_ID,
        project_id=PROJECT_ID,
        visibility="project",
        actor="codex",
        call_id="call:e2e-create",
    )
    assert draft is not None
    synthesis_id = draft.memory_id
    assert draft.status == "draft"
    assert synthesis_id not in _layer_ids(_supply(engine))
    assert engine.get_memory_dict(synthesis_id) is None

    verified_v1 = synthesis_store.verify(
        synthesis_id,
        actor="reviewer",
        call_id="call:e2e-verify-v1",
        expected_revision=1,
    )
    assert verified_v1.status == "verified"
    visible_v1 = _supply(engine)
    assert synthesis_id in _layer_ids(visible_v1)
    assert _raw_ids(visible_v1)[:2] == [SOURCE_A, SOURCE_B]
    assert visible_v1.audit_metadata["synthesis_provenance"][synthesis_id][
        "verified_by_actor"
    ] == "reviewer"
    assert lancedb.inserted[-1]["memory_id"] == synthesis_id

    corrected_source = (
        "Alpha now records corrected canonical evidence after the original "
        "observation changed, forcing dependent synthesis invalidation."
    )
    assert engine.update_memory_fields(SOURCE_A, content=corrected_source) is True
    assert synthesis_store.get(synthesis_id).status == "stale"
    scan_report = scan_synthesis_integrity(engine)
    assert scan_report.stale_ids == ()
    assert synthesis_store.get(synthesis_id).status == "stale"
    assert synthesis_id not in _layer_ids(_supply(engine))
    assert engine.get_memory_dict(synthesis_id) is None

    refreshed = SynthesisStore(conn).refresh(
        synthesis_id,
        (
            "The quasarlock revision two conclusion incorporates corrected Alpha "
            "evidence and independent Beta confirmation for audited reuse."
        ),
        [SOURCE_A, SOURCE_B],
        expected_revision=1,
        actor="codex",
        call_id="call:e2e-refresh-v2",
    )
    assert (refreshed.status, refreshed.revision) == ("draft", 2)
    assert synthesis_id not in _layer_ids(_supply(engine))

    verified_v2 = SynthesisStore(conn).verify(
        synthesis_id,
        actor="reviewer-v2",
        call_id="call:e2e-verify-v2",
        expected_revision=2,
    )
    assert (verified_v2.status, verified_v2.revision) == ("verified", 2)
    assert engine._refresh_canonical_cache_if_changed(force=True) is True
    pending_actions = {
        (payload["action"], payload["revision"])
        for (payload_json,) in conn.execute(
            "SELECT payload_json FROM store_outbox "
            "WHERE tool_name = 'synthesis_index' AND status = 'pending'"
        ).fetchall()
        for payload in [json.loads(payload_json)]
    }
    assert {("delete", 1), ("upsert", 2)} <= pending_actions

    deleted_before_replay = list(lancedb.deleted)
    replay_report = replay_synthesis_index_jobs(engine)
    assert replay_report.failed == 0
    assert replay_report.succeeded >= 2
    assert lancedb.deleted == deleted_before_replay
    assert lancedb.inserted[-1]["memory_id"] == synthesis_id
    assert "quasarlock revision two" in str(lancedb.inserted[-1]["text"]).lower()
    persisted_v2 = engine._sqlite.get(synthesis_id)
    assert embedder.inputs[-1] == persisted_v2["embedding_text"]
    assert lancedb.inserted[-1]["text"] == persisted_v2["search_text"]

    visible_v2 = _supply(engine)
    assert synthesis_id in _layer_ids(visible_v2)
    assert _raw_ids(visible_v2)[:2] == [SOURCE_A, SOURCE_B]
    provenance_v2 = visible_v2.audit_metadata["synthesis_provenance"][synthesis_id]
    assert provenance_v2["revision"] == 2
    assert provenance_v2["verified_by_actor"] == "reviewer-v2"
    assert provenance_v2["verified_by_call_id"] == "call:e2e-verify-v2"
    _assert_proposals_are_isolated(
        engine,
        lancedb,
        proposal_ids,
        set(proposal_contents.values()),
    )

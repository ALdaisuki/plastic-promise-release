from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage
from plastic_promise.core.memory_index import IndexMaterial
from plastic_promise.core.memory_proposals import (
    MemoryProposalStore,
    ProposalCandidate,
    ProposalPolicyError,
    classify_proposal_candidates,
    contains_secret,
    ensure_memory_proposal_schema,
    expire_memory_proposals,
    promote_memory_proposal,
    proposal_mode,
)
from plastic_promise.mcp.tools import memory as memory_tools
from plastic_promise.mcp.tools.memory import handle_memory_store
from plastic_promise.mcp.tools.reflection import handle_feedback_apply
from plastic_promise.memory.pipeline import MemoryPipeline, PreparedMemory

UTC = timezone.utc


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def store(conn):
    return MemoryProposalStore(conn)


@pytest.fixture
def candidate():
    return ProposalCandidate(
        content="The user prefers concise technical explanations.",
        category="preference",
        project_id="project:plastic-promise",
        visibility="project",
        origin_role="user",
        origin_turn_hash="sha256:turn-one",
        origin_call_id="call_capture_one",
        origin_visibility="project",
        metadata={"session": "session:codex:test"},
    )


@pytest.fixture
def proposal_engine(tmp_path):
    storage = _SQLiteStorage(str(tmp_path / "memory-proposals.db"))
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    engine._loaded_memory_version = _version(storage._conn)
    engine.canonical_sync_ok = True
    yield engine
    memory_tools._fuzzy_buffers.pop(id(engine), None)
    memory_tools._rec_mem_cache.pop(id(engine), None)
    storage._conn.close()


def _version(conn) -> int:
    return int(conn.execute("SELECT version FROM memory_version").fetchone()[0])


def _payload(result) -> dict:
    return json.loads(result[0].text)


def _review_runtime(
    *,
    actor: str = "runtime-reviewer",
    call_id: str = "call:runtime-review",
    project_id: str = "project:plastic-promise",
    trust_score: float = 0.95,
    defense_decision: str = "allow",
) -> dict:
    return {
        "actor": actor,
        "call_id": call_id,
        "project_id": project_id,
        "trust_score": trust_score,
        "trust_tier": "high",
        "defense_decision": defense_decision,
    }


def _public_args(**overrides) -> dict:
    args = {
        "content": "The user prefers concise technical explanations.",
        "memory_type": "experience",
        "source": "user",
        "project_id": "project:plastic-promise",
        "visibility": "project",
        "source_class": "user_fact",
        "origin_role": "user",
        "origin_turn_hash": "sha256:public-turn",
        "origin_visibility": "project",
        "call_id": "call-public-store",
    }
    args.update(overrides)
    return args


def _extracted_preference(content: str):
    return [
        SimpleNamespace(
            category="preference",
            source_segment=content,
            l0_abstract="User preference for concise explanations",
            l1_summary="- The user prefers concise technical explanations.",
            l2_content=content,
            confidence=0.95,
            importance=0.9,
        )
    ]


def _prepared(content: str = "The user prefers concise technical explanations."):
    material = IndexMaterial(
        vector_text=content,
        search_text=content,
        policy="legacy",
        embedding_hash="embedding-hash",
    )
    return PreparedMemory(
        content=content,
        category="preference",
        tier="L1",
        tags=("cat:preference",),
        vector=tuple([0.25] * 1024),
        index_material=material,
        metadata={
            "domain": "uncategorized",
            "importance": 0.9,
            "raw_content": content,
            "l0_abstract": "User preference for concise explanations",
            "l1_summary": "- The user prefers concise technical explanations.",
            "l2_content": content,
        },
    )


def _proposal_for_engine(engine, *, turn_hash="sha256:proposal-turn", content=None):
    candidate = ProposalCandidate(
        content=content or "The user prefers concise technical explanations.",
        category="preference",
        project_id="project:plastic-promise",
        visibility="project",
        origin_role="user",
        origin_turn_hash=turn_hash,
        origin_call_id="call-capture",
        origin_visibility="project",
    )
    return MemoryProposalStore(engine._sqlite._conn).create_many([candidate])[0]


def _legacy_pipeline():
    pipeline = Mock()
    pipeline.store_urgent.return_value = "fuzzy_legacy"
    pipeline.process_pipeline.return_value = {
        "pipeline": {"embedded\u2192migrated": 1}
    }
    return pipeline


def test_memory_store_returns_canonical_id_after_pipeline_dedup(
    proposal_engine, monkeypatch
):
    from plastic_promise.mcp import server as mcp_server

    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "off")
    canonical_content = "Canonical survivor preserves the durable wording."
    proposal_engine.register_memory(
        {
            "id": "fuzzy_canonical",
            "content": canonical_content,
            "memory_type": "experience",
            "source": "user",
            "scope": "canonical-scope",
            "project_id": "project:plastic-promise",
            "visibility": "project",
            "source_class": "user_fact",
            "domain": "canonical-domain",
            "entity_ids": ["entity:canonical"],
        }
    )
    pipeline = _legacy_pipeline()
    pipeline.store_urgent.return_value = "fuzzy_submitted"
    pipeline.process_pipeline.return_value = {
        "pipeline": {"embedded→migrated": 0},
        "migration_outcomes": {
            "fuzzy_submitted": {
                "status": "deduplicated",
                "canonical_memory_id": "fuzzy_canonical",
            }
        },
    }
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)
    monkeypatch.setattr(
        memory_tools,
        "_extract_entity_ids",
        lambda _content, _engine: ["entity:dedup-canonical"],
    )
    notifications = []
    monkeypatch.setattr(mcp_server, "notify_issue_change", notifications.append)

    payload = _payload(asyncio.run(handle_memory_store(proposal_engine, _public_args())))

    assert payload["stored"] is True
    assert payload["memory_id"] == "fuzzy_canonical"
    assert payload["submitted_memory_id"] == "fuzzy_submitted"
    assert payload["deduplicated"] is True
    assert payload["created"] is False
    assert payload["content_preview"] == canonical_content
    assert payload["scope"] == "canonical-scope"
    assert payload["project_id"] == "project:plastic-promise"
    assert payload["visibility"] == "project"
    assert payload["source_class"] == "user_fact"
    assert payload["entity_ids"] == ["entity:canonical"]
    assert notifications == [
        {
            "type": "memory_stored",
            "memory_id": "fuzzy_canonical",
            "submitted_memory_id": "fuzzy_submitted",
            "deduplicated": True,
            "created": False,
            "content_preview": canonical_content,
            "memory_type": "experience",
            "domain": "canonical-domain",
            "project_id": "project:plastic-promise",
            "visibility": "project",
            "source_class": "user_fact",
            "timestamp": notifications[0]["timestamp"],
        }
    ]
    assert any(
        edge["from"] == "fuzzy_canonical" for edge in proposal_engine._graph_edges
    )
    assert all(edge["from"] != "fuzzy_submitted" for edge in proposal_engine._graph_edges)


def test_memory_store_does_not_borrow_another_migration_outcome(
    proposal_engine, monkeypatch
):
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "off")
    pipeline = _legacy_pipeline()
    pipeline.store_urgent.return_value = "fuzzy_missing"
    pipeline.process_pipeline.return_value = {
        "pipeline": {"embedded→migrated": 1},
        "migration_outcomes": {
            "fuzzy_other": {
                "status": "stored",
                "canonical_memory_id": "fuzzy_other",
            }
        },
    }
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)
    monkeypatch.setattr(proposal_engine, "memory_exists", lambda _memory_id: False)

    payload = _payload(asyncio.run(handle_memory_store(proposal_engine, _public_args())))

    assert payload["stored"] is False
    assert payload["memory_id"] == "fuzzy_missing"
    assert "durable migration" in payload["warnings"][-1]


def test_schema_is_idempotent_and_constrained(conn):
    ensure_memory_proposal_schema(conn)
    ensure_memory_proposal_schema(conn)

    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(memory_proposals)").fetchall()
    }

    assert {
        "proposal_id",
        "project_id",
        "visibility",
        "origin_visibility",
        "content",
        "content_hash",
        "category",
        "origin_role",
        "origin_turn_hash",
        "origin_call_id",
        "status",
        "approval_actor",
        "approval_call_id",
        "promoted_memory_id",
        "rejection_reason",
        "metadata_json",
        "expires_at",
        "redacted_at",
        "created_at",
        "updated_at",
    } <= columns


def test_legacy_unconstrained_schema_is_atomically_rebuilt_and_invalid_adoption_downgraded():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE memory_proposals (
            proposal_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'project',
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            category TEXT NOT NULL,
            origin_role TEXT NOT NULL,
            origin_turn_hash TEXT NOT NULL,
            origin_call_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            approval_actor TEXT NOT NULL DEFAULT '',
            approval_call_id TEXT NOT NULL DEFAULT '',
            promoted_memory_id TEXT NOT NULL DEFAULT '',
            rejection_reason TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            expires_at TEXT NOT NULL,
            redacted_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    now = "2026-07-10T00:00:00Z"
    expires = "2026-07-17T00:00:00Z"
    rows = [
        (
            "proposal_legacy_pending",
            "The user prefers concise technical explanations.",
            "pending",
        ),
        (
            "proposal_legacy_invalid_adopted",
            "The user decided SQLite remains canonical.",
            "adopted",
        ),
    ]
    for proposal_id, content, status in rows:
        conn.execute(
            "INSERT INTO memory_proposals "
            "(proposal_id, project_id, visibility, content, content_hash, category, "
            "origin_role, origin_turn_hash, status, expires_at, created_at, updated_at) "
            "VALUES (?, 'project:test', 'project', ?, ?, 'fact', 'user', ?, ?, ?, ?, ?)",
            (
                proposal_id,
                content,
                "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
                f"sha256:{proposal_id}",
                status,
                expires,
                now,
                now,
            ),
        )
    conn.commit()

    ensure_memory_proposal_schema(conn)

    store = MemoryProposalStore(conn)
    assert store.get("proposal_legacy_pending")["status"] == "pending"
    downgraded = store.get("proposal_legacy_invalid_adopted")
    assert downgraded["status"] == "pending"
    assert downgraded["approval_actor"] == ""
    assert downgraded["approval_call_id"] == ""
    assert downgraded["promoted_memory_id"] == ""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE memory_proposals SET status = 'adopted' "
            "WHERE proposal_id = 'proposal_legacy_pending'"
        )
    conn.rollback()
    assert "between 1 and 500" in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memory_proposals'"
    ).fetchone()[0].lower()
    conn.close()


def test_adopt_prepared_revalidates_tampered_existing_adopted_row(store, candidate):
    proposal = store.create_many([candidate])[0]
    store.adopt_prepared(
        proposal["proposal_id"],
        "mem_one",
        actor="reviewer",
        call_id="call-review",
    )
    store.conn.execute("PRAGMA ignore_check_constraints = ON")
    store.conn.execute(
        "UPDATE memory_proposals SET approval_actor = '', approval_call_id = '', "
        "promoted_memory_id = '' WHERE proposal_id = ?",
        (proposal["proposal_id"],),
    )
    store.conn.commit()
    store.conn.execute("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(ProposalPolicyError, match="invalid_adopted_proposal"):
        store.adopt_prepared(
            proposal["proposal_id"],
            "mem_two",
            actor="reviewer",
            call_id="call-replay",
        )


def test_schema_rejects_raw_policy_bypass(store, candidate):
    proposal, = store.create_many([candidate])

    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "UPDATE memory_proposals SET origin_role = 'assistant' WHERE proposal_id = ?",
            (proposal["proposal_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "UPDATE memory_proposals SET content = '' WHERE proposal_id = ?",
            (proposal["proposal_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "UPDATE memory_proposals SET status = 'adopted' WHERE proposal_id = ?",
            (proposal["proposal_id"],),
        )


def test_proposal_mode_defaults_off_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("PP_MEMORY_PROPOSALS", raising=False)
    assert proposal_mode() == "off"

    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "shadow")
    assert proposal_mode() == "shadow"

    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "unexpected")
    with pytest.raises(ProposalPolicyError, match="unknown_proposal_mode"):
        proposal_mode()


@pytest.mark.parametrize(
    "content",
    [
        "-----BEGIN PRIVATE KEY-----\nabc",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "github_pat_abcdefghijklmnopqrstuvwxyz1234567890",
        "AKIAABCDEFGHIJKLMNOP",
        "password = CorrectHorseBatteryStaple",
        "postgresql://admin:secret@db.internal/app",
    ],
)
def test_common_secret_shapes_are_detected(content):
    assert contains_secret(content)


def test_configured_secret_regex_is_detected(monkeypatch):
    monkeypatch.setenv(
        "PP_MEMORY_PROPOSAL_SECRET_PATTERNS",
        '["internal-code-[0-9]{4}"]',
    )

    assert contains_secret("The internal-code-4821 is reserved")
    assert not contains_secret("This ordinary preference is safe to retain")


def test_candidate_accepts_normalized_nonempty_and_500_but_rejects_outside_bounds(
    store, candidate
):
    now = datetime(2026, 7, 10, tzinfo=UTC)

    assert store.create_many([replace(candidate, content="x")], now=now)[0][
        "status"
    ] == "pending"
    assert store.create_many(
        [
            replace(
                candidate,
                content="x" * 500,
                origin_turn_hash="sha256:turn-two",
            )
        ],
        now=now,
    )[0]["status"] == "pending"

    with pytest.raises(ProposalPolicyError, match="empty_content"):
        store.create_many(
            [replace(candidate, content="  ", origin_turn_hash="sha256:empty")]
        )
    with pytest.raises(ProposalPolicyError, match="content_too_long"):
        store.create_many(
            [replace(candidate, content="x" * 501, origin_turn_hash="sha256:long")]
        )


def test_create_many_normalizes_and_deduplicates_fingerprint(store, candidate):
    first, = store.create_many([candidate])
    replay, = store.create_many(
        [replace(candidate, content="  The user prefers concise   technical explanations.  ")]
    )

    assert replay["proposal_id"] == first["proposal_id"]
    assert replay["content_hash"] == first["content_hash"]
    assert replay["content"] == "The user prefers concise technical explanations."
    assert store.conn.execute(
        "SELECT COUNT(*) FROM memory_proposals"
    ).fetchone()[0] == 1


def test_create_many_deduplicates_batch_and_caps_at_five(store, candidate):
    duplicate_batch = [candidate, replace(candidate, content=f"  {candidate.content}  ")]
    assert len(store.create_many(duplicate_batch)) == 1

    too_many = [
        replace(
            candidate,
            content=f"The user selected durable option number {index:02d}.",
            origin_turn_hash="sha256:one-bounded-turn",
        )
        for index in range(6)
    ]
    with pytest.raises(ProposalPolicyError, match="too_many_candidates"):
        store.create_many(too_many)


def test_invalid_later_candidate_leaves_no_partial_batch(store, candidate):
    invalid = replace(
        candidate,
        content="The assistant inferred this preference from context.",
        origin_role="assistant",
        origin_turn_hash="sha256:second-turn",
    )

    with pytest.raises(ProposalPolicyError, match="user_origin_required"):
        store.create_many([candidate, invalid])

    assert store.conn.execute(
        "SELECT COUNT(*) FROM memory_proposals"
    ).fetchone()[0] == 0


def test_secret_is_rejected_before_plaintext_persistence(store, candidate):
    with pytest.raises(ProposalPolicyError, match="secret_detected"):
        store.create_many(
            [replace(candidate, content="ghp_abcdefghijklmnopqrstuvwxyz123456")]
        )

    assert store.conn.execute(
        "SELECT COUNT(*) FROM memory_proposals"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"transcript": "User: durable preference"}, "metadata_payload_rejected"),
        ({"nested": {"raw_content": "full turn"}}, "metadata_payload_rejected"),
        ({"audit_note": "Assistant: inferred a private preference"}, "metadata_payload_rejected"),
        ({"audit_note": "password = CorrectHorseBatteryStaple"}, "secret_detected"),
        ({"audit_note": "x" * 5000}, "metadata_too_large"),
    ],
)
def test_metadata_cannot_bypass_plaintext_or_secret_policy(
    store, candidate, metadata, reason
):
    with pytest.raises(ProposalPolicyError, match=reason):
        store.create_many([replace(candidate, metadata=metadata)])

    assert store.conn.execute(
        "SELECT COUNT(*) FROM memory_proposals"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"category": "entity"}, "unknown_category"),
        ({"origin_role": "system"}, "user_origin_required"),
        ({"origin_turn_hash": ""}, "origin_turn_hash_required"),
        ({"project_id": ""}, "project_id_required"),
        ({"visibility": "global"}, "visibility_widening"),
        ({"visibility": "unknown"}, "invalid_visibility"),
    ],
)
def test_candidate_policy_fails_closed(store, candidate, changes, reason):
    with pytest.raises(ProposalPolicyError, match=reason):
        store.create_many([replace(candidate, **changes)])


def test_expiry_redacts_plaintext_but_retains_hash(store, candidate):
    created_at = datetime(2026, 7, 1, 12, 30, tzinfo=UTC)
    proposal, = store.create_many([candidate], now=created_at)

    assert proposal["created_at"].endswith("Z")
    assert proposal["expires_at"].endswith("Z")
    assert store.expire_and_redact(now=created_at + timedelta(days=8)) == 1

    row = store.get(proposal["proposal_id"])
    assert row["status"] == "expired"
    assert row["content"] == ""
    assert row["content_hash"]
    assert row["redacted_at"].endswith("Z")
    assert store.expire_and_redact(now=created_at + timedelta(days=8)) == 0


def test_reject_requires_actor_call_and_is_idempotent(store, candidate):
    proposal, = store.create_many([candidate])

    with pytest.raises(ProposalPolicyError, match="review_actor_required"):
        store.reject(proposal["proposal_id"], actor="", call_id="call_review")
    with pytest.raises(ProposalPolicyError, match="review_call_id_required"):
        store.reject(proposal["proposal_id"], actor="reviewer", call_id="")

    rejected = store.reject(
        proposal["proposal_id"],
        actor="reviewer",
        call_id="call_review",
        reason="not_durable",
    )
    replay = store.reject(
        proposal["proposal_id"],
        actor="another-reviewer",
        call_id="call_replay",
        reason="different_reason",
    )

    assert rejected["status"] == "rejected"
    assert rejected["content"] == ""
    assert rejected["approval_actor"] == "reviewer"
    assert rejected["approval_call_id"] == "call_review"
    assert rejected["rejection_reason"] == "not_durable"
    assert replay == rejected


@pytest.mark.parametrize(
    "unsafe_reason",
    [
        "User: preserve this entire private transcript",
        "password = CorrectHorseBatteryStaple",
        "an arbitrary reviewer paragraph with user plaintext",
    ],
)
def test_reject_persists_only_bounded_reason_codes(store, candidate, unsafe_reason):
    proposal = store.create_many([candidate])[0]

    rejected = store.reject(
        proposal["proposal_id"],
        actor="reviewer",
        call_id="call-review",
        reason=unsafe_reason,
    )

    assert rejected["rejection_reason"] == "reviewer_rejected"
    persisted = json.dumps(rejected, ensure_ascii=False)
    assert unsafe_reason not in persisted


def test_adopt_prepared_requires_evidence_and_is_idempotent(store, candidate):
    proposal, = store.create_many([candidate])

    with pytest.raises(ProposalPolicyError, match="review_actor_required"):
        store.adopt_prepared(
            proposal["proposal_id"],
            actor="",
            call_id="call_review",
            promoted_memory_id="mem_one",
        )
    with pytest.raises(ProposalPolicyError, match="promoted_memory_id_required"):
        store.adopt_prepared(
            proposal["proposal_id"],
            actor="reviewer",
            call_id="call_review",
            promoted_memory_id="",
        )

    adopted = store.adopt_prepared(
        proposal["proposal_id"],
        actor="reviewer",
        call_id="call_review",
        promoted_memory_id="mem_one",
    )
    replay = store.adopt_prepared(
        proposal["proposal_id"],
        actor="another-reviewer",
        call_id="call_replay",
        promoted_memory_id="mem_two",
    )

    assert adopted["status"] == "adopted"
    assert adopted["approval_actor"] == "reviewer"
    assert adopted["approval_call_id"] == "call_review"
    assert adopted["promoted_memory_id"] == "mem_one"
    assert replay == adopted


def test_adopt_prepared_participates_in_existing_transaction(conn, store, candidate):
    proposal, = store.create_many([candidate])
    conn.execute("CREATE TABLE promotion_probe (memory_id TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="rollback promotion"), conn:
        conn.execute("INSERT INTO promotion_probe VALUES ('mem_one')")
        store.adopt_prepared(
            proposal["proposal_id"],
            "mem_one",
            actor="reviewer",
            call_id="call_review",
        )
        raise RuntimeError("rollback promotion")

    assert conn.execute("SELECT COUNT(*) FROM promotion_probe").fetchone()[0] == 0
    assert store.get(proposal["proposal_id"])["status"] == "pending"


def test_expired_pending_proposal_cannot_be_adopted_and_is_redacted(store, candidate):
    created_at = datetime(2026, 7, 1, tzinfo=UTC)
    proposal, = store.create_many([candidate], now=created_at)

    with pytest.raises(ProposalPolicyError, match="proposal_expired"):
        store.adopt_prepared(
            proposal["proposal_id"],
            actor="reviewer",
            call_id="call_review",
            promoted_memory_id="mem_one",
            now=created_at + timedelta(days=7, seconds=1),
        )

    expired = store.get(proposal["proposal_id"])
    assert expired["status"] == "expired"
    assert expired["content"] == ""
    assert expired["content_hash"]


def test_classifier_extracts_atomic_user_candidates_without_transcript():
    extracted = [
        SimpleNamespace(
            category="preference",
            source_segment="The user prefers compact technical status reports.",
            confidence=0.91,
        ),
        SimpleNamespace(
            category="decision",
            source_segment="The user decided SQLite remains the canonical store.",
            confidence=0.88,
        ),
    ]

    result = classify_proposal_candidates(
        "A bounded user turn that contains two durable statements.",
        extract=lambda _: extracted,
        project_id="project:plastic-promise",
        visibility="project",
        origin_visibility="project",
        origin_turn_hash="sha256:classifier-turn",
        origin_call_id="call_classifier",
    )

    assert result.decision == "propose"
    assert result.reason_codes == ()
    assert [item.category for item in result.candidates] == [
        "preference",
        "decision",
    ]
    assert all("Assistant:" not in item.content for item in result.candidates)


@pytest.mark.parametrize(
    "extracted",
    [
        [],
        [SimpleNamespace(category="entity", source_segment="A durable entity value.", confidence=0.9)],
        [SimpleNamespace(category="fact", source_segment="A durable but uncertain fact.", confidence=0.49)],
        [SimpleNamespace(category="fact", source_segment="A non-finite confidence fact.", confidence=float("nan"))],
        [SimpleNamespace(category="fact", source_segment="Assistant: inferred durable fact.", confidence=0.9)],
        [
            SimpleNamespace(
                category="fact",
                source_segment=f"The user supplied bounded fact number {index:02d}.",
                confidence=0.9,
            )
            for index in range(6)
        ],
    ],
)
def test_uncertain_classification_fails_closed(extracted):
    result = classify_proposal_candidates(
        "Conversation input",
        extract=lambda _: extracted,
    )

    assert result.decision == "reject"
    assert result.candidates == ()
    assert result.reason_codes == ("proposal_classification_uncertain",)


def test_classifier_secret_scan_precedes_extractor():
    calls = []

    result = classify_proposal_candidates(
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        extract=lambda _: calls.append("called"),
    )

    assert result.decision == "reject"
    assert result.reason_codes == ("secret_detected",)
    assert calls == []


def test_off_keeps_legacy_write_without_proposal_classification(
    proposal_engine, monkeypatch
):
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "off")
    pipeline = _legacy_pipeline()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)
    monkeypatch.setattr(
        memory_tools,
        "classify_proposal_candidates",
        Mock(side_effect=AssertionError("off mode classified proposal content")),
    )

    payload = _payload(asyncio.run(handle_memory_store(proposal_engine, _public_args())))

    assert payload["stored"] is True
    assert "proposal_shadow" not in payload
    pipeline.store_urgent.assert_called_once()
    assert proposal_engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM memory_proposals"
    ).fetchone()[0] == 0


def test_shadow_keeps_legacy_write_and_emits_hash_only_diagnostic(
    proposal_engine, monkeypatch
):
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "shadow")
    monkeypatch.setattr(
        "plastic_promise.smart_extractor.extract_memories",
        lambda content, **_kwargs: _extracted_preference(content),
    )
    pipeline = _legacy_pipeline()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    payload = _payload(asyncio.run(handle_memory_store(proposal_engine, _public_args())))

    assert payload["stored"] is True
    shadow = payload["proposal_shadow"]
    assert shadow["content_hash"] == "sha256:" + hashlib.sha256(
        _public_args()["content"].encode("utf-8")
    ).hexdigest()
    assert shadow["would_propose"] is True
    assert "content" not in shadow
    assert _public_args()["content"] not in json.dumps(shadow)
    pipeline.store_urgent.assert_called_once()
    assert proposal_engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM memory_proposals"
    ).fetchone()[0] == 0


def test_on_routes_public_user_fact_before_pipeline_and_ignores_commit_mode(
    proposal_engine, monkeypatch
):
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")
    monkeypatch.setattr(
        "plastic_promise.smart_extractor.extract_memories",
        lambda content, **_kwargs: _extracted_preference(content),
    )
    pipeline = _legacy_pipeline()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    payload = _payload(
        asyncio.run(
            handle_memory_store(
                proposal_engine,
                _public_args(commit_mode="direct"),
            )
        )
    )

    assert payload["stored"] is False
    assert payload["status"] == "pending"
    assert len(payload["proposal_ids"]) == 1
    pipeline.store_urgent.assert_not_called()
    row = MemoryProposalStore(proposal_engine._sqlite._conn).get(
        payload["proposal_ids"][0]
    )
    assert row["origin_role"] == "user"
    assert row["content"] == _public_args()["content"]


def test_public_call_cannot_spoof_internal_origin_with_schema_fields(
    proposal_engine, monkeypatch
):
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")
    monkeypatch.setattr(
        "plastic_promise.smart_extractor.extract_memories",
        lambda content, **_kwargs: _extracted_preference(content),
    )
    pipeline = _legacy_pipeline()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    payload = _payload(
        asyncio.run(
            handle_memory_store(
                proposal_engine,
                _public_args(
                    source="system",
                    source_class="telemetry",
                    origin_role="system",
                    origin_kind="audit",
                    commit_mode="direct",
                ),
            )
        )
    )

    assert payload["status"] == "pending"
    pipeline.store_urgent.assert_not_called()


@pytest.mark.parametrize(
    ("source", "origin_kind", "origin_uri"),
    [
        ("auto_context_inject", "auto_context_inject", "mcp://auto_context_inject"),
        ("memory_sync_files", "memory_sync_files", "mcp://memory_sync_files"),
        ("skill_session_complete", "skill_artifact", "mcp://skill_session_complete"),
    ],
)
def test_public_route_names_cannot_create_trusted_memory_origin(
    proposal_engine,
    monkeypatch,
    source,
    origin_kind,
    origin_uri,
):
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")
    monkeypatch.setattr(
        "plastic_promise.smart_extractor.extract_memories",
        lambda content, **_kwargs: _extracted_preference(content),
    )
    pipeline = _legacy_pipeline()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    payload = _payload(
        asyncio.run(
            handle_memory_store(
                proposal_engine,
                _public_args(
                    source=source,
                    source_class="telemetry",
                    origin_role="system",
                    origin_kind=origin_kind,
                    origin_uri=origin_uri,
                    commit_mode="direct",
                ),
            )
        )
    )

    assert payload["status"] == "pending"
    pipeline.store_urgent.assert_not_called()


def test_trusted_runtime_route_preserves_internal_experience_path(
    proposal_engine, monkeypatch
):
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")
    pipeline = _legacy_pipeline()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    with memory_tools._trusted_memory_origin("audit_run"):
        payload = _payload(
            asyncio.run(
                handle_memory_store(
                    proposal_engine,
                    _public_args(
                        content="[AUDIT] durable compliance event",
                        source="system",
                        source_class="telemetry",
                        origin_kind="audit",
                    ),
                )
            )
        )

    assert payload["stored"] is True
    pipeline.store_urgent.assert_called_once()
    assert proposal_engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM memory_proposals"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("content", "extractor", "reason"),
    [
        (
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            lambda _content, **_kwargs: (_ for _ in ()).throw(
                AssertionError("secret reached extractor")
            ),
            "secret_detected",
        ),
        (
            "This statement cannot be classified with sufficient certainty.",
            lambda _content, **_kwargs: [],
            "proposal_classification_uncertain",
        ),
    ],
)
def test_on_policy_failure_never_reaches_pipeline_or_generic_outbox(
    proposal_engine, monkeypatch, content, extractor, reason
):
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")
    monkeypatch.setattr(
        "plastic_promise.smart_extractor.extract_memories",
        extractor,
    )
    pipeline = _legacy_pipeline()
    outbox = Mock(side_effect=AssertionError("proposal failure reached generic outbox"))
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)
    monkeypatch.setattr(memory_tools, "record_outbox_event", outbox)

    payload = _payload(
        asyncio.run(
            handle_memory_store(
                proposal_engine,
                _public_args(content=content),
            )
        )
    )

    assert payload["stored"] is False
    assert payload["status"] == "rejected"
    assert payload["reason"] == reason
    pipeline.store_urgent.assert_not_called()
    outbox.assert_not_called()
    assert proposal_engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM store_outbox"
    ).fetchone()[0] == 0


def test_pipeline_rejects_unapproved_protected_user_content_before_extraction(
    monkeypatch,
):
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")
    monkeypatch.setattr(
        "plastic_promise.smart_extractor.extract_memories",
        Mock(side_effect=AssertionError("protected content reached extraction")),
    )
    pipeline = MemoryPipeline()

    with pytest.raises(ProposalPolicyError, match="approval_required"):
        pipeline.store_urgent(
            "user fact",
            source="user",
            source_class="user_fact",
        )

    assert pipeline._buffer == {}


def test_prepare_approved_candidate_is_pure_and_immutable(monkeypatch):
    content = "The user prefers concise technical explanations."
    monkeypatch.setattr(
        "plastic_promise.smart_extractor.extract_memories",
        lambda value, **_kwargs: _extracted_preference(value),
    )
    rec_mem = Mock()
    lancedb = Mock()
    embedder = SimpleNamespace(
        model_name="test-embedder",
        embed=lambda _text: [0.25] * 1024,
    )
    pipeline = MemoryPipeline(rec_mem=rec_mem, embedder=embedder, lancedb=lancedb)

    prepared = pipeline.prepare_approved_candidate(
        content,
        category="preference",
        source="user",
        source_class="user_fact",
    )

    assert prepared.content == content
    assert prepared.category == "preference"
    assert prepared.vector == tuple([0.25] * 1024)
    assert prepared.index_material.vector_text
    assert pipeline._buffer == {}
    rec_mem.assert_not_called()
    lancedb.assert_not_called()
    with pytest.raises((AttributeError, TypeError)):
        prepared.content = "mutated"


def test_promotion_failure_rolls_back_memory_proposal_lineage_and_version(
    proposal_engine, monkeypatch
):
    proposal = _proposal_for_engine(proposal_engine)
    conn = proposal_engine._sqlite._conn
    before_version = _version(conn)
    pipeline = Mock()
    pipeline.prepare_approved_candidate.return_value = _prepared()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    def raise_error(*_args, **_kwargs):
        raise RuntimeError("lineage failed")

    monkeypatch.setattr(
        "plastic_promise.core.traceability.record_memory_lineage",
        raise_error,
    )

    with pytest.raises(RuntimeError, match="lineage failed"):
        promote_memory_proposal(
            proposal_engine,
            proposal["proposal_id"],
            actor="reviewer",
            call_id="call-review",
        )

    assert conn.execute(
        "SELECT 1 FROM memories WHERE origin_ref = ?",
        (proposal["proposal_id"],),
    ).fetchone() is None
    assert MemoryProposalStore(conn).get(proposal["proposal_id"])["status"] == "pending"
    assert conn.execute(
        "SELECT 1 FROM memory_lineage WHERE parent_memory_id = ?",
        (proposal["proposal_id"],),
    ).fetchone() is None
    assert _version(conn) == before_version


def test_promotion_index_job_failure_rolls_back_adoption_and_canonical_memory(
    proposal_engine, monkeypatch
):
    proposal = _proposal_for_engine(proposal_engine)
    conn = proposal_engine._sqlite._conn
    before_version = _version(conn)
    pipeline = Mock()
    pipeline.prepare_approved_candidate.return_value = _prepared()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    def raise_error(*_args, **_kwargs):
        raise RuntimeError("index job publish failed")

    monkeypatch.setattr(
        "plastic_promise.core.traceability.enqueue_memory_index_upsert",
        raise_error,
    )

    with pytest.raises(RuntimeError, match="index job publish failed"):
        promote_memory_proposal(
            proposal_engine,
            proposal["proposal_id"],
            actor="reviewer",
            call_id="call-review",
        )

    assert MemoryProposalStore(conn).get(proposal["proposal_id"])["status"] == "pending"
    assert conn.execute(
        "SELECT 1 FROM memories WHERE origin_ref = ?",
        (proposal["proposal_id"],),
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM memory_lineage WHERE parent_memory_id = ?",
        (proposal["proposal_id"],),
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM store_outbox WHERE tool_name = 'memory_index'"
    ).fetchone() is None
    assert _version(conn) == before_version
    db_path = next(
        row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"
    )
    reopened = sqlite3.connect(db_path)
    try:
        assert reopened.execute(
            "SELECT status FROM memory_proposals WHERE proposal_id = ?",
            (proposal["proposal_id"],),
        ).fetchone() == ("pending",)
        assert reopened.execute(
            "SELECT 1 FROM memories WHERE origin_ref = ?",
            (proposal["proposal_id"],),
        ).fetchone() is None
        assert reopened.execute(
            "SELECT 1 FROM store_outbox WHERE tool_name = 'memory_index'"
        ).fetchone() is None
    finally:
        reopened.close()


def test_promotion_retry_is_idempotent_deterministic_and_enqueues_after_commit(
    proposal_engine, monkeypatch
):
    proposal = _proposal_for_engine(proposal_engine)
    conn = proposal_engine._sqlite._conn
    before_version = _version(conn)
    pipeline = Mock()
    pipeline.prepare_approved_candidate.return_value = _prepared()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    first = promote_memory_proposal(
        proposal_engine,
        proposal["proposal_id"],
        actor="reviewer",
        call_id="call-review",
    )
    second = promote_memory_proposal(
        proposal_engine,
        proposal["proposal_id"],
        actor="other-reviewer",
        call_id="call-replay",
    )

    expected_id = "proposal_mem_" + hashlib.sha256(
        proposal["proposal_id"].encode("utf-8")
    ).hexdigest()[:20]
    assert first.memory_id == second.memory_id == expected_id
    assert first.status == second.status == "adopted"
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE origin_ref = ?",
        (proposal["proposal_id"],),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_lineage "
        "WHERE memory_id = ? AND parent_memory_id = ? "
        "AND relation = 'promoted_from_proposal'",
        (expected_id, proposal["proposal_id"]),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM store_outbox "
        "WHERE tool_name = 'memory_index' AND status = 'pending'",
    ).fetchone()[0] == 1
    job_payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM store_outbox WHERE tool_name = 'memory_index'"
        ).fetchone()[0]
    )
    assert job_payload["memory_version"] == before_version + 1
    assert job_payload["material_revision"] == "embedding-hash"
    assert not conn.in_transaction
    assert _version(conn) == before_version + 1
    pipeline.prepare_approved_candidate.assert_called_once()


def test_promotion_exact_dedup_reuses_existing_canonical_id_and_replay(
    proposal_engine, monkeypatch
):
    content = "The user prefers concise technical explanations."
    turn_hash = "sha256:existing-origin-turn"
    proposal = _proposal_for_engine(
        proposal_engine,
        turn_hash=turn_hash,
        content=content,
    )
    storage = proposal_engine._sqlite
    storage.upsert(
        "existing-memory",
        {
            "id": "existing-memory",
            "content": content,
            "memory_type": "experience",
            "source": "user",
            "tier": "L1",
            "scope": "global",
            "category": "preference",
            "tags": ["cat:preference"],
            "domain": "uncategorized",
            "project_id": "project:plastic-promise",
            "visibility": "project",
            "source_class": "preference",
            "origin_kind": "memory_proposal",
            "origin_uri": "proposal://older",
            "origin_ref": "proposal_older",
            "origin_hash": turn_hash,
            "metadata_json": {
                "memory_index": {
                    "policy": "legacy",
                    "embedding_hash": "embedding-hash",
                }
            },
            "embedding_text": content,
            "embedding_hash": "embedding-hash",
            "search_text": content,
        },
    )
    conn = storage._conn
    before_version = _version(conn)
    pipeline = Mock()
    pipeline.prepare_approved_candidate.return_value = _prepared(content)
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    first = promote_memory_proposal(
        proposal_engine,
        proposal["proposal_id"],
        actor="reviewer",
        call_id="call-dedup",
    )
    replay = promote_memory_proposal(
        proposal_engine,
        proposal["proposal_id"],
        actor="reviewer",
        call_id="call-dedup-replay",
    )

    assert first.memory_id == replay.memory_id == "existing-memory"
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE project_id = ? AND content = ? "
        "AND origin_hash = ?",
        ("project:plastic-promise", content, turn_hash),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_lineage "
        "WHERE memory_id = 'existing-memory' AND parent_memory_id = ? "
        "AND relation = 'promoted_from_proposal'",
        (proposal["proposal_id"],),
    ).fetchone()[0] == 1
    assert MemoryProposalStore(conn).get(proposal["proposal_id"])[
        "promoted_memory_id"
    ] == "existing-memory"
    assert _version(conn) == before_version + 1
    pipeline.prepare_approved_candidate.assert_called_once()


def test_promotion_preparation_failure_leaves_proposal_pending(
    proposal_engine, monkeypatch
):
    proposal = _proposal_for_engine(proposal_engine)
    pipeline = Mock()
    pipeline.prepare_approved_candidate.side_effect = RuntimeError("embedding failed")
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    with pytest.raises(RuntimeError, match="embedding failed"):
        promote_memory_proposal(
            proposal_engine,
            proposal["proposal_id"],
            actor="reviewer",
            call_id="call-review",
        )

    conn = proposal_engine._sqlite._conn
    assert MemoryProposalStore(conn).get(proposal["proposal_id"])["status"] == "pending"
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE origin_ref = ?",
        (proposal["proposal_id"],),
    ).fetchone()[0] == 0


def test_feedback_apply_promotes_rejects_and_refuses_ignored_proposals(
    proposal_engine, monkeypatch
):
    adopted = _proposal_for_engine(proposal_engine, turn_hash="sha256:adopt")
    rejected = _proposal_for_engine(
        proposal_engine,
        turn_hash="sha256:reject",
        content="The user decided SQLite remains the canonical memory store.",
    )
    ignored = _proposal_for_engine(
        proposal_engine,
        turn_hash="sha256:ignored",
        content="The user prefers explicit evidence in every review report.",
    )
    pipeline = Mock()
    pipeline.prepare_approved_candidate.return_value = _prepared()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    adopted_payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                proposal_engine,
                {
                    "item_id": adopted["proposal_id"],
                    "feedback_type": "adopted",
                    "actor": "declared-reviewer",
                    "call_id": "call:declared-adopt",
                },
                _runtime_context=_review_runtime(call_id="call:runtime-adopt"),
            )
        )
    )
    rejected_payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                proposal_engine,
                {
                    "item_id": rejected["proposal_id"],
                    "feedback_type": "rejected",
                    "actor": "reviewer",
                    "call_id": "call-reject",
                    "rejection_reason": "not durable",
                },
                _runtime_context=_review_runtime(call_id="call:runtime-reject"),
            )
        )
    )
    ignored_payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                proposal_engine,
                {
                    "item_id": ignored["proposal_id"],
                    "feedback_type": "ignored",
                    "actor": "reviewer",
                    "call_id": "call-ignore",
                },
            )
        )
    )

    assert adopted_payload["updated"] is True
    assert adopted_payload["status"] == "adopted"
    assert adopted_payload["memory_id"].startswith("proposal_mem_")
    assert rejected_payload["updated"] is True
    assert rejected_payload["status"] == "rejected"
    adopted_row = MemoryProposalStore(proposal_engine._sqlite._conn).get(
        adopted["proposal_id"]
    )
    rejected_row = MemoryProposalStore(proposal_engine._sqlite._conn).get(
        rejected["proposal_id"]
    )
    assert adopted_row["approval_actor"] == "runtime-reviewer"
    assert adopted_row["approval_call_id"] == "call:runtime-adopt"
    assert rejected_row["approval_actor"] == "runtime-reviewer"
    assert rejected_row["approval_call_id"] == "call:runtime-reject"
    assert rejected_row["content"] == ""
    assert ignored_payload == {
        "updated": False,
        "item_id": ignored["proposal_id"],
        "feedback_type": "ignored",
        "reason": "proposal_feedback_ignored_not_allowed",
    }


@pytest.mark.parametrize("feedback_type", ["adopted", "rejected"])
def test_feedback_proposal_public_reviewer_fields_cannot_authorize(
    proposal_engine,
    feedback_type,
):
    proposal = _proposal_for_engine(
        proposal_engine,
        turn_hash=f"sha256:forged-{feedback_type}",
    )

    payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                proposal_engine,
                {
                    "item_id": proposal["proposal_id"],
                    "feedback_type": feedback_type,
                    "actor": "forged-reviewer",
                    "call_id": "call:forged",
                    "project_id": "project:other",
                    "trust_score": 1.0,
                    "trust_tier": "high",
                    "defense_decision": "allow",
                    "_runtime_context": _review_runtime(),
                },
            )
        )
    )

    assert payload["updated"] is False
    assert payload["reason"] == "feedback_runtime_authorization_required"
    assert MemoryProposalStore(proposal_engine._sqlite._conn).get(
        proposal["proposal_id"]
    )["status"] == "pending"


@pytest.mark.parametrize(
    "runtime",
    [
        _review_runtime(trust_score=0.20),
        _review_runtime(defense_decision="deny"),
        _review_runtime(defense_decision="ask"),
    ],
)
def test_feedback_proposal_requires_trust_and_allow_decision(
    proposal_engine,
    runtime,
):
    proposal = _proposal_for_engine(
        proposal_engine,
        turn_hash=f"sha256:runtime-denied-{runtime['defense_decision']}-{runtime['trust_score']}",
    )

    payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                proposal_engine,
                {
                    "item_id": proposal["proposal_id"],
                    "feedback_type": "adopted",
                },
                _runtime_context=runtime,
            )
        )
    )

    assert payload["updated"] is False
    assert payload["reason"] == "feedback_runtime_authorization_denied"
    assert MemoryProposalStore(proposal_engine._sqlite._conn).get(
        proposal["proposal_id"]
    )["status"] == "pending"


def test_feedback_proposal_rejects_unconfigured_mcp_runtime_actor(proposal_engine):
    proposal = _proposal_for_engine(
        proposal_engine,
        turn_hash="sha256:generic-mcp-runtime-actor",
    )

    payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                proposal_engine,
                {
                    "item_id": proposal["proposal_id"],
                    "feedback_type": "adopted",
                },
                _runtime_context=_review_runtime(actor="mcp", trust_score=1.0),
            )
        )
    )

    assert payload["updated"] is False
    assert payload["reason"] == "feedback_runtime_authorization_required"
    assert MemoryProposalStore(proposal_engine._sqlite._conn).get(
        proposal["proposal_id"]
    )["status"] == "pending"


def test_feedback_proposal_binds_runtime_project(proposal_engine):
    proposal = _proposal_for_engine(
        proposal_engine,
        turn_hash="sha256:runtime-project-mismatch",
    )

    payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                proposal_engine,
                {
                    "item_id": proposal["proposal_id"],
                    "feedback_type": "rejected",
                    "project_id": "project:plastic-promise",
                },
                _runtime_context=_review_runtime(project_id="project:other"),
            )
        )
    )

    assert payload["updated"] is False
    assert payload["reason"] == "feedback_project_mismatch"
    assert MemoryProposalStore(proposal_engine._sqlite._conn).get(
        proposal["proposal_id"]
    )["status"] == "pending"


def test_feedback_runtime_actor_ignores_untrusted_client_info(monkeypatch):
    from plastic_promise.mcp import server as mcp_server

    forged_client = SimpleNamespace(
        request_context=SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    clientInfo=SimpleNamespace(name="codex-desktop")
                )
            )
        )
    )
    monkeypatch.setattr(mcp_server, "server", forged_client)
    monkeypatch.delenv("PP_MCP_RUNTIME_ACTOR", raising=False)

    assert mcp_server._feedback_runtime_actor() == "mcp"

    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "claude-code")
    assert mcp_server._feedback_runtime_actor() == "claude"


def test_feedback_server_dispatch_uses_private_runtime_authority(
    proposal_engine,
    monkeypatch,
):
    from plastic_promise.mcp import server as mcp_server
    from plastic_promise.mcp.tools import audit_defense

    class RuntimeTrust:
        def get(self, target=""):
            return 0.95 if target == "codex" else 0.20

        def tier(self, target=""):
            return "high" if target == "codex" else "critical"

    proposal = _proposal_for_engine(
        proposal_engine,
        turn_hash="sha256:server-runtime-authority",
    )
    pipeline = Mock()
    pipeline.prepare_approved_candidate.return_value = _prepared()
    runtime_events = []

    def capture_runtime_event(_engine, context, status):
        runtime_events.append((status, context))

    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: proposal_engine)
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", capture_runtime_event)
    monkeypatch.setattr(audit_defense, "_get_trust_manager", lambda: RuntimeTrust())
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setenv("PLASTIC_PROJECT_ID", "project:plastic-promise")

    payload = _payload(
        asyncio.run(
            mcp_server.call_tool(
                "feedback_apply",
                {
                    "item_id": proposal["proposal_id"],
                    "feedback_type": "adopted",
                    "actor": "forged-reviewer",
                    "call_id": "call:forged",
                    "project_id": "project:other",
                    "trust_score": 1.0,
                    "trust_tier": "high",
                    "defense_decision": "allow",
                },
            )
        )
    )

    assert payload["updated"] is True
    row = MemoryProposalStore(proposal_engine._sqlite._conn).get(
        proposal["proposal_id"]
    )
    assert row["approval_actor"] == "codex"
    assert row["approval_call_id"]
    assert row["approval_call_id"] != "call:forged"
    assert [status for status, _context in runtime_events] == [
        "pending",
        "running",
        "completed",
    ]
    for _status, context in runtime_events:
        assert context["actor"] == "codex"
        assert context["project_id"] == "project:plastic-promise"
        assert context["defense_decision"] == "allow"
        assert context["metadata"]["caller_declarations"] == {
            "actor": "forged-reviewer",
            "call_id": "call:forged",
            "project_id": "project:other",
            "trust_score": 1.0,
            "trust_tier": "high",
            "defense_decision": "allow",
        }
        assert context["audit_trace"]["runtime_call_id"] == row["approval_call_id"]


def test_feedback_rejection_never_persists_or_echoes_secret_task_context(
    proposal_engine,
):
    proposal = _proposal_for_engine(proposal_engine, turn_hash="sha256:secret-reject")
    secret = "password = CorrectHorseBatteryStaple"

    payload = _payload(
        asyncio.run(
            handle_feedback_apply(
                proposal_engine,
                {
                    "item_id": proposal["proposal_id"],
                    "feedback_type": "rejected",
                    "actor": "reviewer",
                    "call_id": "call-secret-reject",
                    "rejection_reason": secret,
                    "task_context": f"User: {secret}",
                },
                _runtime_context=_review_runtime(call_id="call:runtime-secret-reject"),
            )
        )
    )

    row = MemoryProposalStore(proposal_engine._sqlite._conn).get(
        proposal["proposal_id"]
    )
    assert payload["updated"] is True
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert row["rejection_reason"] == "reviewer_rejected"
    assert secret not in json.dumps(row, ensure_ascii=False)
    outbox_rows = proposal_engine._sqlite._conn.execute(
        "SELECT payload_json, error_message, metadata_json FROM store_outbox"
    ).fetchall()
    assert secret not in json.dumps(outbox_rows, ensure_ascii=False)


def test_pending_but_expired_proposal_cannot_be_promoted_before_daemon(
    proposal_engine, monkeypatch
):
    proposal = _proposal_for_engine(proposal_engine)
    conn = proposal_engine._sqlite._conn
    conn.execute(
        "UPDATE memory_proposals SET expires_at = '2000-01-01T00:00:00Z' "
        "WHERE proposal_id = ?",
        (proposal["proposal_id"],),
    )
    conn.commit()
    pipeline = Mock()
    pipeline.prepare_approved_candidate.return_value = _prepared()
    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", lambda _engine: pipeline)

    with pytest.raises(ProposalPolicyError, match="proposal_expired"):
        promote_memory_proposal(
            proposal_engine,
            proposal["proposal_id"],
            actor="reviewer",
            call_id="call-review",
        )

    expired = MemoryProposalStore(conn).get(proposal["proposal_id"])
    assert expired["status"] == "expired"
    assert expired["content"] == ""
    pipeline.prepare_approved_candidate.assert_not_called()


def test_expire_memory_proposals_bridge_redacts_bounded_batch(proposal_engine):
    proposal = _proposal_for_engine(proposal_engine)
    conn = proposal_engine._sqlite._conn
    conn.execute(
        "UPDATE memory_proposals SET expires_at = '2000-01-01T00:00:00Z' "
        "WHERE proposal_id = ?",
        (proposal["proposal_id"],),
    )
    conn.commit()

    assert expire_memory_proposals(proposal_engine) == {"expired": 1, "limit": 100}
    row = MemoryProposalStore(conn).get(proposal["proposal_id"])
    assert row["status"] == "expired"
    assert row["content"] == ""

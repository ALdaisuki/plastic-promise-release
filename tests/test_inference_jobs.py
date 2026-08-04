from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from plastic_promise.core import inference_jobs
from plastic_promise.core.backend_inference import (
    CLIENT_LOCAL_RERANK_CONTRACT,
    CLIENT_LOCAL_SCORING_VERSION,
    RERANK_REQUEST_CONTRACT,
    RERANK_SCORING_VERSION,
    material_sha256,
)
from plastic_promise.core.inference_jobs import (
    InferenceJobConflictError,
    InferenceJobError,
    InferenceJobStore,
)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _canonical_hash(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonical_json_bytes(value: dict[str, object]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def _binding(
    *,
    project_id: str = "project:alpha",
    request_id: str = "request-1",
    idempotency_hash: str | None = None,
    input_hash: str | None = None,
) -> dict[str, object]:
    return {
        "contract_version": RERANK_REQUEST_CONTRACT,
        "scoring_version": RERANK_SCORING_VERSION,
        "project_id": project_id,
        "request_id": request_id,
        "idempotency_key_hash": idempotency_hash or _digest(f"idempotency:{project_id}"),
        "candidate_set_version": "candidate-snapshot-1",
        "candidate_set_hash": _digest("candidates"),
        "query_hash": material_sha256("which candidate"),
        "input_hash": input_hash or _digest("bound-input"),
        "provider_policy_revision": "policy-1",
        "top_k": 1,
    }


def _package(
    binding: dict[str, object],
    *,
    text: str = "authoritative candidate",
) -> dict[str, object]:
    material = {
        "contract_version": CLIENT_LOCAL_RERANK_CONTRACT,
        "scoring_version": CLIENT_LOCAL_SCORING_VERSION,
        "project_id": binding["project_id"],
        "request_id": binding["request_id"],
        "candidate_set_version": binding["candidate_set_version"],
        "candidate_set_hash": binding["candidate_set_hash"],
        "query": "which candidate",
        "query_hash": binding["query_hash"],
        "embedding_identity": "embedding:test@revision-1",
        "embedding_dimension": 3,
        "model_identity": "client-local:test@revision-1",
        "top_k": binding["top_k"],
        "candidates": [
            {
                "id": "candidate-1",
                "text": text,
                "base_score": 0.5,
                "material_sha256": material_sha256(text),
                "embedding_sha256": _digest(f"embedding:{text}"),
            }
        ],
    }
    return {**material, "package_hash": _canonical_hash(material)}


@dataclass
class _Clock:
    value: datetime = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _create(store: InferenceJobStore, **kwargs):
    binding = kwargs.pop("binding", _binding())
    return store.create_or_get(
        binding,
        kwargs.pop("package", _package(binding)),
        kwargs.pop("target", "client-local"),
        kwargs.pop("request_material", {"transport": "device"}),
        kwargs.pop("ttl_seconds", 300),
        project_id=kwargs.pop("project_id", str(binding["project_id"])),
        **kwargs,
    )


def test_create_persists_authoritative_record_and_sqlite_safety_settings(tmp_path):
    path = tmp_path / "inference-jobs.db"
    store = InferenceJobStore(path, busy_timeout_ms=7_500)

    created = _create(store)

    assert created.created is True
    assert created.reused is False
    assert created.disposition == "created"
    assert created.job_id == created.job.job_id
    assert created.job.status == "pending"
    assert created.job.target == "client-local"
    assert created.job.request_material == {"transport": "device"}
    assert created.job.created_at.endswith("Z")
    assert created.job.expires_at.endswith("Z")
    assert "authoritative candidate" not in repr(created.job)

    with store._connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 7_500
        row = connection.execute(
            "SELECT package_json, target, execution_hash, lease_token_hash "
            "FROM inference_rerank_jobs"
        ).fetchone()
        assert json.loads(row["package_json"])["candidates"][0]["text"] == (
            "authoritative candidate"
        )
        assert row["target"] == "client-local"
        assert row["execution_hash"].startswith("sha256:")
        assert row["lease_token_hash"] is None

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_retry_reuses_original_package_and_rejects_changed_execution_input(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db")
    binding = _binding()
    original = _create(store, binding=binding, package=_package(binding, text="server original"))

    echoed = _create(store, binding=binding, package=_package(binding, text="client echo"))

    assert echoed.created is False
    assert echoed.job_id == original.job_id
    assert echoed.package["candidates"][0]["text"] == "server original"

    changed_binding = {**binding, "input_hash": _digest("changed input")}
    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_idempotency_conflict$",
    ):
        _create(store, binding=changed_binding, package=_package(changed_binding))

    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_idempotency_conflict$",
    ):
        _create(store, binding=binding, target="cloud")

    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_idempotency_conflict$",
    ):
        _create(store, binding=binding, request_material={"transport": "another-device"})


def test_project_scope_is_server_owned_and_supports_same_hash_in_another_project(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db")
    first_binding = _binding()
    first = _create(store, binding=first_binding)

    assert store.get(first.job_id, "project:other") is None
    with pytest.raises(InferenceJobError, match="^inference_job_project_mismatch$"):
        _create(store, binding=first_binding, project_id="project:other")

    second_binding = _binding(
        project_id="project:other",
        idempotency_hash=str(first_binding["idempotency_key_hash"]),
    )
    second = _create(store, binding=second_binding)
    assert second.created is True
    assert second.job_id != first.job_id


def test_lease_token_is_hash_only_and_expired_lease_is_reclaimable(tmp_path):
    clock = _Clock()
    path = tmp_path / "jobs.db"
    store = InferenceJobStore(path, clock=clock, default_lease_seconds=10)
    record = _create(store).job

    first = store.claim(record.job_id, project_id=record.project_id)
    with sqlite3.connect(path) as connection:
        stored_hash = connection.execute(
            "SELECT lease_token_hash FROM inference_rerank_jobs WHERE job_id = ?",
            (record.job_id,),
        ).fetchone()[0]
    assert first.lease_token not in stored_hash
    assert stored_hash == _digest(first.lease_token)
    assert first.lease_token not in repr(first)
    with pytest.raises(InferenceJobConflictError, match="^inference_job_lease_active$"):
        store.claim(record.job_id, project_id=record.project_id)

    clock.advance(seconds=10)
    assert store.get(record.job_id, record.project_id).status == "pending"
    second = store.claim(record.job_id, project_id=record.project_id)
    assert second.lease_token != first.lease_token
    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_lease_token_invalid$",
    ):
        store.complete(record.job_id, record.project_id, first.lease_token, {"worker": "old"})

    completed = store.complete(
        record.job_id,
        record.project_id,
        second.lease_token,
        {"worker": "new"},
    )
    assert completed.status == "completed"
    assert completed.result == {"worker": "new"}
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT lease_token_hash FROM inference_rerank_jobs WHERE job_id = ?",
                (record.job_id,),
            ).fetchone()[0]
            is None
        )


def test_complete_uses_time_after_connection_wait(monkeypatch, tmp_path):
    clock = _Clock()
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        clock=clock,
        default_lease_seconds=5,
    )
    job = _create(store).job
    lease = store.claim(job.job_id, project_id=job.project_id)
    connect = store._connect

    def delayed_connect():
        connection = connect()
        clock.advance(seconds=10)
        return connection

    monkeypatch.setattr(store, "_connect", delayed_connect)

    with pytest.raises(InferenceJobConflictError, match="^inference_job_not_leased$"):
        store.complete(job.job_id, job.project_id, lease.lease_token, {"ok": True})

    assert store.require(job.job_id, job.project_id).status == "pending"


def test_lease_gateway_api_returns_tuple_and_claim_next_is_project_scoped(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db")
    first = _create(store).job
    other_binding = _binding(
        project_id="project:other",
        idempotency_hash=_digest("other-key"),
        input_hash=_digest("other-input"),
    )
    _create(store, binding=other_binding)

    leased = store.lease(first.job_id, first.project_id, 20)
    assert leased is not None
    leased_job, token = leased
    assert leased_job.status == "leased"
    assert isinstance(token, str) and token
    assert store.claim_next("project:alpha") is None
    assert store.claim_next("project:other") is not None


def test_claim_next_filters_target_without_crossing_project_scope(tmp_path):
    clock = _Clock()
    store = InferenceJobStore(tmp_path / "jobs.db", clock=clock)
    client_binding = _binding(
        idempotency_hash=_digest("client-key"),
        input_hash=_digest("client-input"),
    )
    client_job = _create(store, binding=client_binding, target="client-local").job
    clock.advance(seconds=1)
    cloud_binding = _binding(
        request_id="request-cloud",
        idempotency_hash=_digest("cloud-key"),
        input_hash=_digest("cloud-input"),
    )
    cloud_job = _create(store, binding=cloud_binding, target="cloud").job

    cloud_lease = store.claim_next("project:alpha", target="cloud")
    assert cloud_lease is not None
    assert cloud_lease.job.job_id == cloud_job.job_id
    assert cloud_lease.job.target == "cloud"

    client_lease = store.claim_next("project:alpha", target="client-local")
    assert client_lease is not None
    assert client_lease.job.job_id == client_job.job_id
    assert client_lease.job.target == "client-local"
    assert store.claim_next("project:alpha", target="cloud") is None

    with pytest.raises(InferenceJobError, match="^inference_job_target_invalid$"):
        store.claim_next("project:alpha", target="server-local")


def test_project_active_capacity_is_atomic_and_terminal_jobs_release_slots(tmp_path):
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        busy_timeout_ms=10_000,
        max_active_jobs=1,
    )
    barrier = Barrier(2)

    def submit(index: int):
        binding = _binding(
            request_id=f"request-{index}",
            idempotency_hash=_digest(f"capacity-key-{index}"),
            input_hash=_digest(f"capacity-input-{index}"),
        )
        barrier.wait()
        try:
            return index, "created", _create(store, binding=binding).job
        except InferenceJobError as exc:
            return index, exc.code, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))

    assert sorted(status for _index, status, _job in outcomes) == [
        "created",
        "inference_job_project_capacity_exceeded",
    ]
    winner = next(job for _index, status, job in outcomes if status == "created")
    loser_index = next(
        index
        for index, status, _job in outcomes
        if status == "inference_job_project_capacity_exceeded"
    )
    assert winner is not None

    other_binding = _binding(
        project_id="project:other",
        idempotency_hash=_digest("other-capacity-key"),
        input_hash=_digest("other-capacity-input"),
    )
    assert _create(store, binding=other_binding).created is True

    lease = store.claim(winner.job_id, project_id=winner.project_id)
    store.complete(winner.job_id, winner.project_id, lease.lease_token, {"ok": True})
    loser_binding = _binding(
        request_id=f"request-{loser_index}",
        idempotency_hash=_digest(f"capacity-key-{loser_index}"),
        input_hash=_digest(f"capacity-input-{loser_index}"),
    )
    assert _create(store, binding=loser_binding).created is True


def test_active_reservation_consumes_and_transfers_one_project_capacity_slot(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db", max_active_jobs=1)
    binding = _binding()
    idem = str(binding["idempotency_key_hash"])
    request_hash = str(binding["input_hash"])
    status, token = store.reserve_submission("project:alpha", idem, request_hash, "cloud")
    assert status == "reserved"

    with pytest.raises(
        InferenceJobError,
        match="^inference_job_project_capacity_exceeded$",
    ):
        store.reserve_submission(
            "project:alpha",
            _digest("second-reservation"),
            _digest("second-request"),
            "cloud",
        )

    finalized = store.finalize_submission(
        "project:alpha",
        idem,
        token,
        binding,
        _package(binding),
        "cloud",
        request_material={"request_hash": request_hash},
    )
    assert finalized.created is True
    assert finalized.job.status == "pending"

    with pytest.raises(
        InferenceJobError,
        match="^inference_job_project_capacity_exceeded$",
    ):
        store.reserve_submission(
            "project:alpha",
            _digest("third-reservation"),
            _digest("third-request"),
            "client-local",
        )


def test_retained_row_limit_is_atomic_for_concurrent_reservations(tmp_path):
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        busy_timeout_ms=10_000,
        max_active_jobs=10,
        max_retained_rows_per_project=1,
    )
    barrier = Barrier(2)

    def reserve(index: int):
        barrier.wait()
        try:
            status, _token = store.reserve_submission(
                "project:alpha",
                _digest(f"retained-row-{index}"),
                _digest(f"retained-request-{index}"),
                "cloud",
            )
            return status
        except InferenceJobError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, range(2)))

    assert sorted(outcomes) == [
        "inference_job_project_retained_rows_exceeded",
        "reserved",
    ]
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM inference_rerank_reservations").fetchone()[0]
            == 1
        )


def test_retained_json_byte_limit_is_atomic_for_concurrent_creates(tmp_path):
    bindings = [
        _binding(
            request_id=f"request-json-{index}",
            idempotency_hash=_digest(f"retained-json-{index}"),
            input_hash=_digest(f"retained-json-input-{index}"),
        )
        for index in range(2)
    ]
    packages = [
        _package(binding, text=f"candidate-{index}") for index, binding in enumerate(bindings)
    ]
    request_material = {"transport": "device"}
    single_job_limit = max(
        sum(_canonical_json_bytes(value) for value in (binding, package, request_material))
        for binding, package in zip(bindings, packages, strict=True)
    )
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        busy_timeout_ms=10_000,
        max_active_jobs=10,
        max_retained_json_bytes_per_project=single_job_limit,
    )
    barrier = Barrier(2)

    def create(index: int):
        barrier.wait()
        try:
            return _create(
                store,
                binding=bindings[index],
                package=packages[index],
                request_material=request_material,
            ).disposition
        except InferenceJobError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, range(2)))

    assert sorted(outcomes) == [
        "created",
        "inference_job_project_retained_json_bytes_exceeded",
    ]
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM inference_rerank_jobs").fetchone()[0] == 1


def test_finalize_respects_retained_row_limit_without_consuming_reservation(tmp_path):
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        max_active_jobs=2,
        max_retained_rows_per_project=1,
    )
    binding = _binding()
    idem = str(binding["idempotency_key_hash"])
    request_hash = str(binding["input_hash"])
    _status, token = store.reserve_submission("project:alpha", idem, request_hash, "client-local")

    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_project_retained_rows_exceeded$",
    ):
        store.finalize_submission(
            "project:alpha",
            idem,
            token,
            binding,
            _package(binding),
            "client-local",
            request_material={"request_hash": request_hash},
        )

    assert store.get_reservation("project:alpha", idem)["status"] == "reserved"
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM inference_rerank_jobs").fetchone()[0] == 0


def test_complete_respects_retained_json_byte_limit_atomically(tmp_path):
    binding = _binding()
    package = _package(binding)
    request_material = {"transport": "device"}
    result = {"ok": True}
    base_bytes = sum(_canonical_json_bytes(value) for value in (binding, package, request_material))
    result_bytes = _canonical_json_bytes(result)
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        max_retained_json_bytes_per_project=base_bytes + result_bytes - 1,
    )
    job = _create(
        store,
        binding=binding,
        package=package,
        request_material=request_material,
    ).job
    lease = store.claim(job.job_id, project_id=job.project_id)

    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_project_retained_json_bytes_exceeded$",
    ):
        store.complete(job.job_id, job.project_id, lease.lease_token, result)

    restored = store.require(job.job_id, job.project_id)
    assert restored.status == "leased"
    assert restored.result is None


def test_active_cloud_worker_can_renew_lease_past_original_deadline(tmp_path):
    clock = _Clock()
    store = InferenceJobStore(tmp_path / "jobs.db", clock=clock, default_lease_seconds=10)
    job = _create(store, target="cloud").job
    lease = store.claim(job.job_id, project_id=job.project_id, lease_seconds=10)
    original_deadline = lease.job.lease_expires_at

    clock.advance(seconds=6)
    renewed = store.renew_lease(
        job.job_id,
        job.project_id,
        lease.lease_token,
        lease_seconds=10,
    )
    assert renewed.lease_expires_at > original_deadline

    clock.advance(seconds=5)
    completed = store.complete(
        job.job_id,
        job.project_id,
        lease.lease_token,
        {"provider": "cloud"},
    )
    assert completed.status == "completed"


def test_ttl_expires_pending_and_leased_jobs_but_not_completed_jobs(tmp_path):
    clock = _Clock()
    store = InferenceJobStore(tmp_path / "jobs.db", clock=clock)
    pending_binding = _binding(idempotency_hash=_digest("pending"), input_hash=_digest("p"))
    pending = _create(store, binding=pending_binding, ttl_seconds=10).job
    leased_binding = _binding(
        request_id="request-2",
        idempotency_hash=_digest("leased"),
        input_hash=_digest("l"),
    )
    leased = _create(store, binding=leased_binding, ttl_seconds=10).job
    lease = store.claim(leased.job_id, project_id=leased.project_id, lease_seconds=30)
    completed_binding = _binding(
        request_id="request-3",
        idempotency_hash=_digest("completed"),
        input_hash=_digest("c"),
    )
    completed = _create(store, binding=completed_binding, ttl_seconds=10).job
    completed_lease = store.claim(completed.job_id, project_id=completed.project_id)
    store.complete(
        completed.job_id, completed.project_id, completed_lease.lease_token, {"ok": True}
    )

    clock.advance(seconds=10)

    assert store.expire_due() == 2
    assert store.get(pending.job_id, pending.project_id).status == "expired"
    assert store.get(leased.job_id, leased.project_id).status == "expired"
    assert store.get(completed.job_id, completed.project_id).status == "completed"
    with pytest.raises(InferenceJobConflictError, match="^inference_job_expired$"):
        store.complete(leased.job_id, leased.project_id, lease.lease_token, {"late": True})


def test_completion_is_cas_and_first_valid_writer_wins(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db", busy_timeout_ms=10_000)
    job = _create(store).job
    lease = store.claim(job.job_id, project_id=job.project_id, lease_seconds=60)
    barrier = Barrier(2)

    def finish(worker: str) -> tuple[str, object]:
        barrier.wait()
        try:
            completed = store.complete(
                job.job_id,
                job.project_id,
                lease.lease_token,
                {"worker": worker},
            )
        except InferenceJobError as exc:
            return "error", exc.code
        return "completed", completed.result

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finish, ["a", "b"]))

    assert sorted(status for status, _ in outcomes) == ["completed", "error"]
    assert [value for status, value in outcomes if status == "error"] == [
        "inference_job_already_completed"
    ]
    winner = store.require(job.job_id, job.project_id)
    assert winner.result in ({"worker": "a"}, {"worker": "b"})


def test_policy_failure_is_terminal_and_late_completion_cannot_win(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db")
    job = _create(store, target="cloud").job
    lease = store.claim(job.job_id, project_id=job.project_id, lease_seconds=60)

    failed = store.fail(
        job.job_id,
        job.project_id,
        lease.lease_token,
        "rerank_provider_policy_mismatch",
    )

    assert failed.status == "expired"
    assert failed.failure_code == "rerank_provider_policy_mismatch"
    restored = store.require(job.job_id, job.project_id)
    assert restored.failure_code == failed.failure_code
    with pytest.raises(InferenceJobConflictError, match="^inference_job_expired$"):
        store.complete(job.job_id, job.project_id, lease.lease_token, {"late": True})


@pytest.mark.parametrize(
    ("operation", "reason"),
    [
        ("package_unknown", "inference_job_package_secret_field_forbidden"),
        ("package_tuple", "inference_job_package_json_invalid"),
        ("request_secret", "inference_job_request_material_secret_field_forbidden"),
        ("result_tuple", "inference_job_result_json_invalid"),
        ("result_secret", "inference_job_result_secret_field_forbidden"),
    ],
)
def test_only_bounded_secret_free_json_mappings_are_persisted(tmp_path, operation, reason):
    store = InferenceJobStore(tmp_path / f"{operation}.db")
    binding = _binding()
    package = _package(binding)

    with pytest.raises(InferenceJobError, match=f"^{reason}$"):
        if operation == "package_unknown":
            store.create_or_get(binding, {**package, "api_key": "do-not-store"})
        elif operation == "package_tuple":
            package["candidates"] = tuple(package["candidates"])
            store.create_or_get(binding, package)
        elif operation == "request_secret":
            store.create_or_get(binding, package, request_material={"token": "do-not-store"})
        else:
            job = store.create_or_get(binding, package).job
            lease = store.claim(job.job_id, project_id=job.project_id)
            result = {"items": (1, 2)} if operation == "result_tuple" else {"api_key": "no"}
            store.complete(job.job_id, job.project_id, lease.lease_token, result)


def test_package_and_result_byte_limits_have_stable_codes(tmp_path):
    binding = _binding()
    package_store = InferenceJobStore(tmp_path / "package.db", max_package_bytes=128)
    with pytest.raises(InferenceJobError, match="^inference_job_package_too_large$"):
        package_store.create_or_get(binding, _package(binding))

    result_store = InferenceJobStore(tmp_path / "result.db", max_result_bytes=24)
    job = result_store.create_or_get(binding, _package(binding)).job
    lease = result_store.claim(job.job_id, project_id=job.project_id)
    with pytest.raises(InferenceJobError, match="^inference_job_result_too_large$"):
        result_store.complete(
            job.job_id,
            job.project_id,
            lease.lease_token,
            {"value": "x" * 100},
        )
    assert result_store.require(job.job_id, job.project_id).status == "leased"


def test_reopen_preserves_completed_result_and_authoritative_material(tmp_path):
    path = tmp_path / "jobs.db"
    first_store = InferenceJobStore(path)
    created = _create(first_store)
    lease = first_store.claim(created.job_id, project_id=created.project_id)
    first_store.complete(created.job_id, created.project_id, lease.lease_token, {"rank": ["a"]})

    reopened = InferenceJobStore(path)
    restored = reopened.require(created.job_id, created.project_id)

    assert restored.status == "completed"
    assert restored.result == {"rank": ["a"]}
    assert restored.binding["input_hash"] == created.input_hash
    assert restored.package == created.package
    assert restored.request_material == {"transport": "device"}


def test_preflight_reservation_deduplicates_before_prepare_and_finalizes_atomically(tmp_path):
    clock = _Clock()
    store = InferenceJobStore(tmp_path / "jobs.db", clock=clock)
    binding = _binding()
    idem = str(binding["idempotency_key_hash"])
    request_hash = str(binding["input_hash"])

    status, reservation_token = store.reserve_submission(
        "project:alpha", idem, request_hash, "client-local", ttl_seconds=30
    )
    assert status == "reserved"
    assert reservation_token
    second_status, second_token = store.reserve_submission(
        "project:alpha", idem, request_hash, "client-local", ttl_seconds=30
    )
    assert (second_status, second_token) == ("preparing", None)
    reservation = store.get_reservation("project:alpha", idem)
    assert reservation is not None
    assert reservation["status"] == "reserved"
    assert "reservation_token" not in reservation
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        stored_token_hash = connection.execute(
            "SELECT reservation_token_hash FROM inference_rerank_reservations "
            "WHERE project_id = ? AND idempotency_key_hash = ?",
            ("project:alpha", idem),
        ).fetchone()[0]
    assert stored_token_hash == _digest(reservation_token)
    assert reservation_token not in stored_token_hash

    finalized = store.finalize_submission(
        "project:alpha",
        idem,
        reservation_token,
        binding,
        _package(binding),
        "client-local",
        ttl_seconds=300,
        request_material={"request_hash": request_hash, "source": "preflight"},
    )
    assert finalized.created is True
    assert finalized.job.status == "pending"
    assert finalized.job.expires_at <= reservation["expires_at"]
    assert store.get_reservation("project:alpha", idem)["status"] == "finalized"

    existing_status, existing_token = store.reserve_submission(
        "project:alpha", idem, request_hash, "client-local", ttl_seconds=30
    )
    assert (existing_status, existing_token) == ("existing", None)
    retried = store.finalize_submission(
        "project:alpha",
        idem,
        "unused-after-finalize",
        binding,
        _package(binding, text="client echo"),
        "client-local",
        request_material={"request_hash": request_hash, "source": "preflight"},
    )
    assert retried.created is False
    assert retried.package["candidates"][0]["text"] == "authoritative candidate"


def test_finalized_retry_rejects_changed_execution_hash(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db")
    binding = _binding()
    idem = str(binding["idempotency_key_hash"])
    request_hash = str(binding["input_hash"])
    _status, token = store.reserve_submission(
        "project:alpha",
        idem,
        request_hash,
        "client-local",
    )
    store.finalize_submission(
        "project:alpha",
        idem,
        token,
        binding,
        _package(binding),
        "client-local",
        request_material={"request_hash": request_hash},
    )
    changed = {**binding, "input_hash": _digest("changed-execution-input")}

    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_idempotency_conflict$",
    ):
        store.finalize_submission(
            "project:alpha",
            idem,
            "unused-after-finalize",
            changed,
            _package(changed),
            "client-local",
            request_material={"request_hash": request_hash},
        )


def test_preflight_reservation_binds_request_hash_target_and_one_time_token(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db")
    binding = _binding()
    idem = str(binding["idempotency_key_hash"])
    request_hash = str(binding["input_hash"])
    status, token = store.reserve_submission("project:alpha", idem, request_hash, "cloud")
    assert status == "reserved"

    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_idempotency_conflict$",
    ):
        store.reserve_submission("project:alpha", idem, _digest("other"), "cloud")
    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_target_conflict$",
    ):
        store.finalize_submission(
            "project:alpha",
            idem,
            token,
            binding,
            _package(binding),
            "client-local",
            request_material={"request_hash": request_hash},
        )
    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_reservation_request_mismatch$",
    ):
        store.finalize_submission(
            "project:alpha",
            idem,
            token,
            binding,
            _package(binding),
            "cloud",
            request_material={"request_hash": _digest("wrong")},
        )
    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_reservation_token_invalid$",
    ):
        store.finalize_submission(
            "project:alpha",
            idem,
            "wrong-token",
            binding,
            _package(binding),
            "cloud",
            request_material={"request_hash": request_hash},
        )

    finalized = store.finalize_submission(
        "project:alpha",
        idem,
        token,
        binding,
        _package(binding),
        "cloud",
        request_material={"request_hash": request_hash},
    )
    assert finalized.created is True
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        row = connection.execute(
            "SELECT reservation_token_hash FROM inference_rerank_reservations "
            "WHERE project_id = ? AND idempotency_key_hash = ?",
            ("project:alpha", idem),
        ).fetchone()
    assert row[0] is None
    assert token not in repr(finalized)


def test_expired_reservation_can_be_reclaimed_and_release_keeps_audit_row(tmp_path):
    clock = _Clock()
    store = InferenceJobStore(tmp_path / "jobs.db", clock=clock)
    binding = _binding()
    idem = str(binding["idempotency_key_hash"])
    request_hash = str(binding["input_hash"])
    status, first_token = store.reserve_submission(
        "project:alpha", idem, request_hash, "client-local", ttl_seconds=5
    )
    assert status == "reserved"
    clock.advance(seconds=5)
    assert store.get_reservation("project:alpha", idem)["status"] == "expired"
    status, second_token = store.reserve_submission(
        "project:alpha", idem, request_hash, "client-local", ttl_seconds=5
    )
    assert status == "reserved"
    assert second_token != first_token
    assert store.release_submission("project:alpha", idem, second_token) is True
    assert store.get_reservation("project:alpha", idem)["status"] == "released"
    assert store.release_submission("project:alpha", idem, second_token) is False


def test_preparation_lease_renews_and_takeover_rejects_stale_finalizer(tmp_path):
    clock = _Clock()
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        clock=clock,
        default_lease_seconds=5,
        max_lease_seconds=10,
    )
    binding = _binding()
    idem = str(binding["idempotency_key_hash"])
    request_hash = str(binding["input_hash"])

    status, first_token = store.reserve_submission(
        "project:alpha",
        idem,
        request_hash,
        "cloud",
        ttl_seconds=60,
    )
    assert status == "reserved"
    assert first_token
    first = store.get_reservation("project:alpha", idem)
    assert first is not None
    assert first["preparation_lease_expires_at"] < first["expires_at"]

    clock.advance(seconds=3)
    renewed = store.renew_submission(
        "project:alpha",
        idem,
        first_token,
        lease_seconds=5,
    )
    assert renewed["preparation_lease_expires_at"] > first["preparation_lease_expires_at"]
    assert renewed["expires_at"] == first["expires_at"]

    clock.advance(seconds=5)
    expired = store.get_reservation("project:alpha", idem)
    assert expired is not None
    assert expired["status"] == "expired"

    status, second_token = store.reserve_submission(
        "project:alpha",
        idem,
        request_hash,
        "cloud",
        ttl_seconds=60,
    )
    assert status == "reserved"
    assert second_token and second_token != first_token
    taken_over = store.get_reservation("project:alpha", idem)
    assert taken_over is not None
    assert taken_over["expires_at"] == first["expires_at"]

    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_reservation_token_invalid$",
    ):
        store.finalize_submission(
            "project:alpha",
            idem,
            first_token,
            binding,
            _package(binding),
            "cloud",
            request_material={"request_hash": request_hash},
        )

    finalized = store.finalize_submission(
        "project:alpha",
        idem,
        second_token,
        binding,
        _package(binding),
        "cloud",
        request_material={"request_hash": request_hash},
    )
    assert finalized.created is True
    assert finalized.job.expires_at == first["expires_at"]


def test_retention_cleanup_removes_only_old_terminal_jobs_and_reservations(tmp_path):
    clock = _Clock()
    path = tmp_path / "jobs.db"
    store = InferenceJobStore(path, clock=clock, retention_seconds=10, max_active_jobs=10)

    completed_binding = _binding(
        idempotency_hash=_digest("completed-key"),
        input_hash=_digest("completed-input"),
    )
    completed_idem = str(completed_binding["idempotency_key_hash"])
    completed_request_hash = str(completed_binding["input_hash"])
    _status, completed_token = store.reserve_submission(
        "project:alpha",
        completed_idem,
        completed_request_hash,
        "cloud",
    )
    completed = store.finalize_submission(
        "project:alpha",
        completed_idem,
        completed_token,
        completed_binding,
        _package(completed_binding),
        "cloud",
        request_material={"request_hash": completed_request_hash},
    ).job
    completed_lease = store.claim(completed.job_id, project_id=completed.project_id)
    store.complete(
        completed.job_id,
        completed.project_id,
        completed_lease.lease_token,
        {"ok": True},
    )

    expired_binding = _binding(
        request_id="request-expired",
        idempotency_hash=_digest("expired-key"),
        input_hash=_digest("expired-input"),
    )
    expired = _create(store, binding=expired_binding, ttl_seconds=5).job
    active_binding = _binding(
        request_id="request-active",
        idempotency_hash=_digest("active-key"),
        input_hash=_digest("active-input"),
    )
    active = _create(store, binding=active_binding, ttl_seconds=300).job

    released_idem = _digest("released-reservation")
    _status, released_token = store.reserve_submission(
        "project:alpha", released_idem, _digest("released-request"), "cloud"
    )
    assert store.release_submission("project:alpha", released_idem, released_token)
    expired_reservation_idem = _digest("expired-reservation")
    store.reserve_submission(
        "project:alpha",
        expired_reservation_idem,
        _digest("expired-reservation-request"),
        "cloud",
        ttl_seconds=5,
    )
    active_reservation_idem = _digest("active-reservation")
    store.reserve_submission(
        "project:alpha",
        active_reservation_idem,
        _digest("active-reservation-request"),
        "client-local",
        ttl_seconds=300,
    )

    clock.advance(seconds=5)
    assert store.expire_due() == 1
    clock.advance(seconds=10)
    removed = store.cleanup_retained()

    assert removed == {"jobs": 2, "reservations": 3}
    assert store.get(completed.job_id, completed.project_id) is None
    assert store.get(expired.job_id, expired.project_id) is None
    assert store.require(active.job_id, active.project_id).status == "pending"
    assert store.get_reservation("project:alpha", released_idem) is None
    assert store.get_reservation("project:alpha", expired_reservation_idem) is None
    assert store.get_reservation("project:alpha", active_reservation_idem)["status"] == "reserved"


def test_retention_keeps_finalized_reservation_while_referenced_job_is_active(tmp_path):
    clock = _Clock()
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        clock=clock,
        retention_seconds=5,
        default_lease_seconds=30,
    )
    binding = _binding()
    idem = str(binding["idempotency_key_hash"])
    request_hash = str(binding["input_hash"])
    _status, token = store.reserve_submission(
        "project:alpha",
        idem,
        request_hash,
        "client-local",
        ttl_seconds=300,
    )
    job = store.finalize_submission(
        "project:alpha",
        idem,
        token,
        binding,
        _package(binding),
        "client-local",
        request_material={"request_hash": request_hash},
    ).job

    clock.advance(seconds=5)
    assert store.cleanup_retained() == {"jobs": 0, "reservations": 0}
    assert store.get_reservation("project:alpha", idem)["status"] == "finalized"
    assert store.require(job.job_id, job.project_id).status == "pending"

    lease = store.claim(job.job_id, project_id=job.project_id)
    clock.advance(seconds=5)
    assert store.cleanup_retained() == {"jobs": 0, "reservations": 0}
    assert store.get_reservation("project:alpha", idem)["status"] == "finalized"
    assert store.require(job.job_id, job.project_id).status == "leased"

    store.complete(job.job_id, job.project_id, lease.lease_token, {"ok": True})
    assert store.cleanup_retained() == {"jobs": 0, "reservations": 0}
    assert store.get_reservation("project:alpha", idem)["status"] == "finalized"
    assert store.reserve_submission(
        "project:alpha",
        idem,
        request_hash,
        "client-local",
    ) == ("existing", None)


def test_reserve_post_cleans_elapsed_retention_before_row_admission(tmp_path):
    clock = _Clock()
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        clock=clock,
        retention_seconds=5,
        max_active_jobs=10,
        max_retained_rows_per_project=2,
    )
    for index in range(2):
        idem = _digest(f"released-row-{index}")
        _status, token = store.reserve_submission(
            "project:alpha",
            idem,
            _digest(f"released-request-{index}"),
            "client-local",
        )
        assert store.release_submission("project:alpha", idem, token)

    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_project_retained_rows_exceeded$",
    ):
        store.reserve_submission(
            "project:alpha",
            _digest("blocked-row"),
            _digest("blocked-request"),
            "cloud",
        )

    clock.advance(seconds=5)
    status, token = store.reserve_submission(
        "project:alpha",
        _digest("admitted-after-cleanup"),
        _digest("admitted-request"),
        "cloud",
    )
    assert status == "reserved"
    assert token
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        rows = connection.execute(
            "SELECT status FROM inference_rerank_reservations ORDER BY created_at"
        ).fetchall()
    assert rows == [("reserved",)]


def test_create_post_cleans_elapsed_retention_before_json_byte_admission(tmp_path):
    clock = _Clock()
    first_binding = _binding(
        idempotency_hash=_digest("first-byte-key"),
        input_hash=_digest("first-byte-input"),
    )
    second_binding = _binding(
        request_id="request-2",
        idempotency_hash=_digest("second-byte-key"),
        input_hash=_digest("second-byte-input"),
    )
    first_package = _package(first_binding)
    second_package = _package(second_binding)
    request_material = {"transport": "device"}
    result = {"ok": True}
    first_total = sum(
        _canonical_json_bytes(value)
        for value in (first_binding, first_package, request_material, result)
    )
    second_base = sum(
        _canonical_json_bytes(value) for value in (second_binding, second_package, request_material)
    )
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        clock=clock,
        retention_seconds=5,
        max_retained_json_bytes_per_project=first_total + second_base - 1,
    )
    first = _create(store, binding=first_binding, package=first_package).job
    first_lease = store.claim(first.job_id, project_id=first.project_id)
    store.complete(first.job_id, first.project_id, first_lease.lease_token, result)

    with pytest.raises(
        InferenceJobConflictError,
        match="^inference_job_project_retained_json_bytes_exceeded$",
    ):
        _create(store, binding=second_binding, package=second_package)

    clock.advance(seconds=5)
    second = _create(store, binding=second_binding, package=second_package)
    assert second.created is True
    assert store.get(first.job_id, first.project_id) is None


def test_quota_admission_drains_multiple_retention_batches_before_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(inference_jobs, "_CLEANUP_BATCH_SIZE", 2)
    clock = _Clock()
    store = InferenceJobStore(
        tmp_path / "jobs.db",
        clock=clock,
        retention_seconds=5,
        max_active_jobs=10,
        max_retained_rows_per_project=3,
    )
    for index in range(3):
        idem = _digest(f"retained-batch-{index}")
        status, token = store.reserve_submission(
            "project:alpha",
            idem,
            _digest(f"retained-batch-request-{index}"),
            "client-local",
        )
        assert status == "reserved"
        assert token
        assert store.release_submission("project:alpha", idem, token)

    # Populate through the public API, then shrink the admission ceiling to
    # exercise recovery from an older, over-retained on-disk project.
    store._max_retained_rows_per_project = 1
    clock.advance(seconds=5)
    status, token = store.reserve_submission(
        "project:alpha",
        _digest("admitted-after-multiple-batches"),
        _digest("admitted-after-multiple-batches-request"),
        "cloud",
    )

    assert status == "reserved"
    assert token
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        rows = connection.execute(
            "SELECT status FROM inference_rerank_reservations ORDER BY created_at"
        ).fetchall()
    assert rows == [("reserved",)]


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        (
            {"max_retained_rows_per_project": 0},
            "inference_job_max_retained_rows_per_project_invalid",
        ),
        (
            {"max_retained_rows_per_project": True},
            "inference_job_max_retained_rows_per_project_invalid",
        ),
        (
            {"max_retained_rows_per_project": 1_000_001},
            "inference_job_max_retained_rows_per_project_invalid",
        ),
        (
            {"max_retained_json_bytes_per_project": 0},
            "inference_job_max_retained_json_bytes_per_project_invalid",
        ),
        (
            {"max_retained_json_bytes_per_project": True},
            "inference_job_max_retained_json_bytes_per_project_invalid",
        ),
        (
            {"max_retained_json_bytes_per_project": 64 * 1024 * 1024 * 1024 + 1},
            "inference_job_max_retained_json_bytes_per_project_invalid",
        ),
    ],
)
def test_retained_storage_limits_are_strictly_validated(tmp_path, settings, reason):
    with pytest.raises(InferenceJobError, match=f"^{reason}$"):
        InferenceJobStore(tmp_path / "jobs.db", **settings)


def test_default_retained_row_limit_scales_with_active_capacity(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db", max_active_jobs=7)

    assert store._max_retained_rows_per_project == 28
    assert store._max_retained_json_bytes_per_project == 512 * 1024 * 1024


def test_concurrent_reservation_has_one_owner(tmp_path):
    store = InferenceJobStore(tmp_path / "jobs.db", busy_timeout_ms=10_000)
    binding = _binding()
    idem = str(binding["idempotency_key_hash"])
    request_hash = str(binding["input_hash"])

    def reserve():
        return store.reserve_submission("project:alpha", idem, request_hash, "cloud")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: reserve(), range(2)))
    assert sorted(status for status, _token in outcomes) == ["preparing", "reserved"]
    assert sum(token is not None for _status, token in outcomes) == 1


def test_naive_clock_and_non_mapping_payloads_fail_closed(tmp_path):
    def naive_clock() -> datetime:
        return datetime(2026, 7, 23, 8, 0)

    store = InferenceJobStore(tmp_path / "jobs.db", clock=naive_clock)
    binding = _binding()

    with pytest.raises(InferenceJobError, match="^inference_job_clock_invalid$"):
        store.create_or_get(binding, _package(binding))
    with pytest.raises(InferenceJobError, match="^inference_job_package_mapping_required$"):
        store.create_or_get(binding, [])  # type: ignore[arg-type]

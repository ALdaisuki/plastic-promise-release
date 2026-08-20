from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.activity_update import (
    ACTIVITY_AUDIT_ISSUER,
    ACTIVITY_AUDIT_RECEIPT_SCHEMA,
    ACTIVITY_REDACTION_POLICY_REVISION,
    ACTIVITY_SCOPE_SCHEMA,
    ACTIVITY_SLICE_SCHEMA,
    AGENT_ACTIVITY_UPDATE_SCHEMA,
    ActivityAuditAuthority,
    ActivityAuditReceipt,
    ActivityContractError,
    ActivityScope,
    ActivitySlice,
    AgentActivityUpdate,
    open_server_activity_audit_authority,
)
from plastic_promise.collaboration.contracts import EventCursor, ProjectScope

NOW = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
PROJECT = ProjectScope("project:activity-contract")
ROLE_ASSIGNMENT_SHA256 = "sha256:" + "a" * 64
PREVIOUS_PATH = "plastic_promise/collaboration/contracts.py"
CURRENT_PATH = "plastic_promise/collaboration/activity_update.py"
NEXT_PATH = "tests/test_collaboration_activity_update.py"


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _scope(*, project: ProjectScope = PROJECT) -> ActivityScope:
    return ActivityScope(
        project=project,
        coordination_session_id="coord:activity-contract",
        agent_session_id="agent-session:activity-contract",
        agent_id="agent:activity-contract",
    )


def _slice(
    scope: str,
    summary: str,
    *paths: str,
) -> ActivitySlice:
    return ActivitySlice(scope=scope, paths=paths, summary=summary)


def _test_slice(
    value: str | ActivitySlice | None,
    *,
    scope: str,
    path: str,
) -> ActivitySlice | None:
    if value is None or isinstance(value, ActivitySlice):
        return value
    return _slice(scope, value, path)


def _update(
    *,
    scope: ActivityScope | None = None,
    summary: str = "Implement bounded Agent activity contracts",
    previous: str | ActivitySlice | None = "Task accepted and relevant contracts inspected",
    current: str | ActivitySlice = "Writing the isolated activity module and focused tests",
    next_value: str | ActivitySlice | None = "Run focused tests and return the audit receipt",
    blockers: tuple[str, ...] = (),
    work_item_id: str = "work:activity-contract",
    role_assignment_sha256: str = ROLE_ASSIGNMENT_SHA256,
    cursor: int | EventCursor = 1,
) -> AgentActivityUpdate:
    return AgentActivityUpdate(
        scope=scope or _scope(),
        role="activity_contract",
        summary=summary,
        previous=_test_slice(previous, scope="contract inspection", path=PREVIOUS_PATH),
        current=_test_slice(current, scope="activity contract implementation", path=CURRENT_PATH),
        next=_test_slice(next_value, scope="focused verification", path=NEXT_PATH),
        blockers=blockers,
        work_item_id=work_item_id,
        role_assignment_sha256=role_assignment_sha256,
        cursor=cursor,
    )


def _canonical_digest(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_scope_and_update_are_immutable_bounded_digest_contracts() -> None:
    source_blockers = ["Waiting for no external dependency"]
    source_paths = [CURRENT_PATH, "tests/test_collaboration_activity_update.py"]
    update = AgentActivityUpdate(
        scope=_scope(),
        role="activity_contract",
        summary="A compact activity summary",
        previous=_slice("inspection", "Inspected the collaboration seam", PREVIOUS_PATH),
        current=ActivitySlice(
            scope="implementation",
            paths=source_paths,  # type: ignore[arg-type]
            summary="Implementing the contract",
        ),
        next=_slice("verification", "Verify the focused test file", NEXT_PATH),
        blockers=source_blockers,
        work_item_id="work:activity-contract",
        role_assignment_sha256=ROLE_ASSIGNMENT_SHA256,
        cursor=4,
    )
    source_blockers.append("Mutation after construction")
    source_paths.append("unexpected.py")

    assert update.blockers == ("Waiting for no external dependency",)
    assert update.current.paths == (CURRENT_PATH, "tests/test_collaboration_activity_update.py")
    assert update.scope.to_dict()["schema_version"] == ACTIVITY_SCOPE_SCHEMA
    assert update.current.to_dict()["schema_version"] == ACTIVITY_SLICE_SCHEMA
    assert update.to_dict()["schema_version"] == AGENT_ACTIVITY_UPDATE_SCHEMA
    assert update.content_sha256 == _canonical_digest(update.to_dict())
    assert update.update_sha256 == update.content_sha256
    assert update.scope.scope_sha256 == _canonical_digest(update.scope.to_dict())
    with pytest.raises(FrozenInstanceError):
        update.summary = "tampered"  # type: ignore[misc]


def test_update_projection_contains_required_fields_and_explicitly_grants_nothing() -> None:
    update = _update(blockers=("Need the parent Agent to integrate the isolated module",))

    projection = update.to_dict()

    assert projection["summary"] == update.summary
    assert projection["previous"] == update.previous.to_dict()  # type: ignore[union-attr]
    assert projection["current"] == update.current.to_dict()
    assert projection["next"] == update.next.to_dict()  # type: ignore[union-attr]
    assert projection["blockers"] == list(update.blockers)
    assert projection["work_item_id"] == update.work_item_id
    assert projection["role_assignment_sha256"] == ROLE_ASSIGNMENT_SHA256
    assert projection["cursor"] == 1
    assert projection["redaction_policy_revision"] == ACTIVITY_REDACTION_POLICY_REVISION
    assert projection["role_effect"] == "audit-only"
    assert projection["role_assignment_effect"] == "reference-only"
    assert projection["authority_effect"] == "none"
    assert projection["tool_policy_effect"] == "none"
    assert projection["canonical_memory_effect"] == "none"
    assert projection["current"]["authority_effect"] == "none"  # type: ignore[index]
    assert update.evidence_paths == (CURRENT_PATH, PREVIOUS_PATH)
    assert update.planned_paths == (NEXT_PATH,)


def test_activity_slice_canonicalizes_paths_and_rejects_legacy_shape_coercion() -> None:
    activity_slice = ActivitySlice(
        scope="activity contract",
        paths=(NEXT_PATH, CURRENT_PATH),
        summary="Implement and verify the structured activity contract",
    )

    assert activity_slice.paths == (CURRENT_PATH, NEXT_PATH)
    assert activity_slice.content_sha256 == _canonical_digest(activity_slice.to_dict())
    assert activity_slice.slice_sha256 == activity_slice.content_sha256
    assert activity_slice.to_dict()["authority_effect"] == "none"

    values = _update().to_dict()
    for field, legacy in (
        ("previous", "Legacy previous string"),
        ("current", {"scope": "mapping", "paths": [CURRENT_PATH], "summary": "mapping"}),
        ("next", "Legacy next string"),
    ):
        constructor = {
            "scope": _scope(),
            "role": "activity_contract",
            "summary": "Summary",
            "previous": _slice("inspection", "Previous", PREVIOUS_PATH),
            "current": _slice("implementation", "Current", CURRENT_PATH),
            "next": _slice("verification", "Next", NEXT_PATH),
            "blockers": (),
            "work_item_id": "work:activity-contract",
            "role_assignment_sha256": ROLE_ASSIGNMENT_SHA256,
            "cursor": 1,
        }
        constructor[field] = legacy
        with pytest.raises(ActivityContractError, match=f"^activity_{field}_slice_invalid$"):
            AgentActivityUpdate(**constructor)  # type: ignore[arg-type]
    assert values["schema_version"] == "collaboration-agent-activity-update/v2"


@pytest.mark.parametrize(
    "path",
    [
        "/absolute/path.py",
        "../escape.py",
        "nested/../escape.py",
        "nested//file.py",
        "nested/./file.py",
        "nested\\file.py",
        "C:/repository/file.py",
        "~/repository/file.py",
        "src/*.py",
        "src/file?.py",
        "src/ trailing.py",
        "src/trailing. ",
        "src/control\n.py",
    ],
)
def test_activity_slice_rejects_noncanonical_or_nonportable_paths(path: str) -> None:
    with pytest.raises(ActivityContractError, match="^activity_slice_path_invalid$"):
        _slice("implementation", "Public summary", path)


def test_activity_slice_rejects_empty_duplicate_oversized_and_arbitrary_paths() -> None:
    with pytest.raises(ActivityContractError, match="^activity_slice_paths_empty$"):
        ActivitySlice(scope="implementation", paths=(), summary="Public summary")
    with pytest.raises(ActivityContractError, match="^activity_slice_paths_invalid$"):
        ActivitySlice(
            scope="implementation",
            paths=CURRENT_PATH,  # type: ignore[arg-type]
            summary="Public summary",
        )
    with pytest.raises(ActivityContractError, match="^activity_slice_paths_duplicate$"):
        _slice("implementation", "Public summary", CURRENT_PATH, CURRENT_PATH)
    with pytest.raises(ActivityContractError, match="^activity_slice_paths_too_many$"):
        ActivitySlice(
            scope="implementation",
            paths=tuple(f"src/file-{index}.py" for index in range(33)),
            summary="Public summary",
        )
    with pytest.raises(ActivityContractError, match="^activity_slice_path_invalid$"):
        _slice("implementation", "Public summary", "src/" + "x" * 509)


def test_optional_previous_and_next_are_explicit_null_slices() -> None:
    update = _update(previous=None, next_value=None)

    assert update.previous is None
    assert update.next is None
    assert update.to_dict()["previous"] is None
    assert update.to_dict()["next"] is None
    assert update.evidence_paths == (CURRENT_PATH,)
    assert update.planned_paths == ()


def test_cursor_accepts_only_same_scope_event_cursor_or_bounded_integer() -> None:
    scope = _scope()
    update = _update(
        scope=scope,
        cursor=EventCursor(scope.project, scope.coordination_session_id, 9),
    )
    assert update.cursor == 9

    other_project = ProjectScope("project:other")
    with pytest.raises(ActivityContractError, match="^activity_cursor_scope_mismatch$"):
        _update(scope=scope, cursor=EventCursor(other_project, "coord:other", 1))
    for invalid in (-1, True, 1 << 63, "1"):
        with pytest.raises(ActivityContractError, match="^activity_cursor_invalid$"):
            _update(cursor=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"role": "Reviewer"}, "activity_role_invalid"),
        ({"summary": ""}, "activity_summary_invalid"),
        ({"current": ""}, "activity_current_slice_invalid"),
        ({"previous": "Previous"}, "activity_previous_slice_invalid"),
        ({"next": {"summary": "Next"}}, "activity_next_slice_invalid"),
        ({"work_item_id": "work item"}, "activity_work_item_invalid"),
        (
            {"role_assignment_sha256": "sha256:ABC"},
            "activity_role_assignment_digest_invalid",
        ),
        (
            {"work_item_id": "", "role_assignment_sha256": ROLE_ASSIGNMENT_SHA256},
            "activity_role_assignment_work_required",
        ),
        ({"summary": "x" * 2049}, "activity_summary_invalid"),
        (
            {"blockers": tuple(f"blocker-{index}" for index in range(17))},
            "activity_blockers_too_many",
        ),
        ({"blockers": ("same", "same")}, "activity_blockers_duplicate"),
    ],
)
def test_update_validation_fails_closed_with_stable_codes(
    changes: dict[str, object],
    code: str,
) -> None:
    values: dict[str, object] = {
        "scope": _scope(),
        "role": "activity_contract",
        "summary": "Summary",
        "previous": _slice("inspection", "Previous", PREVIOUS_PATH),
        "current": _slice("implementation", "Current", CURRENT_PATH),
        "next": _slice("verification", "Next", NEXT_PATH),
        "blockers": (),
        "work_item_id": "work:activity-contract",
        "role_assignment_sha256": ROLE_ASSIGNMENT_SHA256,
        "cursor": 1,
    }
    values.update(changes)

    with pytest.raises(ActivityContractError, match=f"^{code}$"):
        AgentActivityUpdate(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "leak",
    [
        "Bearer super-secret-value",
        "api_key=sk-proj-abcdefghijklmnop",
        "github_pat_abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN TEST PRIVATE KEY-----",
    ],
)
def test_updates_reject_secret_like_values_in_every_public_text_channel(leak: str) -> None:
    for changes in (
        {"summary": leak},
        {"previous": leak},
        {"current": leak},
        {"next_value": leak},
        {"blockers": (leak,)},
    ):
        with pytest.raises(ActivityContractError, match="^activity_secret_forbidden$"):
            _update(**changes)  # type: ignore[arg-type]

    for field in ("scope", "summary"):
        values = {
            "scope": "implementation",
            "paths": (CURRENT_PATH,),
            "summary": "Public summary",
        }
        values[field] = leak
        with pytest.raises(ActivityContractError, match="^activity_secret_forbidden$"):
            ActivitySlice(**values)  # type: ignore[arg-type]
    with pytest.raises(ActivityContractError, match="^activity_secret_forbidden$"):
        _slice("implementation", "Public summary", f"src/{leak}.py")


@pytest.mark.parametrize(
    "leak",
    [
        "Raw prompt transcript follows",
        "Private reasoning: hidden details",
        "Chain of thought copied here",
        "Internal monologue excerpt",
        "Scratchpad contents",
    ],
)
def test_updates_reject_private_reasoning_and_prompt_transcripts(leak: str) -> None:
    with pytest.raises(ActivityContractError, match="^activity_private_reasoning_forbidden$"):
        _update(current=leak)
    with pytest.raises(ActivityContractError, match="^activity_private_reasoning_forbidden$"):
        _slice(leak, "Public summary", CURRENT_PATH)


def test_work_without_assignment_is_explicitly_allowed_but_never_authoritative() -> None:
    update = _update(role_assignment_sha256="")

    assert update.work_item_id == "work:activity-contract"
    assert update.role_assignment_sha256 == ""
    assert update.to_dict()["role_assignment_effect"] == "reference-only"
    assert update.to_dict()["authority_effect"] == "none"


def test_audit_receipt_can_only_be_issued_by_server_factory() -> None:
    with pytest.raises(ActivityContractError, match="^activity_audit_receipt_factory_required$"):
        ActivityAuditReceipt()  # type: ignore[call-arg]
    with pytest.raises(ActivityContractError, match="^activity_audit_server_authority_required$"):
        ActivityAuditAuthority()


def test_server_receipt_is_digest_bound_narrative_free_and_non_authoritative() -> None:
    update = _update(blockers=("One public blocker",))
    authority = open_server_activity_audit_authority(clock=MutableClock())

    receipt = authority.issue(update)
    projection = receipt.to_dict()

    assert authority.verify_issued(receipt, update=update) is receipt
    assert authority.verify(receipt, update=update) is True
    assert projection["schema_version"] == ACTIVITY_AUDIT_RECEIPT_SCHEMA
    assert projection["issuer"] == ACTIVITY_AUDIT_ISSUER
    assert projection["activity_update_sha256"] == update.content_sha256
    assert projection["activity_scope_sha256"] == update.scope.content_sha256
    assert projection["validated_at_utc"] == "2026-08-11T08:00:00.000000Z"
    assert projection["redaction"] == "narrative-omitted"
    assert projection["authority_effect"] == "none"
    assert projection["tool_policy_effect"] == "none"
    assert projection["canonical_memory_effect"] == "none"
    serialized = receipt.canonical_json()
    for private_value in (
        update.summary,
        update.previous.scope,  # type: ignore[union-attr]
        update.previous.summary,  # type: ignore[union-attr]
        *update.previous.paths,  # type: ignore[union-attr]
        update.current.scope,
        update.current.summary,
        *update.current.paths,
        update.next.scope,  # type: ignore[union-attr]
        update.next.summary,  # type: ignore[union-attr]
        *update.next.paths,  # type: ignore[union-attr]
        *update.blockers,
    ):
        assert private_value not in serialized
    assert receipt.content_sha256 == _canonical_digest(projection)
    assert receipt.receipt_sha256 == receipt.content_sha256
    assert receipt.activity_sha256 == update.content_sha256


def test_receipt_replay_is_idempotent_and_cursor_history_is_fail_closed() -> None:
    authority = open_server_activity_audit_authority(clock=MutableClock())
    first_update = _update(cursor=3)
    first = authority.issue(first_update)

    assert authority.issue(first_update) is first
    with pytest.raises(ActivityContractError, match="^activity_audit_replay_ambiguous$"):
        authority.issue(first_update, receipt_id="activity-audit:other")
    with pytest.raises(ActivityContractError, match="^activity_audit_cursor_conflict$"):
        authority.issue(_update(cursor=3, current="Different update at the same cursor"))
    authority.issue(_update(cursor=5, current="A later activity update"))
    with pytest.raises(ActivityContractError, match="^activity_audit_cursor_regression$"):
        authority.issue(_update(cursor=4, current="A regressed activity update"))


def test_tampered_cross_authority_and_wrong_update_receipts_are_rejected() -> None:
    update = _update()
    authority = open_server_activity_audit_authority(clock=MutableClock())
    receipt = authority.issue(update)

    forged = object.__new__(ActivityAuditReceipt)
    for field_name in (
        "receipt_id",
        "scope",
        "role",
        "work_item_id",
        "role_assignment_sha256",
        "cursor",
        "activity_update_sha256",
        "activity_scope_sha256",
        "validated_at_utc",
    ):
        object.__setattr__(forged, field_name, getattr(receipt, field_name))
    object.__setattr__(forged, "cursor", receipt.cursor + 1)

    with pytest.raises(ActivityContractError, match="^activity_audit_update_scope_mismatch$"):
        authority.verify_issued(forged, update=update)
    assert authority.verify(forged, update=update) is False
    with pytest.raises(ActivityContractError, match="^activity_audit_update_mismatch$"):
        authority.verify_issued(receipt, update=_update(cursor=2))
    other_authority = open_server_activity_audit_authority(clock=MutableClock())
    with pytest.raises(ActivityContractError, match="^activity_audit_receipt_not_server_issued$"):
        other_authority.verify_issued(receipt, update=update)


def test_update_and_receipt_integrity_checks_detect_internal_tampering() -> None:
    update = _update()
    object.__setattr__(update, "blockers", ["mutable blocker"])
    with pytest.raises(ActivityContractError, match="^activity_blockers_tampered$"):
        update.validate_integrity()

    tampered_slice = _slice("implementation", "Public summary", CURRENT_PATH)
    object.__setattr__(tampered_slice, "paths", [CURRENT_PATH])
    with pytest.raises(ActivityContractError, match="^activity_slice_paths_tampered$"):
        tampered_slice.validate_integrity()

    valid_update = _update()
    receipt = open_server_activity_audit_authority(clock=MutableClock()).issue(valid_update)
    object.__setattr__(receipt, "activity_scope_sha256", "sha256:" + "b" * 64)
    with pytest.raises(ActivityContractError, match="^activity_audit_scope_digest_mismatch$"):
        receipt.validate_integrity(valid_update)


def test_each_required_activity_field_changes_the_update_digest() -> None:
    base = _update()
    variants = (
        replace(base, summary="Different summary"),
        replace(
            base,
            previous=replace(base.previous, summary="Different previous state"),  # type: ignore[arg-type]
        ),
        replace(base, current=replace(base.current, summary="Different current state")),
        replace(
            base,
            next=replace(base.next, summary="Different next state"),  # type: ignore[arg-type]
        ),
        replace(base, blockers=("Different blocker",)),
        replace(base, work_item_id="work:other", role_assignment_sha256=""),
        replace(base, role_assignment_sha256="sha256:" + "b" * 64),
        replace(base, cursor=2),
    )

    assert all(variant.content_sha256 != base.content_sha256 for variant in variants)


def test_server_clock_must_be_aware_and_receipt_time_is_canonical() -> None:
    clock = MutableClock(datetime(2026, 8, 11, 8, 0))
    authority = open_server_activity_audit_authority(clock=clock)
    with pytest.raises(ActivityContractError, match="^activity_audit_clock_invalid$"):
        authority.issue(_update())

    good_clock = MutableClock(NOW + timedelta(microseconds=12))
    receipt = open_server_activity_audit_authority(clock=good_clock).issue(_update())
    assert receipt.validated_at_utc == "2026-08-11T08:00:00.000012Z"

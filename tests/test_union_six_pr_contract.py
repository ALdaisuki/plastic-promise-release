from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path("scripts/verify_union_six_pr_contract.py")
SOURCE_PATH = Path("docs/standards/union-six-pr-contract.json")
PREVIOUS_SOURCE_PATH = Path("docs/standards/history/union-six-pr-contract-2026-08-11.3.json")


def _load_gate():
    spec = importlib.util.spec_from_file_location("verify_union_six_pr_contract", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _contract_payload() -> dict:
    return json.loads(SOURCE_PATH.read_text(encoding="utf-8"))


def _introduction_fixture_payload() -> dict:
    payload = _contract_payload()
    payload["revision"] = "2026-08-11.2"
    payload["revision_lineage"] = {
        "comparison": "sha256-raw-source-bytes",
        "mode": "repository-authority-introduction",
        "previous_canonical": None,
        "provenance": copy.deepcopy(payload["revision_lineage"]["provenance"]),
    }
    return payload


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _git(repo_root: Path, *args: str, commit_time: str | None = None) -> str:
    env = os.environ.copy()
    if commit_time is not None:
        env.update(
            {
                "GIT_AUTHOR_DATE": commit_time,
                "GIT_COMMITTER_DATE": commit_time,
            }
        )
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        env=env,
    )
    return completed.stdout.decode("utf-8").strip()


def _initialize_git_base(repo_root: Path) -> str:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q")
    (repo_root / "README.fixture.md").write_text("fixture base\n", encoding="utf-8")
    _git(repo_root, "add", "README.fixture.md")
    _git(
        repo_root,
        "-c",
        "user.name=Union Fixture",
        "-c",
        "user.email=union-fixture@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture base",
        commit_time="2026-08-11T00:00:00Z",
    )
    return _git(repo_root, "rev-parse", "HEAD")


def _binding(gate, contract: dict, digest: str) -> dict:
    return {
        "path": gate.CONTRACT_PATH.as_posix(),
        "schema": gate.CONTRACT_SCHEMA,
        "revision": contract["revision"],
        "sha256": digest,
        "digest_algorithm": gate.DIGEST_ALGORITHM,
    }


def _empty_bucket() -> dict:
    return {"state": "not-evidenced", "receipts": []}


def _ledger_payload(gate, contract: dict, digest: str) -> dict:
    requirements = {}
    for requirement_id, expected in gate.build_requirement_index(contract).items():
        requirements[requirement_id] = {
            **expected,
            "implementation": _empty_bucket(),
            "test": _empty_bucket(),
            "runtime": _empty_bucket(),
            "production": _empty_bucket(),
        }
    return {
        "schema": gate.EVIDENCE_LEDGER_SCHEMA,
        "contract": _binding(gate, contract, digest),
        "evidence_policy": {
            "classes": list(gate.EVIDENCE_CLASSES),
            "states": list(gate.EVIDENCE_STATES),
            "receipt_schema": {
                "required_fields": list(gate.RECEIPT_FIELDS),
                "id_prefix": "receipt:",
                "sha256_format": "lowercase-hex-64",
                "recorded_at_format": "utc-iso8601",
                "class_must_match_bucket": True,
                "state_must_match_bucket": True,
                "requirement_must_match_bucket": True,
                "content_addressed_ref_prefix": "repo:docs/evidence/",
                "authorities": list(gate.RECEIPT_AUTHORITIES),
                "attestation_kinds": list(gate.ATTESTATION_KINDS),
                "review_channels": list(gate.REVIEW_CHANNELS),
                "verified_requires_attested_authority": True,
                "git_boundary_required": True,
                "source_material_algorithm": gate.SOURCE_MATERIAL_ALGORITHM,
                "diff_material_algorithm": gate.DIFF_MATERIAL_ALGORITHM,
                "artifact_schema": gate.EVIDENCE_ARTIFACT_SCHEMA,
                "artifact_must_bind_receipt_fields": True,
            },
            "rules": ["Evidence is typed and never promoted across classes."],
        },
        "requirements": requirements,
    }


def _manifest_payload(gate, repo_root: Path, contract: dict, digest: str) -> dict:
    manifest = {
        "schema": gate.DERIVED_DOCUMENTS_SCHEMA,
        "contract": _binding(gate, contract, digest),
        "policy": {
            "enforcement_values": list(gate.ENFORCEMENT_VALUES),
            "tracked_drift_is_passing": False,
            "path_base": "repository-root",
            "required_locales": list(gate.REQUIRED_LOCALES),
            "content_set_algorithm": gate.CONTENT_SET_ALGORITHM,
            "content_set_sha256": "0" * 64,
            "rules": ["Required bilingual projections remain aligned."],
        },
        "documents": [
            {
                "id": "test-reference",
                "kind": "bilingual-markdown-pair",
                "enforcement": "required",
                "paths": {
                    "en": "docs/reference.md",
                    "zh_CN": "docs/reference.zh-CN.md",
                },
                "must_exist": True,
                "must_link_contract": True,
                "must_state_union_completion": True,
                "semantic_claims": [
                    {
                        "id": "reference-title",
                        "markers": {"en": ["# Reference"], "zh_CN": ["# Reference"]},
                    }
                ],
                "drift": None,
            }
        ],
        "assets": [],
    }
    manifest["policy"]["content_set_sha256"] = gate.manifest_content_set_sha256(repo_root, manifest)
    return manifest


def _write_manifest_documents(repo_root: Path, gate) -> None:
    target = f"/{gate.CONTRACT_PATH.as_posix()}"
    documents = {
        "docs/reference.md": gate.ENGLISH_UNION_COMPLETION_RULE,
        "docs/reference.zh-CN.md": gate.CHINESE_UNION_COMPLETION_RULE,
    }
    for relative_path, rule in documents.items():
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Reference\n\n[Union six-PR contract]({target})\n\n{rule}\n",
            encoding="utf-8",
        )


def _write_repository(repo_root: Path, payload: dict | None = None) -> dict:
    gate = _load_gate()
    base_revision = _initialize_git_base(repo_root)
    contract = copy.deepcopy(payload if payload is not None else _introduction_fixture_payload())
    source = repo_root / gate.CONTRACT_PATH
    _write_json(source, contract)
    digest = gate.source_sha256(source)
    for language, relative_path in gate.GENERATED_VIEWS.items():
        output = repo_root / relative_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            gate.render_contract(contract, source_digest=digest, language=language),
            encoding="utf-8",
        )
    ledger = _ledger_payload(gate, contract, digest)
    _write_manifest_documents(repo_root, gate)
    manifest = _manifest_payload(gate, repo_root, contract, digest)
    _write_json(repo_root / gate.EVIDENCE_LEDGER_PATH, ledger)
    _write_json(repo_root / gate.DERIVED_DOCUMENTS_PATH, manifest)
    _git(repo_root, "add", ".")
    _git(
        repo_root,
        "-c",
        "user.name=Union Fixture",
        "-c",
        "user.email=union-fixture@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture source",
        commit_time="2026-08-11T00:00:01Z",
    )
    source_revision = _git(repo_root, "rev-parse", "HEAD")
    boundary = gate.build_git_boundary(
        repo_root,
        base_revision=base_revision,
        source_revision=source_revision,
    )
    return {
        "contract": contract,
        "digest": digest,
        "ledger": ledger,
        "manifest": manifest,
        "boundary": boundary,
    }


def _load_fixture_json(repo_root: Path, relative_path: Path) -> dict:
    return json.loads((repo_root / relative_path).read_text(encoding="utf-8"))


def _refresh_manifest_content_digest(repo_root: Path, gate) -> None:
    manifest = _load_fixture_json(repo_root, gate.DERIVED_DOCUMENTS_PATH)
    manifest["policy"]["content_set_sha256"] = gate.manifest_content_set_sha256(repo_root, manifest)
    _write_json(repo_root / gate.DERIVED_DOCUMENTS_PATH, manifest)


def _receipt(
    gate,
    repo_root: Path,
    contract: dict,
    requirement_id: str,
    evidence_class: str,
    *,
    state: str,
    authority: str | None = None,
    review_channel: str = "none",
) -> dict:
    if authority is None:
        if state == "partial":
            authority = "repository"
        elif evidence_class in ("runtime", "production"):
            authority = "plastic-promise-server"
        else:
            authority = "github-protected-workflow"
    attestation_kind = {
        "repository": "content-addressed-artifact",
        "github-protected-workflow": "github-attestation",
        "plastic-promise-server": "server-signed-receipt",
    }[authority]
    artifact_path = (
        Path("docs/evidence")
        / f"{requirement_id.lower()}-{evidence_class}-{state}-{review_channel}.json"
    )
    base_revision = _git(repo_root, "rev-list", "--max-parents=0", "HEAD")
    source_revision = _git(repo_root, "rev-parse", "HEAD")
    boundary = gate.build_git_boundary(
        repo_root,
        base_revision=base_revision,
        source_revision=source_revision,
    )
    requirement_digest = gate.requirement_set_sha256(gate.build_requirement_index(contract))
    contract_digest = gate.source_sha256(repo_root / gate.CONTRACT_PATH)
    evidence_policy = _ledger_payload(gate, contract, contract_digest)["evidence_policy"]
    receipt = {
        "id": f"receipt:test-{requirement_id}-{evidence_class}-{state}-{review_channel}",
        "evidence_class": evidence_class,
        "state": state,
        "requirement_id": requirement_id,
        "contract_revision": contract["revision"],
        "contract_sha256": contract_digest,
        **boundary,
        "requirement_set_sha256": requirement_digest,
        "policy_sha256": gate.evidence_policy_sha256(evidence_policy),
        "authority": authority,
        "attestation_kind": attestation_kind,
        "issuer_id": "agent:independent-reviewer",
        "subject_id": "agent:implementation-author",
        "review_channel": review_channel,
        "exemption_contract_revision": None,
        "exemption_contract_sha256": None,
        "recorded_at": "2026-08-11T00:00:00Z",
    }
    artifact_payload = {
        "schema": gate.EVIDENCE_ARTIFACT_SCHEMA,
        **{
            field: receipt[field] for field in gate.RECEIPT_FIELDS if field not in ("sha256", "ref")
        },
    }
    _write_json(repo_root / artifact_path, artifact_payload)
    artifact_digest = hashlib.sha256((repo_root / artifact_path).read_bytes()).hexdigest()
    receipt["sha256"] = artifact_digest
    receipt["ref"] = f"repo:{artifact_path.as_posix()}#sha256={artifact_digest}"
    return receipt


def test_valid_union_contract_ledger_manifest_and_generated_views_pass(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)

    report = gate.verify_repository(tmp_path)

    assert report == {
        "contract": "docs/standards/union-six-pr-contract.json",
        "derived_documents_manifest": "docs/standards/union-six-pr-derived-documents.json",
        "evidence_ledger": "docs/standards/union-six-pr-evidence-ledger.json",
        "generated_views": [
            "docs/standards/union-six-pr-contract.md",
            "docs/standards/union-six-pr-contract.zh-CN.md",
        ],
        "pull_request_count": 6,
        "requirement_count": len(gate.build_requirement_index(written["contract"])),
        "revision_transition": "introduction-declared",
        "status": "valid",
    }


def test_requirement_index_is_exact_and_preserves_pr_group_ordinal_and_statement_digest():
    gate = _load_gate()
    contract = _contract_payload()

    index = gate.build_requirement_index(contract)

    expected_count = sum(
        len(pr[group]) for pr in contract["pull_requests"] for group in gate.PR_GROUPS
    )
    assert len(index) == expected_count
    first = contract["pull_requests"][0]["delivery_scope"][0]
    assert index[first["id"]] == {
        "pr_id": "PR1",
        "group": "delivery_scope",
        "ordinal": 1,
        "statement_sha256": gate._statement_sha256(first["statement"]),
    }


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_ledger_requirement_set_must_exactly_equal_contract(tmp_path: Path, mutation: str):
    gate = _load_gate()
    _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    if mutation == "missing":
        ledger["requirements"].pop(requirement_id)
        error = "ledger_requirement_missing"
    else:
        ledger["requirements"]["PR9-D99"] = copy.deepcopy(ledger["requirements"][requirement_id])
        error = "ledger_requirement_extra"
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(gate.UnionContractError, match=error):
        gate.verify_repository(tmp_path)


@pytest.mark.parametrize("field,bad_value", [("pr_id", "PR6"), ("group", "required_evidence")])
def test_ledger_rejects_pr_or_group_mapping_drift(tmp_path: Path, field: str, bad_value: str):
    gate = _load_gate()
    _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    ledger["requirements"][requirement_id][field] = bad_value
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(
        gate.UnionContractError,
        match=rf"ledger_requirement_mapping_mismatch:{requirement_id}:{field}",
    ):
        gate.verify_repository(tmp_path)


@pytest.mark.parametrize("companion", ["ledger", "manifest"])
@pytest.mark.parametrize("field", ["revision", "sha256"])
def test_companions_are_bound_to_contract_revision_and_raw_sha(
    tmp_path: Path, companion: str, field: str
):
    gate = _load_gate()
    _write_repository(tmp_path)
    relative_path = (
        gate.EVIDENCE_LEDGER_PATH if companion == "ledger" else gate.DERIVED_DOCUMENTS_PATH
    )
    payload = _load_fixture_json(tmp_path, relative_path)
    payload["contract"][field] = "0" * 64 if field == "sha256" else "stale-revision"
    _write_json(tmp_path / relative_path, payload)

    with pytest.raises(
        gate.UnionContractError,
        match=rf"contract_binding_mismatch:{companion}.contract.{field}",
    ):
        gate.verify_repository(tmp_path)


@pytest.mark.parametrize(
    "source_class,target_class",
    [("implementation", "runtime"), ("test", "production")],
)
def test_lower_class_receipt_cannot_be_promoted_to_runtime_or_production_bucket(
    tmp_path: Path, source_class: str, target_class: str
):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    ledger["requirements"][requirement_id][target_class] = {
        "state": "verified",
        "receipts": [
            _receipt(
                gate,
                tmp_path,
                written["contract"],
                requirement_id,
                source_class,
                state="verified",
            )
        ],
    }
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(gate.UnionContractError, match="evidence_class_mismatch"):
        gate.verify_repository(tmp_path)


@pytest.mark.parametrize("evidence_class", ["implementation", "test", "runtime", "production"])
def test_partial_or_verified_state_requires_same_class_receipt(tmp_path: Path, evidence_class: str):
    gate = _load_gate()
    _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    ledger["requirements"][requirement_id][evidence_class]["state"] = "verified"
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(
        gate.UnionContractError,
        match=rf"evidence_receipt_required:{requirement_id}:{evidence_class}",
    ):
        gate.verify_repository(tmp_path)


def test_not_evidenced_state_forbids_receipts(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    ledger["requirements"][requirement_id]["test"]["receipts"] = [
        _receipt(
            gate,
            tmp_path,
            written["contract"],
            requirement_id,
            "test",
            state="partial",
        )
    ]
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(
        gate.UnionContractError,
        match=rf"evidence_receipt_forbidden:{requirement_id}:test",
    ):
        gate.verify_repository(tmp_path)


def test_production_verified_requires_runtime_verified_for_same_requirement(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    ledger["requirements"][requirement_id]["production"] = {
        "state": "verified",
        "receipts": [
            _receipt(
                gate,
                tmp_path,
                written["contract"],
                requirement_id,
                "production",
                state="verified",
            )
        ],
    }
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(
        gate.UnionContractError,
        match=rf"production_evidence_requires_verified_runtime:{requirement_id}",
    ):
        gate.verify_repository(tmp_path)


@pytest.mark.parametrize(
    "field,bad_value,error",
    [
        ("sha256", "ABC", "must_be_lowercase_hex_64"),
        ("recorded_at", "2026-08-11T00:00:00+08:00", "must_be_utc_iso8601"),
        ("ref", "line one\nline two", "must_be_single_line"),
    ],
)
def test_receipts_require_stable_bounded_typed_fields(
    tmp_path: Path, field: str, bad_value: str, error: str
):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    receipt = _receipt(
        gate,
        tmp_path,
        written["contract"],
        requirement_id,
        "runtime",
        state="partial",
    )
    receipt[field] = bad_value
    ledger["requirements"][requirement_id]["runtime"] = {
        "state": "partial",
        "receipts": [receipt],
    }
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(gate.UnionContractError, match=error):
        gate.verify_repository(tmp_path)


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("mutable-source", "must_be_full_git_object_id"),
        ("requirement-set", "receipt_requirement_set_mismatch"),
        ("missing-artifact", "receipt_artifact_missing"),
        ("local-verified", "verified_receipt_requires_attested_authority"),
        ("self-review", "review_receipt_requires_independent_issuer"),
    ],
)
def test_receipts_fail_closed_on_unbound_or_self_attested_evidence(
    tmp_path: Path, mutation: str, error: str
):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    state = "verified" if mutation in ("local-verified", "self-review") else "partial"
    authority = "repository" if mutation == "local-verified" else None
    review_channel = "standards" if mutation == "self-review" else "none"
    receipt = _receipt(
        gate,
        tmp_path,
        written["contract"],
        requirement_id,
        "test",
        state=state,
        authority=authority,
        review_channel=review_channel,
    )
    if mutation == "mutable-source":
        receipt["source_revision"] = "main"
    elif mutation == "requirement-set":
        receipt["requirement_set_sha256"] = "f" * 64
    elif mutation == "missing-artifact":
        receipt["ref"] = f"repo:docs/evidence/missing.json#sha256={receipt['sha256']}"
    elif mutation == "self-review":
        receipt["subject_id"] = receipt["issuer_id"]
    ledger["requirements"][requirement_id]["test"] = {
        "state": state,
        "receipts": [receipt],
    }
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(gate.UnionContractError, match=error):
        gate.verify_repository(tmp_path)


def test_hash_shaped_source_revision_must_resolve_to_a_real_git_commit(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    receipt = _receipt(
        gate,
        tmp_path,
        written["contract"],
        requirement_id,
        "test",
        state="verified",
    )
    receipt["source_revision"] = "0" * 40
    ledger["requirements"][requirement_id]["test"] = {
        "state": "verified",
        "receipts": [receipt],
    }
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(gate.UnionContractError, match="git_command_failed|git_object_not_commit"):
        gate.verify_repository(tmp_path)


@pytest.mark.parametrize("field", ["source_tree", "source_material_sha256", "diff_sha256"])
def test_receipt_must_match_the_resolved_git_boundary(tmp_path: Path, field: str):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    receipt = _receipt(
        gate,
        tmp_path,
        written["contract"],
        requirement_id,
        "test",
        state="verified",
    )
    receipt[field] = "f" * len(receipt[field])
    ledger["requirements"][requirement_id]["test"] = {
        "state": "verified",
        "receipts": [receipt],
    }
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(gate.UnionContractError, match=rf"receipt_git_boundary_mismatch:.*:{field}"):
        gate.verify_repository(tmp_path)


def test_receipt_must_match_the_exact_git_changed_path_set(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    receipt = _receipt(
        gate,
        tmp_path,
        written["contract"],
        requirement_id,
        "test",
        state="verified",
    )
    receipt["changed_paths"] = [*receipt["changed_paths"], "fabricated/path.py"]
    ledger["requirements"][requirement_id]["test"] = {
        "state": "verified",
        "receipts": [receipt],
    }
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(
        gate.UnionContractError,
        match=r"receipt_git_boundary_mismatch:.*:changed_paths",
    ):
        gate.verify_repository(tmp_path)


def test_evidence_artifact_json_deep_binds_receipt_fields(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    receipt = _receipt(
        gate,
        tmp_path,
        written["contract"],
        requirement_id,
        "test",
        state="verified",
    )
    artifact_path = Path(receipt["ref"].removeprefix("repo:").split("#", 1)[0])
    artifact = _load_fixture_json(tmp_path, artifact_path)
    artifact["issuer_id"] = "agent:different-reviewer"
    _write_json(tmp_path / artifact_path, artifact)
    artifact_digest = hashlib.sha256((tmp_path / artifact_path).read_bytes()).hexdigest()
    receipt["sha256"] = artifact_digest
    receipt["ref"] = f"repo:{artifact_path.as_posix()}#sha256={artifact_digest}"
    ledger["requirements"][requirement_id]["test"] = {
        "state": "verified",
        "receipts": [receipt],
    }
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(
        gate.UnionContractError,
        match=r"evidence_artifact_binding_mismatch:.*:issuer_id",
    ):
        gate.verify_repository(tmp_path)


def test_standards_and_spec_reviews_must_share_one_git_boundary(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    ledger = _load_fixture_json(tmp_path, gate.EVIDENCE_LEDGER_PATH)
    requirement_id = next(iter(ledger["requirements"]))
    standards = _receipt(
        gate,
        tmp_path,
        written["contract"],
        requirement_id,
        "test",
        state="verified",
        review_channel="standards",
    )

    (tmp_path / "second-boundary.txt").write_text("second boundary\n", encoding="utf-8")
    _git(tmp_path, "add", "second-boundary.txt")
    _git(
        tmp_path,
        "-c",
        "user.name=Union Fixture",
        "-c",
        "user.email=union-fixture@example.invalid",
        "commit",
        "-q",
        "-m",
        "second review boundary",
        commit_time="2026-08-11T00:00:02Z",
    )
    spec = _receipt(
        gate,
        tmp_path,
        written["contract"],
        requirement_id,
        "test",
        state="verified",
        review_channel="spec",
    )
    ledger["requirements"][requirement_id]["test"] = {
        "state": "verified",
        "receipts": [standards, spec],
    }
    _write_json(tmp_path / gate.EVIDENCE_LEDGER_PATH, ledger)

    with pytest.raises(gate.UnionContractError, match=r"review_pair_boundary_mismatch:PR1"):
        gate.verify_repository(tmp_path)


def test_manifest_requires_both_locale_paths(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    manifest["documents"][0]["paths"].pop("zh_CN")
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)

    with pytest.raises(gate.UnionContractError, match="missing_fields:zh_CN"):
        gate.verify_repository(tmp_path)


def test_manifest_fails_when_a_required_path_is_missing(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    manifest["documents"][0]["paths"]["en"] = "docs/does-not-exist.md"
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)
    _refresh_manifest_content_digest(tmp_path, gate)

    with pytest.raises(gate.UnionContractError, match="manifest_path_missing"):
        gate.verify_repository(tmp_path)


def test_manifest_required_document_must_link_canonical_contract(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    (tmp_path / "docs/reference.md").write_text(
        f"# Reference\n\n{gate.ENGLISH_UNION_COMPLETION_RULE}\n", encoding="utf-8"
    )
    _refresh_manifest_content_digest(tmp_path, gate)

    with pytest.raises(gate.UnionContractError, match="authority_link_missing"):
        gate.verify_repository(tmp_path)


def test_manifest_required_document_must_state_union_completion(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    target = f"/{gate.CONTRACT_PATH.as_posix()}"
    (tmp_path / "docs/reference.zh-CN.md").write_text(
        f"# Reference\n\n[联合六 PR 合同]({target})\n\n仅跟踪 PR 工作。\n",
        encoding="utf-8",
    )
    _refresh_manifest_content_digest(tmp_path, gate)

    with pytest.raises(gate.UnionContractError, match="union_completion_rule_missing"):
        gate.verify_repository(tmp_path)


def test_manifest_document_kind_is_a_closed_dispatched_enum(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    manifest["documents"][0]["kind"] = "markdown-ish"
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)

    with pytest.raises(gate.UnionContractError, match=r"manifest\.documents\[0\]\.kind"):
        gate.verify_repository(tmp_path)


def test_markdown_document_semantic_claims_are_verified_per_language(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    english = tmp_path / "docs/reference.md"
    english.write_text(
        english.read_text(encoding="utf-8").replace("# Reference", "# Unrelated"),
        encoding="utf-8",
    )
    _refresh_manifest_content_digest(tmp_path, gate)

    with pytest.raises(
        gate.UnionContractError,
        match=r"document_semantic_claim_missing:docs/reference\.md:reference-title:en",
    ):
        gate.verify_repository(tmp_path)


def test_language_neutral_governance_document_uses_one_path(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    governance_path = tmp_path / "GOVERNANCE.md"
    governance_path.write_text("# Governance\n\nsole canonical governance\n", encoding="utf-8")
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    manifest["documents"].append(
        {
            "id": "neutral-governance",
            "kind": "language-neutral-governance-document",
            "enforcement": "required",
            "path": "GOVERNANCE.md",
            "must_exist": True,
            "must_link_contract": False,
            "must_state_union_completion": False,
            "semantic_claims": [
                {
                    "id": "sole-authority",
                    "markers": {"neutral": ["sole canonical governance"]},
                }
            ],
            "drift": None,
        }
    )
    manifest["policy"]["content_set_sha256"] = gate.manifest_content_set_sha256(tmp_path, manifest)
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)

    gate.verify_repository(tmp_path)


def test_repository_manifest_tracks_only_landed_governance_documents_without_fake_pairs():
    gate = _load_gate()
    manifest = json.loads(gate.DERIVED_DOCUMENTS_PATH.read_text(encoding="utf-8"))
    documents = {entry["id"]: entry for entry in manifest["documents"]}

    for entry_id, expected_path in (
        ("agent-governance-protocol", "AGENTS.md"),
        ("coordination-glossary", "CONTEXT.md"),
    ):
        entry = documents[entry_id]
        assert entry["kind"] == "language-neutral-governance-document"
        assert entry["path"] == expected_path
        assert "paths" not in entry
    assert "delivery-program-contract-adr" not in documents


def test_manifest_content_set_digest_rejects_unregistered_content_drift(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    document = tmp_path / "docs/reference.md"
    document.write_text(
        document.read_text(encoding="utf-8") + "\nChanged claim with markers intact.\n",
        encoding="utf-8",
    )

    with pytest.raises(gate.UnionContractError, match="manifest_content_set_mismatch"):
        gate.verify_repository(tmp_path)


def test_asset_semantic_claims_survive_content_digest_refresh(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    revision = written["contract"]["revision"]
    english_path = tmp_path / "docs/asset.svg"
    chinese_path = tmp_path / "docs/asset.zh-CN.svg"
    english_path.write_text(
        f'<svg viewBox="0 0 10 10"><text><tspan>Union contract {revision}; '
        "server-only writer</tspan></text></svg>",
        encoding="utf-8",
    )
    chinese_path.write_text(
        f'<svg viewBox="0 0 10 10"><text><tspan>联合合同 {revision}；'
        "仅 server 可写</tspan></text></svg>",
        encoding="utf-8",
    )
    manifest["assets"].append(
        {
            "id": "semantic-asset",
            "kind": "bilingual-svg-pair",
            "enforcement": "required",
            "paths": {"en": "docs/asset.svg", "zh_CN": "docs/asset.zh-CN.svg"},
            "must_exist": True,
            "must_have_semantic_parity": True,
            "must_bind_contract_revision": True,
            "semantic_claims": [
                {
                    "id": "server-writer",
                    "markers": {
                        "en": ["server-only writer"],
                        "zh_CN": ["仅 server 可写"],
                    },
                }
            ],
            "drift": None,
        }
    )
    manifest["policy"]["content_set_sha256"] = gate.manifest_content_set_sha256(tmp_path, manifest)
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)
    gate.verify_repository(tmp_path)

    chinese_path.write_text(
        f'<svg viewBox="0 0 10 10"><text><tspan>联合合同 {revision}；'
        "compute 可写 SQLite</tspan></text></svg>",
        encoding="utf-8",
    )
    _refresh_manifest_content_digest(tmp_path, gate)

    with pytest.raises(gate.UnionContractError, match="asset_semantic_claim_missing"):
        gate.verify_repository(tmp_path)


def test_svg_semantic_claims_only_read_visible_text_nodes(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    revision = written["contract"]["revision"]
    for language, path, visible, hidden in (
        ("en", "docs/visible.svg", "visible status", "hidden canonical claim"),
        ("zh_CN", "docs/visible.zh-CN.svg", "可见状态", "隐藏规范声明"),
    ):
        del language
        (tmp_path / path).write_text(
            f'<svg viewBox="0 0 10 10"><metadata>{hidden}</metadata>'
            f"<text>Union {revision}; {visible}</text></svg>",
            encoding="utf-8",
        )
    manifest["assets"].append(
        {
            "id": "visible-only",
            "kind": "bilingual-svg-pair",
            "enforcement": "required",
            "paths": {"en": "docs/visible.svg", "zh_CN": "docs/visible.zh-CN.svg"},
            "must_exist": True,
            "must_have_semantic_parity": True,
            "must_bind_contract_revision": True,
            "semantic_claims": [
                {
                    "id": "hidden-claim",
                    "markers": {
                        "en": ["hidden canonical claim"],
                        "zh_CN": ["隐藏规范声明"],
                    },
                }
            ],
            "drift": None,
        }
    )
    manifest["policy"]["content_set_sha256"] = gate.manifest_content_set_sha256(tmp_path, manifest)
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)

    with pytest.raises(gate.UnionContractError, match="asset_semantic_claim_missing"):
        gate.verify_repository(tmp_path)


@pytest.mark.parametrize(
    "unsafe,error",
    [
        ("<script>noop()</script>", "asset_svg_unsafe_element"),
        ("<foreignObject><p>unsafe</p></foreignObject>", "asset_svg_unsafe_element"),
        ('<g onclick="noop()"/>', "asset_svg_unsafe_attribute"),
        ('<a href="https://example.invalid"/>', "asset_svg_external_href"),
    ],
)
def test_svg_rejects_active_or_external_content(tmp_path: Path, unsafe: str, error: str):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    revision = written["contract"]["revision"]
    for path, claim in (
        ("docs/unsafe.svg", "safe claim"),
        ("docs/unsafe.zh-CN.svg", "安全声明"),
    ):
        (tmp_path / path).write_text(
            f'<svg viewBox="0 0 10 10">{unsafe}<text>{revision}; {claim}</text></svg>',
            encoding="utf-8",
        )
    manifest["assets"].append(
        {
            "id": "unsafe-svg",
            "kind": "bilingual-svg-pair",
            "enforcement": "required",
            "paths": {"en": "docs/unsafe.svg", "zh_CN": "docs/unsafe.zh-CN.svg"},
            "must_exist": True,
            "must_have_semantic_parity": True,
            "must_bind_contract_revision": True,
            "semantic_claims": [
                {
                    "id": "safe-claim",
                    "markers": {"en": ["safe claim"], "zh_CN": ["安全声明"]},
                }
            ],
            "drift": None,
        }
    )
    manifest["policy"]["content_set_sha256"] = gate.manifest_content_set_sha256(tmp_path, manifest)
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)

    with pytest.raises(gate.UnionContractError, match=error):
        gate.verify_repository(tmp_path)


def test_svg_parity_includes_style_and_transform_attributes(tmp_path: Path):
    gate = _load_gate()
    written = _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    revision = written["contract"]["revision"]
    english = (
        f'<svg viewBox="0 0 10 10"><g transform="translate(1 1)" style="fill:#fff">'
        f"<text>Union contract {revision}; bounded</text></g></svg>"
    )
    chinese = (
        f'<svg viewBox="0 0 10 10"><g transform="translate(2 2)" style="fill:#000">'
        f"<text>联合合同 {revision}；有界</text></g></svg>"
    )
    (tmp_path / "docs/style.svg").write_text(english, encoding="utf-8")
    (tmp_path / "docs/style.zh-CN.svg").write_text(chinese, encoding="utf-8")
    manifest["assets"].append(
        {
            "id": "style-asset",
            "kind": "bilingual-svg-pair",
            "enforcement": "required",
            "paths": {"en": "docs/style.svg", "zh_CN": "docs/style.zh-CN.svg"},
            "must_exist": True,
            "must_have_semantic_parity": True,
            "must_bind_contract_revision": True,
            "semantic_claims": [
                {"id": "bounded", "markers": {"en": ["bounded"], "zh_CN": ["有界"]}}
            ],
            "drift": None,
        }
    )
    manifest["policy"]["content_set_sha256"] = gate.manifest_content_set_sha256(tmp_path, manifest)
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)

    with pytest.raises(gate.UnionContractError, match="asset_semantic_parity_mismatch"):
        gate.verify_repository(tmp_path)


def test_tracked_drift_requires_explicit_record_and_still_blocks_gate(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    document = manifest["documents"][0]
    document["enforcement"] = "tracked-drift"
    document["drift"] = {
        "id": "DRIFT-001",
        "detected_on": "2026-08-11",
        "blocking": True,
        "reason": {"en": "Known mismatch.", "zh_CN": "已知不一致。"},
        "issue": "issue:DRIFT-001",
        "reference": "docs/reference.md",
        "resolution": {"en": "Regenerate both views.", "zh_CN": "重新生成双语视图。"},
    }
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)

    with pytest.raises(
        gate.UnionContractError,
        match="tracked_drift_blocks_gate:DRIFT-001|tracked_drift_blocks_gate:test-reference",
    ):
        gate.verify_repository(tmp_path)


@pytest.mark.parametrize("missing_field", ["reason", "issue", "reference", "resolution"])
def test_tracked_drift_record_requires_reason_issue_reference_and_resolution(
    tmp_path: Path, missing_field: str
):
    gate = _load_gate()
    _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    document = manifest["documents"][0]
    document["enforcement"] = "tracked-drift"
    document["drift"] = {
        "id": "DRIFT-001",
        "detected_on": "2026-08-11",
        "blocking": True,
        "reason": {"en": "Known mismatch.", "zh_CN": "已知不一致。"},
        "issue": "issue:DRIFT-001",
        "reference": "docs/reference.md",
        "resolution": {"en": "Regenerate both views.", "zh_CN": "重新生成双语视图。"},
    }
    document["drift"].pop(missing_field)
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)

    with pytest.raises(gate.UnionContractError, match=rf"missing_fields:{missing_field}"):
        gate.verify_repository(tmp_path)


def test_manifest_rejects_unknown_fields(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    manifest = _load_fixture_json(tmp_path, gate.DERIVED_DOCUMENTS_PATH)
    manifest["documents"][0]["silent_exception"] = True
    _write_json(tmp_path / gate.DERIVED_DOCUMENTS_PATH, manifest)

    with pytest.raises(gate.UnionContractError, match="unknown_fields:silent_exception"):
        gate.verify_repository(tmp_path)


def test_exact_requirement_id_cannot_mutate_while_keywords_remain(tmp_path: Path):
    gate = _load_gate()
    payload = _contract_payload()
    payload["pull_requests"][3]["collaboration_scope"][1]["id"] = "PR4-C99"
    source = tmp_path / "contract.json"
    _write_json(source, payload)

    with pytest.raises(
        gate.UnionContractError,
        match=r"PR4\.collaboration_scope\[1\]\.id must equal|scope_or_ordinal_mismatch:PR4-C99",
    ):
        gate.validate_contract_source(source)


def test_scope_semantics_mutation_cannot_pass_with_stale_companions(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    source = tmp_path / gate.CONTRACT_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    item = payload["pull_requests"][3]["collaboration_scope"][1]
    item["statement"]["en"] = item["statement"]["en"].replace(
        "peer relevance never changes canonical-memory scores",
        "peer relevance always changes canonical-memory scores",
    )
    _write_json(source, payload)
    digest = gate.source_sha256(source)
    for language, relative_path in gate.GENERATED_VIEWS.items():
        (tmp_path / relative_path).write_text(
            gate.render_contract(payload, source_digest=digest, language=language),
            encoding="utf-8",
        )

    with pytest.raises(
        gate.UnionContractError, match="contract_binding_mismatch:ledger.contract.sha256"
    ):
        gate.verify_repository(tmp_path)


def test_changed_contract_bytes_require_revision_change_when_previous_source_is_supplied(
    tmp_path: Path,
):
    gate = _load_gate()
    previous_payload = _contract_payload()
    previous_path = tmp_path / "previous-contract.json"
    _write_json(previous_path, previous_payload)
    current_payload = copy.deepcopy(previous_payload)
    current_payload["pull_requests"][0]["objective"]["en"] += " Changed."
    _write_repository(tmp_path / "current", current_payload)

    with pytest.raises(gate.UnionContractError, match="contract_revision_not_incremented"):
        gate.verify_repository(
            tmp_path / "current",
            previous_contract=previous_path,
        )


@pytest.mark.parametrize(
    "revision,previous_revision",
    [
        ("2026-08-11.next", "2026-08-11.1"),
        ("2026-08-11.1", "2026-08-11.2"),
        ("2026-02-30.1", "2026-02-28.9"),
    ],
)
def test_contract_revision_is_well_formed_and_strictly_monotonic(
    tmp_path: Path, revision: str, previous_revision: str
):
    gate = _load_gate()
    payload = _contract_payload()
    payload["revision"] = revision
    payload["revision_lineage"] = {
        "comparison": gate.DIGEST_ALGORITHM,
        "mode": "successor",
        "previous_canonical": {
            "revision": previous_revision,
            "sha256": "a" * 64,
            "reference": f"sha256:{'a' * 64}",
        },
        "provenance": [],
    }
    source = tmp_path / "contract.json"
    _write_json(source, payload)

    with pytest.raises(gate.UnionContractError, match="contract_schema_invalid"):
        gate.validate_contract_source(source)


def test_revision_transition_binds_previous_revision_and_raw_sha(tmp_path: Path):
    gate = _load_gate()
    previous_payload = _introduction_fixture_payload()
    previous_path = tmp_path / "previous-contract.json"
    _write_json(previous_path, previous_payload)
    previous_digest = hashlib.sha256(previous_path.read_bytes()).hexdigest()
    current_payload = copy.deepcopy(previous_payload)
    current_payload["revision"] = "2026-08-11.3"
    current_payload["revision_lineage"] = {
        "comparison": gate.DIGEST_ALGORITHM,
        "mode": "successor",
        "previous_canonical": {
            "revision": previous_payload["revision"],
            "sha256": previous_digest,
            "reference": f"sha256:{previous_digest}",
        },
        "provenance": previous_payload["revision_lineage"]["provenance"],
    }
    current_payload["pull_requests"][0]["objective"]["en"] += " Changed."
    _write_repository(tmp_path / "current", current_payload)

    report = gate.verify_repository(
        tmp_path / "current",
        previous_contract=previous_path,
    )

    assert report["revision_transition"] == "successor-verified"


def test_current_revision_introduces_repository_authority_and_audit_digest_is_provenance_only():
    payload = _contract_payload()
    lineage = payload["revision_lineage"]

    assert payload["revision"] == "2026-08-18.1"
    assert lineage["mode"] == "repository-authority-introduction"
    assert lineage["previous_canonical"] is None
    assert lineage["provenance"] == [
        {
            "classification": "provenance-only",
            "claimed_revision": "2026-08-11.1",
            "sha256": "c5c3b197f6fa192d8a7248b3b37e65c6622d55c4bd09fcd05bc4df23373e7b02",
            "reference": (
                "audit-preimage:union-six-pr-contract/2026-08-11.1/"
                "c5c3b197f6fa192d8a7248b3b37e65c6622d55c4bd09fcd05bc4df23373e7b02"
            ),
            "verifiable_canonical_source": False,
        }
    ]


def test_pr5_contract_owns_compute_node_inference_boundary_and_hot_routing():
    pr5 = next(pr for pr in _contract_payload()["pull_requests"] if pr["id"] == "PR5")

    delivery = {item["id"]: item["statement"] for item in pr5["delivery_scope"]}
    collaboration = {item["id"]: item["statement"] for item in pr5["collaboration_scope"]}
    evidence = {item["id"]: item["statement"] for item in pr5["required_evidence"]}

    assert set(delivery) >= {
        "PR5-D04",
        "PR5-D05",
        "PR5-D06",
        "PR5-D07",
        "PR5-D08",
    }
    assert "pp-server-backend MUST NOT construct or invoke" in delivery["PR5-D04"]["en"]
    assert "Only pp-compute-node" in delivery["PR5-D04"]["en"]
    assert "local, cloud, and hybrid" in delivery["PR5-D05"]["en"]
    assert "without MCP restart" in delivery["PR5-D05"]["en"]
    assert "write-only" in delivery["PR5-D06"]["en"]
    assert "identity revalidation receipt" in delivery["PR5-D06"]["en"]
    assert "structured JSON" in delivery["PR5-D07"]["en"]
    assert "embedding and structured JSON defer" in delivery["PR5-D08"]["en"]
    assert "rerank returns original-order" in delivery["PR5-D08"]["en"]

    assert set(collaboration) >= {"PR5-C08", "PR5-C09", "PR5-C10"}
    assert "distinct operation policy and adapter class" in collaboration["PR5-C08"]["en"]
    assert "never emitted as collaboration events" in collaboration["PR5-C09"]["en"]
    assert "preserves in-flight leases and receipts" in collaboration["PR5-C10"]["en"]

    assert set(evidence) >= {"PR5-E07", "PR5-E08", "PR5-E09"}
    assert "server-side provider fallback" in evidence["PR5-E09"]["en"]


def test_server_is_the_only_canonical_clock_authority_without_host_timezone_mutation():
    invariant = next(
        item
        for item in _contract_payload()["cross_cutting_invariants"]
        if item["id"] == "U6-INV-14"
    )

    english = invariant["statement"]["en"]
    chinese = invariant["statement"]["zh_CN"]
    assert "server backend is the sole canonical clock authority" in english
    assert "server-issued" in english
    assert "MUST NOT influence ordering, lease expiry, fencing, idempotency" in english
    assert "MUST NOT write localized values back as authority" in english
    assert "must not mutate Linux, macOS, Windows, WSL2" in english
    assert "服务器后端是唯一 canonical 时钟权威" in chinese
    assert "不得影响排序、租约过期、fence、幂等、回执、晋升或游标推进" in chinese
    assert "不得把本地化时间回写为权威值" in chinese


def test_first_repository_authority_is_verified_against_git_base_without_contract(
    tmp_path: Path,
):
    gate = _load_gate()
    written = _write_repository(tmp_path, _introduction_fixture_payload())

    report = gate.verify_repository(
        tmp_path,
        base_revision=written["boundary"]["base_revision"],
        source_revision=written["boundary"]["source_revision"],
    )

    assert report["revision_transition"] == "introduction-verified"


def test_generated_views_remain_byte_exact_and_digest_bound(tmp_path: Path):
    gate = _load_gate()
    _write_repository(tmp_path)
    english = tmp_path / gate.GENERATED_VIEWS["en"]
    english.write_text(english.read_text(encoding="utf-8") + "\nmanual drift\n", encoding="utf-8")

    with pytest.raises(gate.UnionContractError, match="generated_view_drift"):
        gate.verify_repository(tmp_path)


def test_ci_and_make_preserve_the_git_revision_boundary_gate():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert "documentation-parity:" in workflow
    assert "fetch-depth: 0" in workflow
    assert "PP_BASE_SHA:" in workflow
    assert "PP_SOURCE_SHA:" in workflow
    assert '--base-revision "$base_sha"' in workflow
    assert '--source-revision "$source_sha"' in workflow
    assert "BASE_REVISION ?=" in makefile
    assert "SOURCE_REVISION ?=" in makefile
    assert (
        "PREVIOUS_CONTRACT ?= docs/standards/history/union-six-pr-contract-2026-08-11.3.json"
    ) in makefile
    assert '--base-revision "$(BASE_REVISION)"' in makefile
    assert '--source-revision "$(SOURCE_REVISION)"' in makefile
    assert "--previous-contract" in workflow
    assert "docs/standards/history/union-six-pr-contract-2026-08-11.3.json" in workflow


def test_repository_union_contract_gate_matches_manifest_drift_state():
    gate = _load_gate()
    manifest = json.loads(gate.DERIVED_DOCUMENTS_PATH.read_text(encoding="utf-8"))
    tracked_ids = [
        entry["id"]
        for group in ("documents", "assets")
        for entry in manifest[group]
        if entry["enforcement"] == "tracked-drift"
    ]

    if tracked_ids:
        with pytest.raises(
            gate.UnionContractError,
            match=r"tracked_drift_blocks_gate:",
        ):
            gate.verify_repository(
                Path.cwd(),
                previous_contract=PREVIOUS_SOURCE_PATH,
            )
        return

    report = gate.verify_repository(
        Path.cwd(),
        previous_contract=PREVIOUS_SOURCE_PATH,
    )
    assert report["pull_request_count"] == 6
    assert report["requirement_count"] > 0
    assert report["status"] == "valid"

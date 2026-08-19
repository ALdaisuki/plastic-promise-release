#!/usr/bin/env python3
"""Fail closed when the authoritative union six-PR contract drifts.

The canonical JSON owns scope.  Its generated views, evidence ledger, and
derived-document manifest are projections bound to the canonical revision and
raw-source SHA-256.  This verifier deliberately derives the requirement index
from the canonical source instead of maintaining a second keyword-based copy
of product responsibilities.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_renderer():
    path = Path(__file__).with_name("render_union_six_pr_contract.py")
    spec = importlib.util.spec_from_file_location("union_six_pr_renderer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load union contract renderer: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RENDERER = _load_renderer()
CONTRACT_PATH = Path(_RENDERER.SOURCE_RELATIVE)
GENERATED_VIEWS: dict[str, Path] = dict(_RENDERER.GENERATED_PATHS)
EVIDENCE_LEDGER_PATH = Path("docs/standards/union-six-pr-evidence-ledger.json")
DERIVED_DOCUMENTS_PATH = Path("docs/standards/union-six-pr-derived-documents.json")

CONTRACT_SCHEMA = "plastic-promise/union-six-pr-contract/v1"
EVIDENCE_LEDGER_SCHEMA = "plastic-promise/union-six-pr-evidence-ledger/v1"
DERIVED_DOCUMENTS_SCHEMA = "plastic-promise/union-six-pr-derived-documents/v1"
DIGEST_ALGORITHM = "sha256-raw-source-bytes"
LANGUAGES = ("en", "zh_CN")
PR_GROUPS = ("delivery_scope", "collaboration_scope", "required_evidence")
GROUP_PREFIXES = {
    "delivery_scope": "D",
    "collaboration_scope": "C",
    "required_evidence": "E",
}
EVIDENCE_CLASSES = ("implementation", "test", "runtime", "production")
EVIDENCE_STATES = ("not-evidenced", "partial", "verified", "not-applicable")
RECEIPT_FIELDS = (
    "id",
    "evidence_class",
    "state",
    "requirement_id",
    "contract_revision",
    "contract_sha256",
    "base_revision",
    "base_tree",
    "source_revision",
    "source_tree",
    "source_material_sha256",
    "diff_sha256",
    "changed_paths",
    "requirement_set_sha256",
    "policy_sha256",
    "authority",
    "attestation_kind",
    "issuer_id",
    "subject_id",
    "review_channel",
    "sha256",
    "ref",
    "exemption_contract_revision",
    "exemption_contract_sha256",
    "recorded_at",
)
RECEIPT_AUTHORITIES = (
    "repository",
    "github-protected-workflow",
    "plastic-promise-server",
)
ATTESTATION_KINDS = (
    "content-addressed-artifact",
    "github-attestation",
    "server-signed-receipt",
)
REVIEW_CHANNELS = ("none", "standards", "spec", "deepsec")
LINEAGE_MODES = ("repository-authority-introduction", "successor")
SOURCE_MATERIAL_ALGORITHM = "sha256-git-ls-tree-rz-full-tree-v1"
DIFF_MATERIAL_ALGORITHM = "sha256-git-diff-raw-z-no-abbrev-no-renames-v1"
EVIDENCE_ARTIFACT_SCHEMA = "plastic-promise/evidence-artifact/v1"
ENFORCEMENT_VALUES = ("required", "tracked-drift")
REQUIRED_LOCALES = ("en", "zh_CN")
DOCUMENT_KINDS = ("bilingual-markdown-pair", "language-neutral-governance-document")
NEUTRAL_LOCALE = "neutral"
CONTENT_SET_ALGORITHM = "sha256-entry-id-locale-path-content-v1"

ENGLISH_UNION_COMPLETION_RULE = (
    "A PR is complete only when its delivery scope, collaboration scope, and required "
    "evidence all pass; one-sided completion is not PR completion."
)
CHINESE_UNION_COMPLETION_RULE = (
    "只有交付范围、协作范围和所需证据全部通过，PR 才算完成；任一单侧完成都不等于 PR 完成。"
)

_DIGEST_RE = re.compile(r"SHA-256: (?P<digest>[0-9a-f]{64}) -->")
_HEX_64_RE = re.compile(r"[0-9a-f]{64}\Z")
_REQUIREMENT_ID_RE = re.compile(r"PR(?P<pr>[1-6])-(?P<group>[DCE])(?P<ordinal>[0-9]{2})\Z")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)]+)\)")
_RECEIPT_ID_RE = re.compile(r"receipt:[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,247}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CONTENT_ADDRESSED_REF_RE = re.compile(r"repo:(?P<path>[^#]+)#sha256=(?P<digest>[0-9a-f]{64})\Z")
_MERMAID_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_MERMAID_LABEL_RE = re.compile(r"(\[|\{|\()([^\]\}\)]*)(\]|\}|\))")


class UnionContractError(ValueError):
    """Raised when a union-contract guarantee is lost."""


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UnionContractError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _load_json(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise UnionContractError(f"{label}_missing:{path}") from exc
    except OSError as exc:
        raise UnionContractError(f"{label}_unreadable:{path}:{exc}") from exc
    try:
        payload = json.loads(raw, object_pairs_hook=_duplicate_rejecting_object)
    except UnionContractError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UnionContractError(f"{label}_json_invalid:{exc}") from exc
    if not isinstance(payload, Mapping):
        raise UnionContractError(f"{label}_schema_invalid:root_must_be_object")
    return payload


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UnionContractError(f"schema_invalid:{path}:must_be_object")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise UnionContractError(f"schema_invalid:{path}:must_be_array")
    return value


def _require_text(value: Any, path: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UnionContractError(f"schema_invalid:{path}:must_be_non_empty_string")
    if value != value.strip() or len(value) > max_length:
        raise UnionContractError(f"schema_invalid:{path}:invalid_string_bounds")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise UnionContractError(f"schema_invalid:{path}:must_be_boolean")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: Sequence[str], path: str) -> None:
    expected_set = set(expected)
    actual_set = set(value)
    missing = sorted(expected_set - actual_set)
    unknown = sorted(actual_set - expected_set)
    if missing:
        raise UnionContractError(f"schema_invalid:{path}:missing_fields:{','.join(missing)}")
    if unknown:
        raise UnionContractError(f"schema_invalid:{path}:unknown_fields:{','.join(unknown)}")


def _require_string_list(
    value: Any,
    path: str,
    *,
    allow_empty: bool = False,
    exact: Sequence[str] | None = None,
    max_length: int = 256,
) -> list[str]:
    items = _require_list(value, path)
    result = [
        _require_text(item, f"{path}[{index}]", max_length=max_length)
        for index, item in enumerate(items)
    ]
    if not allow_empty and not result:
        raise UnionContractError(f"schema_invalid:{path}:must_not_be_empty")
    if len(result) != len(set(result)):
        raise UnionContractError(f"schema_invalid:{path}:duplicate_value")
    if exact is not None and result != list(exact):
        raise UnionContractError(f"schema_invalid:{path}:must_equal:{','.join(exact)}")
    return result


def _validate_localized(value: Any, path: str) -> Mapping[str, Any]:
    localized = _require_mapping(value, path)
    _require_exact_keys(localized, LANGUAGES, path)
    for language in LANGUAGES:
        _require_text(localized[language], f"{path}.{language}")
    return localized


def _validate_rule_items(value: Any, path: str) -> None:
    rules = _require_list(value, path)
    if not rules:
        raise UnionContractError(f"schema_invalid:{path}:must_not_be_empty")
    seen: set[str] = set()
    for index, raw_rule in enumerate(rules):
        item_path = f"{path}[{index}]"
        rule = _require_mapping(raw_rule, item_path)
        _require_exact_keys(rule, ("id", "statement"), item_path)
        rule_id = _require_text(rule["id"], f"{item_path}.id", max_length=128)
        if rule_id in seen:
            raise UnionContractError(f"schema_invalid:{path}:duplicate_id:{rule_id}")
        seen.add(rule_id)
        _validate_localized(rule["statement"], f"{item_path}.statement")


def _validate_policy_rules(value: Any, path: str) -> None:
    _require_string_list(value, path, max_length=1024)


def _validate_contract_closed_shape(contract: Mapping[str, Any]) -> None:
    _require_exact_keys(
        contract,
        (
            "schema",
            "revision",
            "revision_lineage",
            "authority",
            "source_precedence",
            "governance_artifacts",
            "experimental_features",
            "cross_cutting_invariants",
            "completion_gates",
            "pull_requests",
        ),
        "contract",
    )
    if contract["schema"] != CONTRACT_SCHEMA:
        raise UnionContractError("schema_invalid:contract.schema")
    _require_text(contract["revision"], "contract.revision", max_length=128)
    revision_lineage = _require_mapping(contract["revision_lineage"], "contract.revision_lineage")
    _require_exact_keys(
        revision_lineage,
        ("comparison", "mode", "previous_canonical", "provenance"),
        "contract.revision_lineage",
    )
    if revision_lineage["comparison"] != DIGEST_ALGORITHM:
        raise UnionContractError("schema_invalid:contract.revision_lineage.comparison")
    mode = revision_lineage["mode"]
    if mode not in LINEAGE_MODES:
        raise UnionContractError("schema_invalid:contract.revision_lineage.mode")
    previous = revision_lineage["previous_canonical"]
    if mode == "repository-authority-introduction":
        if previous is not None:
            raise UnionContractError(
                "schema_invalid:contract.revision_lineage.previous_canonical:"
                "must_be_null_for_introduction"
            )
    else:
        previous_mapping = _require_mapping(
            previous, "contract.revision_lineage.previous_canonical"
        )
        _require_exact_keys(
            previous_mapping,
            ("revision", "sha256", "reference"),
            "contract.revision_lineage.previous_canonical",
        )
        _require_text(
            previous_mapping["revision"],
            "contract.revision_lineage.previous_canonical.revision",
            max_length=128,
        )
        if (
            not isinstance(previous_mapping["sha256"], str)
            or _HEX_64_RE.fullmatch(previous_mapping["sha256"]) is None
        ):
            raise UnionContractError(
                "schema_invalid:contract.revision_lineage.previous_canonical.sha256:"
                "must_be_lowercase_hex_64"
            )
        _require_text(
            previous_mapping["reference"],
            "contract.revision_lineage.previous_canonical.reference",
            max_length=1024,
        )

    provenance = _require_list(
        revision_lineage["provenance"], "contract.revision_lineage.provenance"
    )
    for index, raw_entry in enumerate(provenance):
        path = f"contract.revision_lineage.provenance[{index}]"
        entry = _require_mapping(raw_entry, path)
        _require_exact_keys(
            entry,
            (
                "classification",
                "claimed_revision",
                "sha256",
                "reference",
                "verifiable_canonical_source",
            ),
            path,
        )
        if entry["classification"] != "provenance-only":
            raise UnionContractError(f"schema_invalid:{path}.classification")
        _require_text(entry["claimed_revision"], f"{path}.claimed_revision", max_length=128)
        if not isinstance(entry["sha256"], str) or _HEX_64_RE.fullmatch(entry["sha256"]) is None:
            raise UnionContractError(f"schema_invalid:{path}.sha256:must_be_lowercase_hex_64")
        _require_text(entry["reference"], f"{path}.reference", max_length=1024)
        if entry["verifiable_canonical_source"] is not False:
            raise UnionContractError(
                f"schema_invalid:{path}.verifiable_canonical_source:must_be_false"
            )

    authority = _require_mapping(contract["authority"], "contract.authority")
    _require_exact_keys(
        authority,
        (
            "normative",
            "canonical_source",
            "generated_views",
            "digest_algorithm",
            "scope",
            "change_control",
        ),
        "contract.authority",
    )
    if authority["normative"] is not True:
        raise UnionContractError("schema_invalid:contract.authority.normative")
    if authority["canonical_source"] != CONTRACT_PATH.as_posix():
        raise UnionContractError("schema_invalid:contract.authority.canonical_source")
    _require_string_list(
        authority["generated_views"],
        "contract.authority.generated_views",
        exact=[path.as_posix() for path in GENERATED_VIEWS.values()],
    )
    if authority["digest_algorithm"] != DIGEST_ALGORITHM:
        raise UnionContractError("schema_invalid:contract.authority.digest_algorithm")
    _validate_localized(authority["scope"], "contract.authority.scope")
    _validate_rule_items(authority["change_control"], "contract.authority.change_control")

    precedence = _require_list(contract["source_precedence"], "contract.source_precedence")
    if not precedence:
        raise UnionContractError("schema_invalid:contract.source_precedence:must_not_be_empty")
    for index, raw_entry in enumerate(precedence):
        path = f"contract.source_precedence[{index}]"
        entry = _require_mapping(raw_entry, path)
        _require_exact_keys(entry, ("priority", "source", "classification", "rule"), path)
        if entry["priority"] != index + 1:
            raise UnionContractError(f"schema_invalid:{path}.priority")
        _require_text(entry["source"], f"{path}.source")
        _require_text(entry["classification"], f"{path}.classification", max_length=128)
        _validate_localized(entry["rule"], f"{path}.rule")

    governance = _require_mapping(contract["governance_artifacts"], "contract.governance_artifacts")
    _require_exact_keys(
        governance,
        (
            "evidence_ledger",
            "derived_document_manifest",
            "evidence_classes",
            "evidence_states",
            "rules",
        ),
        "contract.governance_artifacts",
    )
    if governance["evidence_ledger"] != EVIDENCE_LEDGER_PATH.as_posix():
        raise UnionContractError("schema_invalid:contract.governance_artifacts.evidence_ledger")
    if governance["derived_document_manifest"] != DERIVED_DOCUMENTS_PATH.as_posix():
        raise UnionContractError(
            "schema_invalid:contract.governance_artifacts.derived_document_manifest"
        )
    _require_string_list(
        governance["evidence_classes"],
        "contract.governance_artifacts.evidence_classes",
        exact=EVIDENCE_CLASSES,
    )
    _require_string_list(
        governance["evidence_states"],
        "contract.governance_artifacts.evidence_states",
        exact=EVIDENCE_STATES,
    )
    _validate_rule_items(governance["rules"], "contract.governance_artifacts.rules")

    invariants = _require_list(
        contract["cross_cutting_invariants"], "contract.cross_cutting_invariants"
    )
    if not invariants:
        raise UnionContractError(
            "schema_invalid:contract.cross_cutting_invariants:must_not_be_empty"
        )
    invariant_ids: set[str] = set()
    for index, raw_invariant in enumerate(invariants):
        path = f"contract.cross_cutting_invariants[{index}]"
        invariant = _require_mapping(raw_invariant, path)
        _require_exact_keys(invariant, ("id", "title", "statement"), path)
        invariant_id = _require_text(invariant["id"], f"{path}.id", max_length=128)
        if invariant_id in invariant_ids:
            raise UnionContractError(f"schema_invalid:{path}.id:duplicate:{invariant_id}")
        invariant_ids.add(invariant_id)
        _validate_localized(invariant["title"], f"{path}.title")
        _validate_localized(invariant["statement"], f"{path}.statement")

    gates = _require_list(contract["completion_gates"], "contract.completion_gates")
    if not gates:
        raise UnionContractError("schema_invalid:contract.completion_gates:must_not_be_empty")
    gate_ids: set[str] = set()
    for index, raw_gate in enumerate(gates):
        path = f"contract.completion_gates[{index}]"
        gate = _require_mapping(raw_gate, path)
        _require_exact_keys(
            gate,
            ("id", "applies_to", "required_evidence_classes", "statement"),
            path,
        )
        gate_id = _require_text(gate["id"], f"{path}.id", max_length=128)
        if gate_id in gate_ids:
            raise UnionContractError(f"schema_invalid:{path}.id:duplicate:{gate_id}")
        gate_ids.add(gate_id)
        applies_to = _require_string_list(gate["applies_to"], f"{path}.applies_to")
        if not set(applies_to).issubset({f"PR{number}" for number in range(1, 7)}):
            raise UnionContractError(f"schema_invalid:{path}.applies_to:unknown_pr")
        evidence_classes = _require_string_list(
            gate["required_evidence_classes"], f"{path}.required_evidence_classes"
        )
        if not set(evidence_classes).issubset(EVIDENCE_CLASSES):
            raise UnionContractError(
                f"schema_invalid:{path}.required_evidence_classes:unknown_class"
            )
        _validate_localized(gate["statement"], f"{path}.statement")

    experimental = _require_list(
        contract["experimental_features"], "contract.experimental_features"
    )
    experimental_ids: set[str] = set()
    for index, raw_feature in enumerate(experimental):
        path = f"contract.experimental_features[{index}]"
        feature = _require_mapping(raw_feature, path)
        _require_exact_keys(
            feature,
            (
                "id",
                "owning_pr",
                "disposition",
                "activation",
                "rollback",
                "required_gate_id",
                "statement",
            ),
            path,
        )
        feature_id = _require_text(feature["id"], f"{path}.id", max_length=128)
        if feature_id in experimental_ids:
            raise UnionContractError(f"schema_invalid:{path}.id:duplicate:{feature_id}")
        experimental_ids.add(feature_id)
        if feature["owning_pr"] not in [f"PR{number}" for number in range(1, 7)]:
            raise UnionContractError(f"schema_invalid:{path}.owning_pr")
        _require_text(feature["disposition"], f"{path}.disposition", max_length=128)
        _require_text(feature["activation"], f"{path}.activation", max_length=128)
        _require_text(feature["rollback"], f"{path}.rollback", max_length=128)
        if feature["required_gate_id"] not in gate_ids:
            raise UnionContractError(f"schema_invalid:{path}.required_gate_id:unknown")
        _validate_localized(feature["statement"], f"{path}.statement")

    pull_requests = _require_list(contract["pull_requests"], "contract.pull_requests")
    expected_pr_ids = [f"PR{index}" for index in range(1, 7)]
    if [
        entry.get("id") if isinstance(entry, Mapping) else None for entry in pull_requests
    ] != expected_pr_ids:
        raise UnionContractError(
            "schema_invalid:contract.pull_requests:must_be_exactly_PR1_through_PR6"
        )
    for index, raw_pr in enumerate(pull_requests):
        path = f"contract.pull_requests[{index}]"
        pr = _require_mapping(raw_pr, path)
        _require_exact_keys(
            pr,
            (
                "id",
                "slug",
                "title",
                "objective",
                "depends_on",
                "required_gate_ids",
                "delivery_scope",
                "collaboration_scope",
                "required_evidence",
                "completion_rule",
            ),
            path,
        )
        pr_id = expected_pr_ids[index]
        _require_text(pr["slug"], f"{path}.slug", max_length=128)
        _validate_localized(pr["title"], f"{path}.title")
        _validate_localized(pr["objective"], f"{path}.objective")
        dependencies = _require_string_list(
            pr["depends_on"], f"{path}.depends_on", allow_empty=True
        )
        allowed_dependencies = set(expected_pr_ids[:index])
        if not set(dependencies).issubset(allowed_dependencies):
            raise UnionContractError(f"schema_invalid:{path}.depends_on:must_reference_earlier_pr")
        required_gate_ids = _require_string_list(
            pr["required_gate_ids"], f"{path}.required_gate_ids"
        )
        applicable_gate_ids = [str(gate["id"]) for gate in gates if pr_id in gate["applies_to"]]
        if required_gate_ids != applicable_gate_ids:
            raise UnionContractError(
                f"schema_invalid:{path}.required_gate_ids:must_equal_applicable_gates"
            )
        for group in PR_GROUPS:
            items = _require_list(pr[group], f"{path}.{group}")
            if not items:
                raise UnionContractError(f"schema_invalid:{path}.{group}:must_not_be_empty")
            for ordinal, raw_item in enumerate(items, start=1):
                item_path = f"{path}.{group}[{ordinal - 1}]"
                item = _require_mapping(raw_item, item_path)
                _require_exact_keys(item, ("id", "statement"), item_path)
                item_id = _require_text(item["id"], f"{item_path}.id", max_length=32)
                match = _REQUIREMENT_ID_RE.fullmatch(item_id)
                if (
                    match is None
                    or match.group("pr") != pr_id[2:]
                    or match.group("group") != GROUP_PREFIXES[group]
                    or int(match.group("ordinal")) != ordinal
                ):
                    raise UnionContractError(
                        f"schema_invalid:{item_path}.id:scope_or_ordinal_mismatch:{item_id}"
                    )
                _validate_localized(item["statement"], f"{item_path}.statement")

        completion = _require_mapping(pr["completion_rule"], f"{path}.completion_rule")
        _require_exact_keys(
            completion,
            ("operator", "required_groups", "prohibits_partial_completion", "statement"),
            f"{path}.completion_rule",
        )
        if completion["operator"] != "all":
            raise UnionContractError(f"schema_invalid:{path}.completion_rule.operator")
        _require_string_list(
            completion["required_groups"],
            f"{path}.completion_rule.required_groups",
            exact=PR_GROUPS,
        )
        if completion["prohibits_partial_completion"] is not True:
            raise UnionContractError(
                f"schema_invalid:{path}.completion_rule.prohibits_partial_completion"
            )
        _validate_localized(completion["statement"], f"{path}.completion_rule.statement")


def source_sha256(path: Path) -> str:
    """Return the raw-source digest used by the authoritative renderer."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _statement_sha256(statement: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        statement,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_requirement_index(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the exact, ordered requirement index from the canonical contract."""

    try:
        rendered_index = _RENDERER.build_requirement_index(contract)
    except (TypeError, ValueError) as exc:
        raise UnionContractError(f"requirement_index_invalid:{exc}") from exc
    index: dict[str, dict[str, Any]] = {}
    for requirement_id, raw_identity in rendered_index.items():
        identity = dict(raw_identity)
        if requirement_id in index:
            raise UnionContractError(f"requirement_duplicate:{requirement_id}")
        index[str(requirement_id)] = identity
    return index


def requirement_set_sha256(requirement_index: Mapping[str, Mapping[str, Any]]) -> str:
    """Bind the exact ordered requirement identities used by review receipts."""

    encoded = json.dumps(
        requirement_index,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evidence_policy_sha256(policy: Mapping[str, Any]) -> str:
    """Bind the exact closed evidence policy consumed by every receipt."""

    encoded = json.dumps(
        policy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_contract_source(path: Path) -> dict[str, Any]:
    """Validate the canonical source with both renderer and closed-shape rules."""

    try:
        payload = _load_json(path, label="contract")
        contract = _RENDERER.validate_contract(payload)
        _validate_contract_closed_shape(contract)
    except UnionContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise UnionContractError(f"contract_schema_invalid:{exc}") from exc
    return dict(contract)


def _run_git(repo_root: Path, args: Sequence[str], *, label: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise UnionContractError(f"git_unavailable:{label}:{exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise UnionContractError(f"git_command_failed:{label}:{detail}")
    return completed.stdout


def _validate_git_commit(repo_root: Path, revision: Any, path: str) -> str:
    commit = _require_text(revision, path, max_length=80)
    if _GIT_OBJECT_ID_RE.fullmatch(commit) is None:
        raise UnionContractError(f"schema_invalid:{path}:must_be_full_git_object_id")
    object_type = _run_git(repo_root, ("cat-file", "-t", commit), label=f"{path}.type")
    if object_type.strip() != b"commit":
        raise UnionContractError(f"git_object_not_commit:{path}:{commit}")
    resolved = (
        _run_git(
            repo_root,
            ("rev-parse", "--verify", f"{commit}^{{commit}}"),
            label=f"{path}.resolve",
        )
        .decode("ascii")
        .strip()
    )
    if resolved != commit:
        raise UnionContractError(f"git_commit_not_canonical:{path}:{commit}")
    return commit


def _git_tree(repo_root: Path, revision: str, *, path: str) -> str:
    tree = (
        _run_git(
            repo_root,
            ("rev-parse", "--verify", f"{revision}^{{tree}}"),
            label=path,
        )
        .decode("ascii")
        .strip()
    )
    if _GIT_OBJECT_ID_RE.fullmatch(tree) is None:
        raise UnionContractError(f"git_tree_invalid:{path}")
    return tree


def _git_path_bytes(repo_root: Path, revision: str, relative_path: Path) -> bytes | None:
    object_spec = f"{revision}:{relative_path.as_posix()}"
    try:
        exists = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", object_spec],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise UnionContractError(f"git_unavailable:path:{exc}") from exc
    if exists.returncode != 0:
        return None
    return _run_git(
        repo_root,
        ("show", object_spec),
        label=f"show:{object_spec}",
    )


def build_git_boundary(
    repo_root: Path,
    *,
    base_revision: Any,
    source_revision: Any,
) -> dict[str, Any]:
    """Resolve one immutable, deterministic ancestor Git boundary."""

    root = repo_root.resolve()
    base = _validate_git_commit(root, base_revision, "base_revision")
    source = _validate_git_commit(root, source_revision, "source_revision")
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", base, source],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise UnionContractError(f"git_unavailable:ancestor:{exc}") from exc
    if completed.returncode != 0:
        raise UnionContractError(f"git_base_not_ancestor:{base}:{source}")

    source_material = _run_git(
        root,
        ("ls-tree", "-r", "-z", "--full-tree", source),
        label="source_material",
    )
    diff_material = _run_git(
        root,
        ("diff", "--raw", "-z", "--no-abbrev", "--no-renames", base, source, "--"),
        label="diff_material",
    )
    changed_raw = _run_git(
        root,
        ("diff", "--name-only", "-z", "--no-renames", base, source, "--"),
        label="changed_paths",
    )
    try:
        changed_paths = [item.decode("utf-8") for item in changed_raw.split(b"\0") if item]
    except UnicodeDecodeError as exc:
        raise UnionContractError("git_changed_path_not_utf8") from exc
    if changed_paths != sorted(changed_paths) or len(changed_paths) != len(set(changed_paths)):
        raise UnionContractError("git_changed_paths_not_canonical")
    return {
        "base_revision": base,
        "base_tree": _git_tree(root, base, path="base_tree"),
        "source_revision": source,
        "source_tree": _git_tree(root, source, path="source_tree"),
        "source_material_sha256": hashlib.sha256(source_material).hexdigest(),
        "diff_sha256": hashlib.sha256(diff_material).hexdigest(),
        "changed_paths": changed_paths,
    }


def _validate_successor_lineage(
    current_contract: Mapping[str, Any],
    *,
    previous_bytes: bytes,
    previous_reference: str,
) -> None:
    previous = json.loads(previous_bytes, object_pairs_hook=_duplicate_rejecting_object)
    if not isinstance(previous, Mapping) or previous.get("schema") != CONTRACT_SCHEMA:
        raise UnionContractError("previous_contract_schema_mismatch")
    previous_revision = _require_text(
        previous.get("revision"), "previous_contract.revision", max_length=128
    )
    if current_contract["revision"] == previous_revision:
        raise UnionContractError(
            f"contract_revision_not_incremented:{current_contract['revision']}"
        )
    lineage = current_contract["revision_lineage"]
    if lineage["mode"] != "successor":
        raise UnionContractError("contract_successor_lineage_required")
    binding = _require_mapping(
        lineage["previous_canonical"], "contract.revision_lineage.previous_canonical"
    )
    previous_digest = hashlib.sha256(previous_bytes).hexdigest()
    if binding["revision"] != previous_revision:
        raise UnionContractError("contract_revision_lineage_mismatch:revision")
    if binding["sha256"] != previous_digest:
        raise UnionContractError("contract_revision_lineage_mismatch:sha256")
    content_reference = f"sha256:{previous_digest}"
    if binding["reference"] not in {previous_reference, content_reference}:
        raise UnionContractError("contract_revision_lineage_mismatch:reference")


def validate_revision_transition(
    current_path: Path,
    current_contract: Mapping[str, Any],
    previous_path: Path | None,
    *,
    repo_root: Path | None = None,
    base_revision: str | None = None,
    source_revision: str | None = None,
) -> str:
    """Verify first-introduction or successor lineage from immutable bytes/Git objects."""

    try:
        current_bytes = current_path.read_bytes()
    except OSError as exc:
        raise UnionContractError(f"revision_transition_unreadable:{exc}") from exc

    if (base_revision is None) != (source_revision is None):
        raise UnionContractError("git_revision_boundary_incomplete")
    if base_revision is not None and source_revision is not None:
        if repo_root is None:
            raise UnionContractError("git_revision_boundary_requires_repo_root")
        boundary = build_git_boundary(
            repo_root,
            base_revision=base_revision,
            source_revision=source_revision,
        )
        source_bytes = _git_path_bytes(repo_root, boundary["source_revision"], CONTRACT_PATH)
        if source_bytes is None:
            raise UnionContractError("source_revision_contract_missing")
        if source_bytes != current_bytes:
            raise UnionContractError("source_revision_contract_bytes_mismatch")
        base_bytes = _git_path_bytes(repo_root, boundary["base_revision"], CONTRACT_PATH)
        if base_bytes is None:
            if current_contract["revision_lineage"]["mode"] != "repository-authority-introduction":
                raise UnionContractError("contract_introduction_lineage_required")
            if current_contract["revision_lineage"]["previous_canonical"] is not None:
                raise UnionContractError("contract_introduction_forbids_previous_canonical")
            return "introduction-verified"
        if base_bytes == current_bytes:
            return "unchanged-verified"
        _validate_successor_lineage(
            current_contract,
            previous_bytes=base_bytes,
            previous_reference=(f"git:{boundary['base_revision']}:{CONTRACT_PATH.as_posix()}"),
        )
        return "successor-verified"

    if previous_path is not None:
        try:
            previous_bytes = previous_path.read_bytes()
        except OSError as exc:
            raise UnionContractError(f"revision_transition_unreadable:{exc}") from exc
        if previous_bytes == current_bytes:
            return "unchanged-verified"
        if current_contract["revision_lineage"]["mode"] == "repository-authority-introduction":
            previous = json.loads(previous_bytes, object_pairs_hook=_duplicate_rejecting_object)
            previous_revision = _require_text(
                previous.get("revision"), "previous_contract.revision", max_length=128
            )
            if current_contract["revision"] == previous_revision:
                raise UnionContractError(
                    f"contract_revision_not_incremented:{current_contract['revision']}"
                )
            # A historical canonical file may be supplied by local tooling even
            # when the governed Git base does not contain the authority.  It is
            # provenance only for this first repository-authority introduction,
            # never a successor preimage.
            return "introduction-declared"
        _validate_successor_lineage(
            current_contract,
            previous_bytes=previous_bytes,
            previous_reference=f"sha256:{hashlib.sha256(previous_bytes).hexdigest()}",
        )
        return "successor-verified"

    if current_contract["revision_lineage"]["mode"] == "repository-authority-introduction":
        return "introduction-declared"
    raise UnionContractError("contract_revision_lineage_source_required")


def render_contract(payload: dict[str, Any], *, source_digest: str, language: str) -> str:
    """Expose the canonical renderer through the gate's public test seam."""

    return str(_RENDERER.render_contract(payload, source_digest, language))


def _validate_companion_binding(
    binding: Any,
    *,
    path: str,
    contract: Mapping[str, Any],
    digest: str,
) -> None:
    value = _require_mapping(binding, path)
    _require_exact_keys(
        value,
        ("path", "schema", "revision", "sha256", "digest_algorithm"),
        path,
    )
    expected = {
        "path": CONTRACT_PATH.as_posix(),
        "schema": CONTRACT_SCHEMA,
        "revision": contract["revision"],
        "sha256": digest,
        "digest_algorithm": DIGEST_ALGORITHM,
    }
    for field, expected_value in expected.items():
        if value[field] != expected_value:
            raise UnionContractError(f"contract_binding_mismatch:{path}.{field}")


def _validate_utc_timestamp(value: Any, path: str) -> str:
    text = _require_text(value, path, max_length=64)
    if not text.endswith("Z"):
        raise UnionContractError(f"schema_invalid:{path}:must_be_utc_iso8601")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise UnionContractError(f"schema_invalid:{path}:must_be_utc_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise UnionContractError(f"schema_invalid:{path}:must_be_utc_iso8601")
    return text


def _validate_receipt(
    raw_receipt: Any,
    *,
    repo_root: Path,
    path: str,
    evidence_class: str,
    state: str,
    requirement_id: str,
    contract_revision: str,
    contract_digest: str,
    requirement_set_digest: str,
    evidence_policy_digest: str,
    receipt_registry: dict[str, str],
    boundary_cache: dict[tuple[str, str], Mapping[str, Any]],
) -> None:
    receipt = _require_mapping(raw_receipt, path)
    _require_exact_keys(receipt, RECEIPT_FIELDS, path)
    receipt_id = _require_text(receipt["id"], f"{path}.id", max_length=256)
    if _RECEIPT_ID_RE.fullmatch(receipt_id) is None:
        raise UnionContractError(f"schema_invalid:{path}.id:invalid_receipt_id")
    if receipt["evidence_class"] != evidence_class:
        raise UnionContractError(f"evidence_class_mismatch:{path}")
    if receipt["state"] != state:
        raise UnionContractError(f"evidence_state_mismatch:{path}")
    if receipt["requirement_id"] != requirement_id:
        raise UnionContractError(f"receipt_requirement_mismatch:{path}")
    if receipt["contract_revision"] != contract_revision:
        raise UnionContractError(f"receipt_contract_revision_mismatch:{path}")
    if receipt["contract_sha256"] != contract_digest:
        raise UnionContractError(f"receipt_contract_sha256_mismatch:{path}")
    for field in (
        "source_material_sha256",
        "diff_sha256",
        "requirement_set_sha256",
        "policy_sha256",
    ):
        value = receipt[field]
        if not isinstance(value, str) or _HEX_64_RE.fullmatch(value) is None:
            raise UnionContractError(f"schema_invalid:{path}.{field}:must_be_lowercase_hex_64")
    if receipt["requirement_set_sha256"] != requirement_set_digest:
        raise UnionContractError(f"receipt_requirement_set_mismatch:{path}")
    if receipt["policy_sha256"] != evidence_policy_digest:
        raise UnionContractError(f"receipt_policy_mismatch:{path}")

    base_revision = _validate_git_commit(
        repo_root, receipt["base_revision"], f"{path}.base_revision"
    )
    source_revision = _validate_git_commit(
        repo_root, receipt["source_revision"], f"{path}.source_revision"
    )
    boundary_key = (base_revision, source_revision)
    boundary = boundary_cache.get(boundary_key)
    if boundary is None:
        boundary = build_git_boundary(
            repo_root,
            base_revision=base_revision,
            source_revision=source_revision,
        )
        boundary_cache[boundary_key] = boundary
    for field in (
        "base_revision",
        "base_tree",
        "source_revision",
        "source_tree",
        "source_material_sha256",
        "diff_sha256",
    ):
        if receipt[field] != boundary[field]:
            raise UnionContractError(f"receipt_git_boundary_mismatch:{path}:{field}")
    changed_paths = _require_string_list(receipt["changed_paths"], f"{path}.changed_paths")
    for index, changed_path in enumerate(changed_paths):
        relative = Path(changed_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise UnionContractError(
                f"schema_invalid:{path}.changed_paths[{index}]:must_be_safe_relative_path"
            )
    if changed_paths != boundary["changed_paths"]:
        raise UnionContractError(f"receipt_git_boundary_mismatch:{path}:changed_paths")
    source_contract = _git_path_bytes(repo_root, source_revision, CONTRACT_PATH)
    if source_contract is None:
        raise UnionContractError(f"receipt_source_contract_missing:{path}")
    if hashlib.sha256(source_contract).hexdigest() != contract_digest:
        raise UnionContractError(f"receipt_source_contract_mismatch:{path}")

    authority = receipt["authority"]
    if authority not in RECEIPT_AUTHORITIES:
        raise UnionContractError(f"schema_invalid:{path}.authority")
    attestation_kind = receipt["attestation_kind"]
    if attestation_kind not in ATTESTATION_KINDS:
        raise UnionContractError(f"schema_invalid:{path}.attestation_kind")
    expected_attestation = {
        "repository": "content-addressed-artifact",
        "github-protected-workflow": "github-attestation",
        "plastic-promise-server": "server-signed-receipt",
    }[str(authority)]
    if attestation_kind != expected_attestation:
        raise UnionContractError(f"receipt_authority_attestation_mismatch:{path}")
    if state in ("verified", "not-applicable") and authority == "repository":
        raise UnionContractError(f"verified_receipt_requires_attested_authority:{path}")
    if (
        evidence_class in ("runtime", "production")
        and state == "verified"
        and authority != "plastic-promise-server"
    ):
        raise UnionContractError(f"runtime_receipt_requires_server_authority:{path}")

    issuer_id = _require_text(receipt["issuer_id"], f"{path}.issuer_id", max_length=256)
    subject_id = _require_text(receipt["subject_id"], f"{path}.subject_id", max_length=256)
    review_channel = receipt["review_channel"]
    if review_channel not in REVIEW_CHANNELS:
        raise UnionContractError(f"schema_invalid:{path}.review_channel")
    if review_channel != "none":
        if evidence_class != "test":
            raise UnionContractError(f"review_receipt_requires_test_evidence_class:{path}")
        if authority == "repository":
            raise UnionContractError(f"review_receipt_requires_attested_authority:{path}")
        if issuer_id == subject_id:
            raise UnionContractError(f"review_receipt_requires_independent_issuer:{path}")

    exemption_revision = receipt["exemption_contract_revision"]
    exemption_digest = receipt["exemption_contract_sha256"]
    if state == "not-applicable":
        raise UnionContractError(f"not_applicable_requires_verifiable_canonical_exemption:{path}")
    if exemption_revision is not None or exemption_digest is not None:
        raise UnionContractError(f"unexpected_exemption_binding:{path}")
    _validate_utc_timestamp(receipt["recorded_at"], f"{path}.recorded_at")

    digest = receipt["sha256"]
    if not isinstance(digest, str) or _HEX_64_RE.fullmatch(digest) is None:
        raise UnionContractError(f"schema_invalid:{path}.sha256:must_be_lowercase_hex_64")
    reference = _require_text(receipt["ref"], f"{path}.ref", max_length=2048)
    if "\n" in reference or "\r" in reference:
        raise UnionContractError(f"schema_invalid:{path}.ref:must_be_single_line")
    reference_match = _CONTENT_ADDRESSED_REF_RE.fullmatch(reference)
    if reference_match is None:
        raise UnionContractError(f"schema_invalid:{path}.ref:must_be_content_addressed_repo_ref")
    if reference_match.group("digest") != digest:
        raise UnionContractError(f"receipt_ref_digest_mismatch:{path}")
    relative, resolved = _resolve_manifest_path(
        repo_root,
        reference_match.group("path"),
        f"{path}.ref",
    )
    if relative.parts[:2] != ("docs", "evidence"):
        raise UnionContractError(f"schema_invalid:{path}.ref:must_be_under_docs_evidence")
    try:
        actual_digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except FileNotFoundError as exc:
        raise UnionContractError(f"receipt_artifact_missing:{relative}") from exc
    except OSError as exc:
        raise UnionContractError(f"receipt_artifact_unreadable:{relative}:{exc}") from exc
    if actual_digest != digest:
        raise UnionContractError(f"receipt_artifact_digest_mismatch:{path}")

    artifact = _load_json(resolved, label="evidence_artifact")
    artifact_fields = tuple(field for field in RECEIPT_FIELDS if field not in ("sha256", "ref"))
    _require_exact_keys(
        artifact,
        ("schema", *artifact_fields),
        f"{path}.artifact",
    )
    if artifact["schema"] != EVIDENCE_ARTIFACT_SCHEMA:
        raise UnionContractError(f"evidence_artifact_schema_mismatch:{path}")
    for field in artifact_fields:
        if artifact[field] != receipt[field]:
            raise UnionContractError(f"evidence_artifact_binding_mismatch:{path}:{field}")

    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    previous = receipt_registry.setdefault(receipt_id, canonical)
    if previous != canonical:
        raise UnionContractError(f"receipt_id_reused_with_conflicting_content:{receipt_id}")


def _validate_evidence_ledger(
    repo_root: Path,
    *,
    contract: Mapping[str, Any],
    contract_digest: str,
    requirement_index: Mapping[str, Mapping[str, Any]],
) -> None:
    path = repo_root / EVIDENCE_LEDGER_PATH
    ledger = _load_json(path, label="evidence_ledger")
    _require_exact_keys(ledger, ("schema", "contract", "evidence_policy", "requirements"), "ledger")
    if ledger["schema"] != EVIDENCE_LEDGER_SCHEMA:
        raise UnionContractError("schema_invalid:ledger.schema")
    _validate_companion_binding(
        ledger["contract"],
        path="ledger.contract",
        contract=contract,
        digest=contract_digest,
    )

    policy = _require_mapping(ledger["evidence_policy"], "ledger.evidence_policy")
    _require_exact_keys(
        policy, ("classes", "states", "receipt_schema", "rules"), "ledger.evidence_policy"
    )
    _require_string_list(
        policy["classes"], "ledger.evidence_policy.classes", exact=EVIDENCE_CLASSES
    )
    _require_string_list(policy["states"], "ledger.evidence_policy.states", exact=EVIDENCE_STATES)
    receipt_schema = _require_mapping(
        policy["receipt_schema"], "ledger.evidence_policy.receipt_schema"
    )
    _require_exact_keys(
        receipt_schema,
        (
            "required_fields",
            "id_prefix",
            "sha256_format",
            "recorded_at_format",
            "class_must_match_bucket",
            "state_must_match_bucket",
            "requirement_must_match_bucket",
            "content_addressed_ref_prefix",
            "authorities",
            "attestation_kinds",
            "review_channels",
            "verified_requires_attested_authority",
            "git_boundary_required",
            "source_material_algorithm",
            "diff_material_algorithm",
            "artifact_schema",
            "artifact_must_bind_receipt_fields",
        ),
        "ledger.evidence_policy.receipt_schema",
    )
    _require_string_list(
        receipt_schema["required_fields"],
        "ledger.evidence_policy.receipt_schema.required_fields",
        exact=RECEIPT_FIELDS,
    )
    expected_receipt_schema = {
        "id_prefix": "receipt:",
        "sha256_format": "lowercase-hex-64",
        "recorded_at_format": "utc-iso8601",
        "class_must_match_bucket": True,
        "state_must_match_bucket": True,
        "requirement_must_match_bucket": True,
        "content_addressed_ref_prefix": "repo:docs/evidence/",
        "verified_requires_attested_authority": True,
        "git_boundary_required": True,
        "source_material_algorithm": SOURCE_MATERIAL_ALGORITHM,
        "diff_material_algorithm": DIFF_MATERIAL_ALGORITHM,
        "artifact_schema": EVIDENCE_ARTIFACT_SCHEMA,
        "artifact_must_bind_receipt_fields": True,
    }
    for field, expected in expected_receipt_schema.items():
        if receipt_schema[field] != expected:
            raise UnionContractError(
                f"schema_invalid:ledger.evidence_policy.receipt_schema.{field}"
            )
    _require_string_list(
        receipt_schema["authorities"],
        "ledger.evidence_policy.receipt_schema.authorities",
        exact=RECEIPT_AUTHORITIES,
    )
    _require_string_list(
        receipt_schema["attestation_kinds"],
        "ledger.evidence_policy.receipt_schema.attestation_kinds",
        exact=ATTESTATION_KINDS,
    )
    _require_string_list(
        receipt_schema["review_channels"],
        "ledger.evidence_policy.receipt_schema.review_channels",
        exact=REVIEW_CHANNELS,
    )
    _validate_policy_rules(policy["rules"], "ledger.evidence_policy.rules")

    requirements = _require_mapping(ledger["requirements"], "ledger.requirements")
    expected_ids = set(requirement_index)
    actual_ids = set(requirements)
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing:
        raise UnionContractError(f"ledger_requirement_missing:{','.join(missing)}")
    if extra:
        raise UnionContractError(f"ledger_requirement_extra:{','.join(extra)}")

    receipt_registry: dict[str, str] = {}
    boundary_cache: dict[tuple[str, str], Mapping[str, Any]] = {}
    review_boundaries: dict[str, dict[str, set[str]]] = {}
    requirement_set_digest = requirement_set_sha256(requirement_index)
    evidence_policy_digest = evidence_policy_sha256(policy)
    for requirement_id, expected in requirement_index.items():
        path_prefix = f"ledger.requirements.{requirement_id}"
        requirement = _require_mapping(requirements[requirement_id], path_prefix)
        _require_exact_keys(
            requirement,
            ("pr_id", "group", "ordinal", "statement_sha256", *EVIDENCE_CLASSES),
            path_prefix,
        )
        for field in ("pr_id", "group", "ordinal", "statement_sha256"):
            if requirement[field] != expected[field]:
                raise UnionContractError(
                    f"ledger_requirement_mapping_mismatch:{requirement_id}:{field}"
                )

        for evidence_class in EVIDENCE_CLASSES:
            bucket_path = f"{path_prefix}.{evidence_class}"
            bucket = _require_mapping(requirement[evidence_class], bucket_path)
            _require_exact_keys(bucket, ("state", "receipts"), bucket_path)
            state = bucket["state"]
            if state not in EVIDENCE_STATES:
                raise UnionContractError(f"schema_invalid:{bucket_path}.state")
            receipts = _require_list(bucket["receipts"], f"{bucket_path}.receipts")
            if state in ("partial", "verified", "not-applicable") and not receipts:
                raise UnionContractError(
                    f"evidence_receipt_required:{requirement_id}:{evidence_class}"
                )
            if state == "not-evidenced" and receipts:
                raise UnionContractError(
                    f"evidence_receipt_forbidden:{requirement_id}:{evidence_class}"
                )
            bucket_ids: set[str] = set()
            for index, receipt in enumerate(receipts):
                receipt_path = f"{bucket_path}.receipts[{index}]"
                _validate_receipt(
                    receipt,
                    repo_root=repo_root,
                    path=receipt_path,
                    evidence_class=evidence_class,
                    state=str(state),
                    requirement_id=requirement_id,
                    contract_revision=str(contract["revision"]),
                    contract_digest=contract_digest,
                    requirement_set_digest=requirement_set_digest,
                    evidence_policy_digest=evidence_policy_digest,
                    receipt_registry=receipt_registry,
                    boundary_cache=boundary_cache,
                )
                receipt_id = str(receipt["id"])
                if receipt_id in bucket_ids:
                    raise UnionContractError(f"duplicate_receipt_in_bucket:{receipt_id}")
                bucket_ids.add(receipt_id)
                review_channel = str(receipt["review_channel"])
                if review_channel in ("standards", "spec"):
                    boundary_identity = json.dumps(
                        {
                            field: receipt[field]
                            for field in (
                                "contract_revision",
                                "contract_sha256",
                                "base_revision",
                                "base_tree",
                                "source_revision",
                                "source_tree",
                                "source_material_sha256",
                                "diff_sha256",
                                "changed_paths",
                                "requirement_set_sha256",
                            )
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    review_boundaries.setdefault(str(expected["pr_id"]), {}).setdefault(
                        review_channel, set()
                    ).add(boundary_identity)
        if (
            requirement["production"]["state"] == "verified"
            and requirement["runtime"]["state"] != "verified"
        ):
            raise UnionContractError(
                f"production_evidence_requires_verified_runtime:{requirement_id}"
            )

    for pr_id, channels in review_boundaries.items():
        for channel, identities in channels.items():
            if len(identities) > 1:
                raise UnionContractError(f"review_channel_boundary_conflict:{pr_id}:{channel}")
        standards = channels.get("standards")
        spec = channels.get("spec")
        if standards is not None and spec is not None and standards != spec:
            raise UnionContractError(f"review_pair_boundary_mismatch:{pr_id}")


def _resolve_manifest_path(repo_root: Path, value: Any, path: str) -> tuple[Path, Path]:
    relative = Path(_require_text(value, path, max_length=512))
    if relative.is_absolute() or ".." in relative.parts:
        raise UnionContractError(f"schema_invalid:{path}:must_be_safe_relative_path")
    resolved = (repo_root / relative).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise UnionContractError(f"schema_invalid:{path}:escapes_repository") from exc
    return relative, resolved


def manifest_content_set_sha256(repo_root: Path, manifest: Mapping[str, Any]) -> str:
    """Bind every registered path and its present/missing content state."""

    digest = hashlib.sha256()
    for group in ("documents", "assets"):
        entries = _require_list(manifest[group], f"manifest.{group}")
        for index, raw_entry in enumerate(entries):
            entry_path = f"manifest.{group}[{index}]"
            entry = _require_mapping(raw_entry, entry_path)
            entry_id = _require_text(entry.get("id"), f"{entry_path}.id", max_length=128)
            if group == "documents":
                kind = entry.get("kind")
                if kind == "bilingual-markdown-pair":
                    paths = _require_mapping(entry.get("paths"), f"{entry_path}.paths")
                    _require_exact_keys(paths, REQUIRED_LOCALES, f"{entry_path}.paths")
                    localized_paths = [(language, paths[language]) for language in REQUIRED_LOCALES]
                elif kind == "language-neutral-governance-document":
                    localized_paths = [(NEUTRAL_LOCALE, entry.get("path"))]
                else:
                    raise UnionContractError(f"schema_invalid:{entry_path}.kind")
            else:
                paths = _require_mapping(entry.get("paths"), f"{entry_path}.paths")
                _require_exact_keys(paths, REQUIRED_LOCALES, f"{entry_path}.paths")
                localized_paths = [(language, paths[language]) for language in REQUIRED_LOCALES]
            for language, path_value in localized_paths:
                relative, resolved = _resolve_manifest_path(
                    repo_root,
                    path_value,
                    (
                        f"{entry_path}.path"
                        if language == NEUTRAL_LOCALE
                        else f"{entry_path}.paths.{language}"
                    ),
                )
                try:
                    content_state = hashlib.sha256(resolved.read_bytes()).hexdigest()
                except FileNotFoundError:
                    content_state = "missing"
                except OSError as exc:
                    raise UnionContractError(f"manifest_path_unreadable:{relative}:{exc}") from exc
                record = "\0".join(
                    (
                        group,
                        entry_id,
                        language,
                        relative.as_posix(),
                        content_state,
                    )
                )
                digest.update(record.encode("utf-8"))
                digest.update(b"\n")
    return digest.hexdigest()


def _resolve_markdown_target(repo_root: Path, document: Path, target: str) -> Path | None:
    clean = target.strip().strip("<>").split("#", 1)[0]
    if not clean or "://" in clean or clean.startswith("mailto:"):
        return None
    if clean.startswith("/"):
        return (repo_root / clean.lstrip("/")).resolve()
    if clean.startswith("docs/"):
        return (repo_root / clean).resolve()
    return (document.parent / clean).resolve()


def _normalized(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.replace("_", " ").casefold()).split())


def _states_union_completion(body: str, *, language: str) -> bool:
    normalized = _normalized(body)
    if language == "en":
        names_both_halves = (
            "delivery scope" in normalized
            or "deployment scope" in normalized
            or "deployment responsibility" in normalized
        ) and "collaboration scope" in normalized
        rejects_partial = any(
            marker in normalized
            for marker in (
                "one sided completion is not pr completion",
                "neither half may be reported as full pr completion",
                "source slice never proves the whole pr complete",
                "source slice never proves whole pr complete",
            )
        )
    else:
        names_both_halves = ("交付范围" in body or "部署职责" in body) and (
            "协作范围" in body or "协作职责" in body
        )
        rejects_partial = any(
            marker in normalized
            for marker in (
                "不等于整个 pr 完成",
                "任何一半都不得被报告为整个 pr 已完成",
                "任一单侧完成都不等于 pr 完成",
            )
        )
    return names_both_halves and rejects_partial


def _read_manifest_file(path: Path, *, relative_path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise UnionContractError(f"manifest_path_missing:{relative_path}") from exc
    except (OSError, UnicodeError) as exc:
        raise UnionContractError(f"manifest_path_unreadable:{relative_path}:{exc}") from exc


def _validate_contract_link(
    repo_root: Path,
    document: Path,
    relative_path: Path,
    body: str,
    source: Path,
) -> None:
    linked = any(
        _resolve_markdown_target(repo_root, document, match.group("target")) == source.resolve()
        for match in _MARKDOWN_LINK_RE.finditer(body)
    )
    if not linked:
        raise UnionContractError(f"authority_link_missing:{relative_path}")


def _parse_svg(body: str, path: Path) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise UnionContractError(f"asset_svg_invalid:{path}:{exc}") from exc


def _svg_local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _validate_svg_security(root: ET.Element, path: Path) -> None:
    for node in root.iter():
        tag = _svg_local_name(str(node.tag)).casefold()
        if tag in ("script", "foreignobject"):
            raise UnionContractError(f"asset_svg_unsafe_element:{path}:{tag}")
        for raw_name, raw_value in node.attrib.items():
            name = _svg_local_name(raw_name).casefold()
            if name.startswith("on"):
                raise UnionContractError(f"asset_svg_unsafe_attribute:{path}:{name}")
            if name == "href":
                target = raw_value.strip()
                if target and not target.startswith("#"):
                    raise UnionContractError(f"asset_svg_external_href:{path}:{target}")


def _svg_structure_signature(body: str, path: Path) -> tuple[Any, ...]:
    root = _parse_svg(body, path)

    def visit(node: ET.Element) -> tuple[Any, ...]:
        tag = _svg_local_name(str(node.tag))
        attributes = tuple(sorted(node.attrib.items()))
        return (
            tag,
            attributes,
            tuple(visit(child) for child in list(node)),
        )

    return visit(root)


def _asset_semantic_body(body: str, path: Path) -> str:
    if path.suffix.casefold() == ".svg":
        root = _parse_svg(body, path)
        _validate_svg_security(root, path)
        visible: list[str] = []

        def visit(node: ET.Element, in_visible_text: bool = False) -> None:
            tag = _svg_local_name(str(node.tag)).casefold()
            visible_text = in_visible_text or tag in ("text", "tspan")
            if visible_text and node.text:
                visible.append(node.text)
            for child in list(node):
                visit(child, visible_text)
                if visible_text and child.tail:
                    visible.append(child.tail)

        visit(root)
        return " ".join(" ".join(visible).split())
    return body


def _mermaid_structure_signature(body: str) -> tuple[str, ...]:
    lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("%%"):
            continue
        line = _MERMAID_QUOTED_RE.sub('""', line)
        line = _MERMAID_LABEL_RE.sub(lambda match: f"{match.group(1)}{match.group(3)}", line)
        if line.casefold().startswith("subgraph "):
            line = "subgraph"
        lines.append(" ".join(line.split()))
    return tuple(lines)


def _validate_asset_parity(
    en_body: str,
    zh_body: str,
    *,
    en_path: Path,
    zh_path: Path,
) -> None:
    if en_path.suffix.casefold() != zh_path.suffix.casefold():
        raise UnionContractError(f"asset_format_mismatch:{en_path}:{zh_path}")
    suffix = en_path.suffix.casefold()
    if suffix == ".svg":
        if _svg_structure_signature(en_body, en_path) != _svg_structure_signature(zh_body, zh_path):
            raise UnionContractError(f"asset_semantic_parity_mismatch:{en_path}:{zh_path}")
        return
    if suffix == ".mermaid":
        if _mermaid_structure_signature(en_body) != _mermaid_structure_signature(zh_body):
            raise UnionContractError(f"asset_semantic_parity_mismatch:{en_path}:{zh_path}")
        return
    if en_body != zh_body:
        raise UnionContractError(f"asset_semantic_parity_unsupported:{en_path}:{zh_path}")


def _parse_semantic_claims(
    raw_claims: Any,
    *,
    path: str,
    locales: Sequence[str] = REQUIRED_LOCALES,
) -> list[dict[str, Any]]:
    claims = _require_list(raw_claims, path)
    if not claims:
        raise UnionContractError(f"schema_invalid:{path}:must_not_be_empty")
    parsed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_claim in enumerate(claims):
        claim_path = f"{path}[{index}]"
        claim = _require_mapping(raw_claim, claim_path)
        _require_exact_keys(claim, ("id", "markers"), claim_path)
        claim_id = _require_text(claim["id"], f"{claim_path}.id", max_length=128)
        if claim_id in seen_ids:
            raise UnionContractError(f"schema_invalid:{path}:duplicate_claim_id:{claim_id}")
        seen_ids.add(claim_id)
        markers = _require_mapping(claim["markers"], f"{claim_path}.markers")
        _require_exact_keys(markers, locales, f"{claim_path}.markers")
        localized: dict[str, list[str]] = {}
        for language in locales:
            localized[language] = _require_string_list(
                markers[language],
                f"{claim_path}.markers.{language}",
            )
        parsed.append({"id": claim_id, "markers": localized})
    return parsed


def _validate_semantic_claim_markers(
    claims: Sequence[Mapping[str, Any]],
    *,
    bodies: Mapping[str, str],
    paths: Mapping[str, Path],
    locales: Sequence[str] = REQUIRED_LOCALES,
    subject: str = "asset",
) -> None:
    for claim in claims:
        claim_id = str(claim["id"])
        markers = _require_mapping(claim["markers"], f"semantic_claims.{claim_id}.markers")
        for language in locales:
            body = bodies[language]
            for marker in _require_string_list(
                markers[language],
                f"semantic_claims.{claim_id}.markers.{language}",
            ):
                if marker not in body:
                    raise UnionContractError(
                        f"{subject}_semantic_claim_missing:{paths[language]}:{claim_id}:{language}"
                    )


def _validate_drift(
    raw_drift: Any,
    *,
    path: str,
    enforcement: str,
    tracked_drift_is_passing: bool,
) -> None:
    if enforcement == "required":
        if raw_drift is not None:
            raise UnionContractError(f"schema_invalid:{path}:required_entry_must_have_null_drift")
        return
    drift = _require_mapping(raw_drift, path)
    _require_exact_keys(
        drift,
        (
            "id",
            "detected_on",
            "blocking",
            "reason",
            "issue",
            "reference",
            "resolution",
        ),
        path,
    )
    _require_text(drift["id"], f"{path}.id", max_length=128)
    detected = _require_text(drift["detected_on"], f"{path}.detected_on", max_length=32)
    try:
        datetime.strptime(detected, "%Y-%m-%d")
    except ValueError as exc:
        raise UnionContractError(f"schema_invalid:{path}.detected_on:must_be_yyyy_mm_dd") from exc
    if drift["blocking"] is not True:
        raise UnionContractError(f"schema_invalid:{path}.blocking:must_be_true")
    _validate_localized(drift["reason"], f"{path}.reason")
    _require_text(drift["issue"], f"{path}.issue", max_length=512)
    _require_text(drift["reference"], f"{path}.reference", max_length=1024)
    _validate_localized(drift["resolution"], f"{path}.resolution")
    if tracked_drift_is_passing:
        raise UnionContractError("schema_invalid:manifest.policy.tracked_drift_is_passing")


def _validate_derived_manifest(
    repo_root: Path,
    *,
    contract: Mapping[str, Any],
    contract_digest: str,
) -> None:
    manifest_path = repo_root / DERIVED_DOCUMENTS_PATH
    manifest = _load_json(manifest_path, label="derived_documents_manifest")
    _require_exact_keys(
        manifest, ("schema", "contract", "policy", "documents", "assets"), "manifest"
    )
    if manifest["schema"] != DERIVED_DOCUMENTS_SCHEMA:
        raise UnionContractError("schema_invalid:manifest.schema")
    _validate_companion_binding(
        manifest["contract"],
        path="manifest.contract",
        contract=contract,
        digest=contract_digest,
    )

    policy = _require_mapping(manifest["policy"], "manifest.policy")
    _require_exact_keys(
        policy,
        (
            "enforcement_values",
            "tracked_drift_is_passing",
            "path_base",
            "required_locales",
            "content_set_algorithm",
            "content_set_sha256",
            "rules",
        ),
        "manifest.policy",
    )
    _require_string_list(
        policy["enforcement_values"],
        "manifest.policy.enforcement_values",
        exact=ENFORCEMENT_VALUES,
    )
    if policy["tracked_drift_is_passing"] is not False:
        raise UnionContractError("schema_invalid:manifest.policy.tracked_drift_is_passing")
    path_base = _require_text(policy["path_base"], "manifest.policy.path_base", max_length=64)
    if path_base not in (".", "repository-root"):
        raise UnionContractError("schema_invalid:manifest.policy.path_base")
    _require_string_list(
        policy["required_locales"],
        "manifest.policy.required_locales",
        exact=REQUIRED_LOCALES,
    )
    if policy["content_set_algorithm"] != CONTENT_SET_ALGORITHM:
        raise UnionContractError("schema_invalid:manifest.policy.content_set_algorithm")
    content_set_sha256 = policy["content_set_sha256"]
    if not isinstance(content_set_sha256, str) or _HEX_64_RE.fullmatch(content_set_sha256) is None:
        raise UnionContractError(
            "schema_invalid:manifest.policy.content_set_sha256:must_be_lowercase_hex_64"
        )
    _validate_policy_rules(policy["rules"], "manifest.policy.rules")

    source = repo_root / CONTRACT_PATH
    seen_ids: set[str] = set()
    tracked_drift_ids: list[str] = []
    documents = _require_list(manifest["documents"], "manifest.documents")
    if not documents:
        raise UnionContractError("schema_invalid:manifest.documents:must_not_be_empty")
    for index, raw_document in enumerate(documents):
        entry_path = f"manifest.documents[{index}]"
        document = _require_mapping(raw_document, entry_path)
        entry_id = _require_text(document["id"], f"{entry_path}.id", max_length=128)
        if entry_id in seen_ids:
            raise UnionContractError(f"manifest_duplicate_id:{entry_id}")
        seen_ids.add(entry_id)
        kind = _require_text(document["kind"], f"{entry_path}.kind", max_length=128)
        if kind not in DOCUMENT_KINDS:
            raise UnionContractError(f"schema_invalid:{entry_path}.kind")
        path_field = "paths" if kind == "bilingual-markdown-pair" else "path"
        _require_exact_keys(
            document,
            (
                "id",
                "kind",
                "enforcement",
                path_field,
                "must_exist",
                "must_link_contract",
                "must_state_union_completion",
                "semantic_claims",
                "drift",
            ),
            entry_path,
        )
        enforcement = document["enforcement"]
        if enforcement not in ENFORCEMENT_VALUES:
            raise UnionContractError(f"schema_invalid:{entry_path}.enforcement")
        if kind == "bilingual-markdown-pair":
            paths = _require_mapping(document["paths"], f"{entry_path}.paths")
            _require_exact_keys(paths, REQUIRED_LOCALES, f"{entry_path}.paths")
            locales = REQUIRED_LOCALES
            localized_paths = {language: paths[language] for language in locales}
        else:
            locales = (NEUTRAL_LOCALE,)
            localized_paths = {NEUTRAL_LOCALE: document["path"]}
        if _require_bool(document["must_exist"], f"{entry_path}.must_exist") is not True:
            raise UnionContractError(f"schema_invalid:{entry_path}.must_exist:must_be_true")
        must_link = _require_bool(
            document["must_link_contract"], f"{entry_path}.must_link_contract"
        )
        must_complete = _require_bool(
            document["must_state_union_completion"],
            f"{entry_path}.must_state_union_completion",
        )
        semantic_claims = _parse_semantic_claims(
            document["semantic_claims"],
            path=f"{entry_path}.semantic_claims",
            locales=locales,
        )
        _validate_drift(
            document["drift"],
            path=f"{entry_path}.drift",
            enforcement=str(enforcement),
            tracked_drift_is_passing=bool(policy["tracked_drift_is_passing"]),
        )
        if enforcement == "tracked-drift":
            tracked_drift_ids.append(entry_id)
            for language in locales:
                _resolve_manifest_path(
                    repo_root,
                    localized_paths[language],
                    (
                        f"{entry_path}.path"
                        if language == NEUTRAL_LOCALE
                        else f"{entry_path}.paths.{language}"
                    ),
                )
            continue
        bodies: dict[str, str] = {}
        resolved_paths: dict[str, Path] = {}
        for language in locales:
            relative, resolved = _resolve_manifest_path(
                repo_root,
                localized_paths[language],
                (
                    f"{entry_path}.path"
                    if language == NEUTRAL_LOCALE
                    else f"{entry_path}.paths.{language}"
                ),
            )
            body = _read_manifest_file(resolved, relative_path=relative)
            bodies[language] = body
            resolved_paths[language] = relative
            if must_link:
                _validate_contract_link(repo_root, resolved, relative, body, source)
            completion_language = "en" if language == NEUTRAL_LOCALE else language
            if must_complete and not _states_union_completion(body, language=completion_language):
                raise UnionContractError(f"union_completion_rule_missing:{relative}")
        _validate_semantic_claim_markers(
            semantic_claims,
            bodies=bodies,
            paths=resolved_paths,
            locales=locales,
            subject="document",
        )

    assets = _require_list(manifest["assets"], "manifest.assets")
    for index, raw_asset in enumerate(assets):
        entry_path = f"manifest.assets[{index}]"
        asset = _require_mapping(raw_asset, entry_path)
        _require_exact_keys(
            asset,
            (
                "id",
                "kind",
                "enforcement",
                "paths",
                "must_exist",
                "must_have_semantic_parity",
                "must_bind_contract_revision",
                "semantic_claims",
                "drift",
            ),
            entry_path,
        )
        entry_id = _require_text(asset["id"], f"{entry_path}.id", max_length=128)
        if entry_id in seen_ids:
            raise UnionContractError(f"manifest_duplicate_id:{entry_id}")
        seen_ids.add(entry_id)
        _require_text(asset["kind"], f"{entry_path}.kind", max_length=128)
        enforcement = asset["enforcement"]
        if enforcement not in ENFORCEMENT_VALUES:
            raise UnionContractError(f"schema_invalid:{entry_path}.enforcement")
        paths = _require_mapping(asset["paths"], f"{entry_path}.paths")
        _require_exact_keys(paths, REQUIRED_LOCALES, f"{entry_path}.paths")
        if _require_bool(asset["must_exist"], f"{entry_path}.must_exist") is not True:
            raise UnionContractError(f"schema_invalid:{entry_path}.must_exist:must_be_true")
        parity = _require_bool(
            asset["must_have_semantic_parity"], f"{entry_path}.must_have_semantic_parity"
        )
        bind_revision = _require_bool(
            asset["must_bind_contract_revision"], f"{entry_path}.must_bind_contract_revision"
        )
        semantic_claims = _parse_semantic_claims(
            asset["semantic_claims"], path=f"{entry_path}.semantic_claims"
        )
        _validate_drift(
            asset["drift"],
            path=f"{entry_path}.drift",
            enforcement=str(enforcement),
            tracked_drift_is_passing=bool(policy["tracked_drift_is_passing"]),
        )
        if enforcement == "tracked-drift":
            tracked_drift_ids.append(entry_id)
            for language in REQUIRED_LOCALES:
                _resolve_manifest_path(repo_root, paths[language], f"{entry_path}.paths.{language}")
            continue
        bodies: dict[str, str] = {}
        resolved_paths: dict[str, Path] = {}
        for language in REQUIRED_LOCALES:
            relative, resolved = _resolve_manifest_path(
                repo_root, paths[language], f"{entry_path}.paths.{language}"
            )
            bodies[language] = _read_manifest_file(resolved, relative_path=relative)
            resolved_paths[language] = relative
            if bind_revision and str(contract["revision"]) not in bodies[language]:
                raise UnionContractError(f"asset_contract_binding_missing:{relative}")
        if parity:
            _validate_asset_parity(
                bodies["en"],
                bodies["zh_CN"],
                en_path=resolved_paths["en"],
                zh_path=resolved_paths["zh_CN"],
            )
        _validate_semantic_claim_markers(
            semantic_claims,
            bodies={
                language: _asset_semantic_body(bodies[language], resolved_paths[language])
                for language in REQUIRED_LOCALES
            },
            paths=resolved_paths,
        )

    actual_content_set_sha256 = manifest_content_set_sha256(repo_root, manifest)
    if content_set_sha256 != actual_content_set_sha256:
        raise UnionContractError(
            "manifest_content_set_mismatch:"
            f"expected={content_set_sha256}:actual={actual_content_set_sha256}"
        )

    if tracked_drift_ids:
        raise UnionContractError(f"tracked_drift_blocks_gate:{','.join(tracked_drift_ids)}")


def verify_repository(
    repo_root: Path,
    *,
    previous_contract: Path | None = None,
    base_revision: str | None = None,
    source_revision: str | None = None,
) -> dict[str, Any]:
    """Verify canonical bytes, exact requirements, evidence, and projections."""

    root = repo_root.resolve()
    source = root / CONTRACT_PATH
    contract = validate_contract_source(source)
    resolved_previous = previous_contract
    if resolved_previous is not None and not resolved_previous.is_absolute():
        resolved_previous = root / resolved_previous
    revision_transition = validate_revision_transition(
        source,
        contract,
        resolved_previous.resolve() if resolved_previous is not None else None,
        repo_root=root,
        base_revision=base_revision,
        source_revision=source_revision,
    )
    digest = source_sha256(source)
    requirement_index = build_requirement_index(contract)
    try:
        rendered = _RENDERER.render_generated_views(source)
    except (OSError, UnicodeError, ValueError) as exc:
        raise UnionContractError(f"render_failed:{exc}") from exc

    for language, relative_path in GENERATED_VIEWS.items():
        path = root / relative_path
        expected = str(rendered[language]).encode("utf-8")
        try:
            actual = path.read_bytes()
        except FileNotFoundError as exc:
            raise UnionContractError(f"generated_view_missing:{relative_path}") from exc
        if actual != expected:
            raise UnionContractError(f"generated_view_drift:{relative_path}")
        match = _DIGEST_RE.search(actual.decode("utf-8"))
        if match is None or match.group("digest") != digest:
            raise UnionContractError(f"generated_digest_mismatch:{relative_path}")

    _validate_evidence_ledger(
        root,
        contract=contract,
        contract_digest=digest,
        requirement_index=requirement_index,
    )
    _validate_derived_manifest(root, contract=contract, contract_digest=digest)

    return {
        "contract": CONTRACT_PATH.as_posix(),
        "derived_documents_manifest": DERIVED_DOCUMENTS_PATH.as_posix(),
        "evidence_ledger": EVIDENCE_LEDGER_PATH.as_posix(),
        "generated_views": [path.as_posix() for path in GENERATED_VIEWS.values()],
        "pull_request_count": len(contract["pull_requests"]),
        "requirement_count": len(requirement_index),
        "revision_transition": revision_transition,
        "status": "valid",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--previous-contract",
        type=Path,
        help=(
            "Optional previous canonical JSON source. When supplied, changed raw bytes "
            "must also change revision; omit only for first introduction/current-only checks."
        ),
    )
    parser.add_argument(
        "--base-revision",
        help="Full immutable Git commit object for the base side of the governed diff.",
    )
    parser.add_argument(
        "--source-revision",
        help="Full immutable Git commit object for the source side of the governed diff.",
    )
    args = parser.parse_args(argv)
    try:
        report = verify_repository(
            args.repo_root,
            previous_contract=args.previous_contract,
            base_revision=args.base_revision,
            source_revision=args.source_revision,
        )
    except UnionContractError as exc:
        print(f"union_six_pr_contract_invalid:{exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

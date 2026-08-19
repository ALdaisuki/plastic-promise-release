#!/usr/bin/env python3
"""Render and verify the generated views of the union six-PR contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA = "plastic-promise/union-six-pr-contract/v1"
EVIDENCE_LEDGER_SCHEMA = "plastic-promise/union-six-pr-evidence-ledger/v1"
SOURCE_RELATIVE = Path("docs/standards/union-six-pr-contract.json")
EVIDENCE_LEDGER_RELATIVE = Path("docs/standards/union-six-pr-evidence-ledger.json")
DERIVED_DOCUMENT_MANIFEST_RELATIVE = Path("docs/standards/union-six-pr-derived-documents.json")
GENERATED_PATHS = {
    "en": Path("docs/standards/union-six-pr-contract.md"),
    "zh_CN": Path("docs/standards/union-six-pr-contract.zh-CN.md"),
}
LANGUAGE_KEYS = ("en", "zh_CN")
REQUIRED_PR_GROUPS = ("delivery_scope", "collaboration_scope", "required_evidence")
GROUP_ID_MARKERS = {
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
COMMON_GATE_IDS = (
    "U6-GATE-SOURCE-01",
    "U6-GATE-REVISION-01",
    "U6-GATE-DOCUMENT-01",
    "U6-GATE-ASSET-01",
    "U6-GATE-REVIEW-01",
    "U6-GATE-DEEPSEC-01",
    "U6-GATE-EVIDENCE-01",
)
COMPLETION_GATE_IDS = (
    "U6-GATE-SOURCE-01",
    "U6-GATE-REVISION-01",
    "U6-GATE-DOCUMENT-01",
    "U6-GATE-ASSET-01",
    "U6-GATE-REVIEW-01",
    "U6-GATE-DEEPSEC-01",
    "U6-GATE-COMPOSER-01",
    "U6-GATE-EVIDENCE-01",
)
GOVERNANCE_RULE_IDS = ("U6-GOV-01", "U6-GOV-02", "U6-GOV-03", "U6-GOV-04")
REVISION_RE = re.compile(r"(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2})\.(?P<serial>[1-9][0-9]*)\Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    return value


def _require_sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be an array")
    return value


def _require_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value.strip()


def _require_sha256(value: Any, path: str) -> str:
    digest = _require_text(value, path)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{path} must be 64 lowercase hexadecimal characters")
    return digest


def parse_revision(value: Any, path: str) -> tuple[date, int]:
    """Parse the monotonic ``YYYY-MM-DD.N`` contract revision format."""

    revision = _require_text(value, path)
    match = REVISION_RE.fullmatch(revision)
    if match is None:
        raise ValueError(f"{path} must use YYYY-MM-DD.N")
    try:
        revision_day = date.fromisoformat(match.group("day"))
    except ValueError as exc:
        raise ValueError(f"{path} must contain a valid calendar date") from exc
    return revision_day, int(match.group("serial"))


def _statement_digest(statement: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        statement,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _localized(value: Any, language: str, path: str) -> str:
    localized = _require_mapping(value, path)
    for key in LANGUAGE_KEYS:
        _require_text(localized.get(key), f"{path}.{key}")
    return _require_text(localized[language], f"{path}.{language}")


def _validate_items(items: Any, path: str) -> None:
    seen: set[str] = set()
    for index, raw_item in enumerate(_require_sequence(items, path)):
        item = _require_mapping(raw_item, f"{path}[{index}]")
        item_id = _require_text(item.get("id"), f"{path}[{index}].id")
        if item_id in seen:
            raise ValueError(f"duplicate item id {item_id!r} in {path}")
        seen.add(item_id)
        for language in LANGUAGE_KEYS:
            _localized(item.get("statement"), language, f"{path}[{index}].statement")
    if not seen:
        raise ValueError(f"{path} must contain at least one item")


def validate_contract(payload: Any) -> Mapping[str, Any]:
    contract = _require_mapping(payload, "contract")
    if contract.get("schema") != SCHEMA:
        raise ValueError(f"contract.schema must equal {SCHEMA!r}")
    revision = _require_text(contract.get("revision"), "contract.revision")
    revision_key = parse_revision(revision, "contract.revision")

    lineage = _require_mapping(contract.get("revision_lineage"), "contract.revision_lineage")
    if lineage.get("comparison") != "sha256-raw-source-bytes":
        raise ValueError("contract.revision_lineage.comparison must be sha256-raw-source-bytes")
    mode = _require_text(lineage.get("mode"), "contract.revision_lineage.mode")
    if mode not in LINEAGE_MODES:
        raise ValueError(f"contract.revision_lineage.mode must be one of {LINEAGE_MODES!r}")
    previous = lineage.get("previous_canonical")
    if mode == "repository-authority-introduction":
        if previous is not None:
            raise ValueError(
                "contract.revision_lineage.previous_canonical must be null for first introduction"
            )
    else:
        previous_mapping = _require_mapping(
            previous, "contract.revision_lineage.previous_canonical"
        )
        previous_revision = _require_text(
            previous_mapping.get("revision"),
            "contract.revision_lineage.previous_canonical.revision",
        )
        previous_revision_key = parse_revision(
            previous_revision,
            "contract.revision_lineage.previous_canonical.revision",
        )
        if revision_key <= previous_revision_key:
            raise ValueError(
                "contract revision must be newer than revision_lineage.previous_canonical.revision"
            )
        _require_sha256(
            previous_mapping.get("sha256"),
            "contract.revision_lineage.previous_canonical.sha256",
        )
        _require_text(
            previous_mapping.get("reference"),
            "contract.revision_lineage.previous_canonical.reference",
        )

    provenance = _require_sequence(
        lineage.get("provenance"), "contract.revision_lineage.provenance"
    )
    for index, raw_entry in enumerate(provenance):
        entry_path = f"contract.revision_lineage.provenance[{index}]"
        entry = _require_mapping(raw_entry, entry_path)
        if entry.get("classification") != "provenance-only":
            raise ValueError(f"{entry_path}.classification must be provenance-only")
        parse_revision(entry.get("claimed_revision"), f"{entry_path}.claimed_revision")
        _require_sha256(entry.get("sha256"), f"{entry_path}.sha256")
        _require_text(entry.get("reference"), f"{entry_path}.reference")
        if entry.get("verifiable_canonical_source") is not False:
            raise ValueError(f"{entry_path}.verifiable_canonical_source must be false")

    authority = _require_mapping(contract.get("authority"), "contract.authority")
    if authority.get("normative") is not True:
        raise ValueError("contract.authority.normative must be true")
    if authority.get("canonical_source") != SOURCE_RELATIVE.as_posix():
        raise ValueError("contract.authority.canonical_source must name the JSON source")
    generated_views = _require_sequence(
        authority.get("generated_views"), "contract.authority.generated_views"
    )
    expected_views = [path.as_posix() for path in GENERATED_PATHS.values()]
    if generated_views != expected_views:
        raise ValueError(f"generated_views must equal {expected_views!r}")
    if authority.get("digest_algorithm") != "sha256-raw-source-bytes":
        raise ValueError("authority.digest_algorithm must be sha256-raw-source-bytes")
    for language in LANGUAGE_KEYS:
        _localized(authority.get("scope"), language, "contract.authority.scope")
    _validate_items(authority.get("change_control"), "contract.authority.change_control")

    precedence = _require_sequence(contract.get("source_precedence"), "contract.source_precedence")
    priorities: list[int] = []
    for index, raw_entry in enumerate(precedence):
        entry = _require_mapping(raw_entry, f"contract.source_precedence[{index}]")
        priority = entry.get("priority")
        if not isinstance(priority, int) or priority < 1:
            raise ValueError(f"source_precedence[{index}].priority must be a positive int")
        priorities.append(priority)
        _require_text(entry.get("source"), f"source_precedence[{index}].source")
        _require_text(entry.get("classification"), f"source_precedence[{index}].classification")
        for language in LANGUAGE_KEYS:
            _localized(entry.get("rule"), language, f"source_precedence[{index}].rule")
    if priorities != list(range(1, len(priorities) + 1)):
        raise ValueError("source_precedence priorities must be contiguous and ordered")

    governance = _require_mapping(
        contract.get("governance_artifacts"), "contract.governance_artifacts"
    )
    if governance.get("evidence_ledger") != EVIDENCE_LEDGER_RELATIVE.as_posix():
        raise ValueError("governance_artifacts.evidence_ledger must name the canonical ledger")
    if governance.get("derived_document_manifest") != DERIVED_DOCUMENT_MANIFEST_RELATIVE.as_posix():
        raise ValueError(
            "governance_artifacts.derived_document_manifest must name the canonical manifest"
        )
    if governance.get("evidence_classes") != list(EVIDENCE_CLASSES):
        raise ValueError(
            f"governance_artifacts.evidence_classes must equal {list(EVIDENCE_CLASSES)!r}"
        )
    if governance.get("evidence_states") != list(EVIDENCE_STATES):
        raise ValueError(
            f"governance_artifacts.evidence_states must equal {list(EVIDENCE_STATES)!r}"
        )
    governance_rules = _require_sequence(
        governance.get("rules"), "contract.governance_artifacts.rules"
    )
    actual_governance_rule_ids: list[str] = []
    for index, raw_rule in enumerate(governance_rules):
        rule = _require_mapping(raw_rule, f"contract.governance_artifacts.rules[{index}]")
        actual_governance_rule_ids.append(
            _require_text(rule.get("id"), f"contract.governance_artifacts.rules[{index}].id")
        )
        for language in LANGUAGE_KEYS:
            _localized(
                rule.get("statement"),
                language,
                f"contract.governance_artifacts.rules[{index}].statement",
            )
    if actual_governance_rule_ids != list(GOVERNANCE_RULE_IDS):
        raise ValueError(f"governance rule ids must equal {list(GOVERNANCE_RULE_IDS)!r}")

    completion_gates = _require_sequence(
        contract.get("completion_gates"), "contract.completion_gates"
    )
    actual_gate_ids: list[str] = []
    expected_pr_ids = [f"PR{index}" for index in range(1, 7)]
    for index, raw_gate in enumerate(completion_gates):
        gate = _require_mapping(raw_gate, f"contract.completion_gates[{index}]")
        gate_id = _require_text(gate.get("id"), f"contract.completion_gates[{index}].id")
        actual_gate_ids.append(gate_id)
        applies_to = _require_sequence(
            gate.get("applies_to"), f"contract.completion_gates[{index}].applies_to"
        )
        expected_applies_to = ["PR6"] if gate_id == "U6-GATE-COMPOSER-01" else expected_pr_ids
        if applies_to != expected_applies_to:
            raise ValueError(f"{gate_id}.applies_to must equal {expected_applies_to!r}")
        required_classes = _require_sequence(
            gate.get("required_evidence_classes"),
            f"contract.completion_gates[{index}].required_evidence_classes",
        )
        if not required_classes or any(item not in EVIDENCE_CLASSES for item in required_classes):
            raise ValueError(f"{gate_id} names an invalid evidence class")
        if len(set(required_classes)) != len(required_classes):
            raise ValueError(f"{gate_id} repeats an evidence class")
        for language in LANGUAGE_KEYS:
            _localized(
                gate.get("statement"),
                language,
                f"contract.completion_gates[{index}].statement",
            )
    if actual_gate_ids != list(COMPLETION_GATE_IDS):
        raise ValueError(f"completion gate ids must equal {list(COMPLETION_GATE_IDS)!r}")

    experimental_features = _require_sequence(
        contract.get("experimental_features"), "contract.experimental_features"
    )
    if len(experimental_features) != 1:
        raise ValueError("contract.experimental_features must contain exactly workflow-composer")
    workflow_composer = _require_mapping(
        experimental_features[0], "contract.experimental_features[0]"
    )
    expected_feature_fields = {
        "id": "workflow-composer",
        "owning_pr": "PR6",
        "disposition": "included-shadow",
        "activation": "shadow-only",
        "rollback": "fixed-route",
        "required_gate_id": "U6-GATE-COMPOSER-01",
    }
    for key, expected in expected_feature_fields.items():
        if workflow_composer.get(key) != expected:
            raise ValueError(f"workflow-composer.{key} must equal {expected!r}")
    for language in LANGUAGE_KEYS:
        _localized(
            workflow_composer.get("statement"),
            language,
            "contract.experimental_features[0].statement",
        )

    invariants = _require_sequence(
        contract.get("cross_cutting_invariants"), "contract.cross_cutting_invariants"
    )
    invariant_ids: set[str] = set()
    for index, raw_invariant in enumerate(invariants):
        invariant = _require_mapping(raw_invariant, f"contract.cross_cutting_invariants[{index}]")
        invariant_id = _require_text(invariant.get("id"), f"cross_cutting_invariants[{index}].id")
        if invariant_id in invariant_ids:
            raise ValueError(f"duplicate invariant id {invariant_id!r}")
        invariant_ids.add(invariant_id)
        for language in LANGUAGE_KEYS:
            _localized(
                invariant.get("title"),
                language,
                f"cross_cutting_invariants[{index}].title",
            )
            _localized(
                invariant.get("statement"),
                language,
                f"cross_cutting_invariants[{index}].statement",
            )
    if not invariant_ids:
        raise ValueError("cross_cutting_invariants must not be empty")
    expected_invariant_ids = [f"U6-INV-{index:02d}" for index in range(1, 18)]
    actual_invariant_ids = [str(item["id"]) for item in invariants]
    if actual_invariant_ids != expected_invariant_ids:
        raise ValueError(f"invariant ids must equal {expected_invariant_ids!r}")

    pull_requests = _require_sequence(contract.get("pull_requests"), "contract.pull_requests")
    expected_ids = expected_pr_ids
    actual_ids: list[str] = []
    all_item_ids: set[str] = set()
    for index, raw_pr in enumerate(pull_requests):
        pr = _require_mapping(raw_pr, f"contract.pull_requests[{index}]")
        pr_id = _require_text(pr.get("id"), f"pull_requests[{index}].id")
        actual_ids.append(pr_id)
        expected_dependencies = [] if index == 0 else [f"PR{index}"]
        if pr.get("depends_on") != expected_dependencies:
            raise ValueError(f"{pr_id}.depends_on must equal {expected_dependencies!r}")
        expected_gate_ids = list(COMMON_GATE_IDS)
        if pr_id == "PR6":
            expected_gate_ids.insert(-1, "U6-GATE-COMPOSER-01")
        if pr.get("required_gate_ids") != expected_gate_ids:
            raise ValueError(f"{pr_id}.required_gate_ids must equal {expected_gate_ids!r}")
        _require_text(pr.get("slug"), f"pull_requests[{index}].slug")
        for language in LANGUAGE_KEYS:
            _localized(pr.get("title"), language, f"pull_requests[{index}].title")
            _localized(pr.get("objective"), language, f"pull_requests[{index}].objective")
        for group in REQUIRED_PR_GROUPS:
            _validate_items(pr.get(group), f"pull_requests[{index}].{group}")
            for ordinal, item in enumerate(
                _require_sequence(pr[group], f"pull_requests[{index}].{group}"), start=1
            ):
                item_id = str(_require_mapping(item, group)["id"])
                expected_item_id = f"{pr_id}-{GROUP_ID_MARKERS[group]}{ordinal:02d}"
                if item_id != expected_item_id:
                    raise ValueError(
                        f"{pr_id}.{group}[{ordinal - 1}].id must equal {expected_item_id!r}"
                    )
                if item_id in all_item_ids:
                    raise ValueError(f"duplicate global requirement id {item_id!r}")
                all_item_ids.add(item_id)

        completion = _require_mapping(
            pr.get("completion_rule"), f"pull_requests[{index}].completion_rule"
        )
        if completion.get("operator") != "all":
            raise ValueError(f"{pr_id} completion_rule.operator must be all")
        if completion.get("required_groups") != list(REQUIRED_PR_GROUPS):
            raise ValueError(
                f"{pr_id} completion_rule.required_groups must equal {list(REQUIRED_PR_GROUPS)!r}"
            )
        if completion.get("prohibits_partial_completion") is not True:
            raise ValueError(f"{pr_id} must prohibit partial completion")
        for language in LANGUAGE_KEYS:
            _localized(
                completion.get("statement"),
                language,
                f"pull_requests[{index}].completion_rule.statement",
            )
    if actual_ids != expected_ids:
        raise ValueError(f"pull_requests must be exactly {expected_ids!r} in order")
    return contract


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _labels(language: str) -> Mapping[str, str]:
    if language == "en":
        return {
            "title": "Union Six-PR Delivery Contract",
            "warning": (
                "Generated view. Do not edit this file directly. Edit the canonical JSON, "
                "increment its revision, and regenerate both language views."
            ),
            "source": "Canonical source",
            "revision": "Revision",
            "digest": "Source SHA-256",
            "lineage": "Revision lineage",
            "lineage_mode": "Lineage mode",
            "previous_canonical": "Previous canonical source",
            "no_previous_canonical": "none — first repository authority introduction",
            "previous_revision": "Previous revision",
            "previous_digest": "Previous SHA-256",
            "previous_reference": "Previous source reference",
            "provenance": "Provenance-only audit records",
            "claimed_revision": "Claimed revision",
            "verifiable_canonical": "Verifiable canonical source",
            "authority": "Authority and change control",
            "precedence": "Source precedence",
            "priority": "Priority",
            "classification": "Classification",
            "rule": "Rule",
            "invariants": "Cross-cutting invariants",
            "governance": "Governance artifacts and evidence policy",
            "ledger": "Evidence ledger",
            "manifest": "Derived-document manifest",
            "classes": "Evidence classes",
            "states": "Evidence states",
            "gates": "Completion gates",
            "applies": "Applies to",
            "required_classes": "Required evidence classes",
            "experimental": "Experimental feature dispositions",
            "feature": "Feature",
            "disposition": "Disposition",
            "activation": "Activation",
            "rollback": "Rollback",
            "matrix": "Completion matrix",
            "pr": "PR",
            "depends": "Depends on",
            "required_gates": "Required gates",
            "objective": "Objective",
            "delivery": "Delivery scope",
            "collaboration": "Collaboration scope",
            "evidence": "Required evidence",
            "completion": "Completion rule",
            "items": "items",
            "details": "PR contracts",
            "all_required": "ALL groups are mandatory; a missing item means the PR is incomplete.",
        }
    if language == "zh_CN":
        return {
            "title": "联合六 PR 交付合同",
            "warning": "生成视图，请勿直接编辑。请修改规范 JSON、递增 revision，并重新生成两种语言视图。",
            "source": "规范源",
            "revision": "Revision",
            "digest": "源文件 SHA-256",
            "lineage": "Revision 谱系",
            "lineage_mode": "谱系模式",
            "previous_canonical": "上一规范源",
            "no_previous_canonical": "无 — 首次引入 repository authority",
            "previous_revision": "上一 revision",
            "previous_digest": "上一 SHA-256",
            "previous_reference": "上一源码引用",
            "provenance": "仅溯源 audit 记录",
            "claimed_revision": "声称的 revision",
            "verifiable_canonical": "可验证规范源",
            "authority": "权威与变更控制",
            "precedence": "来源优先级",
            "priority": "优先级",
            "classification": "分类",
            "rule": "规则",
            "invariants": "跨 PR 不变量",
            "governance": "治理制品与证据策略",
            "ledger": "证据台账",
            "manifest": "派生文档清单",
            "classes": "证据层级",
            "states": "证据状态",
            "gates": "完成门禁",
            "applies": "适用范围",
            "required_classes": "强制证据层级",
            "experimental": "实验功能 disposition",
            "feature": "功能",
            "disposition": "Disposition",
            "activation": "Activation",
            "rollback": "Rollback",
            "matrix": "完成矩阵",
            "pr": "PR",
            "depends": "依赖",
            "required_gates": "强制门禁",
            "objective": "目标",
            "delivery": "交付范围",
            "collaboration": "协作范围",
            "evidence": "强制证据",
            "completion": "完成规则",
            "items": "项",
            "details": "PR 合同明细",
            "all_required": "所有分组均为强制项；缺失任一项即表示 PR 未完成。",
        }
    raise ValueError(f"unsupported language {language!r}")


def render_contract(payload: Mapping[str, Any], source_digest: str, language: str) -> str:
    """Render one deterministic language view from a validated contract."""

    contract = validate_contract(payload)
    if language not in LANGUAGE_KEYS:
        raise ValueError(f"unsupported language {language!r}")
    if len(source_digest) != 64 or any(char not in "0123456789abcdef" for char in source_digest):
        raise ValueError("source_digest must be 64 lowercase hexadecimal characters")

    labels = _labels(language)
    source = str(contract["authority"]["canonical_source"])
    lines = [
        (f"<!-- GENERATED FILE — DO NOT EDIT. Source: {source}; SHA-256: {source_digest} -->"),
        "",
        f"# {labels['title']}",
        "",
        f"> **{labels['warning']}**",
        "",
        f"- **{labels['source']}:** `{source}`",
        f"- **{labels['revision']}:** `{contract['revision']}`",
        f"- **{labels['digest']}:** `{source_digest}`",
        "",
        f"## {labels['authority']}",
        "",
        _localized(contract["authority"]["scope"], language, "authority.scope"),
        "",
    ]
    for item in contract["authority"]["change_control"]:
        statement = _localized(item["statement"], language, f"change_control.{item['id']}")
        lines.append(f"- **`{item['id']}`** — {statement}")

    lineage = contract["revision_lineage"]
    previous = lineage["previous_canonical"]
    lines.extend(
        [
            "",
            f"### {labels['lineage']}",
            "",
            f"- **{labels['lineage_mode']}:** `{lineage['mode']}`",
        ]
    )
    if previous is None:
        lines.append(f"- **{labels['previous_canonical']}:** {labels['no_previous_canonical']}")
    else:
        lines.extend(
            [
                f"- **{labels['previous_revision']}:** `{previous['revision']}`",
                f"- **{labels['previous_digest']}:** `{previous['sha256']}`",
                f"- **{labels['previous_reference']}:** `{previous['reference']}`",
            ]
        )
    if lineage["provenance"]:
        lines.extend(
            [
                "",
                f"#### {labels['provenance']}",
                "",
                (
                    f"| {labels['claimed_revision']} | SHA-256 | {labels['source']} | "
                    f"{labels['classification']} | {labels['verifiable_canonical']} |"
                ),
                "|---|---|---|---|---|",
            ]
        )
        for provenance in lineage["provenance"]:
            lines.append(
                f"| `{provenance['claimed_revision']}` | `{provenance['sha256']}` | "
                f"`{_escape_table(provenance['reference'])}` | "
                f"`{provenance['classification']}` | "
                f"`{str(provenance['verifiable_canonical_source']).lower()}` |"
            )

    lines.extend(
        [
            "",
            f"## {labels['precedence']}",
            "",
            f"| {labels['priority']} | {labels['source']} | {labels['classification']} | {labels['rule']} |",
            "|---:|---|---|---|",
        ]
    )
    for entry in contract["source_precedence"]:
        rule = _localized(entry["rule"], language, f"source_precedence.{entry['priority']}.rule")
        lines.append(
            f"| {entry['priority']} | {_escape_table(entry['source'])} | "
            f"`{_escape_table(entry['classification'])}` | {_escape_table(rule)} |"
        )

    governance = contract["governance_artifacts"]
    lines.extend(
        [
            "",
            f"## {labels['governance']}",
            "",
            f"- **{labels['ledger']}:** `{governance['evidence_ledger']}`",
            f"- **{labels['manifest']}:** `{governance['derived_document_manifest']}`",
            f"- **{labels['classes']}:** "
            + ", ".join(f"`{item}`" for item in governance["evidence_classes"]),
            f"- **{labels['states']}:** "
            + ", ".join(f"`{item}`" for item in governance["evidence_states"]),
            "",
        ]
    )
    for rule in governance["rules"]:
        statement = _localized(rule["statement"], language, f"{rule['id']}.statement")
        lines.append(f"- **`{rule['id']}`** — {statement}")

    lines.extend(
        [
            "",
            f"## {labels['gates']}",
            "",
            (f"| ID | {labels['applies']} | {labels['required_classes']} | {labels['rule']} |"),
            "|---|---|---|---|",
        ]
    )
    for gate in contract["completion_gates"]:
        statement = _localized(gate["statement"], language, f"{gate['id']}.statement")
        applies_to = ", ".join(f"`{item}`" for item in gate["applies_to"])
        evidence_classes = ", ".join(f"`{item}`" for item in gate["required_evidence_classes"])
        lines.append(
            f"| `{gate['id']}` | {applies_to} | {evidence_classes} | {_escape_table(statement)} |"
        )

    lines.extend(
        [
            "",
            f"## {labels['experimental']}",
            "",
            (
                f"| {labels['feature']} | {labels['pr']} | {labels['disposition']} | "
                f"{labels['activation']} | {labels['rollback']} | {labels['rule']} |"
            ),
            "|---|---|---|---|---|---|",
        ]
    )
    for feature in contract["experimental_features"]:
        statement = _localized(feature["statement"], language, f"{feature['id']}.statement")
        lines.append(
            f"| `{feature['id']}` | `{feature['owning_pr']}` | "
            f"`{feature['disposition']}` | `{feature['activation']}` | "
            f"`{feature['rollback']}` | {_escape_table(statement)} |"
        )

    lines.extend(["", f"## {labels['invariants']}", ""])
    for invariant in contract["cross_cutting_invariants"]:
        title = _localized(invariant["title"], language, f"{invariant['id']}.title")
        statement = _localized(invariant["statement"], language, f"{invariant['id']}.statement")
        lines.extend([f"### `{invariant['id']}` — {title}", "", statement, ""])

    lines.extend(
        [
            f"## {labels['matrix']}",
            "",
            f"> **{labels['all_required']}**",
            "",
            (
                f"| {labels['pr']} | {labels['depends']} | {labels['required_gates']} | "
                f"{labels['objective']} | {labels['delivery']} | "
                f"{labels['collaboration']} | {labels['evidence']} |"
            ),
            "|---|---|---:|---|---:|---:|---:|",
        ]
    )
    for pr in contract["pull_requests"]:
        objective = _localized(pr["objective"], language, f"{pr['id']}.objective")
        dependencies = ", ".join(f"`{item}`" for item in pr["depends_on"]) or "—"
        lines.append(
            f"| **{pr['id']}** | {dependencies} | {len(pr['required_gate_ids'])} | "
            f"{_escape_table(objective)} | "
            f"{len(pr['delivery_scope'])} {labels['items']} | "
            f"{len(pr['collaboration_scope'])} {labels['items']} | "
            f"{len(pr['required_evidence'])} {labels['items']} |"
        )

    lines.extend(["", f"## {labels['details']}", ""])
    for pr in contract["pull_requests"]:
        title = _localized(pr["title"], language, f"{pr['id']}.title")
        objective = _localized(pr["objective"], language, f"{pr['id']}.objective")
        lines.extend(
            [
                f"## {pr['id']} — {title}",
                "",
                f"**{labels['objective']}:** {objective}",
                "",
                f"- **{labels['depends']}:** "
                + (", ".join(f"`{item}`" for item in pr["depends_on"]) or "—"),
                f"- **{labels['required_gates']}:** "
                + ", ".join(f"`{item}`" for item in pr["required_gate_ids"]),
                "",
            ]
        )
        for group, label in (
            ("delivery_scope", labels["delivery"]),
            ("collaboration_scope", labels["collaboration"]),
            ("required_evidence", labels["evidence"]),
        ):
            lines.extend([f"### {label}", ""])
            for item in pr[group]:
                statement = _localized(item["statement"], language, f"{item['id']}.statement")
                lines.append(f"- **`{item['id']}`** — {statement}")
            lines.append("")
        completion = pr["completion_rule"]
        statement = _localized(
            completion["statement"], language, f"{pr['id']}.completion_rule.statement"
        )
        groups = ", ".join(f"`{group}`" for group in completion["required_groups"])
        lines.extend(
            [
                f"### {labels['completion']}",
                "",
                f"- `operator`: `{completion['operator']}`",
                f"- `required_groups`: {groups}",
                "- `prohibits_partial_completion`: `true`",
                f"- {statement}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_requirement_index(payload: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    """Return the exact ordered PR requirement index exposed by the contract seam."""

    contract = validate_contract(payload)
    index: dict[str, Mapping[str, Any]] = {}
    for pr in contract["pull_requests"]:
        for group in REQUIRED_PR_GROUPS:
            for ordinal, item in enumerate(pr[group], start=1):
                index[item["id"]] = {
                    "pr_id": pr["id"],
                    "group": group,
                    "ordinal": ordinal,
                    "statement_sha256": _statement_digest(item["statement"]),
                }
    return index


def build_initial_evidence_ledger(
    payload: Mapping[str, Any], source_digest: str
) -> Mapping[str, Any]:
    """Build a fail-closed ledger with no inferred implementation or runtime claims."""

    contract = validate_contract(payload)
    _require_sha256(source_digest, "source_digest")
    requirements: dict[str, Any] = {}
    for requirement_id, identity in build_requirement_index(contract).items():
        entry = dict(identity)
        for evidence_class in EVIDENCE_CLASSES:
            entry[evidence_class] = {"state": "not-evidenced", "receipts": []}
        requirements[requirement_id] = entry

    return {
        "schema": EVIDENCE_LEDGER_SCHEMA,
        "contract": {
            "path": SOURCE_RELATIVE.as_posix(),
            "schema": SCHEMA,
            "revision": contract["revision"],
            "sha256": source_digest,
            "digest_algorithm": "sha256-raw-source-bytes",
        },
        "evidence_policy": {
            "classes": list(EVIDENCE_CLASSES),
            "states": list(EVIDENCE_STATES),
            "receipt_schema": {
                "required_fields": list(RECEIPT_FIELDS),
                "id_prefix": "receipt:",
                "sha256_format": "lowercase-hex-64",
                "recorded_at_format": "utc-iso8601",
                "class_must_match_bucket": True,
                "state_must_match_bucket": True,
                "requirement_must_match_bucket": True,
                "content_addressed_ref_prefix": "repo:docs/evidence/",
                "authorities": list(RECEIPT_AUTHORITIES),
                "attestation_kinds": list(ATTESTATION_KINDS),
                "review_channels": list(REVIEW_CHANNELS),
                "verified_requires_attested_authority": True,
                "git_boundary_required": True,
                "source_material_algorithm": SOURCE_MATERIAL_ALGORITHM,
                "diff_material_algorithm": DIFF_MATERIAL_ALGORITHM,
                "artifact_schema": EVIDENCE_ARTIFACT_SCHEMA,
                "artifact_must_bind_receipt_fields": True,
            },
            "rules": [
                "requirement ids, PR/group ownership, ordinal, and statement digest must exactly match the canonical contract",
                "not-evidenced requires an empty receipt list; partial, verified, and not-applicable require at least one same-class receipt",
                "each receipt binds its bucket state and requirement id, the exact contract and requirement-set digests, a real ancestor Git base-to-source boundary, source-tree material, deterministic raw diff, changed paths, policy digest, issuer/subject identities, review channel, and a content-addressed JSON artifact under docs/evidence",
                "the evidence artifact must parse as the declared evidence-artifact schema and exactly repeat every receipt field except its own content digest and repository reference",
                "repository-authored artifacts may support partial evidence only; verified or not-applicable states require a protected-workflow or server attestation, and verified runtime/production requires server authority",
                "implementation or test receipts never satisfy runtime or production, and runtime receipts never satisfy production",
                "production verified requires production-class evidence and a verified runtime state for the same requirement",
                "not-applicable is valid only when a same-class receipt cites an explicit exemption in a later canonical contract revision",
            ],
        },
        "requirements": requirements,
    }


def _write_initial_evidence_ledger(
    repo_root: Path, payload: Mapping[str, Any], source_digest: str
) -> None:
    output_path = repo_root / EVIDENCE_LEDGER_RELATIVE
    if output_path.exists():
        raise ValueError(
            f"refusing to overwrite existing evidence ledger {output_path.relative_to(repo_root)}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        build_initial_evidence_ledger(payload, source_digest),
        ensure_ascii=False,
        indent=2,
    )
    output_path.write_text(serialized + "\n", encoding="utf-8")
    print(f"wrote {output_path.relative_to(repo_root)}")


def _load_source(source_path: Path) -> tuple[Mapping[str, Any], str]:
    raw = source_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid contract JSON: {exc}") from exc
    return validate_contract(payload), digest


def render_generated_views(source_path: Path) -> Mapping[str, str]:
    payload, digest = _load_source(source_path)
    return {language: render_contract(payload, digest, language) for language in LANGUAGE_KEYS}


def _write_generated(repo_root: Path, rendered: Mapping[str, str]) -> None:
    for language, content in rendered.items():
        output_path = repo_root / GENERATED_PATHS[language]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        print(f"wrote {output_path.relative_to(repo_root)}")


def _check_generated(repo_root: Path, rendered: Mapping[str, str]) -> int:
    stale: list[str] = []
    for language, expected in rendered.items():
        output_path = repo_root / GENERATED_PATHS[language]
        if not output_path.is_file():
            stale.append(f"missing {output_path.relative_to(repo_root)}")
            continue
        actual = output_path.read_text(encoding="utf-8")
        if actual != expected:
            stale.append(f"stale {output_path.relative_to(repo_root)}")
    if stale:
        for message in stale:
            print(message, file=sys.stderr)
        print(
            "run scripts/render_union_six_pr_contract.py --write-generated",
            file=sys.stderr,
        )
        return 1
    print("union six-PR generated views are current")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        help="contract JSON path (defaults to docs/standards/union-six-pr-contract.json)",
    )
    parser.add_argument(
        "--write-generated",
        action="store_true",
        help="write both generated Markdown views instead of checking them",
    )
    parser.add_argument(
        "--initialize-evidence-ledger",
        action="store_true",
        help="create the initial all-not-evidenced ledger; refuses to overwrite an existing ledger",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    repo_root = _repo_root()
    source_path = args.source or (repo_root / SOURCE_RELATIVE)
    if not source_path.is_absolute():
        source_path = repo_root / source_path
    try:
        payload, digest = _load_source(source_path)
        rendered = {
            language: render_contract(payload, digest, language) for language in LANGUAGE_KEYS
        }
        if args.initialize_evidence_ledger:
            _write_initial_evidence_ledger(repo_root, payload, digest)
        if args.write_generated:
            _write_generated(repo_root, rendered)
            return 0
        if args.initialize_evidence_ledger:
            return 0
        return _check_generated(repo_root, rendered)
    except (OSError, ValueError) as exc:
        print(f"union six-PR contract error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

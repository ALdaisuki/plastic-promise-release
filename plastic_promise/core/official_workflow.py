"""Single typed manifest for the pinned Matt Pocock engineering skills."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

InvocationAuthority = Literal["user", "model"]

UPSTREAM_SKILLS_REPOSITORY = "mattpocock/skills"
UPSTREAM_SKILLS_REVISION = "ed37663cc5fbef691ddfecd080dff42f7e7e350d"
_RECEIPT_REQUIRED_FIELDS = frozenset(
    {"skill", "upstream_revision", "content_sha256", "status", "evidence"}
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])LTAI[A-Z0-9]{12,}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\bBasic\s+[A-Za-z0-9+/]{12,}={0,2}", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}", re.IGNORECASE),
    re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:/?#]+:[^@\s/]+@", re.IGNORECASE),
)

COMPOSITE_SKILL_CALLS: dict[str, dict[str, tuple[str, ...]]] = {
    "grill-me": {"required": ("grilling",), "optional": ()},
    "implement": {"required": ("code-review",), "optional": ("tdd",)},
}


@dataclass(frozen=True)
class OfficialSkill:
    name: str
    authority: InvocationAuthority
    domain: str
    task_type: str
    layer: str
    artifact: str
    closure_mode: Literal["light", "full"]
    content_sha256: str


@dataclass(frozen=True)
class OfficialRoute:
    route_id: str
    label: str
    summary: str
    stages: tuple[str, ...]
    branches: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def branch_map(self) -> dict[str, list[str]]:
        return {name: list(stages) for name, stages in self.branches}


def _skill(
    name: str,
    authority: InvocationAuthority,
    domain: str,
    task_type: str,
    layer: str,
    artifact: str,
    closure_mode: Literal["light", "full"],
    content_sha256: str,
) -> OfficialSkill:
    return OfficialSkill(
        name=name,
        authority=authority,
        domain=domain,
        task_type=task_type,
        layer=layer,
        artifact=artifact,
        closure_mode=closure_mode,
        content_sha256=content_sha256,
    )


OFFICIAL_SKILLS = {
    skill.name: skill
    for skill in (
        _skill(
            "setup-matt-pocock-skills",
            "user",
            "governing",
            "general",
            "bootstrap",
            "Repository workflow configuration.",
            "full",
            "def265a8b15ffb8afc3f335d69e175ba9a7fe3991218984b0e49e8345cde3b20",
        ),
        _skill(
            "ask-matt",
            "user",
            "designing",
            "architecture",
            "routing",
            "A selected official flow with caller attestation.",
            "light",
            "b1a134ada29cbfded84bc9a7f93356ab7a3d7f800edf1f541a2a964118ad45a7",
        ),
        _skill(
            "grill-with-docs",
            "user",
            "designing",
            "architecture",
            "discovery",
            "Updated project vocabulary and decisions.",
            "light",
            "610d091047bcfb9db0f75c057d15538481a721111579fc5ec7f83ad9131a2165",
        ),
        _skill(
            "grill-me",
            "user",
            "designing",
            "architecture",
            "discovery",
            "A shared understanding of a plan or design.",
            "light",
            "6189dfceb7304a6e5558f75d87e68fa3bc7fcf7ba120e44f21f8a61fe01eba54",
        ),
        _skill(
            "grilling",
            "model",
            "designing",
            "architecture",
            "discovery",
            "Resolved decisions from a one-question-at-a-time interview.",
            "light",
            "44331dda57f461db4fec3f2efb6ddabe7aaaa0a57ae0f88a883bc61aed8a0587",
        ),
        _skill(
            "to-spec",
            "user",
            "designing",
            "architecture",
            "specification",
            "A buildable specification with acceptance criteria.",
            "light",
            "267638edd513b5918de626ad5605d261952abb7428cb308869c663ca924e93e7",
        ),
        _skill(
            "to-tickets",
            "user",
            "designing",
            "architecture",
            "planning",
            "Tracer-bullet tickets with blocking edges.",
            "light",
            "5ecdf1d4df8a360ed39df21a2347f97ba177afd449a577da4f6b6ea8e1ebb808",
        ),
        _skill(
            "implement",
            "user",
            "building",
            "code_generation",
            "implementation",
            "A bounded code delta with focused verification.",
            "full",
            "6d3fd9e83b8f36e5213854779db49b256a457a7ebb4a503e53fa7dcff696adc3",
        ),
        _skill(
            "tdd",
            "model",
            "building",
            "code_generation",
            "testing",
            "A red-green vertical slice at an agreed test seam.",
            "full",
            "5363bb2775679fe9311fbb67947f95359169c6e7f1fac77c0f25e190bca6cf2f",
        ),
        _skill(
            "code-review",
            "model",
            "reflecting",
            "code_review",
            "review",
            "Standards and specification review findings.",
            "full",
            "6a65cc61114f96db07ec41e3920e67c9c5bf70dd6e0901eb9460ebcb2bdc209f",
        ),
        _skill(
            "diagnosing-bugs",
            "model",
            "fixing",
            "debugging",
            "debugging",
            "Reproduction, root cause, regression test, and verified fix.",
            "full",
            "7a0779480f323a66d109404646bcc1a14bf0232b45b3e3ea93b652a035718acb",
        ),
        _skill(
            "prototype",
            "model",
            "building",
            "code_generation",
            "exploration",
            "A throwaway answer and explicit keep/discard decision.",
            "full",
            "03074862d4b6e4eaf472aa75146e1d193dd9e3bba0e4303a9b2425562d1d44cc",
        ),
        _skill(
            "research",
            "model",
            "designing",
            "architecture",
            "research",
            "A cited primary-source research note.",
            "light",
            "af378829f015775a3bcd65ff466826722e99359017ae6bae227ca4c9bd14049c",
        ),
        _skill(
            "triage",
            "user",
            "governing",
            "general",
            "triage",
            "An agent-ready issue or verified rejection.",
            "full",
            "d45827c299c021f77b0f146fefa3ee679b13f99e9a2ffdf48e8de2347adeefe1",
        ),
        _skill(
            "wayfinder",
            "user",
            "designing",
            "architecture",
            "discovery",
            "A linked decision map with resolved blockers.",
            "light",
            "257e40665b28ae959ffdcb97d7a72b074360f4a3d201bd84786505308546e434",
        ),
        _skill(
            "improve-codebase-architecture",
            "user",
            "reflecting",
            "code_review",
            "architecture",
            "A prioritized deepening opportunity.",
            "light",
            "4b4cb798c3863d5b6f5c0b4604af1ecb5beb6df82553c972898a91ba38bcf289",
        ),
        _skill(
            "domain-modeling",
            "model",
            "designing",
            "architecture",
            "modeling",
            "Updated glossary, CONTEXT.md, or ADR evidence.",
            "light",
            "152e2c97239affb12a60c5f4a7e74ab546a49ae169688c81f4e2ccc42dafa579",
        ),
        _skill(
            "codebase-design",
            "model",
            "designing",
            "architecture",
            "architecture",
            "A selected deep-module design and rationale.",
            "light",
            "a8d50abac5a4018f60e1d911d4b6f4e36454ca14d6c390c0695a578c7de65dad",
        ),
        _skill(
            "resolving-merge-conflicts",
            "model",
            "fixing",
            "debugging",
            "integration",
            "Resolved conflicts with intent and verification evidence.",
            "full",
            "c7c9ba81362a786aac05d2223123bf1bd2f8a99c3243a72882ede9c68bedfb24",
        ),
        _skill(
            "handoff",
            "user",
            "designing",
            "architecture",
            "handoff",
            "A redacted handoff document for a fresh session.",
            "light",
            "57c9f1f392d7352cdc85b1e39ca49eddc70ce1dc278bd9653fb4f23dfc2560fc",
        ),
        _skill(
            "teach",
            "user",
            "designing",
            "learning",
            "learning",
            "A stateful lesson tied to the learner mission.",
            "light",
            "6d2dbe5e03084cf26fef66b535127b36cd1bcbe9478e26b0626029cd51dc2259",
        ),
        _skill(
            "writing-great-skills",
            "user",
            "reflecting",
            "code_review",
            "reference",
            "A predictable skill definition or review.",
            "light",
            "4d6ccbc3760b1bd4107c495a79872286ea69494003f3b0a719fc95b147457061",
        ),
    )
}


def _route(
    route_id: str,
    label: str,
    summary: str,
    stages: tuple[str, ...],
    branches: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> OfficialRoute:
    return OfficialRoute(route_id, label, summary, stages, branches)


OFFICIAL_ROUTES = {
    route.route_id: route
    for route in (
        _route(
            "setup",
            "Setup",
            "Configure the official skills before selecting a workflow.",
            ("setup-matt-pocock-skills", "ask-matt"),
        ),
        _route(
            "routing",
            "Workflow routing",
            "Ask Matt selects a flow without auto-running user skills.",
            ("ask-matt",),
        ),
        _route(
            "idea-to-ship",
            "Idea to ship",
            "Discovery through implementation and review.",
            ("grill-with-docs", "to-spec", "to-tickets", "implement"),
            (
                ("small-build", ("grill-with-docs", "implement")),
                (
                    "prototype-detour",
                    ("grill-with-docs", "handoff", "prototype", "handoff", "grill-with-docs"),
                ),
            ),
        ),
        _route(
            "spec-to-ship",
            "Specification to ship",
            "Continue from an explicit specification request through delivery.",
            ("to-spec", "to-tickets", "implement"),
        ),
        _route(
            "tickets-to-ship",
            "Tickets to ship",
            "Continue from explicit ticket planning through delivery.",
            ("to-tickets", "implement"),
        ),
        _route(
            "implement-to-review",
            "Implement to review",
            "Run an explicitly requested composite implementation Skill.",
            ("implement",),
        ),
        _route(
            "tdd-to-review",
            "TDD to review",
            "Run an explicitly requested test-driven change through review.",
            ("tdd", "code-review"),
        ),
        _route(
            "small-build",
            "Small build",
            "A single-session build from discovery through review.",
            ("grill-with-docs", "implement"),
        ),
        _route(
            "prototype-detour",
            "Prototype detour",
            "Bridge to throwaway code and return the answer to discovery.",
            ("grill-with-docs", "handoff", "prototype", "handoff", "grill-with-docs"),
        ),
        _route(
            "prototype",
            "Standalone prototype",
            "Answer one explicitly selected design question with throwaway code.",
            ("prototype",),
        ),
        _route(
            "bug-onramp",
            "Bug onramp",
            "Diagnose through a tight loop, repair test-first, then review.",
            ("diagnosing-bugs", "tdd", "code-review"),
        ),
        _route(
            "research-feed",
            "Research feed",
            "Primary-source research feeds project discovery.",
            ("research", "grill-with-docs", "to-spec"),
        ),
        _route(
            "merge-conflict",
            "Merge conflict",
            "Resolve by intent and review the resulting diff.",
            ("resolving-merge-conflicts", "code-review"),
        ),
        _route(
            "review",
            "Review",
            "Review a fixed diff against standards and specification.",
            ("code-review",),
        ),
        _route(
            "triage-to-ship",
            "Triage to ship",
            "Turn incoming work into an agent-ready issue and deliver it.",
            ("triage", "implement"),
        ),
        _route(
            "wayfinder-to-ship",
            "Wayfinder to ship",
            "Resolve a foggy effort into decisions before delivery.",
            ("wayfinder", "to-spec", "to-tickets", "implement"),
        ),
        _route(
            "architecture-feed",
            "Architecture feed",
            "Find a deepening opportunity and return it to discovery.",
            ("improve-codebase-architecture", "to-spec"),
        ),
        _route(
            "domain-modeling",
            "Domain modeling",
            "Sharpen project vocabulary and record the model.",
            ("domain-modeling",),
        ),
        _route(
            "codebase-design",
            "Codebase design",
            "Select a deep-module design and record its rationale.",
            ("codebase-design",),
        ),
        _route(
            "grill-me",
            "Standalone grilling",
            "Sharpen a plan without a codebase.",
            ("grill-me",),
        ),
        _route(
            "grilling",
            "Grilling primitive",
            "Resolve a decision tree one question at a time.",
            ("grilling",),
        ),
        _route("handoff", "Handoff", "Carry redacted context into a fresh session.", ("handoff",)),
        _route("teach", "Teach", "Run a stateful teaching workspace.", ("teach",)),
        _route(
            "writing-great-skills",
            "Writing great skills",
            "Write or review a predictable skill.",
            ("writing-great-skills",),
        ),
    )
}


def build_chain_map() -> dict[str, dict[str, list[str]]]:
    chain = {name: {"predecessors": [], "successors": []} for name in OFFICIAL_SKILLS}
    for route in OFFICIAL_ROUTES.values():
        for left, right in zip(route.stages, route.stages[1:], strict=False):
            if right not in chain[left]["successors"]:
                chain[left]["successors"].append(right)
            if left not in chain[right]["predecessors"]:
                chain[right]["predecessors"].append(left)
    return chain


def declared_branch_transition_step(
    *,
    parent_route_id: str,
    parent_step_index: int,
    current_stage: str,
    target_route_id: str,
    target_stage: str,
) -> int | None:
    """Return the target route index for one declared parent-to-branch handoff."""
    parent = OFFICIAL_ROUTES.get(parent_route_id)
    target = OFFICIAL_ROUTES.get(target_route_id)
    if parent is None or target is None:
        return None
    if not 0 <= parent_step_index < len(parent.stages):
        return None
    if parent.stages[parent_step_index] != current_stage:
        return None
    declared = dict(parent.branches).get(target_route_id)
    if declared is None or tuple(declared) != target.stages:
        return None
    for index, stage in enumerate(target.stages[:-1]):
        if stage == current_stage and target.stages[index + 1] == target_stage:
            return index + 1
    return None


def validate_manifest() -> None:
    known = set(OFFICIAL_SKILLS)
    routed: set[str] = set()
    for route in OFFICIAL_ROUTES.values():
        routed.update(route.stages)
        if not route.stages:
            raise ValueError(f"official route has no stages: {route.route_id}")
        unknown = set(route.stages) - known
        for _name, branch in route.branches:
            routed.update(branch)
            unknown.update(set(branch) - known)
        if unknown:
            raise ValueError(
                f"official route {route.route_id} has unknown stages: {sorted(unknown)}"
            )
    if routed != known:
        raise ValueError(f"unrouted official skills: {sorted(known - routed)}")


def validate_execution_receipt(
    stage_name: str, receipt: Any
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a bounded client attestation for a completed pinned Skill run."""
    if not isinstance(receipt, dict):
        return None, "receipt_must_be_object"
    missing = sorted(_RECEIPT_REQUIRED_FIELDS - set(receipt))
    if missing:
        return None, f"missing_fields:{','.join(missing)}"
    skill = OFFICIAL_SKILLS.get(stage_name)
    if skill is None or str(receipt.get("skill") or "") != stage_name:
        return None, "skill_mismatch"
    if str(receipt.get("upstream_revision") or "") != UPSTREAM_SKILLS_REVISION:
        return None, "upstream_revision_mismatch"
    if str(receipt.get("content_sha256") or "") != skill.content_sha256:
        return None, "content_sha256_mismatch"
    if str(receipt.get("status") or "").casefold() != "completed":
        return None, "status_not_completed"
    evidence = receipt.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return None, "evidence_must_be_nonempty_object"

    composite = COMPOSITE_SKILL_CALLS.get(stage_name)
    if composite is not None:
        invoked = evidence.get("invoked_skills")
        if not isinstance(invoked, list) or not invoked:
            return None, "composite_invoked_skills_required"
        if any(not isinstance(item, str) or not item.strip() for item in invoked):
            return None, "composite_invoked_skills_must_be_strings"
        invoked = [item.strip().casefold() for item in invoked]
        if len(invoked) != len(set(invoked)):
            return None, "composite_invoked_skills_must_be_unique"
        allowed = set(composite["required"]) | set(composite["optional"])
        if not set(invoked) <= allowed:
            return None, "composite_invoked_skills_unknown"
        if not set(composite["required"]) <= set(invoked):
            return None, "composite_required_skill_missing"
        declared_order = [*composite["optional"], *composite["required"]]
        if invoked != [item for item in declared_order if item in invoked]:
            return None, "composite_invoked_skills_order_invalid"
        evidence = {**evidence, "invoked_skills": invoked}

    def secret_field_name(key: Any) -> bool:
        raw_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
        normalized = re.sub(r"[^a-z0-9]+", "_", raw_key.casefold()).strip("_")
        return (
            normalized
            in {
                "api_key",
                "apikey",
                "authorization",
                "auth_header",
                "client_secret",
                "credential",
                "credentials",
                "password",
                "passwd",
                "private_key",
                "secret",
                "secret_access_key",
                "secret_key",
                "signing_key",
                "token",
            }
            or normalized.endswith(("_password", "_passwd", "_secret", "_token"))
            or normalized.endswith(
                (
                    "_api_key",
                    "_private_key",
                    "_secret_access_key",
                    "_secret_key",
                    "_signing_key",
                )
            )
        )

    def contains_secret(value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                if secret_field_name(key):
                    return True
                if contains_secret(nested):
                    return True
        elif isinstance(value, list):
            return any(contains_secret(item) for item in value)
        elif isinstance(value, str):
            return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)
        return False

    if contains_secret(evidence):
        return None, "evidence_contains_secret"
    try:
        encoded_evidence = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return None, "evidence_not_json_serializable"
    if len(encoded_evidence.encode("utf-8")) > 16_384:
        return None, "evidence_too_large"
    return {
        "skill": stage_name,
        "upstream_revision": UPSTREAM_SKILLS_REVISION,
        "content_sha256": skill.content_sha256,
        "status": "completed",
        "evidence": evidence,
    }, None


validate_manifest()

"""域 2: Memory Operations skills — 记忆 CRUD 的高层组合"""

import copy
import json
from collections.abc import Mapping
from typing import Any

from plastic_promise.core.constants import DEDUP_SIMILARITY_THRESHOLD
from plastic_promise.core.synthesis import synthesis_content_hash
from plastic_promise.core.synthesis_retrieval import _source_is_available
from plastic_promise.core.tool_manifest import manifest_for_tool
from plastic_promise.mcp.tools.memory import (
    _ordinary_mutation_authority,
    handle_memory_recall,
    handle_memory_store,
)
from plastic_promise.skills.engine import SkillDef, SkillResult


def _parse_atom_result(result: Any) -> dict[str, Any] | None:
    if not result or not hasattr(result[0], "text"):
        return None
    try:
        payload = json.loads(result[0].text)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _smart_remember_failure(
    *,
    action: str,
    reason: str,
    error_prefix: str,
    duplicate_id: str = "",
    payload: Mapping[str, Any] | None = None,
) -> SkillResult:
    data = dict(payload or {})
    data["action"] = action
    data["reason"] = reason
    if duplicate_id:
        data["duplicate_of"] = duplicate_id
    return SkillResult(
        skill_name="smart-remember",
        success=False,
        data=data,
        atom_results={},
        degrade_log=[f"{error_prefix}: {reason}"],
        audit_trail={},
        errors=[f"{error_prefix} failed: {reason}"],
    )


def _recalled_content_matches_canonical(recalled: str, canonical: str) -> bool:
    if recalled == canonical:
        return True
    # Public recall intentionally caps content at 500 characters.
    return len(recalled) == 500 and canonical.startswith(recalled)


def _smart_remember_mutation_authority(
    runtime_context: Mapping[str, Any] | None,
    params: Mapping[str, Any],
    duplicate: Mapping[str, Any],
    recall_project_id: str,
) -> tuple[tuple[str, str] | None, str]:
    """Bind one similar-duplicate write to server-owned runtime evidence."""
    authority, authority_reason = _ordinary_mutation_authority(
        dict(runtime_context) if isinstance(runtime_context, Mapping) else None,
        record=duplicate,
    )
    if authority is None:
        return None, authority_reason
    if (
        float(runtime_context.get("trust_score")) + 1e-9
        < manifest_for_tool("memory_update").trust_requirement
    ):
        return None, "ordinary_mutation_runtime_authorization_denied"

    runtime_project_id = str(runtime_context.get("project_id") or "").strip()
    requested_project_id = str(params.get("project_id") or "").strip()
    recalled_project_id = str(recall_project_id or "").strip()
    item_project_id = str(duplicate.get("project_id") or "").strip()
    if not recalled_project_id or not item_project_id:
        return None, "ordinary_source_authority_required"
    bound_projects = [runtime_project_id, recalled_project_id, item_project_id]
    if requested_project_id:
        bound_projects.append(requested_project_id)
    if any(project_id != runtime_project_id for project_id in bound_projects):
        return None, "ordinary_mutation_project_mismatch"
    return authority, ""


def _duplicate_authority(
    ctx: Any,
    params: Mapping[str, Any],
    duplicate: Mapping[str, Any],
    recall_project_id: str,
) -> tuple[dict[str, Any] | None, str]:
    memory_id = str(duplicate.get("id") or "").strip()
    project_id = str(duplicate.get("project_id") or "").strip()
    origin_scope = str(duplicate.get("origin_scope") or "").strip().casefold()
    recall_project_id = str(recall_project_id or "").strip()
    if not memory_id or not project_id or not origin_scope or not recall_project_id:
        return None, "ordinary_source_authority_required"
    requested_project = str(params.get("project_id") or "").strip()
    if (
        origin_scope != "project"
        or recall_project_id != project_id
        or (requested_project and requested_project != recall_project_id)
    ):
        return None, "ordinary_mutation_project_mismatch"

    getter = getattr(ctx, "get_memory_dict_for_review", None)
    if not callable(getter):
        return None, "ordinary_source_authority_required"
    try:
        canonical = getter(memory_id)
    except Exception:
        return None, "ordinary_source_authority_required"
    if not isinstance(canonical, dict):
        return None, "ordinary_source_authority_required"

    canonical_project = str(canonical.get("project_id") or "").strip()
    if not canonical_project:
        return None, "ordinary_mutation_source_project_required"
    if canonical_project != project_id:
        return None, "ordinary_mutation_project_mismatch"

    recalled_content = duplicate.get("content")
    canonical_content = canonical.get("content")
    tags = canonical.get("tags")
    metadata = canonical.get("metadata_json")
    if (
        not isinstance(recalled_content, str)
        or not isinstance(canonical_content, str)
        or not isinstance(tags, (list, tuple))
        or not all(isinstance(tag, str) for tag in tags)
        or not isinstance(metadata, dict)
        or "category" not in canonical
    ):
        return None, "ordinary_source_authority_required"
    if not _recalled_content_matches_canonical(recalled_content, canonical_content):
        return None, "ordinary_source_precondition_mismatch"
    try:
        if not _source_is_available(canonical):
            return None, "ordinary_source_precondition_mismatch"
    except Exception:
        return None, "ordinary_source_precondition_mismatch"

    source_snapshot: dict[str, Any] = {
        "tags": list(tags),
        "category": canonical.get("category"),
        "metadata_json": copy.deepcopy(metadata),
    }
    for field in ("worth_success", "worth_failure", "embedding_hash"):
        if field in canonical:
            source_snapshot[field] = copy.deepcopy(canonical[field])
    return {
        "memory_id": memory_id,
        "project_id": project_id,
        "content": canonical_content,
        "source_snapshot": source_snapshot,
    }, ""


async def _smart_remember_handler(ctx, params, atom_results):
    """Choose exactly one dedup/store action after recall has completed."""
    runtime_context = params.get("_runtime_context")
    recall_params = dict(params)
    recall_params.pop("_runtime_context", None)
    recall_params["query"] = str(params.get("content") or "")
    try:
        recall_result = await handle_memory_recall(ctx, recall_params)
    except Exception as exc:
        reason = str(exc).strip() or "memory_recall_failed"
        return _smart_remember_failure(
            action="recall_failed",
            reason=reason,
            error_prefix="memory_recall",
        )
    recall_data = _parse_atom_result(recall_result)
    if recall_data is None:
        return _smart_remember_failure(
            action="recall_failed",
            reason="memory_recall_invalid_response",
            error_prefix="memory_recall",
        )
    if recall_data.get("error"):
        return _smart_remember_failure(
            action="recall_failed",
            reason=str(recall_data["error"]),
            error_prefix="memory_recall",
            payload=recall_data,
        )
    core_results = recall_data.get("core", [])

    duplicate = None
    for item in core_results if isinstance(core_results, list) else []:
        if not isinstance(item, Mapping):
            continue
        try:
            relevance = float(item.get("relevance", 0))
        except (TypeError, ValueError):
            continue
        if relevance >= DEDUP_SIMILARITY_THRESHOLD:
            duplicate = item
            break

    if duplicate is None:
        try:
            store_params = dict(recall_params)
            store_params.pop("query", None)
            for field in (
                "actor",
                "call_id",
                "trust_score",
                "trust_tier",
                "defense_decision",
            ):
                store_params.pop(field, None)
            if isinstance(runtime_context, Mapping):
                runtime_actor = str(runtime_context.get("actor") or "").strip()
                runtime_call_id = str(runtime_context.get("call_id") or "").strip()
                if runtime_actor:
                    store_params["actor"] = runtime_actor
                if runtime_call_id:
                    store_params["call_id"] = runtime_call_id
            store_result = await handle_memory_store(ctx, store_params)
        except Exception as exc:
            reason = str(exc).strip() or "memory_store_failed"
            return _smart_remember_failure(
                action="store_failed",
                reason=reason,
                error_prefix="memory_store",
            )
        store_data = _parse_atom_result(store_result)
        if store_data is None:
            return _smart_remember_failure(
                action="store_failed",
                reason="memory_store_invalid_response",
                error_prefix="memory_store",
            )
        if store_data.get("stored") is not True:
            reason = str(store_data.get("reason") or "memory_store_failed")
            return _smart_remember_failure(
                action="store_failed",
                reason=reason,
                error_prefix="memory_store",
                payload=store_data,
            )
        memory_id = str(store_data.get("memory_id") or "").strip()
        if not memory_id:
            return _smart_remember_failure(
                action="store_failed",
                reason="memory_store_invalid_response",
                error_prefix="memory_store",
                payload=store_data,
            )
        return SkillResult(
            skill_name="smart-remember",
            success=True,
            data={
                "action": "stored",
                "memory_id": memory_id,
                "pipeline": store_data.get("pipeline", {}),
            },
            atom_results={},
            degrade_log=[],
            audit_trail={},
            errors=[],
        )

    duplicate_id = str(duplicate.get("id") or "").strip()
    new_content = str(params.get("content") or "")
    recalled_content = duplicate.get("content")
    mutation_authority: tuple[str, str] | None = None
    if not isinstance(recalled_content, str) or new_content != recalled_content:
        mutation_authority, runtime_reason = _smart_remember_mutation_authority(
            runtime_context,
            params,
            duplicate,
            str(recall_data.get("project_id") or ""),
        )
        if mutation_authority is None:
            return _smart_remember_failure(
                action="update_failed",
                reason=runtime_reason,
                error_prefix="memory_update",
                duplicate_id=duplicate_id,
            )

    authority, authority_reason = _duplicate_authority(
        ctx,
        params,
        duplicate,
        str(recall_data.get("project_id") or ""),
    )
    if authority is None:
        return _smart_remember_failure(
            action="update_failed",
            reason=authority_reason,
            error_prefix="memory_update",
            duplicate_id=duplicate_id,
        )

    if new_content == authority["content"]:
        return SkillResult(
            skill_name="smart-remember",
            success=True,
            data={
                "action": "unchanged",
                "reason": "exact_duplicate",
                "memory_id": authority["memory_id"],
                "duplicate_of": authority["memory_id"],
                "relevance": duplicate.get("relevance"),
            },
            atom_results={},
            degrade_log=[],
            audit_trail={},
            errors=[],
        )

    if new_content == recalled_content:
        return SkillResult(
            skill_name="smart-remember",
            success=True,
            data={
                "action": "unchanged",
                "reason": "exact_duplicate",
                "memory_id": authority["memory_id"],
                "duplicate_of": authority["memory_id"],
                "relevance": duplicate.get("relevance"),
            },
            atom_results={},
            degrade_log=[],
            audit_trail={},
            errors=[],
        )

    if mutation_authority is None:
        mutation_authority, runtime_reason = _smart_remember_mutation_authority(
            runtime_context,
            params,
            duplicate,
            str(recall_data.get("project_id") or ""),
        )
        if mutation_authority is None:
            return _smart_remember_failure(
                action="update_failed",
                reason=runtime_reason,
                error_prefix="memory_update",
                duplicate_id=authority["memory_id"],
            )
    actor, call_id = mutation_authority

    mutate = getattr(ctx, "mutate_ordinary_source", None)
    if not callable(mutate):
        return _smart_remember_failure(
            action="update_failed",
            reason="ordinary_source_mutation_api_required",
            error_prefix="memory_update",
            duplicate_id=authority["memory_id"],
        )
    try:
        mutation = mutate(
            authority["memory_id"],
            operation="replace_content",
            content=new_content,
            reason="smart-remember:similar_duplicate",
            actor=actor,
            call_id=call_id,
            expected_project_id=authority["project_id"],
            expected_content_hash=synthesis_content_hash(authority["content"]),
            expected_source_snapshot=authority["source_snapshot"],
            require_source_available=True,
        )
    except Exception as exc:
        reason = str(exc).strip() or "ordinary_source_mutation_failed"
        return _smart_remember_failure(
            action="update_failed",
            reason=reason,
            error_prefix="memory_update",
            duplicate_id=authority["memory_id"],
        )

    result_memory_id = str(getattr(mutation, "memory_id", "") or "").strip()
    result_operation = str(getattr(mutation, "operation", "") or "").strip()
    if result_memory_id != authority["memory_id"] or result_operation != "corrected":
        return _smart_remember_failure(
            action="update_failed",
            reason="ordinary_source_mutation_invalid_result",
            error_prefix="memory_update",
            duplicate_id=authority["memory_id"],
        )
    return SkillResult(
        skill_name="smart-remember",
        success=True,
        data={
            "action": "updated",
            "memory_id": result_memory_id,
            "duplicate_of": result_memory_id,
            "relevance": duplicate.get("relevance"),
            "committed_memory_version": int(getattr(mutation, "committed_memory_version", 0) or 0),
            "stale_dependents": list(getattr(mutation, "stale_synthesis_ids", ()) or ()),
            "ordinary_index_job_id": str(getattr(mutation, "ordinary_index_job_id", "") or ""),
            "synthesis_index_job_ids": list(getattr(mutation, "synthesis_index_job_ids", ()) or ()),
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )


# -- Skill Definition --

skill_smart_remember = SkillDef(
    name="smart-remember",
    domain="memory_operations",
    description="记忆前自动去重 + 质量门控 — 重复的记忆更新而非新增",
    tier="P0",
    atoms=[
        "principle_activate",
    ],
    degrade_map={
        "principle_activate": "skip",
    },
    handler=_smart_remember_handler,
    allowed_callers=["claude", "pi"],
)

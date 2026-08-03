#!/usr/bin/env python3
"""Validate a Plastic Promise distribution variant contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "plastic-promise-release-variant/v1"
STANDARD_VARIANT_PATH = Path("release/variants/standard.json")

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "variant",
    "distribution",
    "capabilities",
    "runtime",
    "storage",
    "deployment",
    "configuration",
    "content_policy",
    "release",
}
_SECTION_FIELDS = {
    "variant": {"id", "kind", "status"},
    "distribution": {
        "package_name",
        "source_repository",
        "release_repository",
        "visibility",
        "license",
    },
    "runtime": {
        "python_requires",
        "platforms",
        "modes",
        "local_model_runtime",
        "hosted_provider_support",
    },
    "storage": {"canonical", "derived", "bundled_runtime_state"},
    "deployment": {"default_profile", "profiles", "async_pipeline"},
    "configuration": {"secret_values", "secret_transport", "environment_variables"},
    "content_policy": {"include", "exclude"},
    "release": {"artifacts", "provenance", "required_gates"},
}
_DEPLOYMENT_PROFILE_FIELDS = {
    "topology",
    "frontend_location",
    "runtime_location",
    "canonical_state_location",
    "derived_index_location",
    "client_cache",
    "transport",
    "async_worker_location",
}
_ASYNC_PIPELINE_FIELDS = {
    "admission",
    "queue",
    "batching",
    "retry_state",
    "reconcile",
    "project_isolation",
    "client_delivery",
}
_REQUIRED_DEPLOYMENT_PROFILES = {"local-all-in-one", "split-async"}
_REQUIRED_CAPABILITIES = {
    "shared-memory",
    "context-supply",
    "audit-defense",
    "trust-governance",
    "skill-orchestration",
    "task-dispatch",
}
_REQUIRED_EXCLUSIONS = {
    "secret-values",
    "private-keys",
    "runtime-databases",
    "derived-indexes",
    "logs-and-audit-output",
    "backups",
    "deployment-environment-files",
}
_REQUIRED_GATES = {
    "variant-contract",
    "compileall",
    "pytest",
    "release-tree-audit",
    "commit-attestation",
    "tag-attestation",
}
_REQUIRED_PROVENANCE = {
    "source-head-bound",
    "release-tree-attested",
    "commit-tree-attested",
    "annotated-tag-attested",
    "atomic-push",
}
_ALLOWED_RUNTIME_MODES = {"light", "normal", "rust-normal", "full", "rust-full"}
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_VARIANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class ReleaseVariantError(ValueError):
    """Raised when a release variant fails its public contract."""


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseVariantError(f"release_variant_field_not_object:{field}")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseVariantError(f"release_variant_field_not_string:{field}")
    return value


def _require_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ReleaseVariantError(f"release_variant_field_not_list:{field}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ReleaseVariantError(f"release_variant_list_item_invalid:{field}")
    if len(set(value)) != len(value):
        raise ReleaseVariantError(f"release_variant_list_duplicate:{field}")
    return value


def _reject_unknown_fields(payload: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ReleaseVariantError(
            f"release_variant_unknown_field:{field}:{','.join(unknown)}"
        )
    missing = sorted(allowed - set(payload))
    if missing:
        raise ReleaseVariantError(
            f"release_variant_missing_field:{field}:{','.join(missing)}"
        )


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _validate_no_secret_values(payload: dict[str, Any]) -> None:
    for value in _walk_strings(payload):
        if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
            raise ReleaseVariantError("release_variant_secret_value_detected")


def load_release_variant(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseVariantError(f"release_variant_missing:{path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseVariantError(f"release_variant_invalid_json:{path}") from exc
    return _require_mapping(payload, "root")


def validate_release_variant(path: Path, *, repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseVariantError("release_variant_outside_repository") from exc

    payload = load_release_variant(resolved)
    _reject_unknown_fields(payload, _TOP_LEVEL_FIELDS, "root")
    _validate_no_secret_values(payload)

    if payload["schema_version"] != SCHEMA_VERSION:
        raise ReleaseVariantError("release_variant_schema_unsupported")

    sections: dict[str, dict[str, Any]] = {}
    for field, allowed in _SECTION_FIELDS.items():
        section = _require_mapping(payload[field], field)
        _reject_unknown_fields(section, allowed, field)
        sections[field] = section

    variant = sections["variant"]
    variant_id = _require_string(variant["id"], "variant.id")
    if _VARIANT_ID.fullmatch(variant_id) is None:
        raise ReleaseVariantError("release_variant_id_invalid")
    if variant["kind"] != "distribution":
        raise ReleaseVariantError("release_variant_kind_not_distribution")
    if variant["status"] not in {"active", "experimental", "deprecated"}:
        raise ReleaseVariantError("release_variant_status_invalid")

    distribution = sections["distribution"]
    for field in _SECTION_FIELDS["distribution"]:
        _require_string(distribution[field], f"distribution.{field}")
    if distribution["visibility"] != "public":
        raise ReleaseVariantError("release_variant_visibility_not_public")
    if distribution["package_name"] != "plastic-promise":
        raise ReleaseVariantError("release_variant_package_mismatch")
    if distribution["release_repository"].rstrip("/") != (
        "https://github.com/ALdaisuki/plastic-promise-release"
    ):
        raise ReleaseVariantError("release_variant_repository_mismatch")

    capabilities = set(_require_string_list(payload["capabilities"], "capabilities"))
    missing_capabilities = sorted(_REQUIRED_CAPABILITIES - capabilities)
    if missing_capabilities:
        raise ReleaseVariantError(
            "release_variant_capability_missing:" + ",".join(missing_capabilities)
        )

    runtime = sections["runtime"]
    _require_string(runtime["python_requires"], "runtime.python_requires")
    _require_string_list(runtime["platforms"], "runtime.platforms")
    modes = set(_require_string_list(runtime["modes"], "runtime.modes"))
    if not modes <= _ALLOWED_RUNTIME_MODES:
        raise ReleaseVariantError("release_variant_runtime_mode_invalid")
    for field in ("local_model_runtime", "hosted_provider_support"):
        if runtime[field] not in {"optional", "required", "unsupported"}:
            raise ReleaseVariantError(f"release_variant_runtime_policy_invalid:{field}")

    storage = sections["storage"]
    if storage["canonical"] != "sqlite":
        raise ReleaseVariantError("release_variant_canonical_store_invalid")
    derived = _require_string_list(storage["derived"], "storage.derived")
    if "lancedb" not in derived:
        raise ReleaseVariantError("release_variant_derived_store_missing")
    if storage["bundled_runtime_state"] is not False:
        raise ReleaseVariantError("release_variant_runtime_state_must_not_be_bundled")

    deployment = sections["deployment"]
    if deployment["default_profile"] != "split-async":
        raise ReleaseVariantError("release_variant_default_deployment_invalid")
    profiles = _require_mapping(deployment["profiles"], "deployment.profiles")
    missing_profiles = sorted(_REQUIRED_DEPLOYMENT_PROFILES - set(profiles))
    if missing_profiles:
        raise ReleaseVariantError(
            "release_variant_deployment_profile_missing:" + ",".join(missing_profiles)
        )
    for profile_id, profile_value in profiles.items():
        if _VARIANT_ID.fullmatch(profile_id) is None:
            raise ReleaseVariantError("release_variant_deployment_profile_id_invalid")
        profile = _require_mapping(profile_value, f"deployment.profiles.{profile_id}")
        _reject_unknown_fields(
            profile,
            _DEPLOYMENT_PROFILE_FIELDS,
            f"deployment.profiles.{profile_id}",
        )
        for field in _DEPLOYMENT_PROFILE_FIELDS:
            _require_string(profile[field], f"deployment.profiles.{profile_id}.{field}")

    local_profile = profiles["local-all-in-one"]
    expected_local = {
        "topology": "single-host",
        "frontend_location": "local",
        "runtime_location": "local",
        "canonical_state_location": "local-runtime",
        "derived_index_location": "local-runtime",
        "client_cache": "optional-bounded",
        "transport": "loopback-http",
        "async_worker_location": "local-runtime",
    }
    if local_profile != expected_local:
        raise ReleaseVariantError("release_variant_local_profile_invalid")

    split_profile = profiles["split-async"]
    expected_split = {
        "topology": "client-server",
        "frontend_location": "client",
        "runtime_location": "server",
        "canonical_state_location": "server-only",
        "derived_index_location": "server",
        "client_cache": "optional-bounded-no-canonical-db",
        "transport": "secure-tunnel-loopback-http",
        "async_worker_location": "server",
    }
    if split_profile != expected_split:
        raise ReleaseVariantError("release_variant_split_profile_invalid")

    async_pipeline = _require_mapping(
        deployment["async_pipeline"], "deployment.async_pipeline"
    )
    _reject_unknown_fields(
        async_pipeline,
        _ASYNC_PIPELINE_FIELDS,
        "deployment.async_pipeline",
    )
    expected_async_pipeline = {
        "admission": "canonical-enqueue-before-ack",
        "queue": "durable-outbox",
        "batching": "bounded",
        "retry_state": "persistent",
        "reconcile": "required",
        "project_isolation": "required",
        "client_delivery": "status-or-events",
    }
    if async_pipeline != expected_async_pipeline:
        raise ReleaseVariantError("release_variant_async_pipeline_invalid")

    configuration = sections["configuration"]
    if configuration["secret_values"] != "forbidden":
        raise ReleaseVariantError("release_variant_secret_policy_invalid")
    if configuration["secret_transport"] not in {
        "environment-file",
        "process-environment",
        "environment-file-or-process-environment",
    }:
        raise ReleaseVariantError("release_variant_secret_transport_invalid")
    environment_variables = _require_string_list(
        configuration["environment_variables"], "configuration.environment_variables"
    )
    if any(_ENVIRONMENT_NAME.fullmatch(name) is None for name in environment_variables):
        raise ReleaseVariantError("release_variant_environment_name_invalid")

    content_policy = sections["content_policy"]
    _require_string_list(content_policy["include"], "content_policy.include")
    exclusions = set(
        _require_string_list(content_policy["exclude"], "content_policy.exclude")
    )
    missing_exclusions = sorted(_REQUIRED_EXCLUSIONS - exclusions)
    if missing_exclusions:
        raise ReleaseVariantError(
            "release_variant_exclusion_missing:" + ",".join(missing_exclusions)
        )

    release = sections["release"]
    artifacts = set(_require_string_list(release["artifacts"], "release.artifacts"))
    if not {"wheel", "sdist", "annotated-source-tag"} <= artifacts:
        raise ReleaseVariantError("release_variant_artifact_missing")
    provenance = set(_require_string_list(release["provenance"], "release.provenance"))
    if not provenance >= _REQUIRED_PROVENANCE:
        raise ReleaseVariantError("release_variant_provenance_incomplete")
    gates = set(_require_string_list(release["required_gates"], "release.required_gates"))
    if not gates >= _REQUIRED_GATES:
        raise ReleaseVariantError("release_variant_gate_incomplete")

    return payload


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=str(STANDARD_VARIANT_PATH))
    parser.add_argument("--repo-root", default=".")
    return parser


def main() -> int:
    args = build_argparser().parse_args()
    try:
        payload = validate_release_variant(Path(args.path), repo_root=Path(args.repo_root))
    except ReleaseVariantError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "schema_version": payload["schema_version"],
                "variant_id": payload["variant"]["id"],
                "kind": payload["variant"]["kind"],
                "status": "valid",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

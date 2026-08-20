#!/usr/bin/env python3
"""Resolve pinned, secret-free Docker build arguments from a prepared plan."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=_SOURCE_ROOT)
    parser.add_argument("--profile-id", default="split-accelerated")
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--platform", action="append", required=True)
    parser.add_argument("--compute-variant", action="append")
    parser.add_argument("--model-catalog-reference")
    parser.add_argument("--model-catalog-digest")
    parser.add_argument("--artifact-role", required=True)
    parser.add_argument("--artifact-platform", required=True)
    parser.add_argument("--artifact-variant", default="standard")
    parser.add_argument("--verify-head", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _verify_head(repository_root: Path, source_revision: str) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != source_revision:
        raise SystemExit("container_artifact_source_revision_head_mismatch")
    dirty = subprocess.run(
        ["git", "diff", "--quiet"],
        cwd=repository_root,
        check=False,
    )
    if dirty.returncode != 0:
        raise SystemExit("container_artifact_source_tree_dirty")


def main() -> int:
    from plastic_promise.deployment import (
        COMPUTE_VARIANT_CPU,
        COMPUTE_VARIANT_CUDA,
        ArtifactRequest,
        ContainerArtifactCompiler,
    )

    args = _arguments()
    repository_root = args.repository_root.resolve()
    if args.verify_head:
        _verify_head(repository_root, args.source_revision)
    compute_variants = tuple(args.compute_variant or ())
    if args.profile_id == "split-accelerated" and not compute_variants:
        compute_variants = (COMPUTE_VARIANT_CPU, COMPUTE_VARIANT_CUDA)
    request = ArtifactRequest(
        profile_id=args.profile_id,
        source_revision=args.source_revision,
        package_version=args.package_version,
        platforms=tuple(args.platform),
        compute_variants=compute_variants,
        model_catalog_reference=args.model_catalog_reference,
        model_catalog_digest=args.model_catalog_digest,
    )
    plan = ContainerArtifactCompiler(repository_root=repository_root).prepare(request)
    artifact = plan.artifact_for(args.artifact_role, args.artifact_platform, args.artifact_variant)
    base_image_digest = artifact.base_image_reference.rsplit("@", maxsplit=1)[1]
    build_args = {
        "BASE_IMAGE": artifact.base_image_reference,
        "BASE_IMAGE_DIGEST": base_image_digest,
        "SOURCE_REVISION": request.source_revision,
        "PACKAGE_VERSION": request.package_version,
        "BUILD_POLICY_DIGEST": plan.policy_digest,
        "RECIPE_POLICY_DIGEST": plan.recipe_policy_digest,
    }
    if artifact.role == "pp-compute-node":
        build_args["COMPUTE_VARIANT"] = artifact.variant
    payload = {
        "schema_version": "plastic-promise-container-build-identity/v1",
        "artifact_id": artifact.artifact_id,
        "role": artifact.role,
        "platform": artifact.platform,
        "variant": artifact.variant,
        "base_image_reference": artifact.base_image_reference,
        "base_image_digest": base_image_digest,
        "policy_digest": plan.policy_digest,
        "recipe_policy_digest": plan.recipe_policy_digest,
        "expected_oci_labels": plan.expected_oci_labels(artifact.artifact_id),
        "build_args": build_args,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - command-line entry point
    raise SystemExit(main())

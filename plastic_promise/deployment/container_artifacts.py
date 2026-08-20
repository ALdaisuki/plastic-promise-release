"""Build-time contracts for independently inspectable container artifacts.

``ContainerArtifactCompiler`` is intentionally a build-time deep module.  It
turns a small, secret-free request into the complete role/platform/variant
matrix and validates the evidence returned by a build adapter.  It never
starts Docker, opens a database, talks to a registry, or gains deployment
authority.

The production Buildx/CI implementation and a local test fake meet at the
same narrow seam.  That keeps OCI implementation details, GPU availability,
registry transports, and scanner formats out of installers and endpoint
containers.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

import yaml

from plastic_promise.endpoint_roles import compute_package_manifest, endpoint_role_contract

from .catalog import profile_by_id
from .endpoint_contract import (
    ENDPOINT_CONTRACT_SCHEMA_VERSION,
    PP_COMPUTE_NODE,
    PP_LOCAL_EDGE,
    PP_SERVER_BACKEND,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

CONTAINER_ARTIFACT_SCHEMA_VERSION = "plastic-promise-container-artifacts/v1"
CONTAINER_ARTIFACT_BUNDLE_SCHEMA_VERSION = "plastic-promise-container-bundle/v1"
CONTAINER_ARTIFACT_EVIDENCE_SCHEMA_VERSION = "plastic-promise-container-evidence/v2"
CONTAINER_ARTIFACT_RECIPE_POLICY_SCHEMA_VERSION = "plastic-promise-container-recipe-policy/v1"
OCI_BASE_IMAGE_CATALOG_SCHEMA_VERSION = "plastic-promise-oci-base-images/v1"

COMPUTE_VARIANT_CPU = "cpu"
COMPUTE_VARIANT_CUDA = "cuda"
STANDARD_VARIANT = "standard"

_COMPUTE_VARIANTS = frozenset({COMPUTE_VARIANT_CPU, COMPUTE_VARIANT_CUDA})
_SUPPORTED_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})
_CUDA_PLATFORMS = frozenset({"linux/amd64"})
_ROLES = (PP_LOCAL_EDGE, PP_SERVER_BACKEND, PP_COMPUTE_NODE)
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_:-]{1,127}$")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}$")
_PINNED_REVISION = re.compile(r"(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})$")
_PACKAGE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_CATALOG_REFERENCE = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")
_OCI_REFERENCE = re.compile(r"^oci@sha256:[0-9a-f]{64}$")
_PINNED_IMAGE_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,253}@sha256:[0-9a-f]{64}$")
_RECIPE_PATH = re.compile(r"^\.?[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_ALLOWED_DOCKERFILE_OPCODES = frozenset(
    {
        "ARG",
        "COPY",
        "ENTRYPOINT",
        "ENV",
        "EXPOSE",
        "FROM",
        "HEALTHCHECK",
        "LABEL",
        "RUN",
        "USER",
        "WORKDIR",
    }
)
_ALLOWED_COMPUTE_RUN_MOUNT = "--mount=type=cache,target=/root/.cache/pip,sharing=locked"

_BASE_IMAGE_CATALOG_PATH = "deploy/oci-base-images.json"
_SERVER_ROLE_CONTRACT = endpoint_role_contract(PP_SERVER_BACKEND)
_COLLABORATION_FOUNDATION = tuple(
    zip(
        _SERVER_ROLE_CONTRACT.collaboration_modules,
        _SERVER_ROLE_CONTRACT.collaboration_source_paths,
        strict=True,
    )
)
_COLLABORATION_MODULES = tuple(module for module, _ in _COLLABORATION_FOUNDATION)
_COLLABORATION_SOURCE_PATHS = tuple(path for _, path in _COLLABORATION_FOUNDATION)
_COLLABORATION_WRITER_SURFACES = frozenset({"absent", "source-only-unwired"})
_COLLABORATION_EVENT_WRITER_AUTHORITY = "collaboration-event-writer"
_SERVER_RECIPE_AUTHORITY_LABEL = _SERVER_ROLE_CONTRACT.artifact_authority_label
_EDGE_RECIPE_AUTHORITY_LABEL = endpoint_role_contract(PP_LOCAL_EDGE).artifact_authority_label
_COMPUTE_RECIPE_AUTHORITY_LABEL = endpoint_role_contract(PP_COMPUTE_NODE).artifact_authority_label
_COMPUTE_PACKAGE_MANIFEST = compute_package_manifest()
_COMPUTE_CAPABILITY_CONTRACTS = _COMPUTE_PACKAGE_MANIFEST.capability_contracts
_COMPUTE_CAPABILITY_LABEL = _COMPUTE_PACKAGE_MANIFEST.capability_label
_SERVER_SOURCE_EXCLUSIONS = _SERVER_ROLE_CONTRACT.source_exclusions
_SERVER_SOURCE_EXCLUSION_LABEL = ",".join(_SERVER_SOURCE_EXCLUSIONS)
_RECIPE_SOURCE_PATHS = (
    ".dockerignore",
    _BASE_IMAGE_CATALOG_PATH,
    "deploy/local-edge/Dockerfile",
    "deploy/local-edge/compose.yaml",
    "deploy/local-edge/entrypoint.sh",
    "deploy/local-edge/nginx.conf",
    "deploy/server/Dockerfile",
    "deploy/server/compose.yaml",
    "deploy/local-inference-node/Dockerfile",
    "deploy/local-inference-node/compose.cpu.yaml",
    "deploy/local-inference-node/compose.cuda.yaml",
    "deploy/local-inference-node/compose.yaml",
    *_COLLABORATION_SOURCE_PATHS,
)
_REQUIRED_DOCKERIGNORE_PATTERNS = frozenset(
    {
        "*.db",
        "*.sqlite",
        "*.key",
        ".env",
        "state",
        "runtime",
        "logs",
        "data",
        "lancedb",
        "lancedb/**",
        "*.lance",
        "models",
        ".ollama",
        ".cache",
        "*.safetensors",
        "*.gguf",
        "*.onnx",
        "dist",
        "*.oci.tar",
    }
)

_IMAGE_LAYER_EXCLUSIONS = (
    "canonical-sqlite",
    "lancedb-derived-index",
    "model-weights",
    "credentials",
    "host-docker-socket",
    "runtime-state",
    "logs",
    "build-cache",
)


class ContainerArtifactError(ValueError):
    """A stable, non-secret error from the container-artifact interface."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("container_artifact_error_code_invalid")
        self.code = code
        super().__init__(code)

    def public_json(self) -> dict[str, str]:
        """Return the safe projection suitable for CI or an operator UI."""

        return {"code": self.code}


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(payload: Mapping[str, object]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


def _bytes_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _require_sha256(value: str, code: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ContainerArtifactError(code)
    return value


def _unique(values: tuple[str, ...], code: str) -> tuple[str, ...]:
    if not values or len(values) != len(set(values)):
        raise ContainerArtifactError(code)
    return values


def _recipe_text(value: str) -> str:
    """Return a deliberately small comment-free view of a text recipe.

    Compose files are parsed separately with the strict YAML loader below.
    This helper is only for the closed repository-owned Dockerfile, entrypoint,
    nginx, and ignore-file checks; it never executes content. Removing comments
    prevents a text-level policy requirement from being satisfied by prose.
    """

    lines = []
    for raw_line in value.splitlines():
        if raw_line.lstrip().startswith("#"):
            continue
        line = re.sub(r"\s+#.*$", "", raw_line).rstrip()
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _recipe_instructions(value: str) -> tuple[str, ...]:
    """Collapse Dockerfile continuations without interpreting shell content."""

    instructions: list[str] = []
    pending = ""
    for line in _recipe_text(value).splitlines():
        fragment = line.strip()
        pending = f"{pending} {fragment}".strip() if pending else fragment
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        if "<<" in pending:
            raise ContainerArtifactError("container_recipe_heredoc_forbidden")
        instructions.append(pending)
        pending = ""
    if pending:
        if "<<" in pending:
            raise ContainerArtifactError("container_recipe_heredoc_forbidden")
        instructions.append(pending)
    return tuple(instructions)


class _DuplicateComposeKeyError(ValueError):
    """Raised while loading a Compose mapping with a shadowing key."""


class _StrictComposeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys at every mapping depth."""


def _construct_strict_compose_mapping(
    loader: _StrictComposeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[str, object]:
    """Construct one Compose mapping without YAML's last-key-wins behavior."""

    loader.flatten_mapping(node)
    mapping: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise _DuplicateComposeKeyError("compose_mapping_key_invalid")
        if key in mapping:
            raise _DuplicateComposeKeyError("compose_mapping_key_duplicate")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictComposeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_strict_compose_mapping,
)


@dataclass(frozen=True)
class ArtifactCollaborationSurface:
    """Closed source-packaging surface for one endpoint artifact role.

    The value describes source presence only.  ``source-only-unwired`` means
    the collaboration event-log foundation is packaged for later server-owned
    composition; it does not claim configured persistence, a listener, or
    runtime activation.
    """

    modules: tuple[str, ...]
    writer_surface: str
    package_path: ClassVar[str] = "plastic_promise/collaboration"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.modules, tuple)
            or len(self.modules) != len(set(self.modules))
            or any(module not in _COLLABORATION_MODULES for module in self.modules)
        ):
            raise ContainerArtifactError("container_artifact_collaboration_modules_invalid")
        if self.writer_surface not in _COLLABORATION_WRITER_SURFACES:
            raise ContainerArtifactError("container_artifact_collaboration_writer_surface_invalid")
        if (
            self.writer_surface == "absent"
            and self.modules
            or self.writer_surface == "source-only-unwired"
            and self.modules != _COLLABORATION_MODULES
        ):
            raise ContainerArtifactError("container_artifact_collaboration_surface_invalid")

    def _payload(self) -> dict[str, object]:
        return {
            "modules": list(self.modules),
            "writer_surface": self.writer_surface,
        }

    @property
    def digest(self) -> str:
        """Return the canonical identity of this closed collaboration surface."""

        return _sha256(self._payload())

    @property
    def source_paths(self) -> tuple[str, ...]:
        """Return the canonical source paths represented by ``modules``."""

        selected_modules = frozenset(self.modules)
        return tuple(
            path for module, path in _COLLABORATION_FOUNDATION if module in selected_modules
        )

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["digest"] = self.digest
        return payload


def _collaboration_surface_for(role: str) -> ArtifactCollaborationSurface:
    if role == PP_SERVER_BACKEND:
        modules = _COLLABORATION_MODULES
        writer_surface = "source-only-unwired"
    elif role in {PP_LOCAL_EDGE, PP_COMPUTE_NODE}:
        modules = ()
        writer_surface = "absent"
    else:
        raise ContainerArtifactError("container_artifact_role_invalid")
    return ArtifactCollaborationSurface(
        modules=modules,
        writer_surface=writer_surface,
    )


@dataclass(frozen=True)
class BaseImageIdentity:
    """One immutable base-image choice from the versioned recipe catalog."""

    role: str
    variant: str
    reference: str
    platforms: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ContainerArtifactError("container_recipe_base_image_role_invalid")
        if self.role == PP_COMPUTE_NODE:
            if self.variant not in _COMPUTE_VARIANTS:
                raise ContainerArtifactError("container_recipe_base_image_variant_invalid")
        elif self.variant != STANDARD_VARIANT:
            raise ContainerArtifactError("container_recipe_base_image_variant_invalid")
        if _PINNED_IMAGE_REFERENCE.fullmatch(self.reference) is None:
            raise ContainerArtifactError("container_recipe_base_image_reference_unpinned")
        _unique(self.platforms, "container_recipe_base_image_platforms_invalid")
        if any(platform not in _SUPPORTED_PLATFORMS for platform in self.platforms):
            raise ContainerArtifactError("container_recipe_base_image_platform_unsupported")
        if self.variant == COMPUTE_VARIANT_CUDA and tuple(self.platforms) != ("linux/amd64",):
            raise ContainerArtifactError("container_recipe_base_image_cuda_platform_invalid")

    @property
    def digest(self) -> str:
        """Return the immutable OCI index digest from ``reference``."""

        return self.reference.rsplit("@", maxsplit=1)[1]

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "variant": self.variant,
            "reference": self.reference,
            "platforms": list(self.platforms),
        }


@dataclass(frozen=True)
class RecipeSourceDigest:
    """A byte-level digest for one source recipe included in policy evidence."""

    path: str
    digest: str

    def __post_init__(self) -> None:
        if (
            _RECIPE_PATH.fullmatch(self.path) is None
            or self.path.startswith("/")
            or ".." in self.path.split("/")
        ):
            raise ContainerArtifactError("container_recipe_source_path_invalid")
        _require_sha256(self.digest, "container_recipe_source_digest_invalid")

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "digest": self.digest}


@dataclass(frozen=True)
class RecipePolicyReceipt:
    """A source-only, deterministic result of static recipe policy inspection.

    The receipt is deliberately not a statement that Docker built, loaded, or
    ran an image.  It gives every build plan an immutable reference to the
    exact Dockerfiles, Compose templates, ignores, and base-image catalog that
    were inspected before build execution.
    """

    base_images: tuple[BaseImageIdentity, ...]
    source_digests: tuple[RecipeSourceDigest, ...]
    recipe_policy_digest: str
    schema_version: str = CONTAINER_ARTIFACT_RECIPE_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONTAINER_ARTIFACT_RECIPE_POLICY_SCHEMA_VERSION:
            raise ContainerArtifactError("container_recipe_policy_schema_unsupported")
        expected_targets = {
            (PP_LOCAL_EDGE, STANDARD_VARIANT): ("linux/amd64", "linux/arm64"),
            (PP_SERVER_BACKEND, STANDARD_VARIANT): ("linux/amd64", "linux/arm64"),
            (PP_COMPUTE_NODE, COMPUTE_VARIANT_CPU): ("linux/amd64", "linux/arm64"),
            (PP_COMPUTE_NODE, COMPUTE_VARIANT_CUDA): ("linux/amd64",),
        }
        actual_targets = {(item.role, item.variant): item.platforms for item in self.base_images}
        if actual_targets != expected_targets:
            raise ContainerArtifactError("container_recipe_base_image_catalog_incomplete")
        if len(actual_targets) != len(self.base_images):
            raise ContainerArtifactError("container_recipe_base_image_catalog_duplicate")
        if tuple(item.path for item in self.source_digests) != _RECIPE_SOURCE_PATHS:
            raise ContainerArtifactError("container_recipe_source_matrix_invalid")
        _require_sha256(self.recipe_policy_digest, "container_recipe_policy_digest_invalid")
        if self.recipe_policy_digest != _sha256(self._payload()):
            raise ContainerArtifactError("container_recipe_policy_digest_mismatch")

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "base_images": [item.to_dict() for item in self.base_images],
            "source_digests": [item.to_dict() for item in self.source_digests],
        }

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["recipe_policy_digest"] = self.recipe_policy_digest
        return payload

    def base_image_for(self, role: str, variant: str, platform: str) -> BaseImageIdentity:
        """Resolve one already-inspected immutable base image for an artifact."""

        for item in self.base_images:
            if item.role == role and item.variant == variant and platform in item.platforms:
                return item
        raise ContainerArtifactError("container_recipe_base_image_not_available")


class StaticRecipePolicyValidator:
    """Validate the closed source recipe set without invoking Docker or Compose.

    This is intentionally a conservative validator for project-owned recipes,
    not a general-purpose YAML/Dockerfile interpreter.  It validates policy
    critical settings and hashes the complete source recipe set so a later
    build plan and attestation receipt cannot silently refer to a different
    recipe revision.
    """

    def __init__(self, repository_root: Path | None = None) -> None:
        self._repository_root = repository_root or Path(__file__).resolve().parents[2]

    def validate(self) -> RecipePolicyReceipt:
        """Return a receipt or a sanitized first-failure policy code."""

        files = {path: self._read_source(path) for path in _RECIPE_SOURCE_PATHS}
        base_images = self._load_base_images(files[_BASE_IMAGE_CATALOG_PATH])
        self._validate_dockerignore(files[".dockerignore"])
        self._validate_dockerfile(
            files["deploy/local-edge/Dockerfile"],
            role=PP_LOCAL_EDGE,
            variant=STANDARD_VARIANT,
            entrypoint="plastic-promise-local-edge",
            final_user="101",
            allowed_copies=(
                "COPY deploy/local-edge/nginx.conf /etc/nginx/conf.d/default.conf",
                "COPY deploy/local-edge/entrypoint.sh /usr/local/bin/plastic-promise-local-edge",
                "COPY plastic_promise/mcp/dashboard_v2/static/ /usr/share/nginx/html/",
            ),
        )
        self._validate_dockerfile(
            files["deploy/server/Dockerfile"],
            role=PP_SERVER_BACKEND,
            variant=STANDARD_VARIANT,
            entrypoint="plastic-promise-canonical-runtime",
            final_user="ppruntime",
            allowed_copies=(
                "COPY pyproject.toml README.md LICENSE /source/",
                "COPY plastic_promise /source/plastic_promise",
                "COPY --from=server-package /role-package /app",
            ),
            server_source_stage=True,
        )
        self._validate_dockerfile(
            files["deploy/local-inference-node/Dockerfile"],
            role=PP_COMPUTE_NODE,
            variant="${COMPUTE_VARIANT}",
            entrypoint="plastic-promise-local-inference-node",
            final_user="ppnode",
            allowed_copies=(
                "COPY pyproject.toml README.md LICENSE /source/",
                "COPY plastic_promise /source/plastic_promise",
                "COPY --from=compute-package /role-package /app",
            ),
            compute_source_stage=True,
        )
        self._validate_edge_recipe(
            files["deploy/local-edge/compose.yaml"],
            files["deploy/local-edge/nginx.conf"],
            files["deploy/local-edge/entrypoint.sh"],
        )
        self._validate_server_recipe(files["deploy/server/compose.yaml"])
        self._validate_compute_recipe(
            files["deploy/local-inference-node/compose.cpu.yaml"],
            variant=COMPUTE_VARIANT_CPU,
        )
        self._validate_compute_recipe(
            files["deploy/local-inference-node/compose.cuda.yaml"],
            variant=COMPUTE_VARIANT_CUDA,
        )
        self._validate_compute_recipe(
            files["deploy/local-inference-node/compose.yaml"],
            variant=COMPUTE_VARIANT_CUDA,
        )
        source_digests = tuple(
            RecipeSourceDigest(path=path, digest=_bytes_digest(files[path].encode("utf-8")))
            for path in _RECIPE_SOURCE_PATHS
        )
        payload = {
            "schema_version": CONTAINER_ARTIFACT_RECIPE_POLICY_SCHEMA_VERSION,
            "base_images": [item.to_dict() for item in base_images],
            "source_digests": [item.to_dict() for item in source_digests],
        }
        return RecipePolicyReceipt(
            base_images=base_images,
            source_digests=source_digests,
            recipe_policy_digest=_sha256(payload),
        )

    def _read_source(self, relative_path: str) -> str:
        path = self._repository_root / relative_path
        if path.is_symlink() or not path.is_file():
            raise ContainerArtifactError("container_recipe_source_missing")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ContainerArtifactError("container_recipe_source_unreadable") from exc

    @staticmethod
    def _load_base_images(value: str) -> tuple[BaseImageIdentity, ...]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ContainerArtifactError("container_recipe_base_image_catalog_invalid") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != OCI_BASE_IMAGE_CATALOG_SCHEMA_VERSION
        ):
            raise ContainerArtifactError("container_recipe_base_image_catalog_invalid")
        raw_images = payload.get("images")
        if not isinstance(raw_images, list):
            raise ContainerArtifactError("container_recipe_base_image_catalog_invalid")
        images: list[BaseImageIdentity] = []
        for raw in raw_images:
            if not isinstance(raw, dict):
                raise ContainerArtifactError("container_recipe_base_image_catalog_invalid")
            role = raw.get("role")
            variant = raw.get("variant")
            reference = raw.get("reference")
            platforms = raw.get("platforms")
            if (
                not isinstance(role, str)
                or not isinstance(variant, str)
                or not isinstance(reference, str)
                or not isinstance(platforms, list)
                or not all(isinstance(platform, str) for platform in platforms)
            ):
                raise ContainerArtifactError("container_recipe_base_image_catalog_invalid")
            images.append(
                BaseImageIdentity(
                    role=role,
                    variant=variant,
                    reference=reference,
                    platforms=tuple(platforms),
                )
            )
        return tuple(images)

    @staticmethod
    def _validate_dockerignore(value: str) -> None:
        patterns = {
            line.strip()
            for line in _recipe_text(value).splitlines()
            if line.strip() and not line.lstrip().startswith("!")
        }
        if not _REQUIRED_DOCKERIGNORE_PATTERNS.issubset(patterns):
            raise ContainerArtifactError("container_recipe_ignore_pattern_missing")

    @staticmethod
    def _validate_dockerfile(
        value: str,
        *,
        role: str,
        variant: str,
        entrypoint: str,
        final_user: str,
        allowed_copies: tuple[str, ...],
        compute_source_stage: bool = False,
        server_source_stage: bool = False,
    ) -> None:
        instructions = _recipe_instructions(value)
        parsed_instructions: list[tuple[str, str, str]] = []
        for instruction in instructions:
            opcode, separator, argument = instruction.partition(" ")
            if not separator:
                parsed_instructions.append((opcode.upper(), "", instruction))
                continue
            parsed_instructions.append((opcode.upper(), argument, instruction))

        if any(opcode not in _ALLOWED_DOCKERFILE_OPCODES for opcode, _, _ in parsed_instructions):
            raise ContainerArtifactError("container_recipe_dockerfile_opcode_forbidden")

        from_positions = [
            index for index, (opcode, _, _) in enumerate(parsed_instructions) if opcode == "FROM"
        ]
        from_arguments = [parsed_instructions[index][1] for index in from_positions]
        first_is_base_image_arg = bool(parsed_instructions) and (
            parsed_instructions[0][0] == "ARG"
            and parsed_instructions[0][1].partition("=")[0] == "BASE_IMAGE"
        )
        if compute_source_stage or server_source_stage:
            if (
                (compute_source_stage and role != PP_COMPUTE_NODE)
                or (server_source_stage and role != PP_SERVER_BACKEND)
                or not first_is_base_image_arg
                or from_arguments
                != [
                    (
                        "${BASE_IMAGE} AS compute-package"
                        if compute_source_stage
                        else "${BASE_IMAGE} AS server-package"
                    ),
                    "${BASE_IMAGE}",
                ]
                or from_positions[0] != 1
            ):
                code = (
                    "container_recipe_compute_source_stage_invalid"
                    if compute_source_stage
                    else "container_recipe_server_source_stage_invalid"
                )
                raise ContainerArtifactError(code)
            source_stage = parsed_instructions[from_positions[0] + 1 : from_positions[1]]
            final_stage = parsed_instructions[from_positions[1] + 1 :]
            source_non_run = tuple(
                instruction for opcode, _, instruction in source_stage if opcode != "RUN"
            )
            expected_source_non_run = (
                "ARG PACKAGE_VERSION",
                "WORKDIR /source",
                "COPY pyproject.toml README.md LICENSE /source/",
                "COPY plastic_promise /source/plastic_promise",
            )
            if source_non_run != expected_source_non_run:
                raise ContainerArtifactError("container_recipe_compute_source_stage_invalid")
            source_runs = tuple(
                instruction for opcode, _, instruction in source_stage if opcode == "RUN"
            )
            compiler_command = (
                "python3 -m plastic_promise.role_package"
                if compute_source_stage
                else "python -m plastic_promise.role_package"
            )
            if (
                len(source_runs) != 1
                or compiler_command not in source_runs[0]
                or f"--role {role}" not in source_runs[0]
                or "--source-root /source" not in source_runs[0]
                or "--output-root /role-package" not in source_runs[0]
                or '--version "$PACKAGE_VERSION"' not in source_runs[0]
                or "rm -rf /source/plastic_promise" in source_runs[0]
            ):
                raise ContainerArtifactError(
                    "container_recipe_compute_role_package_required"
                    if compute_source_stage
                    else "container_recipe_server_role_package_required"
                )
            final_copies = tuple(
                instruction for opcode, _, instruction in final_stage if opcode == "COPY"
            )
            if final_copies != allowed_copies[2:] or any(
                instruction
                in {
                    "COPY plastic_promise /app/plastic_promise",
                    "COPY . /app",
                    "COPY . /app/",
                }
                for instruction in final_copies
            ):
                raise ContainerArtifactError(
                    "container_recipe_compute_final_copy_invalid"
                    if compute_source_stage
                    else "container_recipe_server_final_copy_invalid"
                )
            contract_instructions = final_stage
        else:
            if compute_source_stage or server_source_stage or role == PP_COMPUTE_NODE:
                raise ContainerArtifactError("container_recipe_compute_source_stage_invalid")
            if (
                not parsed_instructions
                or parsed_instructions[0] != ("ARG", "BASE_IMAGE", "ARG BASE_IMAGE")
                or from_arguments != ["${BASE_IMAGE}"]
            ):
                raise ContainerArtifactError("container_recipe_base_image_arg_required")
            contract_instructions = parsed_instructions

        required_identity_arguments = (
            "BASE_IMAGE",
            "BASE_IMAGE_DIGEST",
            "SOURCE_REVISION",
            "PACKAGE_VERSION",
            "BUILD_POLICY_DIGEST",
            "RECIPE_POLICY_DIGEST",
        )
        arguments: list[tuple[str, str | None]] = []
        identity_instructions = (
            parsed_instructions
            if compute_source_stage or server_source_stage
            else contract_instructions
        )
        for opcode, argument, _ in identity_instructions:
            if opcode != "ARG":
                continue
            name, separator, default = argument.partition("=")
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None:
                raise ContainerArtifactError("container_recipe_identity_arg_required")
            arguments.append((name, default if separator else None))
        for name in required_identity_arguments:
            if any(
                argument_name == name and default is not None
                for argument_name, default in arguments
            ):
                raise ContainerArtifactError("container_recipe_identity_default_forbidden")
        for name in required_identity_arguments:
            if name not in {argument_name for argument_name, _ in arguments}:
                raise ContainerArtifactError("container_recipe_identity_arg_required")
        users = [
            argument
            for opcode, argument, _ in contract_instructions
            if opcode == "USER" and argument and len(argument.split()) == 1
        ]
        if not users or users[-1] in {"root", "0"} or users[-1] != final_user:
            raise ContainerArtifactError("container_recipe_final_user_invalid")
        if any(opcode == "ADD" for opcode, _, _ in parsed_instructions):
            raise ContainerArtifactError("container_recipe_forbidden_add")
        copies = tuple(
            instruction for opcode, _, instruction in parsed_instructions if opcode == "COPY"
        )
        if copies != allowed_copies or any(
            re.match(r"COPY(?:\s+--\S+)*\s+\.\s", item) for item in copies
        ):
            code = "container_recipe_forbidden_copy"
            if compute_source_stage:
                code = "container_recipe_compute_final_copy_invalid"
            elif server_source_stage:
                code = "container_recipe_server_final_copy_invalid"
            raise ContainerArtifactError(code)
        entrypoints = [
            argument for opcode, argument, _ in contract_instructions if opcode == "ENTRYPOINT"
        ]
        if f'["{entrypoint}"]' not in entrypoints:
            raise ContainerArtifactError("container_recipe_entrypoint_mismatch")
        labels: dict[str, str] = {}
        for opcode, argument, _ in contract_instructions:
            if opcode != "LABEL":
                continue
            try:
                assignments = shlex.split(argument, posix=True)
            except ValueError as exc:
                raise ContainerArtifactError("container_recipe_label_or_identity_mismatch") from exc
            if not assignments:
                raise ContainerArtifactError("container_recipe_label_or_identity_mismatch")
            for assignment in assignments:
                name, separator, label_value = assignment.partition("=")
                if not separator or not name or name in labels:
                    raise ContainerArtifactError("container_recipe_label_or_identity_mismatch")
                labels[name] = label_value
        required_labels = {
            "org.plastic-promise.endpoint.role": role,
            "org.plastic-promise.endpoint.variant": variant,
            "org.plastic-promise.endpoint.contract": ENDPOINT_CONTRACT_SCHEMA_VERSION,
            "org.opencontainers.image.revision": "${SOURCE_REVISION}",
            "org.opencontainers.image.version": "${PACKAGE_VERSION}",
            "org.opencontainers.image.base.name": "${BASE_IMAGE}",
            "org.opencontainers.image.base.digest": "${BASE_IMAGE_DIGEST}",
            "org.plastic-promise.build.policy-digest": "${BUILD_POLICY_DIGEST}",
            "org.plastic-promise.build.recipe-policy-digest": "${RECIPE_POLICY_DIGEST}",
        }
        if any(labels.get(name) != label_value for name, label_value in required_labels.items()):
            raise ContainerArtifactError("container_recipe_label_or_identity_mismatch")
        if (
            role == PP_SERVER_BACKEND
            and labels.get("org.plastic-promise.authority") != _SERVER_RECIPE_AUTHORITY_LABEL
        ):
            raise ContainerArtifactError("container_recipe_label_or_identity_mismatch")
        if (
            role == PP_LOCAL_EDGE
            and labels.get("org.plastic-promise.authority") != _EDGE_RECIPE_AUTHORITY_LABEL
        ):
            raise ContainerArtifactError("container_recipe_label_or_identity_mismatch")
        if (
            role == PP_COMPUTE_NODE
            and labels.get("org.plastic-promise.authority") != _COMPUTE_RECIPE_AUTHORITY_LABEL
        ):
            raise ContainerArtifactError("container_recipe_label_or_identity_mismatch")
        if (
            role == PP_SERVER_BACKEND
            and labels.get("org.plastic-promise.server.source-exclusions")
            != _SERVER_SOURCE_EXCLUSION_LABEL
        ):
            raise ContainerArtifactError("container_recipe_label_or_identity_mismatch")
        if role == PP_COMPUTE_NODE and (
            labels.get("org.plastic-promise.compute.capabilities") != _COMPUTE_CAPABILITY_LABEL
            or labels.get("org.plastic-promise.compute.package-manifest")
            != _COMPUTE_PACKAGE_MANIFEST.schema_version
        ):
            raise ContainerArtifactError("container_recipe_label_or_identity_mismatch")
        run_content = "\n".join(
            argument for opcode, argument, _ in contract_instructions if opcode == "RUN"
        )
        required_run_fragments = (
            'test "$SOURCE_REVISION" != "unknown"',
            'test "$PACKAGE_VERSION" != "unknown"',
            'test "$BASE_IMAGE_DIGEST" = "${BASE_IMAGE##*@}"',
        )
        if any(fragment not in run_content for fragment in required_run_fragments):
            raise ContainerArtifactError("container_recipe_label_or_identity_mismatch")
        if role == PP_SERVER_BACKEND:
            server_source_copy = (
                "COPY --from=server-package /role-package /app"
                if server_source_stage
                else "COPY plastic_promise /app/plastic_promise"
            )
            source_copy_index = next(
                (
                    index
                    for index, (_, _, instruction) in enumerate(contract_instructions)
                    if instruction == server_source_copy
                ),
                -1,
            )
            install_runs = tuple(
                (index, argument)
                for index, (opcode, argument, _) in enumerate(contract_instructions)
                if opcode == "RUN" and "python -m pip install --no-cache-dir ." in argument
            )
            if len(install_runs) != 1:
                raise ContainerArtifactError("container_recipe_server_source_cleanup_required")
            install_index, install_run = install_runs[0]
            install_position = install_run.find("python -m pip install --no-cache-dir .")
            cleanup_position = install_run.find("rm -rf /app/plastic_promise")
            build_cleanup_position = install_run.find("/app/build")
            if (
                source_copy_index < 0
                or install_index <= source_copy_index
                or cleanup_position <= install_position
                or build_cleanup_position <= install_position
            ):
                raise ContainerArtifactError("container_recipe_server_source_cleanup_required")
        if role == PP_COMPUTE_NODE:
            source_copy_index = next(
                (
                    index
                    for index, (_, _, instruction) in enumerate(contract_instructions)
                    if instruction == "COPY --from=compute-package /role-package /app"
                ),
                -1,
            )
            install_runs = tuple(
                (index, argument)
                for index, (opcode, argument, _) in enumerate(contract_instructions)
                if opcode == "RUN" and "-m pip install" in argument and " ." in argument
            )
            if len(install_runs) != 1:
                raise ContainerArtifactError("container_recipe_compute_source_cleanup_required")
            install_index, install_run = install_runs[0]
            install_position = install_run.find("-m pip install")
            cleanup_position = install_run.find("rm -rf /app/plastic_promise")
            build_cleanup_position = install_run.find("/app/build")
            if (
                source_copy_index < 0
                or install_index <= source_copy_index
                or cleanup_position <= install_position
                or build_cleanup_position <= install_position
            ):
                raise ContainerArtifactError("container_recipe_compute_source_cleanup_required")
        run_mount_count = 0
        for opcode, argument, _ in parsed_instructions:
            if opcode != "RUN":
                continue
            remaining = argument.lstrip()
            while remaining.startswith("--"):
                flag, separator, remaining = remaining.partition(" ")
                if (
                    not separator
                    or role != PP_COMPUTE_NODE
                    or flag != _ALLOWED_COMPUTE_RUN_MOUNT
                    or run_mount_count
                ):
                    raise ContainerArtifactError("container_recipe_run_mount_forbidden")
                run_mount_count += 1
                remaining = remaining.lstrip()
        if role == PP_COMPUTE_NODE and run_mount_count != 1:
            raise ContainerArtifactError("container_recipe_run_mount_forbidden")
        forbidden_runtime_tokens = (
            "docker.sock",
            "docker run",
            "docker compose",
            "systemctl",
            "ssh ",
            "autossh",
            "tunnel",
            "socat",
            "ngrok",
            "cloudflared",
        )
        if any(token in "\n".join(instructions).lower() for token in forbidden_runtime_tokens):
            raise ContainerArtifactError("container_recipe_forbidden_runtime_command")

    @staticmethod
    def _load_compose(value: str) -> dict[str, object]:
        """Parse a repository-owned Compose file without duplicate-key ambiguity."""

        try:
            payload = yaml.load(value, Loader=_StrictComposeLoader)
        except _DuplicateComposeKeyError as exc:
            raise ContainerArtifactError("container_recipe_compose_duplicate_key") from exc
        except yaml.YAMLError as exc:
            raise ContainerArtifactError("container_recipe_compose_yaml_invalid") from exc
        if not isinstance(payload, dict):
            raise ContainerArtifactError("container_recipe_compose_yaml_invalid")
        return payload

    @staticmethod
    def _compose_mapping(value: object, code: str) -> dict[str, object]:
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise ContainerArtifactError(code)
        return value

    @staticmethod
    def _compose_list(value: object, code: str) -> list[object]:
        if not isinstance(value, list):
            raise ContainerArtifactError(code)
        return value

    @staticmethod
    def _compose_strings(value: object) -> tuple[str, ...]:
        """Return all parsed scalar text, including mapping keys, without comments."""

        strings: list[str] = []
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for key, nested in value.items():
                strings.extend(StaticRecipePolicyValidator._compose_strings(key))
                strings.extend(StaticRecipePolicyValidator._compose_strings(nested))
        elif isinstance(value, list):
            for nested in value:
                strings.extend(StaticRecipePolicyValidator._compose_strings(nested))
        return tuple(strings)

    @classmethod
    def _compose_service(
        cls,
        compose: dict[str, object],
        *,
        role: str,
        top_level_keys: frozenset[str],
    ) -> dict[str, object]:
        if set(compose) != top_level_keys:
            raise ContainerArtifactError("container_recipe_compose_top_level_invalid")
        services = cls._compose_mapping(
            compose.get("services"), "container_recipe_compose_services_invalid"
        )
        if set(services) != {role}:
            raise ContainerArtifactError("container_recipe_compose_service_invalid")
        return cls._compose_mapping(services[role], "container_recipe_compose_service_invalid")

    @staticmethod
    def _compose_only_keys(
        service: dict[str, object], *, expected: frozenset[str], code: str
    ) -> None:
        if set(service) != expected:
            raise ContainerArtifactError(code)

    @classmethod
    def _validate_common_compose_hardening(cls, service: dict[str, object]) -> None:
        forbidden_fields = {
            "privileged": "container_recipe_compose_privileged_forbidden",
            "cap_add": "container_recipe_compose_capability_forbidden",
            "devices": "container_recipe_compose_device_forbidden",
            "device_requests": "container_recipe_compose_device_forbidden",
            "pid": "container_recipe_compose_namespace_forbidden",
            "ipc": "container_recipe_compose_namespace_forbidden",
            "uts": "container_recipe_compose_namespace_forbidden",
        }
        for field, code in forbidden_fields.items():
            if field in service:
                raise ContainerArtifactError(code)
        if service.get("read_only") is not True:
            raise ContainerArtifactError("container_recipe_compose_read_only_required")
        security_opt = cls._compose_list(
            service.get("security_opt"), "container_recipe_compose_security_opt_required"
        )
        if security_opt != ["no-new-privileges:true"]:
            raise ContainerArtifactError("container_recipe_compose_security_opt_required")
        cap_drop = cls._compose_list(
            service.get("cap_drop"), "container_recipe_compose_cap_drop_required"
        )
        if cap_drop != ["ALL"]:
            raise ContainerArtifactError("container_recipe_compose_cap_drop_required")
        scalar_text = "\n".join(value.lower() for value in cls._compose_strings(service))
        if "docker.sock" in scalar_text:
            raise ContainerArtifactError("container_recipe_docker_socket_forbidden")
        if any(
            token in scalar_text
            for token in ("ssh", "autossh", "tunnel", "systemctl", "socat", "ngrok", "cloudflared")
        ):
            raise ContainerArtifactError("container_recipe_forbidden_runtime_command")

    @staticmethod
    def _validate_logging(value: object, code: str) -> None:
        if value != {
            "driver": "local",
            "options": {"max-size": "10m", "max-file": "3"},
        }:
            raise ContainerArtifactError(code)

    def _validate_edge_recipe(self, compose_text: str, nginx: str, entrypoint: str) -> None:
        compose = self._load_compose(compose_text)
        service = self._compose_service(
            compose,
            role=PP_LOCAL_EDGE,
            top_level_keys=frozenset({"services", "networks"}),
        )
        self._validate_common_compose_hardening(service)
        if "network_mode" in service or service.get("ports") != [
            "127.0.0.1:${PP_LOCAL_EDGE_PORT:-19021}:8080"
        ]:
            raise ContainerArtifactError("container_recipe_edge_listener_not_loopback")
        if service.get("networks") != ["local-edge"] or compose.get("networks") != {
            "local-edge": {"internal": True}
        }:
            raise ContainerArtifactError("container_recipe_edge_network_invalid")
        if "volumes" in service:
            raise ContainerArtifactError("container_recipe_edge_mount_forbidden")
        if any(field in service for field in ("build", "gpus")):
            raise ContainerArtifactError("container_recipe_edge_service_contract_invalid")
        self._compose_only_keys(
            service,
            expected=frozenset(
                {
                    "image",
                    "pull_policy",
                    "environment",
                    "ports",
                    "restart",
                    "read_only",
                    "security_opt",
                    "cap_drop",
                    "pids_limit",
                    "labels",
                    "tmpfs",
                    "networks",
                    "logging",
                }
            ),
            code="container_recipe_edge_service_contract_invalid",
        )
        if (
            service.get("image")
            != "${PP_LOCAL_EDGE_IMAGE:?set an immutable local-edge image digest from the release manifest}"
            or service.get("pull_policy") != "never"
        ):
            raise ContainerArtifactError("container_recipe_edge_image_invalid")
        if service.get("environment") != {
            "TZ": "UTC",
            "PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT": "${PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT:-}",
        }:
            raise ContainerArtifactError("container_recipe_edge_service_contract_invalid")
        if service.get("restart") != "unless-stopped" or service.get("pids_limit") != 128:
            raise ContainerArtifactError("container_recipe_edge_service_contract_invalid")
        if service.get("labels") != {
            "org.plastic-promise.endpoint.role": PP_LOCAL_EDGE,
            "org.plastic-promise.authority": _EDGE_RECIPE_AUTHORITY_LABEL,
        }:
            raise ContainerArtifactError("container_recipe_edge_service_contract_invalid")
        if service.get("tmpfs") != [
            "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
            "/var/lib/plastic-promise/edge-session-cache:rw,noexec,nosuid,size=32m,mode=1777",
        ]:
            raise ContainerArtifactError("container_recipe_edge_service_contract_invalid")
        self._validate_logging(
            service.get("logging"), "container_recipe_edge_service_contract_invalid"
        )
        nginx_normalized = _recipe_text(nginx)
        if "proxy_pass" in nginx_normalized:
            raise ContainerArtifactError("container_recipe_edge_proxy_forbidden")
        if "exec nginx -g 'daemon off;'" not in entrypoint:
            raise ContainerArtifactError("container_recipe_edge_entrypoint_invalid")

    def _validate_server_recipe(self, compose_text: str) -> None:
        compose = self._load_compose(compose_text)
        service = self._compose_service(
            compose,
            role=PP_SERVER_BACKEND,
            top_level_keys=frozenset({"services"}),
        )
        self._validate_common_compose_hardening(service)
        if service.get("network_mode") != "host" or "ports" in service or "networks" in service:
            raise ContainerArtifactError("container_recipe_backend_listener_invalid")
        if any(field in service for field in ("build", "gpus")):
            raise ContainerArtifactError("container_recipe_backend_authority_forbidden")
        self._compose_only_keys(
            service,
            expected=frozenset(
                {
                    "image",
                    "pull_policy",
                    "network_mode",
                    "restart",
                    "read_only",
                    "security_opt",
                    "cap_drop",
                    "pids_limit",
                    "labels",
                    "environment",
                    "volumes",
                    "tmpfs",
                    "logging",
                }
            ),
            code="container_recipe_backend_service_contract_invalid",
        )
        if (
            service.get("image")
            != "${PLASTIC_PROMISE_SERVER_IMAGE:?set an immutable server image digest from the release manifest}"
            or service.get("pull_policy") != "never"
        ):
            raise ContainerArtifactError("container_recipe_backend_service_contract_invalid")
        if service.get("restart") != "unless-stopped" or service.get("pids_limit") != 256:
            raise ContainerArtifactError("container_recipe_backend_service_contract_invalid")
        if service.get("labels") != {
            "org.plastic-promise.endpoint.role": PP_SERVER_BACKEND,
            "org.plastic-promise.authority": _SERVER_RECIPE_AUTHORITY_LABEL,
        }:
            raise ContainerArtifactError("container_recipe_backend_service_contract_invalid")
        expected_environment = {
            "TZ": "UTC",
            "PP_ENDPOINT_ROLE": PP_SERVER_BACKEND,
            "PLASTIC_DB_PATH": "${PLASTIC_DB_PATH:?set the canonical database path}",
            "PLASTIC_LANCEDB_PATH": "${PLASTIC_LANCEDB_PATH:?set the derived index path}",
            "PLASTIC_RUNTIME_LOCK_PATH": "${PLASTIC_RUNTIME_LOCK_PATH:?set the shared canonical runtime lock path}",
            "PLASTIC_RUNTIME_MODE": "${PLASTIC_RUNTIME_MODE:-normal}",
        }
        if service.get("environment") != expected_environment:
            raise ContainerArtifactError("container_recipe_backend_canonical_mount_required")
        if service.get("volumes") != [
            {
                "type": "bind",
                "source": "${PP_SERVER_STATE_DIRECTORY:?set the server-owned state directory}",
                "target": "/var/lib/plastic-promise",
            }
        ]:
            raise ContainerArtifactError("container_recipe_backend_canonical_mount_invalid")
        if service.get("tmpfs") != ["/tmp:rw,noexec,nosuid,size=256m"]:
            raise ContainerArtifactError("container_recipe_backend_service_contract_invalid")
        self._validate_logging(
            service.get("logging"), "container_recipe_backend_service_contract_invalid"
        )
        service_text = "\n".join(self._compose_strings(service))
        if any(token in service_text for token in ("/models", "PP_LOCAL_NODE_")):
            raise ContainerArtifactError("container_recipe_backend_authority_forbidden")

    def _validate_compute_recipe(self, compose_text: str, *, variant: str) -> None:
        compose = self._load_compose(compose_text)
        service = self._compose_service(
            compose,
            role=PP_COMPUTE_NODE,
            top_level_keys=frozenset({"services"}),
        )
        self._validate_common_compose_hardening(service)
        if service.get("network_mode") != "host" or "ports" in service or "networks" in service:
            raise ContainerArtifactError("container_recipe_compute_listener_invalid")
        if variant == COMPUTE_VARIANT_CPU:
            if "gpus" in service:
                raise ContainerArtifactError("container_recipe_compute_cpu_gpu_forbidden")
        elif service.get("gpus") != "all":
            raise ContainerArtifactError("container_recipe_compute_cuda_gpu_required")
        expected_keys = {
            "build",
            "image",
            "network_mode",
            "restart",
            "read_only",
            "security_opt",
            "cap_drop",
            "pids_limit",
            "labels",
            "environment",
            "volumes",
            "tmpfs",
            "logging",
        }
        is_compatibility_template = (
            variant == COMPUTE_VARIANT_CUDA
            and service.get("image") == "plastic-promise-local-inference-node:dev"
        )
        if variant == COMPUTE_VARIANT_CUDA:
            expected_keys.add("gpus")
        self._compose_only_keys(
            service,
            expected=frozenset(expected_keys),
            code="container_recipe_compute_contract_invalid",
        )
        expected_image = {
            COMPUTE_VARIANT_CPU: "plastic-promise-compute-node:cpu-dev",
            COMPUTE_VARIANT_CUDA: "plastic-promise-compute-node:cuda-dev",
        }[variant]
        if is_compatibility_template:
            expected_image = "plastic-promise-local-inference-node:dev"
        if service.get("image") != expected_image:
            raise ContainerArtifactError("container_recipe_compute_contract_invalid")
        if service.get("restart") != "unless-stopped" or service.get("pids_limit") != 256:
            raise ContainerArtifactError("container_recipe_compute_contract_invalid")
        base_prefix = "PP_COMPUTE_CPU" if variant == COMPUTE_VARIANT_CPU else "PP_COMPUTE_CUDA"
        expected_build = {
            "context": "../..",
            "dockerfile": "deploy/local-inference-node/Dockerfile",
            "args": {
                "BASE_IMAGE": f"${{{base_prefix}_BASE_IMAGE:?set an immutable {'CPU' if variant == COMPUTE_VARIANT_CPU else 'CUDA'} base image reference}}",
                "BASE_IMAGE_DIGEST": f"${{{base_prefix}_BASE_IMAGE_DIGEST:?set the matching {'CPU' if variant == COMPUTE_VARIANT_CPU else 'CUDA'} base image digest}}",
                "COMPUTE_VARIANT": variant,
                "SOURCE_REVISION": "${PP_BUILD_SOURCE_REVISION:?set an immutable source revision}",
                "PACKAGE_VERSION": "${PP_BUILD_PACKAGE_VERSION:?set a package version}",
                "BUILD_POLICY_DIGEST": "${PP_BUILD_POLICY_DIGEST:?run the container identity resolver}",
                "RECIPE_POLICY_DIGEST": "${PP_RECIPE_POLICY_DIGEST:?run the container identity resolver}",
            },
        }
        if service.get("build") != expected_build:
            raise ContainerArtifactError("container_recipe_compute_contract_invalid")
        embedding_backend_default = "llama.cpp"
        rerank_backend_default = "llama.cpp"
        ollama_host_default = (
            "http://127.0.0.1:11434"
            if variant == COMPUTE_VARIANT_CPU
            else "http://host.docker.internal:11434"
        )
        expected_environment = {
            "TZ": "UTC",
            "PP_ENDPOINT_ROLE": PP_COMPUTE_NODE,
            "PP_LOCAL_NODE_AUTHORIZATION": "${PP_LOCAL_NODE_AUTHORIZATION:?set a private Bearer token}",
            "PP_LOCAL_NODE_BIND_HOST": "127.0.0.1",
            "PP_LOCAL_NODE_PORT": "19130",
            "PP_LOCAL_NODE_MAX_CONCURRENCY": "${PP_LOCAL_NODE_MAX_CONCURRENCY:-1}",
            "PP_LOCAL_NODE_RESOURCE_GUARD": "${PP_LOCAL_NODE_RESOURCE_GUARD:-on}",
            "PP_LOCAL_NODE_RESOURCE_GPU_UTILIZATION_LIMIT": (
                "${PP_LOCAL_NODE_RESOURCE_GPU_UTILIZATION_LIMIT:-70}"
            ),
            "PP_LOCAL_NODE_RESOURCE_SAMPLE_TTL_SECONDS": (
                "${PP_LOCAL_NODE_RESOURCE_SAMPLE_TTL_SECONDS:-2}"
            ),
            "PP_LOCAL_NODE_RESOURCE_RETRY_AFTER_SECONDS": (
                "${PP_LOCAL_NODE_RESOURCE_RETRY_AFTER_SECONDS:-5}"
            ),
            "PP_LOCAL_NODE_ID": "${PP_LOCAL_NODE_ID:?set a non-secret node ID}",
            "PP_LOCAL_NODE_PROVIDER_MODE": "${PP_LOCAL_NODE_PROVIDER_MODE:-local}",
            "PP_LOCAL_NODE_EMBEDDING_BACKEND": f"${{PP_LOCAL_NODE_EMBEDDING_BACKEND:-{embedding_backend_default}}}",
            "PP_LOCAL_NODE_EMBEDDING_MODEL": "${PP_LOCAL_NODE_EMBEDDING_MODEL:?set model identity}",
            "PP_LOCAL_NODE_EMBEDDING_REVISION": "${PP_LOCAL_NODE_EMBEDDING_REVISION:?set a fixed revision}",
            "PP_LOCAL_NODE_EMBEDDING_DIMENSION": "${PP_LOCAL_NODE_EMBEDDING_DIMENSION:?set output dimension}",
            "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION": "${PP_LOCAL_NODE_EMBEDDING_NORMALIZATION:?set normalization}",
            "PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE": "${PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE:-/models/embedding}",
            "PP_LOCAL_NODE_CLOUD_API_KEY": "${PP_LOCAL_NODE_CLOUD_API_KEY:-}",
            "PP_LOCAL_NODE_EMBEDDING_CLOUD_BASE_URL": "${PP_LOCAL_NODE_EMBEDDING_CLOUD_BASE_URL:-}",
            "PP_LOCAL_NODE_EMBEDDING_CLOUD_PATH": "${PP_LOCAL_NODE_EMBEDDING_CLOUD_PATH:-/embeddings}",
            "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL": "${PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL:-http://127.0.0.1:19131}",
            "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH": "${PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH:-/v1/embeddings}",
            "PP_LOCAL_NODE_RERANK_BACKEND": f"${{PP_LOCAL_NODE_RERANK_BACKEND:-{rerank_backend_default}}}",
            "PP_LOCAL_NODE_RERANK_MODEL": "${PP_LOCAL_NODE_RERANK_MODEL:?set model identity}",
            "PP_LOCAL_NODE_RERANK_REVISION": "${PP_LOCAL_NODE_RERANK_REVISION:?set a fixed revision}",
            "PP_LOCAL_NODE_RERANK_MODEL_REFERENCE": "${PP_LOCAL_NODE_RERANK_MODEL_REFERENCE:-/models/rerank}",
            "PP_LOCAL_NODE_RERANK_CLOUD_BASE_URL": "${PP_LOCAL_NODE_RERANK_CLOUD_BASE_URL:-}",
            "PP_LOCAL_NODE_RERANK_CLOUD_PATH": "${PP_LOCAL_NODE_RERANK_CLOUD_PATH:-/rerank}",
            "PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL": "${PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL:-http://127.0.0.1:19132}",
            "PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH": "${PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH:-/rerank}",
            "PP_LOCAL_NODE_STRUCTURED_JSON_BACKEND": "${PP_LOCAL_NODE_STRUCTURED_JSON_BACKEND:-off}",
            "PP_LOCAL_NODE_STRUCTURED_JSON_MODEL": "${PP_LOCAL_NODE_STRUCTURED_JSON_MODEL:-}",
            "PP_LOCAL_NODE_STRUCTURED_JSON_REVISION": "${PP_LOCAL_NODE_STRUCTURED_JSON_REVISION:-}",
            "PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_BASE_URL": "${PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_BASE_URL:-}",
            "PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_PATH": "${PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_PATH:-/chat/completions}",
            "PP_LOCAL_NODE_OLLAMA_HOST": f"${{PP_LOCAL_NODE_OLLAMA_HOST:-{ollama_host_default}}}",
            "EMBEDDER_TIMEOUT": "${PP_LOCAL_NODE_EMBEDDER_TIMEOUT:-10}",
            "PYTORCH_CUDA_ALLOC_CONF": "${PP_LOCAL_NODE_PYTORCH_ALLOC_CONF:-expandable_segments:True}",
            "PP_LOCAL_NODE_MODEL_CACHE_DIR": "/models",
            "HOME": "/tmp",
            "XDG_CACHE_HOME": "/tmp/.cache",
            "HF_HOME": "/tmp/huggingface",
            "SENTENCE_TRANSFORMERS_HOME": "/tmp/sentence-transformers",
        }
        if service.get("environment") != expected_environment:
            raise ContainerArtifactError("container_recipe_compute_contract_invalid")
        if service.get("volumes") != [
            {
                "type": "bind",
                "source": "${PP_LOCAL_NODE_MODEL_DIRECTORY:?set a pre-populated model directory}",
                "target": "/models",
                "read_only": True,
            }
        ]:
            raise ContainerArtifactError("container_recipe_compute_contract_invalid")
        if service.get("tmpfs") != [
            "/tmp:rw,noexec,nosuid,size=512m",
            "/var/lib/plastic-promise/compute-node:rw,noexec,nosuid,size=128m",
        ]:
            raise ContainerArtifactError("container_recipe_compute_contract_invalid")
        self._validate_logging(service.get("logging"), "container_recipe_compute_contract_invalid")
        if service.get("labels") != {
            "org.plastic-promise.endpoint.role": PP_COMPUTE_NODE,
            "org.plastic-promise.authority": _COMPUTE_RECIPE_AUTHORITY_LABEL,
            "org.plastic-promise.compute.variant": variant,
            "org.plastic-promise.compute.capabilities": _COMPUTE_CAPABILITY_LABEL,
            "org.plastic-promise.compute.package-manifest": (
                _COMPUTE_PACKAGE_MANIFEST.schema_version
            ),
        }:
            raise ContainerArtifactError("container_recipe_compute_contract_invalid")
        service_text = "\n".join(self._compose_strings(service))
        if any(
            token in service_text
            for token in ("canonical-state", "PLASTIC_DB_PATH", "PLASTIC_LANCEDB_PATH")
        ):
            raise ContainerArtifactError("container_recipe_compute_canonical_mount_forbidden")


@dataclass(frozen=True)
class ArtifactRequest:
    """Secret-free input for planning a set of OCI artifacts.

    ``model_catalog_reference`` and its digest identify model metadata only;
    neither a model path nor model weights may enter this contract.
    """

    profile_id: str
    source_revision: str
    package_version: str
    platforms: tuple[str, ...]
    compute_variants: tuple[str, ...]
    model_catalog_reference: str | None = None
    model_catalog_digest: str | None = None

    def __post_init__(self) -> None:
        if profile_by_id(self.profile_id) is None:
            raise ContainerArtifactError("container_artifact_profile_unsupported")
        if _PINNED_REVISION.fullmatch(self.source_revision) is None:
            raise ContainerArtifactError("container_artifact_source_revision_not_pinned")
        if _PACKAGE_VERSION.fullmatch(self.package_version) is None:
            raise ContainerArtifactError("container_artifact_package_version_invalid")
        _unique(self.platforms, "container_artifact_platforms_invalid")
        if any(platform not in _SUPPORTED_PLATFORMS for platform in self.platforms):
            raise ContainerArtifactError("container_artifact_platform_unsupported")
        if len(self.compute_variants) != len(set(self.compute_variants)):
            raise ContainerArtifactError("container_artifact_compute_variant_duplicate")
        if any(variant not in _COMPUTE_VARIANTS for variant in self.compute_variants):
            raise ContainerArtifactError("container_artifact_compute_variant_unsupported")
        if self.profile_id == "local-cloud" and self.compute_variants:
            raise ContainerArtifactError("container_artifact_cloud_compute_forbidden")
        if self.profile_id == "split-accelerated" and not self.compute_variants:
            raise ContainerArtifactError("container_artifact_compute_variant_required")
        if COMPUTE_VARIANT_CUDA in self.compute_variants and not any(
            platform in _CUDA_PLATFORMS for platform in self.platforms
        ):
            raise ContainerArtifactError("container_artifact_cuda_platform_unsupported")
        has_catalog = self.model_catalog_reference is not None
        has_catalog_digest = self.model_catalog_digest is not None
        if has_catalog != has_catalog_digest:
            raise ContainerArtifactError("container_artifact_model_catalog_incomplete")
        if self.compute_variants and not has_catalog:
            raise ContainerArtifactError("container_artifact_model_catalog_required")
        if self.model_catalog_reference is not None and (
            _CATALOG_REFERENCE.fullmatch(self.model_catalog_reference) is None
        ):
            raise ContainerArtifactError("container_artifact_model_catalog_reference_invalid")
        if self.model_catalog_digest is not None:
            _require_sha256(
                self.model_catalog_digest,
                "container_artifact_model_catalog_digest_invalid",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile_id,
            "source_revision": self.source_revision,
            "package_version": self.package_version,
            "platforms": list(self.platforms),
            "compute_variants": list(self.compute_variants),
            "model_catalog_reference": self.model_catalog_reference,
            "model_catalog_digest": self.model_catalog_digest,
        }


@dataclass(frozen=True)
class ArtifactMount:
    """A logical mount name, deliberately without a host or container path."""

    name: str
    access: str
    purpose: str

    def __post_init__(self) -> None:
        if _CATALOG_REFERENCE.fullmatch(self.name) is None:
            raise ContainerArtifactError("container_artifact_mount_name_invalid")
        if self.access not in {"read-only", "read-write", "tmpfs"}:
            raise ContainerArtifactError("container_artifact_mount_access_invalid")
        if _CATALOG_REFERENCE.fullmatch(self.purpose) is None:
            raise ContainerArtifactError("container_artifact_mount_purpose_invalid")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "access": self.access, "purpose": self.purpose}


@dataclass(frozen=True)
class ArtifactDescriptor:
    """One independently buildable and inspectable endpoint artifact."""

    artifact_id: str
    role: str
    platform: str
    variant: str
    entrypoint: str
    listener_scope: str
    mounts: tuple[ArtifactMount, ...]
    capabilities: tuple[str, ...]
    authorities: tuple[str, ...]
    base_image_reference: str
    layer_exclusions: tuple[str, ...] = _IMAGE_LAYER_EXCLUSIONS
    read_only_rootfs: bool = True
    non_root: bool = True

    def __post_init__(self) -> None:
        if _CATALOG_REFERENCE.fullmatch(self.artifact_id) is None:
            raise ContainerArtifactError("container_artifact_id_invalid")
        if self.role not in _ROLES:
            raise ContainerArtifactError("container_artifact_role_invalid")
        if self.platform not in _SUPPORTED_PLATFORMS:
            raise ContainerArtifactError("container_artifact_platform_unsupported")
        if self.role == PP_COMPUTE_NODE:
            if self.variant not in _COMPUTE_VARIANTS:
                raise ContainerArtifactError("container_artifact_compute_variant_unsupported")
        elif self.variant != STANDARD_VARIANT:
            raise ContainerArtifactError("container_artifact_standard_variant_required")
        if self.variant == COMPUTE_VARIANT_CUDA and self.platform not in _CUDA_PLATFORMS:
            raise ContainerArtifactError("container_artifact_cuda_platform_unsupported")
        if _CATALOG_REFERENCE.fullmatch(self.entrypoint) is None:
            raise ContainerArtifactError("container_artifact_entrypoint_invalid")
        if _PINNED_IMAGE_REFERENCE.fullmatch(self.base_image_reference) is None:
            raise ContainerArtifactError("container_artifact_base_image_reference_unpinned")
        if self.listener_scope not in {"loopback", "private"}:
            raise ContainerArtifactError("container_artifact_listener_scope_invalid")
        if not self.read_only_rootfs or not self.non_root:
            raise ContainerArtifactError("container_artifact_runtime_hardening_required")
        if len({mount.name for mount in self.mounts}) != len(self.mounts):
            raise ContainerArtifactError("container_artifact_mount_duplicate")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ContainerArtifactError("container_artifact_capability_duplicate")
        if len(set(self.authorities)) != len(self.authorities):
            raise ContainerArtifactError("container_artifact_authority_duplicate")
        has_collaboration_writer_authority = (
            _COLLABORATION_EVENT_WRITER_AUTHORITY in self.authorities
        )
        if has_collaboration_writer_authority != (self.role == PP_SERVER_BACKEND):
            raise ContainerArtifactError("container_artifact_collaboration_authority_invalid")
        if tuple(self.layer_exclusions) != _IMAGE_LAYER_EXCLUSIONS:
            raise ContainerArtifactError("container_artifact_layer_exclusions_invalid")
        canonical_mounts = [mount for mount in self.mounts if mount.name == "canonical-state"]
        if self.role == PP_SERVER_BACKEND:
            if canonical_mounts != [
                ArtifactMount("canonical-state", "read-write", "server-owned-canonical-state")
            ]:
                raise ContainerArtifactError("container_artifact_canonical_mount_required")
        elif canonical_mounts:
            raise ContainerArtifactError("container_artifact_canonical_mount_forbidden")
        if self.role == PP_COMPUTE_NODE:
            required_mounts = {
                ("model-catalog", "read-only", "model-catalog-materialized"),
                ("node-runtime", "read-write", "bounded-node-runtime"),
            }
            actual_mounts = {(mount.name, mount.access, mount.purpose) for mount in self.mounts}
            if not required_mounts.issubset(actual_mounts):
                raise ContainerArtifactError("container_artifact_compute_mounts_required")
            if self.capabilities != _COMPUTE_CAPABILITY_CONTRACTS:
                raise ContainerArtifactError("container_artifact_compute_protocol_invalid")
        elif self.capabilities:
            raise ContainerArtifactError("container_artifact_role_capability_invalid")

    @property
    def collaboration_surface(self) -> ArtifactCollaborationSurface:
        """Return the closed source surface compiled solely from the artifact role."""

        return _collaboration_surface_for(self.role)

    @property
    def collaboration_surface_digest(self) -> str:
        """Return the collaboration source-surface identity bound into policy evidence."""

        return self.collaboration_surface.digest

    def to_dict(self) -> dict[str, object]:
        collaboration_surface = self.collaboration_surface
        return {
            "id": self.artifact_id,
            "role": self.role,
            "platform": self.platform,
            "variant": self.variant,
            "entrypoint": self.entrypoint,
            "listener_scope": self.listener_scope,
            "mounts": [mount.to_dict() for mount in self.mounts],
            "capabilities": list(self.capabilities),
            "authorities": list(self.authorities),
            "collaboration_surface": collaboration_surface.to_dict(),
            "collaboration_surface_digest": collaboration_surface.digest,
            "base_image_reference": self.base_image_reference,
            "layer_exclusions": list(self.layer_exclusions),
            "read_only_rootfs": self.read_only_rootfs,
            "non_root": self.non_root,
        }


@dataclass(frozen=True)
class ArtifactBuildPlan:
    """A deterministic, side-effect-free artifact matrix and policy digest."""

    request: ArtifactRequest
    recipe_policy: RecipePolicyReceipt
    artifacts: tuple[ArtifactDescriptor, ...]
    policy_digest: str
    schema_version: str = CONTAINER_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONTAINER_ARTIFACT_SCHEMA_VERSION:
            raise ContainerArtifactError("container_artifact_schema_unsupported")
        if not self.artifacts:
            raise ContainerArtifactError("container_artifact_matrix_empty")
        if len({artifact.artifact_id for artifact in self.artifacts}) != len(self.artifacts):
            raise ContainerArtifactError("container_artifact_id_duplicate")
        if self.artifacts != tuple(
            ContainerArtifactCompiler._matrix(self.request, self.recipe_policy)
        ):
            raise ContainerArtifactError("container_artifact_matrix_mismatch")
        _require_sha256(self.policy_digest, "container_artifact_policy_digest_invalid")
        if self.policy_digest != _sha256(self._policy_payload()):
            raise ContainerArtifactError("container_artifact_policy_digest_mismatch")

    def _policy_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request": self.request.to_dict(),
            "recipe_policy": self.recipe_policy.to_dict(),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    def to_dict(self) -> dict[str, object]:
        payload = self._policy_payload()
        payload["policy_digest"] = self.policy_digest
        return payload

    def artifact_for(
        self, role: str, platform: str, variant: str = STANDARD_VARIANT
    ) -> ArtifactDescriptor:
        """Return one planned descriptor without exposing build implementation."""

        for artifact in self.artifacts:
            if (artifact.role, artifact.platform, artifact.variant) == (
                role,
                platform,
                variant,
            ):
                return artifact
        raise ContainerArtifactError("container_artifact_not_planned")

    def expected_oci_labels(self, artifact_id: str) -> dict[str, str]:
        """Return the exact labels a build adapter must inspect for one image."""

        artifact = next((item for item in self.artifacts if item.artifact_id == artifact_id), None)
        if artifact is None:
            raise ContainerArtifactError("container_artifact_not_planned")
        labels = {
            "org.opencontainers.image.revision": self.request.source_revision,
            "org.opencontainers.image.version": self.request.package_version,
            "org.plastic-promise.endpoint.role": artifact.role,
            "org.plastic-promise.endpoint.variant": artifact.variant,
            "org.plastic-promise.endpoint.contract": ENDPOINT_CONTRACT_SCHEMA_VERSION,
            "org.opencontainers.image.base.name": artifact.base_image_reference,
            "org.opencontainers.image.base.digest": artifact.base_image_reference.rsplit(
                "@", maxsplit=1
            )[1],
            "org.plastic-promise.build.policy-digest": self.policy_digest,
            "org.plastic-promise.build.recipe-policy-digest": self.recipe_policy.recipe_policy_digest,
        }
        if artifact.role == PP_COMPUTE_NODE:
            labels["org.plastic-promise.authority"] = _COMPUTE_RECIPE_AUTHORITY_LABEL
            labels["org.plastic-promise.compute.capabilities"] = _COMPUTE_CAPABILITY_LABEL
            labels["org.plastic-promise.compute.package-manifest"] = (
                _COMPUTE_PACKAGE_MANIFEST.schema_version
            )
        elif artifact.role == PP_SERVER_BACKEND:
            labels["org.plastic-promise.authority"] = _SERVER_RECIPE_AUTHORITY_LABEL
            labels["org.plastic-promise.server.source-exclusions"] = _SERVER_SOURCE_EXCLUSION_LABEL
        elif artifact.role == PP_LOCAL_EDGE:
            labels["org.plastic-promise.authority"] = _EDGE_RECIPE_AUTHORITY_LABEL
        return labels

    @property
    def recipe_policy_digest(self) -> str:
        """Expose the already-validated static-recipe identity for an executor."""

        return self.recipe_policy.recipe_policy_digest


@dataclass(frozen=True)
class ArtifactEvidenceReceipt:
    """Structured OCI/SBOM/provenance binding returned by a build inspector.

    The receipt is the narrow trust boundary between a Buildx/CI adapter and
    the pure compiler.  The adapter must derive its image, SBOM, and provenance
    digests from the OCI layout it inspected; the compiler then makes every
    evidence subject, source identity, base image, and recipe policy match the
    prepared artifact plan.  It deliberately contains no credentials, paths,
    or raw scanner output.
    """

    artifact_id: str
    role: str
    platform: str
    variant: str
    source_revision: str
    package_version: str
    base_image_reference: str
    recipe_policy_digest: str
    policy_digest: str
    collaboration_surface_digest: str
    application_inventory_digest: str
    oci_layout_digest: str
    image_digest: str
    oci_labels_digest: str
    sbom_digest: str
    sbom_subject_digest: str
    provenance_digest: str
    provenance_subject_digest: str
    schema_version: str = CONTAINER_ARTIFACT_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONTAINER_ARTIFACT_EVIDENCE_SCHEMA_VERSION:
            raise ContainerArtifactError("container_artifact_evidence_schema_unsupported")
        if _CATALOG_REFERENCE.fullmatch(self.artifact_id) is None:
            raise ContainerArtifactError("container_artifact_evidence_id_invalid")
        if self.role not in _ROLES or self.platform not in _SUPPORTED_PLATFORMS:
            raise ContainerArtifactError("container_artifact_evidence_target_invalid")
        if self.role == PP_COMPUTE_NODE:
            if self.variant not in _COMPUTE_VARIANTS:
                raise ContainerArtifactError("container_artifact_evidence_variant_invalid")
        elif self.variant != STANDARD_VARIANT:
            raise ContainerArtifactError("container_artifact_evidence_variant_invalid")
        if self.variant == COMPUTE_VARIANT_CUDA and self.platform not in _CUDA_PLATFORMS:
            raise ContainerArtifactError("container_artifact_cuda_platform_unsupported")
        if _PINNED_REVISION.fullmatch(self.source_revision) is None:
            raise ContainerArtifactError("container_artifact_evidence_source_revision_invalid")
        if _PACKAGE_VERSION.fullmatch(self.package_version) is None:
            raise ContainerArtifactError("container_artifact_evidence_package_version_invalid")
        if _PINNED_IMAGE_REFERENCE.fullmatch(self.base_image_reference) is None:
            raise ContainerArtifactError("container_artifact_evidence_base_image_invalid")
        for value, code in (
            (self.recipe_policy_digest, "container_artifact_evidence_recipe_policy_invalid"),
            (self.policy_digest, "container_artifact_evidence_policy_invalid"),
            (
                self.collaboration_surface_digest,
                "container_artifact_evidence_collaboration_surface_invalid",
            ),
            (
                self.application_inventory_digest,
                "container_artifact_evidence_application_inventory_invalid",
            ),
            (self.oci_layout_digest, "container_artifact_oci_layout_digest_invalid"),
            (self.image_digest, "container_artifact_image_digest_invalid"),
            (self.oci_labels_digest, "container_artifact_labels_digest_invalid"),
            (self.sbom_digest, "container_artifact_sbom_digest_invalid"),
            (self.sbom_subject_digest, "container_artifact_sbom_subject_invalid"),
            (self.provenance_digest, "container_artifact_provenance_digest_invalid"),
            (self.provenance_subject_digest, "container_artifact_provenance_subject_invalid"),
        ):
            _require_sha256(value, code)
        if self.sbom_subject_digest != self.image_digest:
            raise ContainerArtifactError("container_artifact_sbom_subject_mismatch")
        if self.provenance_subject_digest != self.image_digest:
            raise ContainerArtifactError("container_artifact_provenance_subject_mismatch")

    def _payload(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "role": self.role,
            "platform": self.platform,
            "variant": self.variant,
            "source_revision": self.source_revision,
            "package_version": self.package_version,
            "base_image_reference": self.base_image_reference,
            "recipe_policy_digest": self.recipe_policy_digest,
            "policy_digest": self.policy_digest,
            "collaboration_surface_digest": self.collaboration_surface_digest,
            "application_inventory_digest": self.application_inventory_digest,
            "oci_layout_digest": self.oci_layout_digest,
            "image_digest": self.image_digest,
            "oci_labels_digest": self.oci_labels_digest,
            "sbom_digest": self.sbom_digest,
            "sbom_subject_digest": self.sbom_subject_digest,
            "provenance_digest": self.provenance_digest,
            "provenance_subject_digest": self.provenance_subject_digest,
        }

    @property
    def digest(self) -> str:
        """Return the canonical digest of this complete, non-secret receipt."""

        return _sha256(self._payload())

    def to_dict(self) -> dict[str, str]:
        payload = self._payload()
        payload["receipt_digest"] = self.digest
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> ArtifactEvidenceReceipt:
        """Parse the exact secret-free receipt emitted by the OCI verifier."""

        if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
            raise ContainerArtifactError("container_artifact_evidence_payload_invalid")
        required = {
            "schema_version",
            "artifact_id",
            "role",
            "platform",
            "variant",
            "source_revision",
            "package_version",
            "base_image_reference",
            "recipe_policy_digest",
            "policy_digest",
            "collaboration_surface_digest",
            "application_inventory_digest",
            "oci_layout_digest",
            "image_digest",
            "oci_labels_digest",
            "sbom_digest",
            "sbom_subject_digest",
            "provenance_digest",
            "provenance_subject_digest",
            "receipt_digest",
        }
        if set(payload) != required or not all(
            isinstance(value, str) for value in payload.values()
        ):
            raise ContainerArtifactError("container_artifact_evidence_payload_invalid")
        receipt = cls(
            schema_version=payload["schema_version"],
            artifact_id=payload["artifact_id"],
            role=payload["role"],
            platform=payload["platform"],
            variant=payload["variant"],
            source_revision=payload["source_revision"],
            package_version=payload["package_version"],
            base_image_reference=payload["base_image_reference"],
            recipe_policy_digest=payload["recipe_policy_digest"],
            policy_digest=payload["policy_digest"],
            collaboration_surface_digest=payload["collaboration_surface_digest"],
            application_inventory_digest=payload["application_inventory_digest"],
            oci_layout_digest=payload["oci_layout_digest"],
            image_digest=payload["image_digest"],
            oci_labels_digest=payload["oci_labels_digest"],
            sbom_digest=payload["sbom_digest"],
            sbom_subject_digest=payload["sbom_subject_digest"],
            provenance_digest=payload["provenance_digest"],
            provenance_subject_digest=payload["provenance_subject_digest"],
        )
        if payload["receipt_digest"] != receipt.digest:
            raise ContainerArtifactError("container_artifact_evidence_receipt_digest_mismatch")
        return receipt

    def validate_against(self, plan: ArtifactBuildPlan, descriptor: ArtifactDescriptor) -> None:
        """Bind all returned evidence fields to a prepared plan descriptor."""

        if (
            self.artifact_id,
            self.role,
            self.platform,
            self.variant,
        ) != (
            descriptor.artifact_id,
            descriptor.role,
            descriptor.platform,
            descriptor.variant,
        ):
            raise ContainerArtifactError("container_artifact_evidence_target_mismatch")
        if (
            self.source_revision != plan.request.source_revision
            or self.package_version != plan.request.package_version
            or self.base_image_reference != descriptor.base_image_reference
            or self.recipe_policy_digest != plan.recipe_policy_digest
            or self.policy_digest != plan.policy_digest
        ):
            raise ContainerArtifactError("container_artifact_evidence_binding_mismatch")
        if self.collaboration_surface_digest != descriptor.collaboration_surface_digest:
            raise ContainerArtifactError(
                "container_artifact_evidence_collaboration_surface_mismatch"
            )
        expected_labels_digest = _sha256(plan.expected_oci_labels(descriptor.artifact_id))
        if self.oci_labels_digest != expected_labels_digest:
            raise ContainerArtifactError("container_artifact_evidence_labels_mismatch")


@dataclass(frozen=True)
class ArtifactMaterialization:
    """One non-secret, immutable image result plus its structured evidence receipt."""

    artifact_id: str
    role: str
    platform: str
    variant: str
    immutable_reference: str
    image_digest: str
    oci_layout_digest: str
    oci_labels_digest: str
    sbom_digest: str
    provenance_digest: str
    evidence_receipt: ArtifactEvidenceReceipt

    def __post_init__(self) -> None:
        if _CATALOG_REFERENCE.fullmatch(self.artifact_id) is None:
            raise ContainerArtifactError("container_artifact_evidence_id_invalid")
        if self.role not in _ROLES or self.platform not in _SUPPORTED_PLATFORMS:
            raise ContainerArtifactError("container_artifact_evidence_target_invalid")
        if self.role == PP_COMPUTE_NODE:
            if self.variant not in _COMPUTE_VARIANTS:
                raise ContainerArtifactError("container_artifact_evidence_variant_invalid")
        elif self.variant != STANDARD_VARIANT:
            raise ContainerArtifactError("container_artifact_evidence_variant_invalid")
        if self.variant == COMPUTE_VARIANT_CUDA and self.platform not in _CUDA_PLATFORMS:
            raise ContainerArtifactError("container_artifact_cuda_platform_unsupported")
        if _OCI_REFERENCE.fullmatch(self.immutable_reference) is None:
            raise ContainerArtifactError("container_artifact_immutable_reference_invalid")
        for value, code in (
            (self.image_digest, "container_artifact_image_digest_invalid"),
            (self.oci_layout_digest, "container_artifact_oci_layout_digest_invalid"),
            (self.oci_labels_digest, "container_artifact_labels_digest_invalid"),
            (self.sbom_digest, "container_artifact_sbom_digest_invalid"),
        ):
            _require_sha256(value, code)
        if self.immutable_reference != f"oci@{self.image_digest}":
            raise ContainerArtifactError("container_artifact_reference_digest_mismatch")
        evidence = self.evidence_receipt
        if (
            evidence.artifact_id,
            evidence.role,
            evidence.platform,
            evidence.variant,
            evidence.image_digest,
            evidence.oci_layout_digest,
            evidence.oci_labels_digest,
            evidence.sbom_digest,
            evidence.provenance_digest,
        ) != (
            self.artifact_id,
            self.role,
            self.platform,
            self.variant,
            self.image_digest,
            self.oci_layout_digest,
            self.oci_labels_digest,
            self.sbom_digest,
            self.provenance_digest,
        ):
            raise ContainerArtifactError("container_artifact_evidence_receipt_mismatch")

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "role": self.role,
            "platform": self.platform,
            "variant": self.variant,
            "immutable_reference": self.immutable_reference,
            "image_digest": self.image_digest,
            "oci_layout_digest": self.oci_layout_digest,
            "oci_labels_digest": self.oci_labels_digest,
            "sbom_digest": self.sbom_digest,
            "provenance_digest": self.provenance_digest,
            "collaboration_surface_digest": self.evidence_receipt.collaboration_surface_digest,
            "application_inventory_digest": self.evidence_receipt.application_inventory_digest,
            "evidence_schema_version": self.evidence_receipt.schema_version,
            "evidence_receipt_digest": self.evidence_receipt.digest,
        }


@dataclass(frozen=True)
class ArtifactInspectionReceipt:
    """The portable result of artifact-policy inspection, not a deployment receipt."""

    policy_digest: str
    artifact_ids: tuple[str, ...]
    schema_version: str = CONTAINER_ARTIFACT_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONTAINER_ARTIFACT_BUNDLE_SCHEMA_VERSION:
            raise ContainerArtifactError("container_artifact_bundle_schema_unsupported")
        _require_sha256(self.policy_digest, "container_artifact_policy_digest_invalid")
        _unique(self.artifact_ids, "container_artifact_inspection_ids_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_digest": self.policy_digest,
            "artifact_ids": list(self.artifact_ids),
            "outcome": "pass",
        }


@dataclass(frozen=True)
class ArtifactBundle:
    """Verified immutable descriptors returned after a build adapter completes."""

    plan: ArtifactBuildPlan
    materializations: tuple[ArtifactMaterialization, ...]
    inspection: ArtifactInspectionReceipt

    def __post_init__(self) -> None:
        planned = {artifact.artifact_id: artifact for artifact in self.plan.artifacts}
        actual = {item.artifact_id: item for item in self.materializations}
        if len(actual) != len(self.materializations) or set(actual) != set(planned):
            raise ContainerArtifactError("container_artifact_evidence_incomplete")
        for artifact_id, materialization in actual.items():
            descriptor = planned[artifact_id]
            materialization.evidence_receipt.validate_against(self.plan, descriptor)
        if self.inspection.policy_digest != self.plan.policy_digest:
            raise ContainerArtifactError("container_artifact_inspection_policy_mismatch")
        if self.inspection.artifact_ids != tuple(
            artifact.artifact_id for artifact in self.plan.artifacts
        ):
            raise ContainerArtifactError("container_artifact_inspection_ids_mismatch")

    def inspection_projection(self) -> dict[str, object]:
        """Return a safe, read-only view for a host controller or dashboard."""

        return {
            "schema_version": CONTAINER_ARTIFACT_BUNDLE_SCHEMA_VERSION,
            "policy_digest": self.plan.policy_digest,
            "descriptors": [item.to_dict() for item in self.plan.artifacts],
            "artifacts": [item.to_dict() for item in self.materializations],
            "inspection": self.inspection.to_dict(),
        }


class ArtifactBuildExecutor(Protocol):
    """A build-time adapter; implementations may be Buildx, CI, or a test fake."""

    def materialize(
        self, plan: ArtifactBuildPlan, artifact: ArtifactDescriptor
    ) -> ArtifactMaterialization:
        """Build and inspect one descriptor with its pinned policy context."""


class ContainerArtifactCompiler:
    """Compile and inspect the complete OCI artifact policy behind two methods."""

    def __init__(self, repository_root: Path | None = None) -> None:
        self._repository_root = repository_root

    def prepare(self, request: ArtifactRequest) -> ArtifactBuildPlan:
        """Return the complete role/platform/variant matrix without side effects."""

        recipe_policy = StaticRecipePolicyValidator(self._repository_root).validate()
        artifacts = tuple(self._matrix(request, recipe_policy))
        payload = {
            "schema_version": CONTAINER_ARTIFACT_SCHEMA_VERSION,
            "request": request.to_dict(),
            "recipe_policy": recipe_policy.to_dict(),
            "artifacts": [artifact.to_dict() for artifact in artifacts],
        }
        return ArtifactBuildPlan(
            request=request,
            recipe_policy=recipe_policy,
            artifacts=artifacts,
            policy_digest=_sha256(payload),
        )

    def materialize(
        self, plan: ArtifactBuildPlan, executor: ArtifactBuildExecutor
    ) -> ArtifactBundle:
        """Delegate builds to one adapter and validate the full immutable evidence set."""

        materializations = tuple(
            executor.materialize(plan, artifact) for artifact in plan.artifacts
        )
        inspection = ArtifactInspectionReceipt(
            policy_digest=plan.policy_digest,
            artifact_ids=tuple(artifact.artifact_id for artifact in plan.artifacts),
        )
        return ArtifactBundle(
            plan=plan,
            materializations=materializations,
            inspection=inspection,
        )

    @staticmethod
    def _matrix(
        request: ArtifactRequest, recipe_policy: RecipePolicyReceipt
    ) -> list[ArtifactDescriptor]:
        descriptors: list[ArtifactDescriptor] = []
        for platform in request.platforms:
            descriptors.append(_edge_descriptor(platform, recipe_policy))
            descriptors.append(_backend_descriptor(platform, recipe_policy))
            for variant in request.compute_variants:
                if variant == COMPUTE_VARIANT_CUDA and platform not in _CUDA_PLATFORMS:
                    continue
                descriptors.append(_compute_descriptor(platform, variant, recipe_policy))
        return descriptors


def _artifact_id(role: str, platform: str, variant: str) -> str:
    return "-".join((role.removeprefix("pp-"), platform.replace("/", "-"), variant))


def _edge_descriptor(platform: str, recipe_policy: RecipePolicyReceipt) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        artifact_id=_artifact_id(PP_LOCAL_EDGE, platform, STANDARD_VARIANT),
        role=PP_LOCAL_EDGE,
        platform=platform,
        variant=STANDARD_VARIANT,
        entrypoint="plastic-promise-local-edge",
        listener_scope="loopback",
        mounts=(ArtifactMount("edge-session-cache", "read-write", "bounded-edge-cache"),),
        capabilities=(),
        authorities=("loopback-status-projection",),
        base_image_reference=recipe_policy.base_image_for(
            PP_LOCAL_EDGE, STANDARD_VARIANT, platform
        ).reference,
    )


def _backend_descriptor(platform: str, recipe_policy: RecipePolicyReceipt) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        artifact_id=_artifact_id(PP_SERVER_BACKEND, platform, STANDARD_VARIANT),
        role=PP_SERVER_BACKEND,
        platform=platform,
        variant=STANDARD_VARIANT,
        entrypoint="plastic-promise-canonical-runtime",
        listener_scope="private",
        mounts=(
            ArtifactMount("canonical-state", "read-write", "server-owned-canonical-state"),
            ArtifactMount("backend-tmp", "tmpfs", "bounded-backend-tmp"),
        ),
        capabilities=(),
        authorities=(
            "canonical-sqlite-single-writer",
            "lancedb-promotion-decision",
            "deployment-receipt-persistence",
            _COLLABORATION_EVENT_WRITER_AUTHORITY,
        ),
        base_image_reference=recipe_policy.base_image_for(
            PP_SERVER_BACKEND, STANDARD_VARIANT, platform
        ).reference,
    )


def _compute_descriptor(
    platform: str, variant: str, recipe_policy: RecipePolicyReceipt
) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        artifact_id=_artifact_id(PP_COMPUTE_NODE, platform, variant),
        role=PP_COMPUTE_NODE,
        platform=platform,
        variant=variant,
        entrypoint="plastic-promise-local-inference-node",
        listener_scope="loopback",
        mounts=(
            ArtifactMount("model-catalog", "read-only", "model-catalog-materialized"),
            ArtifactMount("node-runtime", "read-write", "bounded-node-runtime"),
            ArtifactMount("node-tmp", "tmpfs", "bounded-node-tmp"),
        ),
        capabilities=_COMPUTE_CAPABILITY_CONTRACTS,
        authorities=("typed-derived-inference",),
        base_image_reference=recipe_policy.base_image_for(
            PP_COMPUTE_NODE, variant, platform
        ).reference,
    )


__all__ = [
    "CONTAINER_ARTIFACT_BUNDLE_SCHEMA_VERSION",
    "CONTAINER_ARTIFACT_EVIDENCE_SCHEMA_VERSION",
    "CONTAINER_ARTIFACT_RECIPE_POLICY_SCHEMA_VERSION",
    "CONTAINER_ARTIFACT_SCHEMA_VERSION",
    "OCI_BASE_IMAGE_CATALOG_SCHEMA_VERSION",
    "COMPUTE_VARIANT_CPU",
    "COMPUTE_VARIANT_CUDA",
    "STANDARD_VARIANT",
    "ArtifactBuildExecutor",
    "ArtifactBuildPlan",
    "ArtifactBundle",
    "ArtifactCollaborationSurface",
    "ArtifactDescriptor",
    "ArtifactEvidenceReceipt",
    "ArtifactInspectionReceipt",
    "ArtifactMaterialization",
    "ArtifactMount",
    "ArtifactRequest",
    "BaseImageIdentity",
    "ContainerArtifactCompiler",
    "ContainerArtifactError",
    "RecipePolicyReceipt",
    "RecipeSourceDigest",
    "StaticRecipePolicyValidator",
]

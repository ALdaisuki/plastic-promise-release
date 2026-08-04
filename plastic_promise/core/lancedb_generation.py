"""Fail-closed lifecycle management for immutable LanceDB generations.

This module deliberately does not open LanceDB or select a production index. It
manages offline generation artifacts below a private root and requires an
application-supplied ``ArtifactVerifier`` before a build can complete or a
generation can be selected. Filesystem operations are anchored to stable
directory descriptors so path replacement cannot redirect manifest, cleanup,
or current-pointer operations.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from packaging.version import InvalidVersion, Version

from plastic_promise.core.fusion_policy import validate_fusion_candidate_binding
from plastic_promise.core.recall_experiment import (
    MAX_V1_CONTROL_ALGORITHM,
    MAX_V1_CONTROL_DATASET_SOURCE,
    MAX_V1_CONTROL_EMBEDDING_DIMENSION,
    MAX_V1_CONTROL_INDEX_TEXT_POLICY,
    MAX_V1_CONTROL_POLICY,
    MAX_V1_CONTROL_RETRIEVAL_CONFIGURATION,
    MAX_V1_CONTROL_RUNTIME,
    MAX_V1_CONTROL_RUNTIME_ROUTE,
    MINIMUM_DEPENDENCY_VERSIONS,
    RECALL_QUALITY_REPORT_SCHEMA,
    heldout_report_contract,
)
from plastic_promise.core.recall_quality import (
    DATASET_SCHEMA_VERSION as RECALL_QUALITY_DATASET_SCHEMA,
)
from plastic_promise.core.recall_quality_environment import canonical_embedding_provider

MANIFEST_SCHEMA = "plastic-promise/lancedb-generation/v2"
QUALITY_REPORT_SCHEMA = "plastic-promise/lancedb-quality/v1"
QUALITY_GATE_POLICY = "plastic-promise/lancedb-promotion-gate/v1"
MANIFEST_NAME = "manifest.json"
INDEX_DIRECTORY = "index"
CURRENT_LINK = "current"
GENERATIONS_DIRECTORY = "generations"
SELECTIONS_DIRECTORY = "selections"
INDEX_MATERIAL_SCHEMA = "plastic-promise/lancedb-index-material/v1"

MIN_HIT_AT_1 = 0.01
MIN_HIT_AT_5 = 0.05
MIN_MRR = 0.01
MAX_P95_MS = 5_000.0

_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_ARTIFACT_FILE_BYTES = 8 * 1024 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_GENERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ACTIVATION_ID = re.compile(r"[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_INDEX_TEXT_POLICIES = frozenset({"legacy", "compact-v2"})
_RESERVED_GENERATION_IDS = {CURRENT_LINK, GENERATIONS_DIRECTORY, SELECTIONS_DIRECTORY}
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class GenerationError(RuntimeError):
    """Base error for invalid or unsafe generation operations."""


class ManifestError(GenerationError):
    """A generation manifest is missing, malformed, or inconsistent."""


class PromotionError(GenerationError):
    """A generation cannot be promoted or selected for rollback."""


@dataclass(frozen=True)
class GenerationSpec:
    """Immutable identity and canonical-source binding for one full build."""

    generation_id: str
    index_schema: str
    embedding_model: str
    model_revision: str
    embedding_dimension: int
    source_db_sha256: str
    source_row_count: int
    benchmark_corpus_sha256: str
    benchmark_corpus_count: int
    benchmark_cases_sha256: str
    benchmark_case_count: int
    index_outbox_watermark: int | None = None
    index_outbox_digest: str | None = None
    index_outbox_job_count: int | None = None
    index_outbox_source_fingerprint: str | None = None
    # Full identity of the embedder that owns derived index material.  The
    # base model/revision fields remain the quality-report contract; this
    # optional value binds endpoint, provider, and chunking decorations without
    # changing the manifest top-level schema.
    embedding_index_identity: str | None = None
    # Policy and canonical indexed-text digest are a pair. Older manifests may
    # omit both, but a new source-bound generation must never declare one
    # without the other.
    index_text_policy: str | None = None
    index_material_sha256: str | None = None

    def __post_init__(self) -> None:
        _validate_generation_id(self.generation_id)
        _validate_identity_text("index_schema", self.index_schema)
        _validate_identity_text("embedding_model", self.embedding_model)
        _validate_identity_text("model_revision", self.model_revision)
        _validate_positive_integer("embedding_dimension", self.embedding_dimension)
        if not isinstance(self.source_db_sha256, str) or not _SHA256.fullmatch(
            self.source_db_sha256
        ):
            raise ValueError("invalid_source_db_sha256")
        _validate_nonnegative_integer("source_row_count", self.source_row_count)
        if not isinstance(self.benchmark_corpus_sha256, str) or not _SHA256.fullmatch(
            self.benchmark_corpus_sha256
        ):
            raise ValueError("invalid_benchmark_corpus_sha256")
        _validate_positive_integer("benchmark_corpus_count", self.benchmark_corpus_count)
        if not isinstance(self.benchmark_cases_sha256, str) or not _SHA256.fullmatch(
            self.benchmark_cases_sha256
        ):
            raise ValueError("invalid_benchmark_cases_sha256")
        _validate_positive_integer("benchmark_case_count", self.benchmark_case_count)
        _validate_outbox_identity(
            self.index_outbox_watermark,
            self.index_outbox_digest,
            self.index_outbox_job_count,
        )
        if self.index_outbox_source_fingerprint is not None and (
            not isinstance(self.index_outbox_source_fingerprint, str)
            or _SHA256.fullmatch(self.index_outbox_source_fingerprint) is None
            or self.index_outbox_watermark is None
        ):
            raise ValueError("invalid_index_outbox_source_fingerprint")
        if self.embedding_index_identity is not None and self.index_outbox_watermark is None:
            raise ValueError("invalid_embedding_index_identity")
        if self.embedding_index_identity is not None:
            _validate_identity_text("embedding_index_identity", self.embedding_index_identity)
        _validate_index_material_binding(
            self.index_text_policy,
            self.index_material_sha256,
        )

    @property
    def identity_sha256(self) -> str:
        identity = {
            "generation_id": self.generation_id,
            "index_schema": self.index_schema,
            "embedding_model": self.embedding_model,
            "model_revision": self.model_revision,
            "embedding_dimension": self.embedding_dimension,
            "source_db_sha256": self.source_db_sha256,
            "source_row_count": self.source_row_count,
            "benchmark_corpus_sha256": self.benchmark_corpus_sha256,
            "benchmark_corpus_count": self.benchmark_corpus_count,
            "benchmark_cases_sha256": self.benchmark_cases_sha256,
            "benchmark_case_count": self.benchmark_case_count,
            "index_outbox_watermark": self.index_outbox_watermark,
            "index_outbox_digest": self.index_outbox_digest,
            "index_outbox_job_count": self.index_outbox_job_count,
        }
        # Preserve the exact identity shape of manifests written before the
        # logical source fingerprint extension.
        if self.index_outbox_source_fingerprint is not None:
            identity["index_outbox_source_fingerprint"] = self.index_outbox_source_fingerprint
        if self.embedding_index_identity is not None:
            identity["embedding_index_identity"] = self.embedding_index_identity
        if self.index_text_policy is not None:
            identity["index_text_policy"] = self.index_text_policy
            identity["index_material_sha256"] = self.index_material_sha256
        return _json_sha256(identity)


@dataclass(frozen=True)
class QualityReportGenerationIdentity:
    """Generation identity attested by normalized benchmark evidence."""

    embedding_model: str
    model_revision: str
    embedding_dimension: int
    index_text_policy: str
    benchmark_corpus_sha256: str
    benchmark_corpus_count: int
    benchmark_cases_sha256: str
    benchmark_case_count: int

    def __post_init__(self) -> None:
        _validate_identity_text("embedding_model", self.embedding_model)
        _validate_identity_text("model_revision", self.model_revision)
        _validate_positive_integer("embedding_dimension", self.embedding_dimension)
        _validate_index_text_policy(self.index_text_policy)
        if not _is_sha256(self.benchmark_corpus_sha256):
            raise ValueError("invalid_benchmark_corpus_sha256")
        _validate_positive_integer("benchmark_corpus_count", self.benchmark_corpus_count)
        if not _is_sha256(self.benchmark_cases_sha256):
            raise ValueError("invalid_benchmark_cases_sha256")
        _validate_positive_integer("benchmark_case_count", self.benchmark_case_count)


@dataclass(frozen=True)
class BuildResult:
    """Evidence returned by an isolated generation build callback."""

    row_count: int
    quality_report: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        _validate_nonnegative_integer("built_row_count", self.row_count)
        if self.quality_report is not None and not isinstance(self.quality_report, Mapping):
            raise ValueError("invalid_quality_report")


@dataclass(frozen=True)
class ArtifactVerificationRequest:
    """Pinned descriptor and expected identity supplied to an offline verifier.

    ``index_fd`` is a duplicate owned by the manager and is valid only for the
    duration of the verifier call. A verifier must use descriptor-relative,
    no-follow reads; it must not reopen the public generation path.
    """

    index_fd: int
    generation_id: str
    index_schema: str
    embedding_model: str
    model_revision: str
    embedding_dimension: int
    expected_tree_sha256: str
    index_text_policy: str | None = None
    expected_index_material_sha256: str | None = None

    def __post_init__(self) -> None:
        _validate_index_material_binding(
            self.index_text_policy,
            self.expected_index_material_sha256,
        )


@dataclass(frozen=True)
class ArtifactVerification:
    """Identity and actual row count independently observed in an artifact."""

    row_count: int
    index_schema: str
    embedding_model: str
    model_revision: str
    embedding_dimension: int
    index_material_sha256: str | None = None

    def __post_init__(self) -> None:
        _validate_nonnegative_integer("artifact_row_count", self.row_count)
        _validate_identity_text("artifact_index_schema", self.index_schema)
        _validate_identity_text("artifact_embedding_model", self.embedding_model)
        _validate_identity_text("artifact_model_revision", self.model_revision)
        _validate_positive_integer("artifact_embedding_dimension", self.embedding_dimension)
        if self.index_material_sha256 is not None and not _is_sha256(self.index_material_sha256):
            raise ValueError("invalid_artifact_index_material_sha256")


class ArtifactVerifier(Protocol):
    """Offline verifier contract; production adapters live outside this module."""

    def __call__(self, request: ArtifactVerificationRequest) -> ArtifactVerification: ...


@dataclass(frozen=True)
class GenerationManifest:
    """Persisted lifecycle, content binding, and verification record."""

    generation_id: str
    index_schema: str
    embedding_model: str
    model_revision: str
    embedding_dimension: int
    source_db_sha256: str
    source_row_count: int
    benchmark_corpus_sha256: str
    benchmark_corpus_count: int
    benchmark_cases_sha256: str
    benchmark_case_count: int
    identity_sha256: str
    build_status: str
    built_row_count: int | None
    index_tree_sha256: str | None
    quality_report: dict[str, Any] | None
    quality_report_sha256: str | None
    verification_status: str
    created_at: str
    completed_at: str | None
    verified_at: str | None
    manifest_sha256: str
    manifest_schema: str = MANIFEST_SCHEMA
    index_outbox: dict[str, Any] | None = None
    index_text_policy: str | None = None
    index_material_sha256: str | None = None
    # The outbox extension was added without changing MANIFEST_SCHEMA. Keep a
    # private marker so manifests written before that extension can retain and
    # validate their original hashes instead of being silently re-sealed.
    _legacy_without_outbox: bool = field(default=False, repr=False, compare=False)
    # The index-material extension likewise retained MANIFEST_SCHEMA. Preserve
    # the exact serialized shape and digest of manifests written before it.
    _legacy_without_index_material: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def building(cls, spec: GenerationSpec) -> GenerationManifest:
        index_outbox = None
        if spec.index_outbox_watermark is not None:
            index_outbox = {
                "watermark": spec.index_outbox_watermark,
                "immutable_digest": spec.index_outbox_digest,
                "job_count": spec.index_outbox_job_count,
                "reconciled": False,
            }
            if spec.index_outbox_source_fingerprint is not None:
                index_outbox["source_fingerprint"] = spec.index_outbox_source_fingerprint
            if spec.embedding_index_identity is not None:
                index_outbox["embedding_index_identity"] = spec.embedding_index_identity
        manifest = cls(
            generation_id=spec.generation_id,
            index_schema=spec.index_schema,
            embedding_model=spec.embedding_model,
            model_revision=spec.model_revision,
            embedding_dimension=spec.embedding_dimension,
            source_db_sha256=spec.source_db_sha256,
            source_row_count=spec.source_row_count,
            benchmark_corpus_sha256=spec.benchmark_corpus_sha256,
            benchmark_corpus_count=spec.benchmark_corpus_count,
            benchmark_cases_sha256=spec.benchmark_cases_sha256,
            benchmark_case_count=spec.benchmark_case_count,
            identity_sha256=spec.identity_sha256,
            build_status="building",
            built_row_count=None,
            index_tree_sha256=None,
            quality_report=None,
            quality_report_sha256=None,
            verification_status="unverified",
            created_at=_utc_now(),
            completed_at=None,
            verified_at=None,
            manifest_sha256="",
            index_outbox=index_outbox,
            index_text_policy=spec.index_text_policy,
            index_material_sha256=spec.index_material_sha256,
        )
        return manifest.reseal()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> GenerationManifest:
        expected = {
            "manifest_schema",
            "generation_id",
            "index_schema",
            "embedding_model",
            "model_revision",
            "embedding_dimension",
            "source_db_sha256",
            "source_row_count",
            "benchmark_corpus_sha256",
            "benchmark_corpus_count",
            "benchmark_cases_sha256",
            "benchmark_case_count",
            "identity_sha256",
            "build_status",
            "built_row_count",
            "index_tree_sha256",
            "quality_report",
            "quality_report_sha256",
            "verification_status",
            "created_at",
            "completed_at",
            "verified_at",
            "manifest_sha256",
            "index_outbox",
            "index_text_policy",
            "index_material_sha256",
        }
        # Accept manifests written before the outbox evidence extension. The
        # old identity and manifest digests intentionally did not include a
        # null index_outbox field, so validation must preserve that shape.
        legacy_without_outbox = "index_outbox" not in payload
        if legacy_without_outbox:
            payload = dict(payload)
            payload["index_outbox"] = None
        material_fields = {"index_text_policy", "index_material_sha256"}
        present_material_fields = material_fields.intersection(payload)
        legacy_without_index_material = not present_material_fields
        if present_material_fields and present_material_fields != material_fields:
            raise ManifestError("invalid_manifest_fields")
        if present_material_fields and any(payload.get(name) is None for name in material_fields):
            raise ManifestError("invalid_manifest_values")
        if legacy_without_index_material:
            payload = dict(payload)
            payload["index_text_policy"] = None
            payload["index_material_sha256"] = None
        if set(payload) != expected:
            raise ManifestError("invalid_manifest_fields")
        try:
            manifest = cls(
                **{key: payload[key] for key in expected},
                _legacy_without_outbox=legacy_without_outbox,
                _legacy_without_index_material=legacy_without_index_material,
            )
        except (TypeError, ValueError) as exc:
            raise ManifestError("invalid_manifest_values") from exc
        manifest.validate()
        return manifest

    @property
    def spec(self) -> GenerationSpec:
        if self.index_outbox is not None and not isinstance(self.index_outbox, Mapping):
            raise ValueError("invalid_index_outbox_evidence")
        return GenerationSpec(
            generation_id=self.generation_id,
            index_schema=self.index_schema,
            embedding_model=self.embedding_model,
            model_revision=self.model_revision,
            embedding_dimension=self.embedding_dimension,
            source_db_sha256=self.source_db_sha256,
            source_row_count=self.source_row_count,
            benchmark_corpus_sha256=self.benchmark_corpus_sha256,
            benchmark_corpus_count=self.benchmark_corpus_count,
            benchmark_cases_sha256=self.benchmark_cases_sha256,
            benchmark_case_count=self.benchmark_case_count,
            index_outbox_watermark=(
                self.index_outbox.get("watermark") if self.index_outbox else None
            ),
            index_outbox_digest=(
                self.index_outbox.get("immutable_digest") if self.index_outbox else None
            ),
            index_outbox_job_count=(
                self.index_outbox.get("job_count") if self.index_outbox else None
            ),
            index_outbox_source_fingerprint=(
                self.index_outbox.get("source_fingerprint") if self.index_outbox else None
            ),
            embedding_index_identity=(
                self.index_outbox.get("embedding_index_identity") if self.index_outbox else None
            ),
            index_text_policy=self.index_text_policy,
            index_material_sha256=self.index_material_sha256,
        )

    def reseal(self) -> GenerationManifest:
        unsigned = replace(self, manifest_sha256="")
        return replace(unsigned, manifest_sha256=_json_sha256(unsigned._binding_dict()))

    def validate(self) -> None:
        if self.manifest_schema != MANIFEST_SCHEMA:
            raise ManifestError("unsupported_manifest_schema")
        try:
            spec = self.spec
        except ValueError as exc:
            raise ManifestError(str(exc)) from exc
        try:
            _validate_outbox_evidence(self.index_outbox, generation_id=self.generation_id)
        except ValueError as exc:
            raise ManifestError(str(exc)) from exc
        expected_identity_sha256 = (
            _legacy_generation_identity_sha256(spec)
            if self._legacy_without_outbox
            else spec.identity_sha256
        )
        if self.identity_sha256 != expected_identity_sha256:
            raise ManifestError("manifest_identity_mismatch")
        if self.build_status not in {"building", "complete"}:
            raise ManifestError("invalid_build_status")
        if self.verification_status not in {"unverified", "verified"}:
            raise ManifestError("invalid_verification_status")
        _validate_timestamp("created_at", self.created_at, required=True)

        if self.build_status == "building":
            if any(
                value is not None
                for value in (
                    self.built_row_count,
                    self.index_tree_sha256,
                    self.quality_report,
                    self.quality_report_sha256,
                    self.completed_at,
                )
            ):
                raise ManifestError("building_manifest_has_completion_evidence")
            if self.verification_status != "unverified" or self.verified_at is not None:
                raise ManifestError("building_manifest_has_verification_evidence")
        else:
            try:
                _validate_nonnegative_integer("built_row_count", self.built_row_count)
            except ValueError as exc:
                raise ManifestError(str(exc)) from exc
            if not isinstance(self.index_tree_sha256, str) or not _SHA256.fullmatch(
                self.index_tree_sha256
            ):
                raise ManifestError("invalid_index_tree_sha256")
            if not isinstance(self.quality_report, dict):
                raise ManifestError("invalid_quality_report")
            if not isinstance(self.quality_report_sha256, str) or not _SHA256.fullmatch(
                self.quality_report_sha256
            ):
                raise ManifestError("invalid_quality_report_sha256")
            try:
                _validate_quality_report(self.quality_report, spec=spec)
                quality_sha256 = _json_sha256(self.quality_report)
            except (PromotionError, TypeError, ValueError) as exc:
                raise ManifestError(str(exc)) from exc
            if quality_sha256 != self.quality_report_sha256:
                raise ManifestError("quality_report_digest_mismatch")
            _validate_timestamp("completed_at", self.completed_at, required=True)

        if self.verification_status == "verified":
            if self.build_status != "complete":
                raise ManifestError("verified_generation_not_complete")
            _validate_timestamp("verified_at", self.verified_at, required=True)
        elif self.verified_at is not None:
            raise ManifestError("unverified_generation_has_verified_at")

        if not isinstance(self.manifest_sha256, str) or not _SHA256.fullmatch(self.manifest_sha256):
            raise ManifestError("invalid_manifest_sha256")
        expected_manifest_sha256 = _json_sha256(replace(self, manifest_sha256="")._binding_dict())
        if self.manifest_sha256 != expected_manifest_sha256:
            raise ManifestError("manifest_digest_mismatch")

    def _binding_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("manifest_sha256", None)
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "manifest_schema": self.manifest_schema,
            "generation_id": self.generation_id,
            "index_schema": self.index_schema,
            "embedding_model": self.embedding_model,
            "model_revision": self.model_revision,
            "embedding_dimension": self.embedding_dimension,
            "source_db_sha256": self.source_db_sha256,
            "source_row_count": self.source_row_count,
            "benchmark_corpus_sha256": self.benchmark_corpus_sha256,
            "benchmark_corpus_count": self.benchmark_corpus_count,
            "benchmark_cases_sha256": self.benchmark_cases_sha256,
            "benchmark_case_count": self.benchmark_case_count,
            "identity_sha256": self.identity_sha256,
            "build_status": self.build_status,
            "built_row_count": self.built_row_count,
            "index_tree_sha256": self.index_tree_sha256,
            "quality_report": self.quality_report,
            "quality_report_sha256": self.quality_report_sha256,
            "verification_status": self.verification_status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "verified_at": self.verified_at,
            "manifest_sha256": self.manifest_sha256,
            "index_outbox": self.index_outbox,
            "index_text_policy": self.index_text_policy,
            "index_material_sha256": self.index_material_sha256,
        }
        if self._legacy_without_outbox:
            payload.pop("index_outbox", None)
        if self._legacy_without_index_material:
            payload.pop("index_text_policy", None)
            payload.pop("index_material_sha256", None)
        return payload


BuildCallback = Callable[[Path], BuildResult]
RuntimeEnvironmentValidator = Callable[[GenerationSpec, Mapping[str, Any]], None]


@dataclass(frozen=True)
class _CurrentState:
    generation_id: str
    current_target: str
    current_identity: tuple[int, int, int]
    activation_id: str | None


class GenerationManager:
    """Build, validate, promote, and roll back isolated index generations."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        create: bool = True,
        artifact_verifier: ArtifactVerifier | None = None,
        runtime_environment_validator: RuntimeEnvironmentValidator | None = None,
    ) -> None:
        if artifact_verifier is not None and not callable(artifact_verifier):
            raise TypeError("artifact_verifier_must_be_callable")
        if runtime_environment_validator is not None and not callable(
            runtime_environment_validator
        ):
            raise TypeError("runtime_environment_validator_must_be_callable")
        self._thread_lock = threading.RLock()
        self._lifecycle_depth = 0
        self._lifecycle_exclusive = False
        self._closed = False
        self._artifact_verifier = artifact_verifier
        self._runtime_environment_validator = runtime_environment_validator
        self.root, self._root_fd = _open_root_directory(root, create=create)
        self._generations_fd = -1
        self._selections_fd = -1
        self._selections_identity: tuple[int, int] | None = None
        try:
            self._root_identity = _directory_identity(os.fstat(self._root_fd))
            _require_private_directory(self._root_fd, "generation_root_insecure_permissions")
            if create:
                try:
                    os.mkdir(GENERATIONS_DIRECTORY, mode=0o700, dir_fd=self._root_fd)
                    _fsync_fd(self._root_fd, GenerationError, "generation_root_fsync_failed")
                except FileExistsError:
                    pass
            self._generations_fd = _open_directory_at(
                self._root_fd,
                GENERATIONS_DIRECTORY,
                "generations_root_not_found",
            )
            self._generations_identity = _directory_identity(os.fstat(self._generations_fd))
            _require_private_directory(
                self._generations_fd,
                "generations_root_insecure_permissions",
            )
        except BaseException:
            if self._selections_fd >= 0:
                os.close(self._selections_fd)
            if self._generations_fd >= 0:
                os.close(self._generations_fd)
            os.close(self._root_fd)
            self._closed = True
            raise
        self.generations_path = self.root / GENERATIONS_DIRECTORY
        self.current_path = self.root / CURRENT_LINK

    def close(self) -> None:
        with self._thread_lock:
            if self._closed:
                return
            self._closed = True
            if self._selections_fd >= 0:
                os.close(self._selections_fd)
                self._selections_fd = -1
            if self._generations_fd >= 0:
                os.close(self._generations_fd)
            os.close(self._root_fd)

    def __enter__(self) -> GenerationManager:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort descriptor hygiene.
        with suppress(Exception):
            self.close()

    def build_generation(
        self,
        spec: GenerationSpec,
        build_callback: BuildCallback,
    ) -> GenerationManifest:
        """Run a full offline build and verify it inside an inactive directory."""

        if not isinstance(spec, GenerationSpec):
            raise TypeError("spec_must_be_generation_spec")
        if not callable(build_callback):
            raise TypeError("build_callback_must_be_callable")
        if spec.index_text_policy is None or spec.index_material_sha256 is None:
            raise GenerationError("generation_index_material_binding_required")
        if self._artifact_verifier is None:
            raise GenerationError("artifact_verifier_required")
        with self._lifecycle_lock(exclusive=True):
            self._assert_anchors()
            if _lexists_at(self._generations_fd, spec.generation_id):
                raise GenerationError("generation_already_exists")
            try:
                os.mkdir(spec.generation_id, mode=0o700, dir_fd=self._generations_fd)
            except FileExistsError as exc:
                raise GenerationError("generation_already_exists") from exc
            generation_identity = _entry_identity_at(self._generations_fd, spec.generation_id)
            succeeded = False
            generation_fd = -1
            index_fd = -1
            try:
                generation_fd = _open_directory_at(
                    self._generations_fd,
                    spec.generation_id,
                    "generation_staging_replaced",
                )
                os.mkdir(INDEX_DIRECTORY, mode=0o700, dir_fd=generation_fd)
                index_fd = _open_directory_at(
                    generation_fd,
                    INDEX_DIRECTORY,
                    "generation_index_replaced",
                )
                building = GenerationManifest.building(spec)
                self._write_manifest_fd(generation_fd, building, expected_identity=None)

                self._assert_public_build_path(spec.generation_id, generation_fd, index_fd)
                result = build_callback(self._generation_path(spec.generation_id) / INDEX_DIRECTORY)
                if not isinstance(result, BuildResult):
                    raise GenerationError("build_callback_must_return_build_result")
                self._assert_anchors()
                _require_entry_identity(
                    self._generations_fd,
                    spec.generation_id,
                    os.fstat(generation_fd),
                    "generation_staging_replaced",
                )
                _require_entry_identity(
                    generation_fd,
                    INDEX_DIRECTORY,
                    os.fstat(index_fd),
                    "generation_index_replaced",
                )

                quality_report = _copy_json_mapping(result.quality_report)
                if quality_report is None:
                    raise GenerationError("quality_report_missing")
                try:
                    _validate_quality_report(quality_report, spec=spec)
                except PromotionError as exc:
                    raise GenerationError(str(exc)) from exc
                index_tree_sha256 = _index_tree_sha256(index_fd)
                self._verify_artifact(
                    index_fd,
                    spec=spec,
                    expected_row_count=result.row_count,
                    expected_tree_sha256=index_tree_sha256,
                    error_type=GenerationError,
                )
                completed = replace(
                    building,
                    build_status="complete",
                    built_row_count=result.row_count,
                    index_tree_sha256=index_tree_sha256,
                    quality_report=quality_report,
                    quality_report_sha256=_json_sha256(quality_report),
                    completed_at=_utc_now(),
                ).reseal()
                completed.validate()
                manifest_identity = _entry_identity_at(generation_fd, MANIFEST_NAME)
                self._write_manifest_fd(
                    generation_fd,
                    completed,
                    expected_identity=manifest_identity,
                )
                persisted = self._read_manifest_fd(generation_fd, index_fd, spec.generation_id)
                if persisted != completed:
                    raise GenerationError("manifest_persistence_mismatch")
                succeeded = True
                return completed
            finally:
                if index_fd >= 0:
                    os.close(index_fd)
                if generation_fd >= 0:
                    os.close(generation_fd)
                if not succeeded:
                    self._remove_failed_staging(spec.generation_id, generation_identity)

    def load_manifest(self, generation_id: str) -> GenerationManifest:
        with self._lifecycle_lock(exclusive=False):
            self._assert_anchors()
            manifest, generation_fd, index_fd = self._open_and_read_manifest(generation_id)
            os.close(index_fd)
            os.close(generation_fd)
            return manifest

    def list_manifests(self) -> list[GenerationManifest]:
        with self._lifecycle_lock(exclusive=False):
            self._assert_anchors()
            manifests: list[GenerationManifest] = []
            try:
                names = sorted(os.listdir(self._generations_fd))
            except OSError as exc:
                raise GenerationError("generations_root_unreadable") from exc
            for name in names:
                try:
                    _validate_generation_id(name)
                    entry = os.stat(name, dir_fd=self._generations_fd, follow_symlinks=False)
                except (OSError, ValueError) as exc:
                    raise GenerationError("invalid_generation_entry") from exc
                if not stat.S_ISDIR(entry.st_mode):
                    raise GenerationError("invalid_generation_entry")
                manifest, generation_fd, index_fd = self._open_and_read_manifest(name)
                os.close(index_fd)
                os.close(generation_fd)
                manifests.append(manifest)
            return manifests

    def verify_candidate(
        self,
        generation_id: str,
        *,
        connection: sqlite3.Connection | None = None,
        expected_embedding_index_identity: str | None = None,
    ) -> GenerationManifest:
        """Verify an inactive generation without changing the current pointer."""

        if expected_embedding_index_identity is not None:
            try:
                _validate_identity_text(
                    "expected_embedding_index_identity",
                    expected_embedding_index_identity,
                )
            except ValueError as exc:
                raise PromotionError(str(exc)) from exc

        transaction_started = False
        manifest_updated = False
        with self._lifecycle_lock(exclusive=True):
            self._assert_anchors()
            manifest, generation_fd, index_fd = self._open_and_read_manifest(generation_id)
            original_manifest = manifest

            def restore_manifest() -> None:
                nonlocal manifest_updated
                if not manifest_updated:
                    return
                try:
                    current_identity = _entry_identity_at(generation_fd, MANIFEST_NAME)
                    self._write_manifest_fd(
                        generation_fd,
                        original_manifest,
                        expected_identity=current_identity,
                    )
                    restored = self._read_manifest_fd(
                        generation_fd,
                        index_fd,
                        generation_id,
                    )
                    if restored != original_manifest:
                        raise PromotionError("verification_manifest_restore_mismatch")
                except Exception as restore_exc:
                    raise PromotionError("verification_state_restore_failed") from restore_exc
                manifest_updated = False

            try:
                if self._current_generation_id(required=False) == generation_id:
                    raise PromotionError("candidate_generation_must_be_inactive")
                if (
                    expected_embedding_index_identity is not None
                    and manifest.spec.embedding_index_identity != expected_embedding_index_identity
                ):
                    raise PromotionError("generation_embedding_index_identity_mismatch")
                transaction_started = self._begin_source_freshness_transaction(
                    manifest,
                    connection,
                    operation="verification",
                )
                self._validate_promotable(manifest, index_fd, require_reconciled=True)
                self._validate_current_runtime_environment(
                    manifest.spec,
                    manifest.quality_report,
                    operation="verification",
                )
                if manifest.verification_status != "verified":
                    manifest = replace(
                        manifest,
                        verification_status="verified",
                        verified_at=_utc_now(),
                    ).reseal()
                    manifest.validate()
                    manifest_identity = _entry_identity_at(generation_fd, MANIFEST_NAME)
                    self._write_manifest_fd(
                        generation_fd,
                        manifest,
                        expected_identity=manifest_identity,
                    )
                    manifest_updated = True
                    manifest = self._read_manifest_fd(generation_fd, index_fd, generation_id)
                    self._validate_promotable(manifest, index_fd, require_reconciled=True)
                    self._validate_current_runtime_environment(
                        manifest.spec,
                        manifest.quality_report,
                        operation="verification",
                    )
                if transaction_started:
                    try:
                        assert connection is not None
                        connection.commit()
                    except Exception as exc:
                        with suppress(BaseException):
                            assert connection is not None
                            connection.rollback()
                        transaction_started = False
                        restore_manifest()
                        raise PromotionError("verification_commit_failed") from exc
                    transaction_started = False
                return manifest
            except BaseException:
                if transaction_started:
                    with suppress(BaseException):
                        assert connection is not None
                        connection.rollback()
                    transaction_started = False
                restore_manifest()
                raise
            finally:
                os.close(index_fd)
                os.close(generation_fd)

    def promote(
        self,
        generation_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> GenerationManifest:
        transaction_started = False
        pointer_switched = False
        manifest_updated = False
        with self._lifecycle_lock(exclusive=True):
            self._assert_anchors()
            manifest, generation_fd, index_fd = self._open_and_read_manifest(generation_id)
            original_manifest = manifest
            original_current_state: _CurrentState | None = None
            newly_verified = manifest.verification_status != "verified"

            def restore_manifest() -> None:
                nonlocal manifest_updated
                if not manifest_updated:
                    return
                try:
                    current_identity = _entry_identity_at(generation_fd, MANIFEST_NAME)
                    self._write_manifest_fd(
                        generation_fd,
                        original_manifest,
                        expected_identity=current_identity,
                    )
                    restored = self._read_manifest_fd(
                        generation_fd,
                        index_fd,
                        generation_id,
                    )
                    if restored != original_manifest:
                        raise PromotionError("promotion_manifest_restore_mismatch")
                except Exception as restore_exc:
                    raise PromotionError("promotion_state_restore_failed") from restore_exc
                manifest_updated = False

            def restore_pointer() -> None:
                nonlocal pointer_switched
                if not pointer_switched:
                    return
                try:
                    self._restore_current_state(original_current_state)
                except Exception as restore_exc:
                    raise PromotionError("promotion_state_restore_failed") from restore_exc
                pointer_switched = False

            try:
                original_current_state = self._current_state(required=False)
                # Never move the current pointer to a source-bound generation
                # unless the SQLite source is still covered.  The writer lock
                # remains held through the pointer switch and commit.
                transaction_started = self._begin_source_freshness_transaction(
                    manifest,
                    connection,
                    operation="promotion",
                )
                self._validate_promotable(manifest, index_fd, require_reconciled=True)
                if newly_verified:
                    manifest = replace(
                        manifest,
                        verification_status="verified",
                        verified_at=_utc_now(),
                    ).reseal()
                    manifest.validate()
                    manifest_identity = _entry_identity_at(generation_fd, MANIFEST_NAME)
                    self._write_manifest_fd(
                        generation_fd,
                        manifest,
                        expected_identity=manifest_identity,
                    )
                    manifest_updated = True
                    manifest = self._read_manifest_fd(generation_fd, index_fd, generation_id)
                    self._validate_promotable(manifest, index_fd, require_reconciled=True)
                self._validate_current_runtime_environment(
                    manifest.spec,
                    manifest.quality_report,
                    operation="promotion",
                )
                self._atomic_switch(generation_id)
                pointer_switched = True
                if transaction_started:
                    try:
                        assert connection is not None
                        connection.commit()
                    except Exception as exc:
                        with suppress(BaseException):
                            assert connection is not None
                            connection.rollback()
                        transaction_started = False
                        restore_pointer()
                        restore_manifest()
                        raise PromotionError("promotion_commit_failed") from exc
                    transaction_started = False
                return manifest
            except BaseException:
                if transaction_started:
                    with suppress(BaseException):
                        assert connection is not None
                        connection.rollback()
                    transaction_started = False
                restore_pointer()
                restore_manifest()
                raise
            finally:
                os.close(index_fd)
                os.close(generation_fd)

    def rollback(
        self,
        generation_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> GenerationManifest:
        transaction_started = False
        pointer_switched = False
        with self._lifecycle_lock(exclusive=True):
            self._assert_anchors()
            manifest, generation_fd, index_fd = self._open_and_read_manifest(generation_id)
            original_current_state: _CurrentState | None = None

            def restore_pointer() -> None:
                nonlocal pointer_switched
                if not pointer_switched:
                    return
                try:
                    self._restore_current_state(original_current_state)
                except Exception as restore_exc:
                    raise PromotionError("rollback_state_restore_failed") from restore_exc
                pointer_switched = False

            try:
                original_current_state = self._current_state(required=False)
                if manifest.verification_status != "verified":
                    raise PromotionError("generation_not_verified")
                transaction_started = self._begin_source_freshness_transaction(
                    manifest,
                    connection,
                    operation="rollback",
                )
                self._validate_promotable(manifest, index_fd, require_reconciled=True)
                self._validate_current_runtime_environment(
                    manifest.spec,
                    manifest.quality_report,
                    operation="rollback",
                )
                self._atomic_switch(generation_id)
                pointer_switched = True
                if transaction_started:
                    try:
                        assert connection is not None
                        connection.commit()
                    except Exception as exc:
                        with suppress(BaseException):
                            assert connection is not None
                            connection.rollback()
                        transaction_started = False
                        restore_pointer()
                        raise PromotionError("rollback_commit_failed") from exc
                    transaction_started = False
                return manifest
            except BaseException:
                if transaction_started:
                    with suppress(BaseException):
                        assert connection is not None
                        connection.rollback()
                    transaction_started = False
                restore_pointer()
                raise
            finally:
                os.close(index_fd)
                os.close(generation_fd)

    def mark_reconciled(
        self,
        generation_id: str,
        receipt: Mapping[str, Any],
        *,
        connection: sqlite3.Connection,
    ) -> GenerationManifest:
        """Persist an explicit SQLite/index-outbox reconciliation receipt."""

        if not isinstance(receipt, Mapping):
            raise PromotionError("reconciliation_receipt_invalid")
        if not isinstance(connection, sqlite3.Connection) or connection.in_transaction:
            raise PromotionError("reconciliation_database_required")
        from plastic_promise.core.index_outbox_reconciliation import (
            assert_index_outbox_fresh,
            assert_reconciliation_receipt_persisted,
            canonical_source_fingerprint,
            validate_reconciliation_receipt,
        )

        transaction_started = False
        manifest_updated = False
        with self._lifecycle_lock(exclusive=True):
            self._assert_anchors()
            manifest, generation_fd, index_fd = self._open_and_read_manifest(generation_id)
            original_manifest = manifest

            def restore_manifest() -> None:
                """Restore the pre-reconciliation manifest after a late failure."""

                nonlocal manifest_updated
                if not manifest_updated:
                    return
                try:
                    current_identity = _entry_identity_at(generation_fd, MANIFEST_NAME)
                    self._write_manifest_fd(
                        generation_fd,
                        original_manifest,
                        expected_identity=current_identity,
                    )
                    restored = self._read_manifest_fd(
                        generation_fd,
                        index_fd,
                        generation_id,
                    )
                    if restored != original_manifest:
                        raise PromotionError("reconciliation_manifest_restore_mismatch")
                except Exception as restore_exc:
                    raise PromotionError("reconciliation_state_restore_failed") from restore_exc
                manifest_updated = False

            try:
                evidence = manifest.index_outbox
                if evidence is None:
                    raise PromotionError("generation_has_no_outbox_evidence")

                # ``reconcile_index_outbox`` commits before this filesystem
                # manifest update.  Take a writer lock and re-check the full
                # watermark while holding it, so a newer job cannot arrive
                # between the two operations and leave a stale manifest marked
                # as reconciled.  Manifests written by the pre-fingerprint
                # manager may be exercised with a metadata-only test database;
                # preserve that legacy compatibility only when no outbox table
                # exists at all.  A real generation always has this table.
                try:
                    has_outbox_table = (
                        connection.execute(
                            "SELECT 1 FROM sqlite_master "
                            "WHERE type = 'table' AND name = 'store_outbox'"
                        ).fetchone()
                        is not None
                    )
                except sqlite3.Error as exc:
                    raise PromotionError("reconciliation_database_unreadable") from exc
                if has_outbox_table:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        transaction_started = True
                        freshness_evidence = dict(evidence)
                        freshness_evidence["reconciled"] = True
                        assert_index_outbox_fresh(
                            connection,
                            evidence=freshness_evidence,
                        )
                    except PromotionError:
                        raise
                    except Exception as exc:
                        raise PromotionError(str(exc)) from exc
                elif evidence.get("source_fingerprint") is not None:
                    # New source-bound manifests cannot be safely marked
                    # without the outbox table that produced their evidence.
                    raise PromotionError("reconciliation_database_outbox_missing")

                expected_source_fingerprint = evidence.get("source_fingerprint")
                if expected_source_fingerprint is not None:
                    try:
                        observed_source_fingerprint = canonical_source_fingerprint(connection)
                    except Exception as exc:
                        raise PromotionError(str(exc)) from exc
                    if observed_source_fingerprint != expected_source_fingerprint:
                        raise PromotionError("generation_source_snapshot_mismatch")
                if evidence.get("reconciled") is True:
                    stored = evidence.get("receipt")
                    try:
                        if not isinstance(stored, Mapping) or dict(receipt) != dict(stored):
                            raise PromotionError("reconciliation_receipt_mismatch")
                        validate_reconciliation_receipt(
                            stored,
                            generation_id=manifest.generation_id,
                            manifest_hash=str(stored.get("manifest_hash") or ""),
                            evidence=evidence,
                        )
                        assert_reconciliation_receipt_persisted(connection, stored)
                    except PromotionError:
                        raise
                    except Exception as exc:
                        raise PromotionError(str(exc)) from exc
                    if transaction_started:
                        connection.commit()
                        transaction_started = False
                    return manifest
                try:
                    validate_reconciliation_receipt(
                        receipt,
                        generation_id=manifest.generation_id,
                        manifest_hash=manifest.manifest_sha256,
                        evidence=evidence,
                    )
                    assert_reconciliation_receipt_persisted(connection, receipt)
                except Exception as exc:
                    if isinstance(exc, PromotionError):
                        raise
                    raise PromotionError(str(exc)) from exc
                updated_evidence = dict(evidence)
                updated_evidence["reconciled"] = True
                updated_evidence["receipt"] = dict(receipt)
                updated = replace(
                    manifest,
                    index_outbox=updated_evidence,
                    _legacy_without_outbox=False,
                ).reseal()
                updated.validate()
                identity = _entry_identity_at(generation_fd, MANIFEST_NAME)
                self._write_manifest_fd(
                    generation_fd,
                    updated,
                    expected_identity=identity,
                )
                manifest_updated = True
                persisted = self._read_manifest_fd(generation_fd, index_fd, generation_id)
                if persisted != updated:
                    raise PromotionError("reconciliation_manifest_persistence_mismatch")
                if transaction_started:
                    try:
                        connection.commit()
                    except Exception as exc:
                        # The manifest and the SQLite writer lock form a small
                        # cross-resource transaction.  If ending the lock
                        # fails, never leave a reconciled manifest behind on
                        # an uncertain SQLite state.  Restore the prior
                        # manifest while the generation lock is still held;
                        # the next explicit reconcile can safely retry.
                        with suppress(BaseException):
                            connection.rollback()
                        transaction_started = False
                        restore_manifest()
                        raise PromotionError("reconciliation_commit_failed") from exc
                    transaction_started = False
                return updated
            except BaseException:
                # A failed read-back or filesystem operation after the atomic
                # replace must not leave the manifest claiming reconciliation.
                restore_manifest()
                raise
            finally:
                if transaction_started:
                    with suppress(sqlite3.Error):
                        connection.rollback()
                os.close(index_fd)
                os.close(generation_fd)

    def resolve_current_manifest(self) -> GenerationManifest:
        with self._lifecycle_lock(exclusive=False):
            self._assert_anchors()
            _generation_id, manifest = self._resolve_verified_current()
            return manifest

    def resolve_verified_current_generation(self) -> tuple[GenerationManifest, Path]:
        """Return one verified manifest and its concrete index path atomically."""

        with self._lifecycle_lock(exclusive=False):
            self._assert_anchors()
            generation_id, manifest = self._resolve_verified_current()
            return manifest, self._generation_path(generation_id) / INDEX_DIRECTORY

    def resolve_verified_current_selection(
        self,
    ) -> tuple[GenerationManifest, Path, str]:
        """Return the verified generation and this exact current-link instance."""

        with self._lifecycle_lock(exclusive=False):
            self._assert_anchors()
            generation_id, manifest = self._resolve_verified_current()
            selection_identity = self._current_selection_identity_locked(generation_id)
            return (
                manifest,
                self._generation_path(generation_id) / INDEX_DIRECTORY,
                selection_identity,
            )

    def current_selection_identity(self) -> str:
        """Identify one activation of the current pointer, including rollback."""

        with self._lifecycle_lock(exclusive=False):
            self._assert_anchors()
            generation_id = self._current_generation_id(required=True)
            assert generation_id is not None
            return self._current_selection_identity_locked(generation_id)

    def resolve_verified_current_index(self) -> Path:
        """Return the validated current generation's concrete index path.

        The returned path is suitable for a caller-provided read-only adapter.
        This method does not open LanceDB and does not change production wiring.
        """

        _manifest, index_path = self.resolve_verified_current_generation()
        return index_path

    def current_manifest(self) -> GenerationManifest | None:
        with self._lifecycle_lock(exclusive=False):
            self._assert_anchors()
            generation_id = self._current_generation_id(required=False)
            if generation_id is None:
                return None
            manifest, generation_fd, index_fd = self._open_and_read_manifest(generation_id)
            os.close(index_fd)
            os.close(generation_fd)
            return manifest

    def current_manifest_metadata(self) -> GenerationManifest | None:
        """Read bounded current manifest metadata without hashing the index tree.

        This is intentionally limited to operator status surfaces. Runtime
        selection, verification, and promotion must continue to use the
        full-integrity manifest readers.
        """

        with self._lifecycle_lock(exclusive=False):
            self._assert_anchors()
            generation_id = self._current_generation_id(required=False)
            if generation_id is None:
                return None
            manifest, generation_fd, index_fd = self._open_and_read_manifest(
                generation_id,
                verify_index_tree=False,
            )
            os.close(index_fd)
            os.close(generation_fd)
            return manifest

    @contextmanager
    def _lifecycle_lock(self, *, exclusive: bool):
        with self._thread_lock:
            self._ensure_open()
            outermost = self._lifecycle_depth == 0
            previous_exclusive = self._lifecycle_exclusive
            if outermost or (exclusive and not previous_exclusive):
                operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                try:
                    fcntl.flock(self._root_fd, operation)
                except OSError as exc:
                    raise GenerationError("generation_lock_failed") from exc
            self._lifecycle_depth += 1
            self._lifecycle_exclusive = previous_exclusive or exclusive
            try:
                yield
            finally:
                self._lifecycle_depth -= 1
                self._lifecycle_exclusive = previous_exclusive
                if outermost:
                    fcntl.flock(self._root_fd, fcntl.LOCK_UN)
                elif exclusive and not previous_exclusive:
                    fcntl.flock(self._root_fd, fcntl.LOCK_SH)

    def _ensure_open(self) -> None:
        if self._closed:
            raise GenerationError("generation_manager_closed")

    def _assert_anchors(self) -> None:
        try:
            root_entry = os.stat(self.root, follow_symlinks=False)
            generations_entry = os.stat(
                GENERATIONS_DIRECTORY,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise GenerationError("generation_root_replaced") from exc
        if _directory_identity(root_entry) != self._root_identity:
            raise GenerationError("generation_root_replaced")
        if _directory_identity(os.fstat(self._root_fd)) != self._root_identity:
            raise GenerationError("generation_root_replaced")
        if _directory_identity(generations_entry) != self._generations_identity:
            raise GenerationError("generations_root_replaced")
        if _directory_identity(os.fstat(self._generations_fd)) != self._generations_identity:
            raise GenerationError("generations_root_replaced")
        if self._selections_fd >= 0:
            assert self._selections_identity is not None
            try:
                selections_entry = os.stat(
                    SELECTIONS_DIRECTORY,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise GenerationError("current_selection_root_replaced") from exc
            if _directory_identity(selections_entry) != self._selections_identity:
                raise GenerationError("current_selection_root_replaced")
            if _directory_identity(os.fstat(self._selections_fd)) != self._selections_identity:
                raise GenerationError("current_selection_root_replaced")

    def _assert_public_build_path(
        self,
        generation_id: str,
        generation_fd: int,
        index_fd: int,
    ) -> None:
        try:
            generation_entry = os.stat(
                self._generation_path(generation_id),
                follow_symlinks=False,
            )
            index_entry = os.stat(
                self._generation_path(generation_id) / INDEX_DIRECTORY,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise GenerationError("generation_build_path_replaced") from exc
        if not _same_entry(generation_entry, os.fstat(generation_fd)):
            raise GenerationError("generation_build_path_replaced")
        if not _same_entry(index_entry, os.fstat(index_fd)):
            raise GenerationError("generation_build_path_replaced")

    def _generation_path(self, generation_id: str) -> Path:
        _validate_generation_id(generation_id)
        return self.generations_path / generation_id

    def _resolve_verified_current(self) -> tuple[str, GenerationManifest]:
        generation_id = self._current_generation_id(required=True)
        assert generation_id is not None
        manifest, generation_fd, index_fd = self._open_and_read_manifest(generation_id)
        try:
            if manifest.verification_status != "verified":
                raise PromotionError("current_generation_not_verified")
            self._validate_promotable(manifest, index_fd, require_reconciled=True)
            self._assert_public_build_path(generation_id, generation_fd, index_fd)
            return generation_id, manifest
        finally:
            os.close(index_fd)
            os.close(generation_fd)

    def _open_and_read_manifest(
        self,
        generation_id: str,
        *,
        verify_index_tree: bool = True,
    ) -> tuple[GenerationManifest, int, int]:
        _validate_generation_id(generation_id)
        generation_fd = _open_directory_at(
            self._generations_fd,
            generation_id,
            "generation_not_found",
        )
        try:
            index_fd = _open_directory_at(
                generation_fd,
                INDEX_DIRECTORY,
                "generation_index_not_found",
            )
        except BaseException:
            os.close(generation_fd)
            raise
        try:
            manifest = self._read_manifest_fd(
                generation_fd,
                index_fd,
                generation_id,
                verify_index_tree=verify_index_tree,
            )
        except BaseException:
            os.close(index_fd)
            os.close(generation_fd)
            raise
        return manifest, generation_fd, index_fd

    def _read_manifest_fd(
        self,
        generation_fd: int,
        index_fd: int,
        generation_id: str,
        *,
        verify_index_tree: bool = True,
    ) -> GenerationManifest:
        try:
            manifest_fd = os.open(MANIFEST_NAME, _READ_FILE_FLAGS, dir_fd=generation_fd)
        except OSError as exc:
            raise ManifestError("manifest_not_regular_file") from exc
        try:
            before = os.fstat(manifest_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ManifestError("manifest_not_regular_file")
            raw = _read_bounded_fd(manifest_fd, _MAX_MANIFEST_BYTES, "manifest_too_large")
            after = os.fstat(manifest_fd)
            if not _same_file_version(before, after):
                raise ManifestError("manifest_changed_while_reading")
            _require_entry_identity(
                generation_fd,
                MANIFEST_NAME,
                after,
                "manifest_replaced_while_reading",
                error_type=ManifestError,
            )
        finally:
            os.close(manifest_fd)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestError("manifest_unreadable") from exc
        if not isinstance(payload, dict):
            raise ManifestError("manifest_not_object")
        manifest = GenerationManifest.from_dict(payload)
        if manifest.generation_id != generation_id:
            raise ManifestError("manifest_generation_id_mismatch")
        if verify_index_tree and manifest.build_status == "complete":
            observed_tree_sha256 = _index_tree_sha256(index_fd)
            if observed_tree_sha256 != manifest.index_tree_sha256:
                raise ManifestError("index_tree_digest_mismatch")
        return manifest

    def _write_manifest_fd(
        self,
        generation_fd: int,
        manifest: GenerationManifest,
        *,
        expected_identity: tuple[int, int, int] | None,
    ) -> None:
        manifest.validate()
        payload = (
            json.dumps(
                manifest.to_dict(),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > _MAX_MANIFEST_BYTES:
            raise ManifestError("manifest_too_large")
        temporary_name = f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
        temporary_fd = -1
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=generation_fd,
            )
            _write_all(temporary_fd, payload)
            os.fsync(temporary_fd)
            temporary_stat = os.fstat(temporary_fd)
            if not stat.S_ISREG(temporary_stat.st_mode) or temporary_stat.st_nlink != 1:
                raise ManifestError("manifest_temporary_not_regular")
            if expected_identity is None:
                if _lexists_at(generation_fd, MANIFEST_NAME):
                    raise ManifestError("manifest_destination_unexpected")
            else:
                observed_identity = _entry_identity_at(generation_fd, MANIFEST_NAME)
                if observed_identity != expected_identity:
                    raise ManifestError("manifest_replaced_before_write")
            _require_entry_identity(
                generation_fd,
                temporary_name,
                temporary_stat,
                "manifest_temporary_replaced",
                error_type=ManifestError,
            )
            os.replace(
                temporary_name,
                MANIFEST_NAME,
                src_dir_fd=generation_fd,
                dst_dir_fd=generation_fd,
            )
            _fsync_fd(generation_fd, ManifestError, "manifest_directory_fsync_failed")
        except ManifestError:
            raise
        except OSError as exc:
            raise ManifestError("manifest_write_failed") from exc
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            if _lexists_at(generation_fd, temporary_name):
                with suppress(OSError):
                    os.unlink(temporary_name, dir_fd=generation_fd)

    def _begin_source_freshness_transaction(
        self,
        manifest: GenerationManifest,
        connection: sqlite3.Connection | None,
        *,
        operation: str,
    ) -> bool:
        """Lock and validate a source-bound generation before pointer changes."""

        evidence = manifest.index_outbox
        if not isinstance(evidence, Mapping) or evidence.get("source_fingerprint") is None:
            return False
        if not isinstance(connection, sqlite3.Connection) or connection.in_transaction:
            raise PromotionError(f"{operation}_database_required")
        from plastic_promise.core.index_outbox_reconciliation import (
            assert_index_outbox_fresh,
        )

        try:
            connection.execute("BEGIN IMMEDIATE")
            assert_index_outbox_fresh(connection, evidence=evidence)
        except PromotionError:
            with suppress(BaseException):
                connection.rollback()
            raise
        except Exception as exc:
            with suppress(BaseException):
                connection.rollback()
            raise PromotionError(str(exc)) from exc
        return True

    def _validate_promotable(
        self,
        manifest: GenerationManifest,
        index_fd: int,
        *,
        require_reconciled: bool = False,
    ) -> None:
        if manifest.build_status != "complete":
            raise PromotionError("generation_build_incomplete")
        if manifest.index_text_policy is None or manifest.index_material_sha256 is None:
            raise PromotionError("generation_index_material_binding_required")
        if manifest.built_row_count != manifest.source_row_count:
            raise PromotionError("row_count_mismatch")
        if require_reconciled and (
            manifest.index_outbox is None or manifest.index_outbox.get("reconciled") is not True
        ):
            raise PromotionError("generation_outbox_reconciliation_required")
        try:
            _validate_quality_report(manifest.quality_report, spec=manifest.spec)
        except PromotionError:
            raise
        observed_tree_sha256 = _index_tree_sha256(index_fd)
        if observed_tree_sha256 != manifest.index_tree_sha256:
            raise PromotionError("index_tree_digest_mismatch")
        assert manifest.built_row_count is not None
        self._verify_artifact(
            index_fd,
            spec=manifest.spec,
            expected_row_count=manifest.built_row_count,
            expected_tree_sha256=observed_tree_sha256,
            error_type=PromotionError,
        )

    def _validate_current_runtime_environment(
        self,
        spec: GenerationSpec,
        report: Mapping[str, Any] | None,
        *,
        operation: str,
    ) -> None:
        benchmark = report.get("benchmark") if isinstance(report, Mapping) else None
        if not isinstance(benchmark, Mapping) or "candidate_dimension" not in benchmark:
            return
        validator = self._runtime_environment_validator
        if validator is None:
            raise PromotionError(f"{operation}_runtime_environment_validator_required")
        try:
            validator(spec, report)
        except PromotionError:
            raise
        except Exception as exc:
            reason = str(exc)
            if not reason or re.fullmatch(r"[a-z0-9_]+", reason) is None:
                reason = f"{operation}_runtime_environment_verification_failed"
            raise PromotionError(reason) from exc

    def _verify_artifact(
        self,
        index_fd: int,
        *,
        spec: GenerationSpec,
        expected_row_count: int,
        expected_tree_sha256: str,
        error_type: type[GenerationError],
    ) -> None:
        verifier = self._artifact_verifier
        if verifier is None:
            raise error_type("artifact_verifier_required")
        verifier_fd = os.open(".", _DIRECTORY_FLAGS, dir_fd=index_fd)
        request = ArtifactVerificationRequest(
            index_fd=verifier_fd,
            generation_id=spec.generation_id,
            index_schema=spec.index_schema,
            embedding_model=spec.embedding_model,
            model_revision=spec.model_revision,
            embedding_dimension=spec.embedding_dimension,
            expected_tree_sha256=expected_tree_sha256,
            index_text_policy=spec.index_text_policy,
            expected_index_material_sha256=spec.index_material_sha256,
        )
        try:
            try:
                verification = verifier(request)
            except Exception as exc:
                raise error_type("artifact_verification_failed") from exc
        finally:
            with suppress(OSError):
                os.close(verifier_fd)
        if not isinstance(verification, ArtifactVerification):
            raise error_type("artifact_verifier_invalid_result")
        if verification.row_count != expected_row_count:
            raise error_type("artifact_row_count_mismatch")
        if (
            verification.index_schema != spec.index_schema
            or verification.embedding_model != spec.embedding_model
            or verification.model_revision != spec.model_revision
            or verification.embedding_dimension != spec.embedding_dimension
        ):
            raise error_type("artifact_identity_mismatch")
        if verification.index_material_sha256 != spec.index_material_sha256:
            raise error_type("artifact_index_material_mismatch")
        if _index_tree_sha256(index_fd) != expected_tree_sha256:
            raise error_type("artifact_changed_during_verification")

    def _remove_failed_staging(
        self,
        generation_id: str,
        expected_identity: tuple[int, int, int],
    ) -> None:
        try:
            observed = os.stat(
                generation_id,
                dir_fd=self._generations_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError:
            return
        if _entry_identity(observed) != expected_identity or not stat.S_ISDIR(observed.st_mode):
            return
        try:
            generation_fd = _open_directory_at(
                self._generations_fd,
                generation_id,
                "generation_staging_replaced",
            )
        except GenerationError:
            return
        try:
            _remove_tree_contents(generation_fd)
            _require_entry_identity(
                self._generations_fd,
                generation_id,
                os.fstat(generation_fd),
                "generation_staging_replaced",
            )
        except GenerationError:
            return
        finally:
            os.close(generation_fd)
        try:
            os.rmdir(generation_id, dir_fd=self._generations_fd)
            _fsync_fd(
                self._generations_fd,
                GenerationError,
                "generation_cleanup_fsync_failed",
            )
        except OSError:
            return

    def _atomic_switch(self, generation_id: str) -> None:
        _validate_generation_id(generation_id)
        old_state = self._current_state(required=False)
        activation_id = self._create_selection_link(generation_id)
        target = f"{SELECTIONS_DIRECTORY}/{activation_id}"
        temporary_name = f".{CURRENT_LINK}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        switched = False
        try:
            os.symlink(target, temporary_name, dir_fd=self._root_fd)
            temporary = self._symlink_state(temporary_name)
            if temporary is None or temporary[0] != target:
                raise PromotionError("current_temporary_invalid")
            if self._current_state(required=False) != old_state:
                raise PromotionError("current_pointer_changed")
            os.replace(
                temporary_name,
                CURRENT_LINK,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
            )
            switched = True
            _fsync_fd(self._root_fd, PromotionError, "current_pointer_fsync_failed")
            current = self._current_state(required=True)
            if (
                current is None
                or current.generation_id != generation_id
                or current.current_target != target
                or current.activation_id != activation_id
            ):
                raise PromotionError("current_pointer_switch_corrupted")
        except PromotionError:
            if switched:
                self._restore_current_state(old_state)
            raise
        except GenerationError as exc:
            if switched:
                self._restore_current_state(old_state)
            raise PromotionError("current_pointer_switch_corrupted") from exc
        except OSError as exc:
            if switched:
                self._restore_current_state(old_state)
            raise PromotionError("current_pointer_switch_failed") from exc
        finally:
            if _lexists_at(self._root_fd, temporary_name):
                with suppress(OSError):
                    os.unlink(temporary_name, dir_fd=self._root_fd)

    def _restore_current_state(self, state: _CurrentState | None) -> None:
        try:
            if state is None:
                if _lexists_at(self._root_fd, CURRENT_LINK):
                    os.unlink(CURRENT_LINK, dir_fd=self._root_fd)
            else:
                temporary_name = f".{CURRENT_LINK}.restore.{uuid.uuid4().hex}.tmp"
                try:
                    os.symlink(state.current_target, temporary_name, dir_fd=self._root_fd)
                    os.replace(
                        temporary_name,
                        CURRENT_LINK,
                        src_dir_fd=self._root_fd,
                        dst_dir_fd=self._root_fd,
                    )
                finally:
                    if _lexists_at(self._root_fd, temporary_name):
                        os.unlink(temporary_name, dir_fd=self._root_fd)
            _fsync_fd(self._root_fd, PromotionError, "current_pointer_restore_fsync_failed")
        except OSError as exc:
            raise PromotionError("current_pointer_restore_failed") from exc

    def _symlink_state(self, name: str) -> tuple[str, tuple[int, int, int]] | None:
        try:
            before = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GenerationError("current_pointer_unreadable") from exc
        if not stat.S_ISLNK(before.st_mode):
            raise GenerationError("current_pointer_not_symlink")
        try:
            target = os.readlink(name, dir_fd=self._root_fd)
            after = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except OSError as exc:
            raise GenerationError("current_pointer_unreadable") from exc
        if _entry_identity(before) != _entry_identity(after):
            raise GenerationError("current_pointer_changed")
        return target, _entry_identity(after)

    def _current_state(
        self,
        *,
        required: bool,
    ) -> _CurrentState | None:
        state = self._symlink_state(CURRENT_LINK)
        if state is None:
            if required:
                raise GenerationError("current_generation_not_set")
            return None
        raw_target, identity = state
        target = Path(raw_target)
        if target.is_absolute() or len(target.parts) != 2:
            raise GenerationError("invalid_current_target")
        if target.parts[0] == GENERATIONS_DIRECTORY:
            generation_id = target.parts[1]
            activation_id = None
        elif target.parts[0] == SELECTIONS_DIRECTORY:
            activation_id = target.parts[1]
            if _ACTIVATION_ID.fullmatch(activation_id) is None:
                raise GenerationError("invalid_current_target")
            generation_id = self._selection_generation_id(activation_id)
        else:
            raise GenerationError("invalid_current_target")
        try:
            _validate_generation_id(generation_id)
        except ValueError as exc:
            raise GenerationError("invalid_current_target") from exc
        return _CurrentState(
            generation_id=generation_id,
            current_target=raw_target,
            current_identity=identity,
            activation_id=activation_id,
        )

    def _current_generation_id(self, *, required: bool) -> str | None:
        state = self._current_state(required=required)
        return state.generation_id if state is not None else None

    def _current_selection_identity_locked(self, expected_generation_id: str) -> str:
        """Return the durable one-time identity selected by ``current``."""

        state = self._current_state(required=True)
        if state is None or state.generation_id != expected_generation_id:
            raise GenerationError("current_pointer_changed")
        if state.activation_id is None:
            raise GenerationError("current_selection_identity_unavailable")
        return state.activation_id

    def _create_selection_link(self, generation_id: str) -> str:
        """Create a retained activation link whose name can never be reused."""

        selections_fd = self._open_selections_directory(create=True)
        selection_target = f"../{GENERATIONS_DIRECTORY}/{generation_id}"
        try:
            for _attempt in range(16):
                activation_id = secrets.token_hex(32)
                if _ACTIVATION_ID.fullmatch(activation_id) is None:
                    raise PromotionError("current_selection_identity_invalid")
                try:
                    os.symlink(selection_target, activation_id, dir_fd=selections_fd)
                except FileExistsError:
                    continue
                observed = self._selection_link_state(selections_fd, activation_id)
                if observed[0] != selection_target:
                    raise PromotionError("current_selection_link_corrupted")
                _fsync_fd(
                    selections_fd,
                    PromotionError,
                    "current_selection_directory_fsync_failed",
                )
                return activation_id
        finally:
            os.close(selections_fd)
        raise PromotionError("current_selection_identity_exhausted")

    def _selection_generation_id(self, activation_id: str) -> str:
        selections_fd = self._open_selections_directory(create=False)
        try:
            raw_target, _identity = self._selection_link_state(selections_fd, activation_id)
        finally:
            os.close(selections_fd)
        target = Path(raw_target)
        if target.is_absolute() or target.parts[:2] != ("..", GENERATIONS_DIRECTORY):
            raise GenerationError("invalid_current_selection_target")
        if len(target.parts) != 3:
            raise GenerationError("invalid_current_selection_target")
        try:
            _validate_generation_id(target.parts[2])
        except ValueError as exc:
            raise GenerationError("invalid_current_selection_target") from exc
        return target.parts[2]

    def _open_selections_directory(self, *, create: bool) -> int:
        if self._selections_fd >= 0:
            self._assert_anchors()
            return os.dup(self._selections_fd)
        if create:
            try:
                os.mkdir(SELECTIONS_DIRECTORY, mode=0o700, dir_fd=self._root_fd)
                _fsync_fd(self._root_fd, PromotionError, "generation_root_fsync_failed")
            except FileExistsError:
                pass
            except OSError as exc:
                raise PromotionError("current_selection_root_create_failed") from exc
        selections_fd = _open_directory_at(
            self._root_fd,
            SELECTIONS_DIRECTORY,
            "current_selection_root_not_found",
        )
        try:
            _require_private_directory(
                selections_fd,
                "current_selection_root_insecure_permissions",
            )
            self._selections_identity = _directory_identity(os.fstat(selections_fd))
            self._selections_fd = selections_fd
        except BaseException:
            os.close(selections_fd)
            raise
        return os.dup(self._selections_fd)

    @staticmethod
    def _selection_link_state(
        selections_fd: int,
        activation_id: str,
    ) -> tuple[str, tuple[int, int, int]]:
        try:
            before = os.stat(activation_id, dir_fd=selections_fd, follow_symlinks=False)
            raw_target = os.readlink(activation_id, dir_fd=selections_fd)
            after = os.stat(activation_id, dir_fd=selections_fd, follow_symlinks=False)
        except OSError as exc:
            raise GenerationError("current_selection_link_unreadable") from exc
        if not stat.S_ISLNK(before.st_mode):
            raise GenerationError("current_selection_link_not_symlink")
        if not _same_file_version(before, after):
            raise GenerationError("current_selection_link_changed")
        return raw_target, _entry_identity(after)


def _validate_generation_id(generation_id: str) -> None:
    if (
        not isinstance(generation_id, str)
        or not _GENERATION_ID.fullmatch(generation_id)
        or ".." in generation_id
        or generation_id.startswith(".")
        or generation_id.endswith(".")
        or generation_id.casefold() in _RESERVED_GENERATION_IDS
    ):
        raise ValueError("invalid_generation_id")


def _validate_identity_text(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid_{name}")


def _validate_index_text_policy(value: object) -> None:
    if value not in _INDEX_TEXT_POLICIES:
        raise ValueError("invalid_index_text_policy")


def _validate_index_material_binding(
    index_text_policy: object,
    index_material_sha256: object,
) -> None:
    if index_text_policy is None and index_material_sha256 is None:
        return
    if index_text_policy is None or index_material_sha256 is None:
        raise ValueError("incomplete_index_material_binding")
    _validate_index_text_policy(index_text_policy)
    if not _is_sha256(index_material_sha256):
        raise ValueError("invalid_index_material_sha256")


def _validate_positive_integer(name: str, value: object) -> None:
    if not _is_positive_integer(value):
        raise ValueError(f"invalid_{name}")


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_nonnegative_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid_{name}")


def _validate_outbox_identity(
    watermark: object,
    digest: object,
    job_count: object,
) -> None:
    values = (watermark, digest, job_count)
    if all(value is None for value in values):
        return
    if (
        isinstance(watermark, bool)
        or not isinstance(watermark, int)
        or watermark < 0
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
        or isinstance(job_count, bool)
        or not isinstance(job_count, int)
        or job_count < 0
    ):
        raise ValueError("invalid_index_outbox_identity")


def _validate_outbox_evidence(value: object, *, generation_id: str | None = None) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError("invalid_index_outbox_evidence")
    required = {
        "watermark",
        "immutable_digest",
        "job_count",
        "reconciled",
    }
    if not required.issubset(value):
        raise ValueError("invalid_index_outbox_evidence")
    _validate_outbox_identity(
        value.get("watermark"),
        value.get("immutable_digest"),
        value.get("job_count"),
    )
    source_fingerprint = value.get("source_fingerprint")
    if source_fingerprint is not None and (
        not isinstance(source_fingerprint, str)
        or not _SHA256.fullmatch(source_fingerprint)
        or value.get("watermark") is None
    ):
        raise ValueError("invalid_index_outbox_source_fingerprint")
    embedding_index_identity = value.get("embedding_index_identity")
    if embedding_index_identity is not None:
        try:
            _validate_identity_text("embedding_index_identity", embedding_index_identity)
        except ValueError as exc:
            raise ValueError("invalid_embedding_index_identity") from exc
    if not isinstance(value.get("reconciled"), bool):
        raise ValueError("invalid_index_outbox_evidence")
    if value.get("reconciled"):
        receipt = value.get("receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("reconciled_index_outbox_receipt_missing")
        try:
            from plastic_promise.core.index_outbox_reconciliation import (
                validate_reconciliation_receipt,
            )

            validate_reconciliation_receipt(
                receipt,
                generation_id=str(generation_id or receipt.get("generation_id") or ""),
                manifest_hash=str(receipt.get("manifest_hash") or ""),
                evidence=value,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError("reconciled_index_outbox_receipt_invalid") from exc


def _validate_timestamp(name: str, value: object, *, required: bool) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ManifestError(f"invalid_{name}")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ManifestError(f"invalid_{name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ManifestError(f"invalid_{name}")


def _json_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def index_material_sha256(
    index_text_policy: str,
    rows: Mapping[str, Mapping[str, object]],
) -> str:
    """Hash the policy and artifact-observable material in canonical ID order."""

    _validate_index_text_policy(index_text_policy)
    if not isinstance(rows, Mapping):
        raise ValueError("invalid_index_material_rows")
    canonical_rows: list[dict[str, str]] = []
    for memory_id in sorted(rows):
        row = rows[memory_id]
        try:
            _validate_identity_text("index_material_memory_id", memory_id)
        except ValueError as exc:
            raise ValueError("invalid_index_material_rows") from exc
        if not isinstance(row, Mapping):
            raise ValueError("invalid_index_material_rows")
        canonical: dict[str, str] = {"memory_id": memory_id}
        for field_name in ("text", "tier", "category", "scope"):
            value = row.get(field_name)
            if not isinstance(value, str) or (field_name == "text" and not value.strip()):
                raise ValueError("invalid_index_material_rows")
            canonical[field_name] = value
        canonical_rows.append(canonical)
    return _json_sha256(
        {
            "schema": INDEX_MATERIAL_SCHEMA,
            "index_text_policy": index_text_policy,
            "rows": canonical_rows,
        }
    )


def _legacy_generation_identity_sha256(spec: GenerationSpec) -> str:
    """Hash the pre-outbox generation identity shape.

    MANIFEST_SCHEMA remained v2 when the outbox binding was added. Existing
    manifests therefore legitimately omit the three outbox identity fields.
    """

    return _json_sha256(
        {
            "generation_id": spec.generation_id,
            "index_schema": spec.index_schema,
            "embedding_model": spec.embedding_model,
            "model_revision": spec.model_revision,
            "embedding_dimension": spec.embedding_dimension,
            "source_db_sha256": spec.source_db_sha256,
            "source_row_count": spec.source_row_count,
            "benchmark_corpus_sha256": spec.benchmark_corpus_sha256,
            "benchmark_corpus_count": spec.benchmark_corpus_count,
            "benchmark_cases_sha256": spec.benchmark_cases_sha256,
            "benchmark_case_count": spec.benchmark_case_count,
        }
    )


def _copy_json_mapping(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        copied = json.loads(
            json.dumps(
                dict(payload),
                ensure_ascii=True,
                sort_keys=True,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("quality_report_not_json_serializable") from exc
    if not isinstance(copied, dict):
        raise ValueError("invalid_quality_report")
    return copied


def adapt_recall_quality_report(
    report: Mapping[str, Any],
    *,
    candidate_manifest: object | None = None,
) -> dict[str, Any]:
    """Validate and sanitize one real ``benchmark_recall_quality`` report.

    The benchmark report intentionally contains detailed per-case diagnostics
    and server logs. None of those fields are persisted. This adapter verifies
    the complete live-run attestations and returns the fixed, secret-free
    evidence schema accepted by :class:`GenerationManager`.
    """

    try:
        raw = _copy_json_mapping(report)
    except ValueError as exc:
        raise PromotionError("recall_quality_report_not_json") from exc
    if raw is None:
        raise PromotionError("recall_quality_report_missing")
    _require_exact_fields(
        raw,
        {
            "schema_version",
            "dataset_schema_version",
            "dataset_revision",
            "dataset_role",
            "dataset_fingerprint",
            "corpus",
            "cases",
            "candidate",
            "candidate_dimension",
            "candidate_id",
            "manifest_hash",
            "fusion_config",
            "backend",
            "execution",
            "environment",
            "isolated_corpus",
            "smoke",
            "public_call_counts",
            "public_transport_call_counts",
            "fusion_attestation",
            "server_logs",
            "publishable_claim",
            "publishability_reason",
            "metrics",
            "quality",
            "gate",
            "best_constituent_gate",
            "usage",
        },
        "recall_quality_report_fields_invalid",
    )
    if raw.get("schema_version") != RECALL_QUALITY_REPORT_SCHEMA:
        raise PromotionError("recall_quality_report_schema_invalid")
    if raw.get("dataset_schema_version") != RECALL_QUALITY_DATASET_SCHEMA:
        raise PromotionError("recall_quality_dataset_schema_invalid")
    dataset_revision = _required_identity(
        raw.get("dataset_revision"),
        "benchmark_dataset_revision",
        "recall_quality_dataset_revision_invalid",
    )
    candidate = _required_identity(
        raw.get("candidate"),
        "benchmark_candidate",
        "recall_quality_candidate_invalid",
    )
    if candidate not in {"legacy", "compact-v2"}:
        raise PromotionError("recall_quality_candidate_invalid")
    if raw.get("publishable_claim") is not True:
        raise PromotionError("recall_quality_report_not_publishable")
    if raw.get("publishability_reason") != (
        "isolated live backend and store-recall-supply smoke passed"
    ):
        raise PromotionError("recall_quality_publishability_invalid")

    corpus = _adapt_recall_corpus(raw.get("corpus"))
    cases = _adapt_recall_cases(raw.get("cases"))
    benchmark_binding, expected_case_identities = _adapt_v2_benchmark_binding(
        raw,
        dataset_revision=dataset_revision,
        corpus=corpus,
        cases=cases,
    )
    if candidate_manifest is not None:
        _validate_candidate_manifest_binding(raw, candidate_manifest)
    backend = _adapt_recall_backend(raw.get("backend"), candidate=candidate)
    usage = _adapt_recall_usage(raw.get("usage"))
    if backend["usage"] != usage:
        raise PromotionError("recall_quality_usage_mismatch")
    execution = _validate_recall_execution(raw.get("execution"))
    environment = _adapt_recall_environment(
        raw.get("environment"),
        backend=backend,
        candidate=candidate,
        fusion_policy=benchmark_binding["candidate_id"],
        execution=execution,
    )
    _validate_isolated_recall_corpus(
        raw.get("isolated_corpus"),
        expected_count=corpus["count"],
    )
    smoke = _adapt_recall_smoke(raw.get("smoke"))
    fusion_attestation = _validate_live_call_evidence(
        raw.get("public_call_counts"),
        raw.get("public_transport_call_counts"),
        raw.get("fusion_attestation"),
        expected_queries=cases["count"],
        execution=execution,
        candidate_id=benchmark_binding["candidate_id"],
        fusion_config=benchmark_binding["fusion_config"],
        backend=backend,
    )
    if not isinstance(raw.get("server_logs"), Mapping):
        raise PromotionError("recall_quality_server_log_evidence_invalid")

    metrics = _adapt_recall_metrics(
        raw.get("metrics"),
        raw_quality=raw.get("quality"),
        raw_gate=raw.get("gate"),
        raw_constituent_gate=raw.get("best_constituent_gate"),
        expected_case_count=cases["count"],
        expected_case_identities=expected_case_identities,
    )
    if backend["warm_p50_ms"] != metrics.pop("_raw_p50_ms"):
        raise PromotionError("recall_quality_backend_latency_mismatch")
    if backend["warm_p95_ms"] != metrics["p95_ms"]:
        raise PromotionError("recall_quality_backend_latency_mismatch")
    normalized_backend = {
        "mode": "live",
        "fallback_used": False,
        "degraded_used": False,
        "model": backend["model"],
        "revision": backend["model_revision"],
        "dimension": backend["dimension"],
        "provider": backend["provider"],
        "transport": backend["transport"],
        "index_text_policy": backend["index_text_policy"],
        "requested_policy": backend["requested_policy"],
        "effective_policy": backend["effective_policy"],
        "requested_runtime": backend["requested_runtime"],
        "effective_runtime": backend["effective_runtime"],
        "runtime_route": backend["runtime_route"],
        "rust_runtime": backend["rust_runtime"],
    }
    normalized = {
        "schema": QUALITY_REPORT_SCHEMA,
        "benchmark": {
            "report_schema": RECALL_QUALITY_REPORT_SCHEMA,
            "dataset_schema": RECALL_QUALITY_DATASET_SCHEMA,
            "dataset_revision": dataset_revision,
            "candidate": candidate,
            **benchmark_binding,
        },
        "gate": {
            "status": "pass",
            "policy": QUALITY_GATE_POLICY,
            "thresholds": _promotion_thresholds(),
        },
        "degraded": False,
        "publishable_claim": True,
        "backend": normalized_backend,
        "cases": cases,
        "corpus": corpus,
        "environment": environment,
        "fusion_attestation": fusion_attestation,
        "smoke": smoke,
        "usage": usage,
        "metrics": metrics,
    }
    identity = _quality_report_identity_from_fields(normalized)
    validation_spec = GenerationSpec(
        generation_id="benchmark-evidence",
        index_schema="benchmark-evidence/v1",
        embedding_model=identity.embedding_model,
        model_revision=identity.model_revision,
        embedding_dimension=identity.embedding_dimension,
        source_db_sha256="0" * 64,
        source_row_count=0,
        benchmark_corpus_sha256=identity.benchmark_corpus_sha256,
        benchmark_corpus_count=identity.benchmark_corpus_count,
        benchmark_cases_sha256=identity.benchmark_cases_sha256,
        benchmark_case_count=identity.benchmark_case_count,
        index_text_policy=identity.index_text_policy,
        index_material_sha256="0" * 64,
    )
    _validate_quality_report(normalized, spec=validation_spec)
    return normalized


def quality_report_generation_identity(
    report: Mapping[str, Any],
) -> QualityReportGenerationIdentity:
    """Extract a generation spec identity from normalized quality evidence."""

    try:
        copied = _copy_json_mapping(report)
    except ValueError as exc:
        raise PromotionError("quality_report_not_json_serializable") from exc
    if copied is None:
        raise PromotionError("quality_report_missing")
    identity = _quality_report_identity_from_fields(copied)
    validation_spec = GenerationSpec(
        generation_id="benchmark-evidence",
        index_schema="benchmark-evidence/v1",
        embedding_model=identity.embedding_model,
        model_revision=identity.model_revision,
        embedding_dimension=identity.embedding_dimension,
        source_db_sha256="0" * 64,
        source_row_count=0,
        benchmark_corpus_sha256=identity.benchmark_corpus_sha256,
        benchmark_corpus_count=identity.benchmark_corpus_count,
        benchmark_cases_sha256=identity.benchmark_cases_sha256,
        benchmark_case_count=identity.benchmark_case_count,
        index_text_policy=identity.index_text_policy,
        index_material_sha256="0" * 64,
    )
    _validate_quality_report(copied, spec=validation_spec)
    return identity


def _adapt_v2_benchmark_binding(
    raw: Mapping[str, Any],
    *,
    dataset_revision: str,
    corpus: Mapping[str, Any],
    cases: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[tuple[str, str, str], ...]]:
    """Validate the independently frozen held-out contract before metrics."""

    if raw.get("dataset_role") != "held-out":
        raise PromotionError("recall_quality_dataset_role_invalid")
    fingerprint = raw.get("dataset_fingerprint")
    if not _is_sha256(fingerprint):
        raise PromotionError("recall_quality_dataset_fingerprint_invalid")
    contract = heldout_report_contract(fingerprint)
    if not isinstance(contract, Mapping):
        raise PromotionError("recall_quality_heldout_contract_unknown")
    if raw.get("candidate_dimension") != "fusion_policy":
        raise PromotionError("recall_quality_candidate_dimension_invalid")
    candidate_id = _required_identity(
        raw.get("candidate_id"),
        "recall_quality_candidate_id",
        "recall_quality_candidate_id_invalid",
    )
    manifest_hash = raw.get("manifest_hash")
    canonical_fusion_config = _validate_v2_fusion_evidence(
        candidate_id,
        raw.get("fusion_config"),
        manifest_hash,
    )
    expected_fields = contract.get("fields")
    if not isinstance(expected_fields, Mapping):
        raise PromotionError("recall_quality_heldout_contract_invalid")
    checks = {
        "dataset_schema_version": raw.get("dataset_schema_version"),
        "dataset_revision": dataset_revision,
        "corpus.revision": corpus.get("revision"),
        "corpus.provenance_revision": corpus.get("provenance_revision"),
        "corpus.sha256": corpus.get("sha256"),
        "corpus.count": corpus.get("count"),
        "cases.sha256": cases.get("sha256"),
        "cases.count": cases.get("count"),
    }
    for name, expected in expected_fields.items():
        if checks.get(str(name)) != expected:
            raise PromotionError("recall_quality_heldout_contract_mismatch")
    identities = contract.get("case_identities")
    if not isinstance(identities, (list, tuple)) or not identities:
        raise PromotionError("recall_quality_heldout_contract_invalid")
    normalized_identities: list[tuple[str, str, str]] = []
    for identity in identities:
        if (
            not isinstance(identity, (list, tuple))
            or len(identity) != 3
            or not all(isinstance(item, str) and item for item in identity)
        ):
            raise PromotionError("recall_quality_heldout_contract_invalid")
        normalized_identities.append((identity[0], identity[1], identity[2]))
    backend = raw.get("backend")
    if not isinstance(backend, Mapping):
        raise PromotionError("recall_quality_backend_invalid")
    if (
        backend.get("requested_policy") != candidate_id
        or backend.get("effective_policy") != candidate_id
    ):
        raise PromotionError("recall_quality_fusion_policy_mismatch")
    return (
        {
            "dataset_role": "held-out",
            "dataset_fingerprint": fingerprint,
            "candidate_dimension": "fusion_policy",
            "candidate_id": candidate_id,
            "manifest_hash": manifest_hash,
            "fusion_config": canonical_fusion_config,
        },
        tuple(normalized_identities),
    )


def _validate_v2_fusion_evidence(
    candidate_id: object,
    fusion_config: object,
    manifest_hash: object,
) -> dict[str, Any] | None:
    """Validate a frozen WRRF winner or the preregistered max-v1 control."""

    if candidate_id == "max-v1":
        if manifest_hash != "" or fusion_config is not None:
            raise PromotionError("recall_quality_fusion_binding_invalid")
        return None
    if not isinstance(candidate_id, str) or not candidate_id.startswith("wrrf-v1:"):
        raise PromotionError("recall_quality_candidate_id_invalid")
    if not _is_sha256(manifest_hash):
        raise PromotionError("recall_quality_manifest_hash_invalid")
    try:
        return validate_fusion_candidate_binding(candidate_id, fusion_config)
    except (TypeError, ValueError) as exc:
        raise PromotionError("recall_quality_fusion_binding_invalid") from exc


def _validate_candidate_manifest_binding(
    report: Mapping[str, Any],
    candidate_manifest: object,
) -> None:
    """Bind report claims to a separately parsed frozen candidate manifest."""

    def value(name: str, default: object = None) -> object:
        if isinstance(candidate_manifest, Mapping):
            return candidate_manifest.get(name, default)
        return getattr(candidate_manifest, name, default)

    manifest_hash = value("manifest_hash")
    if manifest_hash is None and callable(getattr(candidate_manifest, "to_dict", None)):
        manifest_hash = candidate_manifest.to_dict().get("manifest_hash")
    if report.get("manifest_hash") != manifest_hash:
        raise PromotionError("recall_quality_manifest_binding_mismatch")
    expected = {
        "candidate_id": value("candidate_id"),
        "candidate_dimension": value("candidate_dimension"),
        "dataset_fingerprint": value("heldout_fingerprint"),
        "environment.source_commit": value("source_commit"),
        "environment.dirty_fingerprint": value("dirty_fingerprint"),
        "environment.comparison_environment_fingerprint": value(
            "comparison_environment_fingerprint"
        ),
        "environment.retrieval_configuration": value("retrieval_configuration"),
        "environment.embedding_configuration": value("embedding_configuration"),
        "environment.dependencies": value("dependency_versions"),
        "backend.runtime_route": value("runtime_route"),
    }
    for path, expected_value in expected.items():
        current: object = report
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                current = None
                break
            current = current[part]
        if current != expected_value:
            raise PromotionError("recall_quality_manifest_binding_mismatch")
    config = value("fusion_config")
    if hasattr(config, "k"):
        config = {
            "k": config.k,
            "channels": list(config.channels),
            "weights": dict(config.weights),
            "windows": dict(config.windows),
        }
    if report.get("fusion_config") != config:
        raise PromotionError("recall_quality_manifest_binding_mismatch")


def _adapt_recall_corpus(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_corpus_invalid")
    _require_exact_fields(
        value,
        {"sha256", "count", "revision", "provenance_revision"},
        "recall_quality_corpus_invalid",
    )
    if not _is_sha256(value.get("sha256")) or not _is_positive_integer(value.get("count")):
        raise PromotionError("recall_quality_corpus_invalid")
    revision = _required_identity(
        value.get("revision"),
        "benchmark_corpus_revision",
        "recall_quality_corpus_invalid",
    )
    provenance_revision = _required_identity(
        value.get("provenance_revision"),
        "benchmark_corpus_provenance_revision",
        "recall_quality_corpus_invalid",
    )
    return {
        "sha256": value["sha256"],
        "count": value["count"],
        "revision": revision,
        "provenance_revision": provenance_revision,
    }


def _adapt_recall_cases(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_cases_invalid")
    _require_exact_fields(value, {"sha256", "count"}, "recall_quality_cases_invalid")
    if not _is_sha256(value.get("sha256")) or not _is_positive_integer(value.get("count")):
        raise PromotionError("recall_quality_cases_invalid")
    return {"sha256": value["sha256"], "count": value["count"]}


def _adapt_recall_backend(value: object, *, candidate: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_backend_invalid")
    required = {
        "mode",
        "deterministic",
        "fallback_used",
        "degraded_used",
        "model",
        "model_revision",
        "dimension",
        "index_text_policy",
        "runtime",
        "provider",
        "usage",
        "transport",
        "server_pid",
        "requested_policy",
        "effective_policy",
        "requested_runtime",
        "effective_runtime",
        "cold_latency_ms",
        "warm_p50_ms",
        "warm_p95_ms",
        "channel_result_names",
        "runtime_route",
        "rust_runtime",
    }
    if not required.issubset(value):
        raise PromotionError("recall_quality_backend_invalid")
    if (
        value.get("mode") != "live"
        or value.get("deterministic") is not False
        or value.get("fallback_used") is not False
        or value.get("degraded_used") is not False
        or value.get("transport") != "streamable-http"
        or value.get("index_text_policy") != candidate
        or not _is_positive_integer(value.get("server_pid"))
    ):
        raise PromotionError("recall_quality_backend_not_live")
    model = _required_identity(
        value.get("model"),
        "benchmark_embedding_model",
        "recall_quality_backend_identity_invalid",
    )
    model_revision = _required_identity(
        value.get("model_revision"),
        "benchmark_model_revision",
        "recall_quality_backend_identity_invalid",
    )
    dimension = value.get("dimension")
    if not _is_positive_integer(dimension):
        raise PromotionError("recall_quality_backend_identity_invalid")
    provider = canonical_embedding_provider(
        _required_identity(
            value.get("provider"),
            "benchmark_backend_provider",
            "recall_quality_backend_identity_invalid",
        )
    )
    provider = _required_identity(
        provider,
        "benchmark_backend_provider",
        "recall_quality_backend_identity_invalid",
    )
    runtime_route = _required_identity(
        value.get("runtime_route"),
        "benchmark_runtime_route",
        "recall_quality_backend_runtime_invalid",
    )
    usage = _adapt_recall_usage(value.get("usage"))
    requested_policy = _required_identity(
        value.get("requested_policy"),
        "benchmark_requested_policy",
        "recall_quality_backend_policy_invalid",
    )
    requested_runtime = _required_identity(
        value.get("requested_runtime"),
        "benchmark_requested_runtime",
        "recall_quality_backend_runtime_invalid",
    )
    effective_policy = _required_identity(
        value.get("effective_policy"),
        "benchmark_effective_policy",
        "recall_quality_backend_policy_invalid",
    )
    if effective_policy != requested_policy:
        raise PromotionError("recall_quality_backend_policy_invalid")
    effective_runtime = _required_identity(
        value.get("effective_runtime"),
        "benchmark_effective_runtime",
        "recall_quality_backend_runtime_invalid",
    )
    if effective_runtime != requested_runtime:
        raise PromotionError("recall_quality_backend_runtime_invalid")
    if runtime_route != f"{effective_runtime}-http-mcp":
        raise PromotionError("recall_quality_backend_runtime_invalid")
    rust_runtime = _validated_rust_runtime_identity(
        value.get("rust_runtime"),
        effective_runtime=effective_runtime,
        invalid_reason="recall_quality_backend_runtime_invalid",
    )
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping) or not runtime:
        raise PromotionError("recall_quality_backend_runtime_invalid")
    for name, item in runtime.items():
        _required_identity(
            name, "benchmark_runtime_field", "recall_quality_backend_runtime_invalid"
        )
        _required_identity(
            item,
            "benchmark_runtime_value",
            "recall_quality_backend_runtime_invalid",
        )
    channels = value.get("channel_result_names")
    if (
        not isinstance(channels, list)
        or not all(isinstance(item, str) for item in channels)
        or not {"bm25", "vector", "fused"}.issubset(channels)
    ):
        raise PromotionError("recall_quality_backend_channels_invalid")
    latencies: dict[str, float] = {}
    for name in ("cold_latency_ms", "warm_p50_ms", "warm_p95_ms"):
        latency = _bounded_metric(value.get(name), minimum=0.0)
        if latency is None:
            raise PromotionError("recall_quality_backend_latency_invalid")
        latencies[name] = latency
    return {
        "model": model,
        "model_revision": model_revision,
        "dimension": dimension,
        "provider": provider,
        "usage": usage,
        "runtime": dict(runtime),
        "transport": "streamable-http",
        "index_text_policy": candidate,
        "requested_policy": requested_policy,
        "effective_policy": effective_policy,
        "requested_runtime": requested_runtime,
        "effective_runtime": effective_runtime,
        "runtime_route": runtime_route,
        "rust_runtime": rust_runtime,
        **latencies,
    }


def _validated_rust_runtime_identity(
    value: object,
    *,
    effective_runtime: object,
    invalid_reason: str,
) -> dict[str, str] | None:
    """Validate the build-bound identity of the effective native runtime."""

    if effective_runtime not in {"python", "rust"}:
        raise PromotionError(invalid_reason)
    if effective_runtime == "python":
        if value is not None:
            raise PromotionError(invalid_reason)
        return None
    if not isinstance(value, Mapping):
        raise PromotionError(invalid_reason)
    _require_exact_fields(
        value,
        {"module", "version", "binary_sha256", "source_sha256"},
        invalid_reason,
    )
    if value.get("module") != "context_engine_core":
        raise PromotionError(invalid_reason)
    version = _required_identity(
        value.get("version"),
        "benchmark_rust_runtime_version",
        invalid_reason,
    )
    binary_sha256 = value.get("binary_sha256")
    source_sha256 = value.get("source_sha256")
    if not _is_sha256(binary_sha256) or not _is_sha256(source_sha256):
        raise PromotionError(invalid_reason)
    return {
        "module": "context_engine_core",
        "version": version,
        "binary_sha256": binary_sha256,
        "source_sha256": source_sha256,
    }


def _adapt_recall_usage(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_usage_invalid")
    return _adapt_embedding_usage(
        value,
        invalid_reason="recall_quality_usage_invalid",
        cost_reason="recall_quality_cost_invalid",
    )


def _adapt_embedding_usage(
    value: Mapping[str, Any],
    *,
    invalid_reason: str,
    cost_reason: str,
) -> dict[str, Any]:
    legacy_fields = {
        "embedding_requests",
        "embedding_input_tokens",
        "cost_usd",
        "pricing_revision",
    }
    current_fields = legacy_fields | {"cost", "cost_currency"}
    fields = set(value)
    if fields not in (legacy_fields, current_fields):
        raise PromotionError(invalid_reason)
    if not _is_positive_integer(value.get("embedding_requests")) or not _is_positive_integer(
        value.get("embedding_input_tokens")
    ):
        raise PromotionError(invalid_reason)
    cost_usd = _bounded_metric(value.get("cost_usd"), minimum=0.0)
    if fields == legacy_fields:
        if cost_usd is None:
            raise PromotionError(cost_reason)
        cost = cost_usd
        cost_currency = "USD"
    else:
        cost = _bounded_metric(value.get("cost"), minimum=0.0)
        if cost is None:
            raise PromotionError(cost_reason)
        cost_currency = value.get("cost_currency")
        if cost_currency not in {"USD", "CNY"}:
            raise PromotionError(invalid_reason)
        expected_cost_usd = cost if cost_currency == "USD" else None
        if cost_usd != expected_cost_usd:
            raise PromotionError("embedding_cost_currency_mismatch")
    pricing_revision = _required_identity(
        value.get("pricing_revision"),
        "embedding_pricing_revision",
        invalid_reason,
    )
    return {
        "embedding_requests": value["embedding_requests"],
        "embedding_input_tokens": value["embedding_input_tokens"],
        "cost": cost,
        "cost_currency": cost_currency,
        "cost_usd": cost_usd,
        "pricing_revision": pricing_revision,
    }


def _validate_recall_execution(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_execution_invalid")
    _require_exact_fields(value, {"warmup", "repeat"}, "recall_quality_execution_invalid")
    warmup = value.get("warmup")
    repeat = value.get("repeat")
    if (
        isinstance(warmup, bool)
        or not isinstance(warmup, int)
        or warmup < 0
        or not _is_positive_integer(repeat)
    ):
        raise PromotionError("recall_quality_execution_invalid")
    return {"warmup": warmup, "repeat": repeat}


def _adapt_recall_environment(
    value: object,
    *,
    backend: Mapping[str, Any],
    candidate: str,
    fusion_policy: str,
    execution: Mapping[str, int],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_environment_invalid")
    _require_exact_fields(
        value,
        {
            "provider",
            "configured_model",
            "configured_model_revision",
            "supply_runtime",
            "code_revision",
            "dataset_source",
            "source_fingerprint",
            "environment_fingerprint",
            "comparison_environment_fingerprint",
            "source_files",
            "dependencies",
            "retrieval_configuration",
            "source_commit",
            "dirty_fingerprint",
            "embedding_configuration",
        },
        "recall_quality_environment_invalid",
    )
    source_commit = value.get("source_commit")
    code_revision = value.get("code_revision")
    source_fingerprint = value.get("source_fingerprint")
    if not isinstance(source_commit, str) or not _SOURCE_REVISION.fullmatch(source_commit):
        raise PromotionError("recall_quality_environment_invalid")
    if not isinstance(code_revision, str) or not _SOURCE_REVISION.fullmatch(code_revision):
        raise PromotionError("recall_quality_environment_invalid")
    if not _is_sha256(source_fingerprint):
        raise PromotionError("recall_quality_environment_invalid")
    environment_fingerprint = value.get("environment_fingerprint")
    if not _is_sha256(environment_fingerprint):
        raise PromotionError("recall_quality_environment_invalid")
    comparison_environment_fingerprint = value.get("comparison_environment_fingerprint")
    if not _is_sha256(comparison_environment_fingerprint):
        raise PromotionError("recall_quality_environment_invalid")
    dirty_fingerprint = value.get("dirty_fingerprint")
    if not _is_sha256(dirty_fingerprint):
        raise PromotionError("recall_quality_environment_invalid")
    provider = canonical_embedding_provider(
        _required_identity(
            value.get("provider"),
            "benchmark_provider",
            "recall_quality_environment_invalid",
        )
    )
    provider = _required_identity(
        provider,
        "benchmark_provider",
        "recall_quality_environment_invalid",
    )
    configured_model = _required_identity(
        value.get("configured_model"),
        "benchmark_configured_model",
        "recall_quality_environment_invalid",
    )
    supply_runtime = _required_identity(
        value.get("supply_runtime"),
        "benchmark_supply_runtime",
        "recall_quality_environment_invalid",
    )
    dataset_source = _required_identity(
        value.get("dataset_source"),
        "benchmark_dataset_source",
        "recall_quality_environment_invalid",
    )
    if configured_model != backend["model"]:
        raise PromotionError("recall_quality_environment_model_mismatch")
    configured_model_revision = _required_identity(
        value.get("configured_model_revision"),
        "benchmark_configured_model_revision",
        "recall_quality_environment_invalid",
    )
    if configured_model_revision != backend["model_revision"]:
        raise PromotionError("recall_quality_environment_model_mismatch")
    if provider != backend["provider"]:
        raise PromotionError("recall_quality_environment_provider_mismatch")
    source_files = value.get("source_files")
    if not isinstance(source_files, list) or not source_files:
        raise PromotionError("recall_quality_environment_invalid")
    normalized_source_files: list[str] = []
    for source_file in source_files:
        normalized_source_file = _required_identity(
            source_file,
            "benchmark_source_file",
            "recall_quality_environment_invalid",
        )
        if (
            Path(normalized_source_file).is_absolute()
            or ".." in Path(normalized_source_file).parts
            or "\\" in normalized_source_file
        ):
            raise PromotionError("recall_quality_environment_invalid")
        normalized_source_files.append(normalized_source_file)
    if (
        len(set(normalized_source_files)) != len(normalized_source_files)
        or normalized_source_files != sorted(normalized_source_files)
        or dataset_source not in normalized_source_files
    ):
        raise PromotionError("recall_quality_environment_invalid")
    dependencies = value.get("dependencies")
    if not isinstance(dependencies, Mapping) or set(dependencies) != {"lancedb", "pyarrow"}:
        raise PromotionError("recall_quality_dependencies_invalid")
    for name, version in dependencies.items():
        _required_identity(name, "benchmark_dependency", "recall_quality_dependencies_invalid")
        normalized_version = _required_identity(
            version,
            "benchmark_dependency_version",
            "recall_quality_dependencies_invalid",
        )
        if normalized_version == "unavailable":
            raise PromotionError("recall_quality_dependencies_invalid")
        try:
            parsed_version = Version(normalized_version)
        except InvalidVersion as exc:
            raise PromotionError("recall_quality_dependencies_invalid") from exc
        minimum = MINIMUM_DEPENDENCY_VERSIONS.get(name)
        if minimum is not None and parsed_version < minimum:
            raise PromotionError("recall_quality_dependencies_invalid")
    retrieval_configuration = value.get("retrieval_configuration")
    if not isinstance(retrieval_configuration, Mapping) or not retrieval_configuration:
        raise PromotionError("recall_quality_configuration_invalid")
    embedding_configuration = value.get("embedding_configuration")
    if not isinstance(embedding_configuration, Mapping) or set(embedding_configuration) != {
        "provider",
        "model",
        "model_revision",
        "dimension",
    }:
        raise PromotionError("recall_quality_embedding_configuration_invalid")
    normalized_embedding_configuration = dict(embedding_configuration)
    normalized_embedding_configuration["provider"] = canonical_embedding_provider(
        normalized_embedding_configuration.get("provider")
    )
    if normalized_embedding_configuration != {
        "provider": provider,
        "model": configured_model,
        "model_revision": configured_model_revision,
        "dimension": backend["dimension"],
    }:
        raise PromotionError("recall_quality_embedding_configuration_mismatch")
    if provider.casefold() in {"unknown", "fallback", "none"}:
        raise PromotionError("recall_quality_embedding_configuration_invalid")
    if fusion_policy == MAX_V1_CONTROL_POLICY and (
        candidate != MAX_V1_CONTROL_INDEX_TEXT_POLICY
        or source_commit != code_revision
        or dataset_source != MAX_V1_CONTROL_DATASET_SOURCE
        or supply_runtime != MAX_V1_CONTROL_RUNTIME
        or dict(retrieval_configuration) != dict(MAX_V1_CONTROL_RETRIEVAL_CONFIGURATION)
        or backend["dimension"] != MAX_V1_CONTROL_EMBEDDING_DIMENSION
    ):
        raise PromotionError("recall_quality_max_v1_control_environment_invalid")
    configuration = {
        "provider": provider,
        "configured_model": configured_model,
        "configured_model_revision": configured_model_revision,
        "supply_runtime": supply_runtime,
        "candidate": candidate,
        "execution": dict(execution),
        "retrieval_configuration": dict(retrieval_configuration),
        "runtime": dict(backend["runtime"]),
        "requested_policy": backend["requested_policy"],
        "requested_runtime": backend["requested_runtime"],
        "rust_runtime": backend["rust_runtime"],
    }
    try:
        configuration_sha256 = _json_sha256(configuration)
        dependencies_sha256 = _json_sha256(dependencies)
    except (TypeError, ValueError) as exc:
        raise PromotionError("recall_quality_environment_invalid") from exc
    return {
        "source_commit": source_commit,
        "code_revision": code_revision,
        "dirty_fingerprint": dirty_fingerprint,
        "source_fingerprint": source_fingerprint,
        "environment_fingerprint": environment_fingerprint,
        "comparison_environment_fingerprint": comparison_environment_fingerprint,
        "source_files": normalized_source_files,
        "dataset_source": dataset_source,
        "configuration_sha256": configuration_sha256,
        "dependencies_sha256": dependencies_sha256,
        "embedding_configuration": normalized_embedding_configuration,
        "retrieval_configuration": dict(retrieval_configuration),
        "dependencies": dict(dependencies),
        "supply_runtime": supply_runtime,
        "execution": dict(execution),
        "runtime": dict(backend["runtime"]),
    }


def _validate_isolated_recall_corpus(value: object, *, expected_count: int) -> None:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_isolated_corpus_invalid")
    required = {"seeded", "canonical_count", "derived_count", "eligible_count"}
    if not required.issubset(value) or set(value) - required - {"seed_transport"}:
        raise PromotionError("recall_quality_isolated_corpus_invalid")
    if (
        value.get("seeded") is not True
        or value.get("canonical_count") != expected_count
        or not _is_positive_integer(value.get("eligible_count"))
        or value.get("derived_count") != value.get("eligible_count")
        or ("seed_transport" in value and value.get("seed_transport") != "public-memory-tools")
    ):
        raise PromotionError("recall_quality_isolated_corpus_invalid")


def _adapt_recall_smoke(value: object) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_smoke_invalid")
    _require_exact_fields(
        value,
        {"store", "recall", "supply", "verified_visible", "forbidden_hidden", "passed"},
        "recall_quality_smoke_invalid",
    )
    if any(value.get(name) is not True for name in value):
        raise PromotionError("recall_quality_smoke_invalid")
    return {
        "store": True,
        "recall": True,
        "context": True,
        "verified_visible": True,
        "forbidden_hidden": True,
        "passed": True,
    }


def _validate_live_call_evidence(
    public_counts_value: object,
    transport_counts_value: object,
    attestation_value: object,
    *,
    expected_queries: int,
    execution: Mapping[str, int],
    candidate_id: str,
    fusion_config: Mapping[str, Any] | None,
    backend: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(public_counts_value, Mapping) or not isinstance(
        transport_counts_value, Mapping
    ):
        raise PromotionError("recall_quality_public_call_evidence_invalid")
    expected_names = {"memory_recall", "context_supply"}
    setup_names = {"memory_store", "memory_update", "feedback_apply"}
    public_names = set(public_counts_value)
    if (
        not expected_names.issubset(public_names)
        or public_names - expected_names - setup_names
        or set(transport_counts_value) != expected_names
        or any(
            isinstance(public_counts_value.get(name), bool)
            or not isinstance(public_counts_value.get(name), int)
            or public_counts_value.get(name, -1) < 0
            for name in public_names & setup_names
        )
    ):
        raise PromotionError("recall_quality_public_call_evidence_invalid")
    transport_calls_per_tool = expected_queries * (execution["warmup"] + execution["repeat"])
    for name in expected_names:
        if public_counts_value.get(name) != expected_queries:
            raise PromotionError("recall_quality_public_call_evidence_invalid")
        if transport_counts_value.get(name) != transport_calls_per_tool:
            raise PromotionError("recall_quality_public_call_evidence_invalid")
    if not isinstance(attestation_value, Mapping):
        raise PromotionError("recall_quality_fusion_attestation_invalid")
    _require_exact_fields(
        attestation_value,
        {"attested_calls", "errors", "observed", "algorithm", "config"},
        "recall_quality_fusion_attestation_invalid",
    )
    errors = attestation_value.get("errors")
    attested_calls = attestation_value.get("attested_calls")
    expected_observed = [
        candidate_id,
        backend.get("requested_runtime"),
        backend.get("effective_runtime"),
    ]
    expected_algorithm = (
        MAX_V1_CONTROL_ALGORITHM if candidate_id == MAX_V1_CONTROL_POLICY else "weighted-rrf-v1"
    )
    expected_config = None
    if fusion_config is not None:
        expected_config = {
            **dict(fusion_config),
            "config_hash": candidate_id.partition(":")[2],
        }
    if (
        errors != []
        or attested_calls != transport_calls_per_tool * len(expected_names)
        or attestation_value.get("observed") != expected_observed
        or attestation_value.get("algorithm") != expected_algorithm
        or attestation_value.get("config") != expected_config
        or backend.get("requested_policy") != candidate_id
        or backend.get("effective_policy") != candidate_id
    ):
        raise PromotionError("recall_quality_fusion_attestation_invalid")
    if candidate_id == MAX_V1_CONTROL_POLICY and (
        backend.get("requested_runtime") != MAX_V1_CONTROL_RUNTIME
        or backend.get("effective_runtime") != MAX_V1_CONTROL_RUNTIME
        or backend.get("runtime_route") != MAX_V1_CONTROL_RUNTIME_ROUTE
    ):
        raise PromotionError("recall_quality_max_v1_control_runtime_invalid")
    return {
        "attested_calls": attested_calls,
        "errors": [],
        "observed": list(expected_observed),
        "algorithm": expected_algorithm,
        "config": expected_config,
    }


def _adapt_recall_metrics(
    value: object,
    *,
    raw_quality: object,
    raw_gate: object,
    raw_constituent_gate: object,
    expected_case_count: int,
    expected_case_identities: Sequence[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_metrics_invalid")
    _require_exact_fields(
        value,
        {
            "case_count",
            "hit_at",
            "mrr",
            "forbidden_hit_rate",
            "p50_ms",
            "p95_ms",
            "fallback_rate",
            "degradation_rate",
            "fallback_or_degradation_rate",
            "language",
            "group",
            "channels",
            "channel_states",
            "cases",
        },
        "recall_quality_metrics_invalid",
    )
    try:
        normalized_value = _copy_json_mapping(value)
        if normalized_value is None:
            raise ValueError("metrics must be a JSON object")
        normalized_channels = normalized_value.get("channels")
        if not isinstance(normalized_channels, Mapping):
            raise ValueError("metrics.channels must be a JSON object")
        for channel in normalized_channels.values():
            if not isinstance(channel, dict):
                raise ValueError("metrics channel must be a JSON object")
            for legacy_name, canonical_name in (
                ("by_language", "language"),
                ("by_group", "group"),
            ):
                if legacy_name in channel:
                    if canonical_name in channel:
                        raise ValueError("metrics channel split aliases are ambiguous")
                    channel[canonical_name] = channel.pop(legacy_name)
        from plastic_promise.core.recall_quality import (
            evaluate_best_constituent_gate,
            metric_summary_from_dict,
            quality_payload,
        )

        summary = metric_summary_from_dict(normalized_value)
        expected_quality = quality_payload(summary)
        expected_constituent_gate = evaluate_best_constituent_gate(summary)
    except (TypeError, ValueError) as exc:
        raise PromotionError("recall_quality_metrics_invalid") from exc
    if raw_quality != expected_quality:
        raise PromotionError("recall_quality_quality_projection_mismatch")
    if raw_constituent_gate != expected_constituent_gate:
        raise PromotionError("recall_quality_constituent_gate_mismatch")
    _validate_recall_gate(raw_gate)
    if summary.case_count != expected_case_count:
        raise PromotionError("recall_quality_case_count_mismatch")
    case_results = value.get("cases")
    if not isinstance(case_results, list) or len(case_results) != expected_case_count:
        raise PromotionError("recall_quality_case_results_invalid")
    seen_case_ids: set[str] = set()
    language_counts = {"en": 0, "zh": 0, "cross-lingual": 0}
    for result in case_results:
        if not isinstance(result, Mapping):
            raise PromotionError("recall_quality_case_results_invalid")
        case_id = result.get("case_id")
        language = result.get("language")
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in seen_case_ids
            or language not in language_counts
            or result.get("fallback_used") is not False
            or result.get("degraded") is not False
            or result.get("forbidden_hit") is not False
        ):
            raise PromotionError("recall_quality_case_results_invalid")
        seen_case_ids.add(case_id)
        language_counts[language] += 1
    if expected_case_identities is not None:
        observed = tuple(
            (result.get("case_id"), result.get("language"), result.get("group"))
            for result in case_results
        )
        if observed != tuple(expected_case_identities):
            raise PromotionError("recall_quality_case_identity_mismatch")
    normalized = _normalized_metric_slice(value)
    language_value = value.get("language")
    if not isinstance(language_value, Mapping) or set(language_value) != set(language_counts):
        raise PromotionError("recall_quality_language_metrics_invalid")
    normalized_language: dict[str, dict[str, Any]] = {}
    for language, count in language_counts.items():
        split = language_value.get(language)
        if not isinstance(split, Mapping) or split.get("case_count") != count:
            raise PromotionError("recall_quality_language_metrics_invalid")
        normalized_language[language] = _normalized_metric_slice(split)
    normalized["language"] = normalized_language
    normalized["_raw_p50_ms"] = value.get("p50_ms")
    return normalized


def _normalized_metric_slice(value: Mapping[str, Any]) -> dict[str, Any]:
    hit_at = value.get("hit_at")
    if not isinstance(hit_at, Mapping) or set(hit_at) != {"1", "3", "5", "10"}:
        raise PromotionError("recall_quality_hit_at_invalid")
    p50_ms = _bounded_metric(value.get("p50_ms"), minimum=0.0)
    if p50_ms is None:
        raise PromotionError("recall_quality_latency_invalid")
    return {
        "case_count": value.get("case_count"),
        "hit_at": {"1": hit_at.get("1"), "5": hit_at.get("5")},
        "mrr": value.get("mrr"),
        "forbidden_hit_rate": value.get("forbidden_hit_rate"),
        "p95_ms": value.get("p95_ms"),
        "fallback_rate": value.get("fallback_rate"),
        "degradation_rate": value.get("degradation_rate"),
        "fallback_or_degradation_rate": value.get("fallback_or_degradation_rate"),
    }


def _validate_recall_gate(value: object) -> None:
    if not isinstance(value, Mapping):
        raise PromotionError("recall_quality_gate_invalid")
    _require_exact_fields(
        value,
        {"status", "checks", "failures", "absolute_status", "best_constituent_status"},
        "recall_quality_gate_invalid",
    )
    checks = value.get("checks")
    if (
        value.get("status") != "pass"
        or value.get("failures") != []
        or value.get("absolute_status") not in {"pass", "not_configured"}
        or value.get("best_constituent_status") != "pass"
        or not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(check, Mapping) or check.get("passed") is not True for check in checks
        )
    ):
        raise PromotionError("recall_quality_gate_failed")


def _quality_report_identity_from_fields(
    report: Mapping[str, Any],
) -> QualityReportGenerationIdentity:
    backend = report.get("backend")
    benchmark = report.get("benchmark")
    corpus = report.get("corpus")
    cases = report.get("cases")
    if (
        not isinstance(backend, Mapping)
        or not isinstance(benchmark, Mapping)
        or not isinstance(corpus, Mapping)
        or not isinstance(cases, Mapping)
    ):
        raise PromotionError("quality_report_identity_invalid")
    try:
        return QualityReportGenerationIdentity(
            embedding_model=backend.get("model"),
            model_revision=backend.get("revision"),
            embedding_dimension=backend.get("dimension"),
            index_text_policy=benchmark.get("candidate"),
            benchmark_corpus_sha256=corpus.get("sha256"),
            benchmark_corpus_count=corpus.get("count"),
            benchmark_cases_sha256=cases.get("sha256"),
            benchmark_case_count=cases.get("count"),
        )
    except (TypeError, ValueError) as exc:
        raise PromotionError("quality_report_identity_invalid") from exc


def _required_identity(value: object, name: str, reason: str) -> str:
    try:
        _validate_identity_text(name, value)
    except ValueError as exc:
        raise PromotionError(reason) from exc
    assert isinstance(value, str)
    return value


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _promotion_thresholds() -> dict[str, float]:
    return {
        "min_hit_at_1": MIN_HIT_AT_1,
        "min_hit_at_5": MIN_HIT_AT_5,
        "min_mrr": MIN_MRR,
        "max_p95_ms": MAX_P95_MS,
        "max_forbidden_hit_rate": 0.0,
        "max_fallback_or_degradation_rate": 0.0,
    }


def _validate_normalized_fusion_attestation(
    value: object,
    *,
    benchmark: Mapping[str, Any],
    backend: Mapping[str, Any],
    expected_attested_calls: int,
) -> None:
    if not isinstance(value, Mapping):
        raise PromotionError("fusion_attestation_evidence_missing")
    _require_exact_fields(
        value,
        {"attested_calls", "errors", "observed", "algorithm", "config"},
        "fusion_attestation_evidence_invalid",
    )
    candidate_id = benchmark.get("candidate_id")
    expected_observed = [
        candidate_id,
        backend.get("requested_runtime"),
        backend.get("effective_runtime"),
    ]
    expected_algorithm = (
        MAX_V1_CONTROL_ALGORITHM if candidate_id == MAX_V1_CONTROL_POLICY else "weighted-rrf-v1"
    )
    fusion_config = benchmark.get("fusion_config")
    expected_config = None
    if fusion_config is not None:
        if not isinstance(fusion_config, Mapping):
            raise PromotionError("fusion_attestation_evidence_invalid")
        expected_config = {
            **dict(fusion_config),
            "config_hash": str(candidate_id).partition(":")[2],
        }
    if (
        value.get("errors") != []
        or value.get("attested_calls") != expected_attested_calls
        or value.get("observed") != expected_observed
        or value.get("algorithm") != expected_algorithm
        or value.get("config") != expected_config
        or backend.get("requested_policy") != candidate_id
        or backend.get("effective_policy") != candidate_id
    ):
        raise PromotionError("fusion_attestation_evidence_invalid")


def _validate_normalized_heldout_contract(
    *,
    benchmark: Mapping[str, Any],
    corpus: Mapping[str, Any],
    cases: Mapping[str, Any],
) -> None:
    """Rebind normalized evidence to the independently frozen held-out data."""

    contract = heldout_report_contract(benchmark.get("dataset_fingerprint"))
    if not isinstance(contract, Mapping):
        raise PromotionError("benchmark_evidence_invalid")
    expected_fields = contract.get("fields")
    if not isinstance(expected_fields, Mapping):
        raise PromotionError("benchmark_evidence_invalid")
    observed_fields = {
        "dataset_schema_version": benchmark.get("dataset_schema"),
        "dataset_revision": benchmark.get("dataset_revision"),
        "corpus.revision": corpus.get("revision"),
        "corpus.provenance_revision": corpus.get("provenance_revision"),
        "corpus.sha256": corpus.get("sha256"),
        "corpus.count": corpus.get("count"),
        "cases.sha256": cases.get("sha256"),
        "cases.count": cases.get("count"),
    }
    if set(expected_fields) != set(observed_fields) or any(
        observed_fields[name] != expected for name, expected in expected_fields.items()
    ):
        raise PromotionError("recall_quality_heldout_contract_mismatch")


def _validate_quality_report(
    report: Mapping[str, Any] | None,
    *,
    spec: GenerationSpec,
) -> None:
    if not isinstance(report, Mapping):
        raise PromotionError("quality_report_missing")
    benchmark_probe = report.get("benchmark")
    report_v2 = isinstance(benchmark_probe, Mapping) and "candidate_dimension" in benchmark_probe
    report_fields = {
        "schema",
        "benchmark",
        "gate",
        "degraded",
        "publishable_claim",
        "backend",
        "cases",
        "corpus",
        "environment",
        "smoke",
        "usage",
        "metrics",
    }
    if report_v2:
        report_fields.add("fusion_attestation")
    _require_exact_fields(
        report,
        report_fields,
        "quality_report_fields_invalid",
    )
    if report.get("schema") != QUALITY_REPORT_SCHEMA:
        raise PromotionError("quality_report_schema_invalid")
    if report.get("degraded") is not False:
        raise PromotionError("quality_report_degraded")
    if report.get("publishable_claim") is not True:
        raise PromotionError("publishable_claim_required")

    benchmark = report.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise PromotionError("benchmark_evidence_missing")
    benchmark_base_fields = {"report_schema", "dataset_schema", "dataset_revision", "candidate"}
    benchmark_v2 = "candidate_dimension" in benchmark
    _require_exact_fields(
        benchmark,
        benchmark_base_fields
        if not benchmark_v2
        else benchmark_base_fields
        | {
            "dataset_role",
            "dataset_fingerprint",
            "candidate_dimension",
            "candidate_id",
            "manifest_hash",
            "fusion_config",
        },
        "benchmark_evidence_invalid",
    )
    if (
        benchmark.get("report_schema") != RECALL_QUALITY_REPORT_SCHEMA
        or benchmark.get("dataset_schema") != RECALL_QUALITY_DATASET_SCHEMA
        or benchmark.get("candidate") not in {"legacy", "compact-v2"}
    ):
        raise PromotionError("benchmark_evidence_invalid")
    if spec.index_text_policy is not None and benchmark.get("candidate") != spec.index_text_policy:
        raise PromotionError("index_text_policy_mismatch")
    try:
        _validate_identity_text("benchmark_dataset_revision", benchmark.get("dataset_revision"))
    except ValueError as exc:
        raise PromotionError("benchmark_evidence_invalid") from exc
    if benchmark_v2:
        if benchmark.get("dataset_role") != "held-out" or not _is_sha256(
            benchmark.get("dataset_fingerprint")
        ):
            raise PromotionError("benchmark_evidence_invalid")
        contract = heldout_report_contract(benchmark.get("dataset_fingerprint"))
        if not isinstance(contract, Mapping):
            raise PromotionError("benchmark_evidence_invalid")
        expected = contract.get("fields")
        if not isinstance(expected, Mapping):
            raise PromotionError("benchmark_evidence_invalid")
        if benchmark.get("dataset_schema") != expected.get("dataset_schema_version"):
            raise PromotionError("benchmark_evidence_invalid")
        if benchmark.get("dataset_revision") != expected.get("dataset_revision"):
            raise PromotionError("benchmark_evidence_invalid")
        try:
            _validate_v2_fusion_evidence(
                benchmark.get("candidate_id"),
                benchmark.get("fusion_config"),
                benchmark.get("manifest_hash"),
            )
        except (PromotionError, TypeError, ValueError) as exc:
            raise PromotionError("benchmark_evidence_invalid") from exc

    gate = report.get("gate")
    if not isinstance(gate, Mapping):
        raise PromotionError("quality_gate_invalid")
    _require_exact_fields(gate, {"status", "policy", "thresholds"}, "quality_gate_invalid")
    if gate.get("status") != "pass" or gate.get("policy") != QUALITY_GATE_POLICY:
        raise PromotionError("quality_gate_failed")
    thresholds = gate.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise PromotionError("quality_thresholds_invalid")
    expected_thresholds = _promotion_thresholds()
    _require_exact_fields(
        thresholds,
        set(expected_thresholds),
        "quality_thresholds_invalid",
    )
    if any(thresholds.get(name) != value for name, value in expected_thresholds.items()):
        raise PromotionError("quality_thresholds_invalid")

    backend = report.get("backend")
    if not isinstance(backend, Mapping):
        raise PromotionError("live_backend_evidence_missing")
    backend_fields = {
        "mode",
        "fallback_used",
        "degraded_used",
        "model",
        "revision",
        "dimension",
    }
    _require_exact_fields(
        backend,
        backend_fields
        if not benchmark_v2
        else backend_fields
        | {
            "provider",
            "transport",
            "index_text_policy",
            "requested_policy",
            "effective_policy",
            "requested_runtime",
            "effective_runtime",
            "runtime_route",
            "rust_runtime",
        },
        "live_backend_evidence_invalid",
    )
    if (
        backend.get("mode") != "live"
        or backend.get("fallback_used") is not False
        or backend.get("degraded_used") is not False
    ):
        raise PromotionError("live_backend_evidence_invalid")
    if (
        backend.get("model") != spec.embedding_model
        or backend.get("revision") != spec.model_revision
        or backend.get("dimension") != spec.embedding_dimension
    ):
        raise PromotionError("backend_identity_mismatch")
    if benchmark_v2:
        candidate_id = benchmark.get("candidate_id")
        requested_runtime = backend.get("requested_runtime")
        effective_runtime = backend.get("effective_runtime")
        if (
            backend.get("transport") != "streamable-http"
            or backend.get("index_text_policy") != benchmark.get("candidate")
            or backend.get("requested_policy") != candidate_id
            or backend.get("effective_policy") != candidate_id
            or not isinstance(requested_runtime, str)
            or not requested_runtime
            or effective_runtime != requested_runtime
            or backend.get("runtime_route") != f"{effective_runtime}-http-mcp"
        ):
            raise PromotionError("live_backend_evidence_invalid")
        try:
            _validate_identity_text("backend_provider", backend.get("provider"))
        except ValueError as exc:
            raise PromotionError("live_backend_evidence_invalid") from exc
        _validated_rust_runtime_identity(
            backend.get("rust_runtime"),
            effective_runtime=effective_runtime,
            invalid_reason="live_backend_evidence_invalid",
        )
        if candidate_id == MAX_V1_CONTROL_POLICY and (
            requested_runtime != MAX_V1_CONTROL_RUNTIME
            or effective_runtime != MAX_V1_CONTROL_RUNTIME
            or backend.get("runtime_route") != MAX_V1_CONTROL_RUNTIME_ROUTE
            or backend.get("dimension") != MAX_V1_CONTROL_EMBEDDING_DIMENSION
        ):
            raise PromotionError("max_v1_control_runtime_invalid")

    cases = report.get("cases")
    if not isinstance(cases, Mapping):
        raise PromotionError("fixed_case_evidence_missing")
    _require_exact_fields(cases, {"sha256", "count"}, "fixed_case_evidence_invalid")
    case_sha256 = cases.get("sha256")
    case_count = cases.get("count")
    if (
        not isinstance(case_sha256, str)
        or not _SHA256.fullmatch(case_sha256)
        or not _is_positive_integer(case_count)
    ):
        raise PromotionError("fixed_case_evidence_invalid")
    if case_sha256 != spec.benchmark_cases_sha256 or case_count != spec.benchmark_case_count:
        raise PromotionError("fixed_case_binding_mismatch")

    corpus = report.get("corpus")
    if not isinstance(corpus, Mapping):
        raise PromotionError("fixed_corpus_evidence_missing")
    _require_exact_fields(
        corpus,
        {"sha256", "count", "revision", "provenance_revision"},
        "fixed_corpus_evidence_invalid",
    )
    try:
        _validate_identity_text("corpus_revision", corpus.get("revision"))
        _validate_identity_text(
            "corpus_provenance_revision",
            corpus.get("provenance_revision"),
        )
    except ValueError as exc:
        raise PromotionError("fixed_corpus_evidence_invalid") from exc
    if (
        corpus.get("sha256") != spec.benchmark_corpus_sha256
        or corpus.get("count") != spec.benchmark_corpus_count
    ):
        raise PromotionError("fixed_corpus_binding_mismatch")
    if benchmark_v2:
        _validate_normalized_heldout_contract(
            benchmark=benchmark,
            corpus=corpus,
            cases=cases,
        )

    environment = report.get("environment")
    if not isinstance(environment, Mapping):
        raise PromotionError("environment_evidence_missing")
    environment_fields = {
        "source_commit",
        "source_fingerprint",
        "configuration_sha256",
        "dependencies_sha256",
    }
    if benchmark_v2:
        environment_fields |= {
            "code_revision",
            "dirty_fingerprint",
            "environment_fingerprint",
            "comparison_environment_fingerprint",
            "source_files",
            "dataset_source",
            "embedding_configuration",
            "retrieval_configuration",
            "dependencies",
            "supply_runtime",
            "execution",
            "runtime",
        }
    _require_exact_fields(environment, environment_fields, "environment_evidence_invalid")
    if not isinstance(environment.get("source_commit"), str) or not _SOURCE_REVISION.fullmatch(
        environment["source_commit"]
    ):
        raise PromotionError("environment_evidence_invalid")
    for field_name in ("source_fingerprint", "configuration_sha256", "dependencies_sha256"):
        value = environment.get(field_name)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise PromotionError("environment_evidence_invalid")
    if benchmark_v2:
        if (
            not _is_sha256(environment.get("dirty_fingerprint"))
            or not _is_sha256(environment.get("environment_fingerprint"))
            or not _is_sha256(environment.get("comparison_environment_fingerprint"))
        ):
            raise PromotionError("environment_evidence_invalid")
        code_revision = environment.get("code_revision")
        if not isinstance(code_revision, str) or not _SOURCE_REVISION.fullmatch(code_revision):
            raise PromotionError("environment_evidence_invalid")
        dataset_source = environment.get("dataset_source")
        try:
            _validate_identity_text("benchmark_dataset_source", dataset_source)
        except ValueError as exc:
            raise PromotionError("environment_evidence_invalid") from exc
        source_files = environment.get("source_files")
        if not isinstance(source_files, list) or not source_files:
            raise PromotionError("environment_evidence_invalid")
        normalized_source_files: list[str] = []
        for source_file in source_files:
            try:
                _validate_identity_text("benchmark_source_file", source_file)
            except ValueError as exc:
                raise PromotionError("environment_evidence_invalid") from exc
            assert isinstance(source_file, str)
            if (
                Path(source_file).is_absolute()
                or ".." in Path(source_file).parts
                or "\\" in source_file
            ):
                raise PromotionError("environment_evidence_invalid")
            normalized_source_files.append(source_file)
        if (
            normalized_source_files != sorted(normalized_source_files)
            or len(normalized_source_files) != len(set(normalized_source_files))
            or dataset_source not in normalized_source_files
        ):
            raise PromotionError("environment_evidence_invalid")
        embedding_configuration = environment.get("embedding_configuration")
        if not isinstance(embedding_configuration, Mapping) or embedding_configuration != {
            "provider": backend.get("provider"),
            "model": backend.get("model"),
            "model_revision": backend.get("revision"),
            "dimension": backend.get("dimension"),
        }:
            raise PromotionError("environment_evidence_invalid")
        dependencies = environment.get("dependencies")
        if not isinstance(dependencies, Mapping) or set(dependencies) != {"lancedb", "pyarrow"}:
            raise PromotionError("environment_evidence_invalid")
        for name, dependency_version in dependencies.items():
            if not isinstance(dependency_version, str) or not dependency_version.strip():
                raise PromotionError("environment_evidence_invalid")
            try:
                parsed_version = Version(dependency_version)
            except InvalidVersion as exc:
                raise PromotionError("environment_evidence_invalid") from exc
            minimum = MINIMUM_DEPENDENCY_VERSIONS.get(name)
            if minimum is not None and parsed_version < minimum:
                raise PromotionError("environment_evidence_invalid")
        retrieval_configuration = environment.get("retrieval_configuration")
        if not isinstance(retrieval_configuration, Mapping) or not retrieval_configuration:
            raise PromotionError("environment_evidence_invalid")
        supply_runtime = environment.get("supply_runtime")
        try:
            _validate_identity_text("benchmark_supply_runtime", supply_runtime)
        except ValueError as exc:
            raise PromotionError("environment_evidence_invalid") from exc
        execution = environment.get("execution")
        runtime = environment.get("runtime")
        if not isinstance(execution, Mapping) or set(execution) != {"warmup", "repeat"}:
            raise PromotionError("environment_evidence_invalid")
        if (
            isinstance(execution.get("warmup"), bool)
            or not isinstance(execution.get("warmup"), int)
            or execution.get("warmup", -1) < 0
            or not _is_positive_integer(execution.get("repeat"))
        ):
            raise PromotionError("environment_evidence_invalid")
        if not isinstance(runtime, Mapping) or not runtime:
            raise PromotionError("environment_evidence_invalid")
        for runtime_name, runtime_value in runtime.items():
            try:
                _validate_identity_text("benchmark_runtime_field", runtime_name)
                _validate_identity_text("benchmark_runtime_value", runtime_value)
            except ValueError as exc:
                raise PromotionError("environment_evidence_invalid") from exc
        configuration = {
            "provider": backend.get("provider"),
            "configured_model": backend.get("model"),
            "configured_model_revision": backend.get("revision"),
            "supply_runtime": supply_runtime,
            "candidate": backend.get("index_text_policy"),
            "execution": dict(execution),
            "retrieval_configuration": dict(retrieval_configuration),
            "runtime": dict(runtime),
            "requested_policy": backend.get("requested_policy"),
            "requested_runtime": backend.get("requested_runtime"),
            "rust_runtime": backend.get("rust_runtime"),
        }
        if environment.get("configuration_sha256") != _json_sha256(
            configuration
        ) or environment.get("dependencies_sha256") != _json_sha256(dependencies):
            raise PromotionError("environment_evidence_invalid")
        if benchmark.get("candidate_id") == MAX_V1_CONTROL_POLICY and (
            benchmark.get("candidate") != MAX_V1_CONTROL_INDEX_TEXT_POLICY
            or environment.get("source_commit") != code_revision
            or dataset_source != MAX_V1_CONTROL_DATASET_SOURCE
            or supply_runtime != MAX_V1_CONTROL_RUNTIME
            or dict(retrieval_configuration) != dict(MAX_V1_CONTROL_RETRIEVAL_CONFIGURATION)
            or embedding_configuration.get("dimension") != MAX_V1_CONTROL_EMBEDDING_DIMENSION
        ):
            raise PromotionError("max_v1_control_environment_invalid")

        _validate_normalized_fusion_attestation(
            report.get("fusion_attestation"),
            benchmark=benchmark,
            backend=backend,
            expected_attested_calls=(
                int(case_count) * (int(execution["warmup"]) + int(execution["repeat"])) * 2
            ),
        )

    smoke = report.get("smoke")
    if not isinstance(smoke, Mapping):
        raise PromotionError("store_recall_context_evidence_missing")
    _require_exact_fields(
        smoke,
        {"store", "recall", "context", "verified_visible", "forbidden_hidden", "passed"},
        "store_recall_context_evidence_invalid",
    )
    if any(smoke.get(name) is not True for name in smoke):
        raise PromotionError("store_recall_context_evidence_invalid")

    usage = report.get("usage")
    if not isinstance(usage, Mapping):
        raise PromotionError("embedding_usage_evidence_missing")
    _adapt_embedding_usage(
        usage,
        invalid_reason="embedding_usage_evidence_invalid",
        cost_reason="cost_metric_invalid",
    )

    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        raise PromotionError("retrieval_metrics_missing")
    overall_fields = _metric_fields() | {"language"}
    _require_exact_fields(metrics, overall_fields, "retrieval_metrics_fields_invalid")
    overall_count = _validate_retrieval_metric_slice(metrics)
    if case_count != overall_count:
        raise PromotionError("fixed_case_evidence_invalid")

    language = metrics.get("language")
    if not isinstance(language, Mapping):
        raise PromotionError("language_evidence_missing")
    required_languages = {"en", "zh", "cross-lingual"}
    _require_exact_fields(language, required_languages, "language_evidence_invalid")
    language_count = 0
    for split_name in ("en", "zh", "cross-lingual"):
        split = language.get(split_name)
        if not isinstance(split, Mapping):
            raise PromotionError("language_evidence_invalid")
        _require_exact_fields(split, _metric_fields(), "language_evidence_invalid")
        language_count += _validate_retrieval_metric_slice(split)
    if language_count != overall_count:
        raise PromotionError("language_evidence_invalid")


def _metric_fields() -> set[str]:
    return {
        "case_count",
        "hit_at",
        "mrr",
        "forbidden_hit_rate",
        "p95_ms",
        "fallback_rate",
        "degradation_rate",
        "fallback_or_degradation_rate",
    }


def _validate_retrieval_metric_slice(metrics: Mapping[str, Any]) -> int:
    case_count = metrics.get("case_count")
    if not _is_positive_integer(case_count):
        raise PromotionError("retrieval_case_count_invalid")
    hit_at = metrics.get("hit_at")
    if not isinstance(hit_at, Mapping):
        raise PromotionError("hit_at_metric_missing")
    _require_exact_fields(hit_at, {"1", "5"}, "hit_at_metric_invalid")
    hit_at_1 = _bounded_metric(hit_at.get("1"), minimum=MIN_HIT_AT_1, maximum=1.0)
    hit_at_5 = _bounded_metric(hit_at.get("5"), minimum=MIN_HIT_AT_5, maximum=1.0)
    if hit_at_1 is None or hit_at_5 is None or hit_at_1 > hit_at_5:
        raise PromotionError("hit_at_metric_invalid")
    mrr = _bounded_metric(metrics.get("mrr"), minimum=MIN_MRR, maximum=1.0)
    if mrr is None:
        raise PromotionError("mrr_metric_invalid")
    p95_ms = _bounded_metric(metrics.get("p95_ms"), minimum=0.0, maximum=MAX_P95_MS)
    if p95_ms is None:
        raise PromotionError("p95_metric_invalid")
    forbidden = _bounded_metric(metrics.get("forbidden_hit_rate"), minimum=0.0, maximum=0.0)
    if forbidden is None:
        raise PromotionError("forbidden_metric_invalid")
    for field_name in ("fallback_rate", "degradation_rate", "fallback_or_degradation_rate"):
        rate = _bounded_metric(metrics.get(field_name), minimum=0.0, maximum=0.0)
        if rate is None:
            raise PromotionError("fallback_or_degradation_detected")
    return case_count


def _require_exact_fields(
    payload: Mapping[str, Any],
    expected: set[str],
    reason: str,
) -> None:
    if set(payload) != expected:
        raise PromotionError(reason)


def _bounded_metric(
    value: object,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < minimum:
        return None
    if maximum is not None and numeric > maximum:
        return None
    return numeric


def _open_root_directory(
    root: str | os.PathLike[str],
    *,
    create: bool,
) -> tuple[Path, int]:
    requested = Path(root).expanduser()
    absolute = Path(os.path.abspath(os.fspath(requested)))
    parts = absolute.parts
    if not parts or parts[0] != os.sep:
        raise GenerationError("generation_root_invalid")
    current_fd = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for index, component in enumerate(parts[1:], start=1):
            try:
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise GenerationError("generation_root_not_found") from None
                mode = 0o700 if index == len(parts) - 1 else 0o755
                with suppress(FileExistsError):
                    os.mkdir(component, mode=mode, dir_fd=current_fd)
                try:
                    child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                except OSError as exc:
                    raise GenerationError("generation_root_creation_raced") from exc
                _fsync_fd(current_fd, GenerationError, "generation_parent_fsync_failed")
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise GenerationError("generation_root_must_not_contain_symlink") from exc
                raise GenerationError("generation_root_unreadable") from exc
            os.close(current_fd)
            current_fd = child_fd
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            raise GenerationError("generation_root_not_directory")
        return absolute, current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_directory_at(parent_fd: int, name: str, reason: str) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise GenerationError(reason) from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(before.st_mode) or not _same_entry(before, opened):
        os.close(descriptor)
        raise GenerationError(reason)
    return descriptor


def _require_private_directory(descriptor: int, reason: str) -> None:
    mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
    if mode & 0o022:
        raise GenerationError(reason)


def _directory_identity(value: os.stat_result) -> tuple[int, int]:
    if not stat.S_ISDIR(value.st_mode):
        raise GenerationError("directory_identity_invalid")
    return value.st_dev, value.st_ino


def _entry_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)


def _entry_identity_at(parent_fd: int, name: str) -> tuple[int, int, int]:
    try:
        value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise GenerationError("filesystem_entry_not_found") from exc
    return _entry_identity(value)


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return _entry_identity(left) == _entry_identity(right)


def _same_file_version(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_entry(left, right)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _require_entry_identity(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    reason: str,
    *,
    error_type: type[GenerationError] = GenerationError,
) -> None:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise error_type(reason) from exc
    if not _same_entry(observed, expected):
        raise error_type(reason)


def _index_tree_sha256(index_fd: int) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"plastic-promise/index-tree/v1\0")
    _hash_directory(index_fd, b"", hasher)
    return hasher.hexdigest()


def _hash_directory(directory_fd: int, relative: bytes, hasher: Any) -> None:
    before_directory = os.fstat(directory_fd)
    if not stat.S_ISDIR(before_directory.st_mode):
        raise GenerationError("index_contains_unsafe_entry")
    try:
        names = sorted(os.listdir(directory_fd), key=os.fsencode)
    except OSError as exc:
        raise GenerationError("index_tree_unreadable") from exc
    for name in names:
        encoded_name = os.fsencode(name)
        child_relative = encoded_name if not relative else relative + b"/" + encoded_name
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise GenerationError("index_tree_changed_while_hashing") from exc
        if stat.S_ISLNK(entry.st_mode):
            raise GenerationError("index_contains_symlink")
        if stat.S_ISDIR(entry.st_mode):
            child_fd = _open_directory_at(directory_fd, name, "index_tree_changed_while_hashing")
            try:
                _hash_record(hasher, b"D", child_relative)
                _hash_directory(child_fd, child_relative, hasher)
                _require_entry_identity(
                    directory_fd,
                    name,
                    os.fstat(child_fd),
                    "index_tree_changed_while_hashing",
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise GenerationError("index_contains_unsafe_entry")
        if entry.st_size < 0 or entry.st_size > _MAX_ARTIFACT_FILE_BYTES:
            raise GenerationError("index_file_too_large")
        try:
            file_fd = os.open(name, _READ_FILE_FLAGS, dir_fd=directory_fd)
        except OSError as exc:
            raise GenerationError("index_tree_changed_while_hashing") from exc
        try:
            opened = os.fstat(file_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not _same_entry(entry, opened)
            ):
                raise GenerationError("index_tree_changed_while_hashing")
            _hash_record(hasher, b"F", child_relative)
            hasher.update(opened.st_size.to_bytes(8, "big", signed=False))
            total = 0
            while True:
                chunk = os.read(file_fd, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > opened.st_size:
                    raise GenerationError("index_tree_changed_while_hashing")
                hasher.update(chunk)
            after = os.fstat(file_fd)
            if total != opened.st_size or not _same_file_version(opened, after):
                raise GenerationError("index_tree_changed_while_hashing")
            _require_entry_identity(
                directory_fd,
                name,
                after,
                "index_tree_changed_while_hashing",
            )
        finally:
            os.close(file_fd)
    after_directory = os.fstat(directory_fd)
    if not _same_file_version(before_directory, after_directory):
        raise GenerationError("index_tree_changed_while_hashing")


def _hash_record(hasher: Any, kind: bytes, path: bytes) -> None:
    hasher.update(kind)
    hasher.update(len(path).to_bytes(8, "big", signed=False))
    hasher.update(path)


def _remove_tree_contents(directory_fd: int) -> None:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise GenerationError("generation_cleanup_failed") from exc
    for name in names:
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GenerationError("generation_cleanup_failed") from exc
        if stat.S_ISDIR(entry.st_mode):
            child_fd = _open_directory_at(directory_fd, name, "generation_cleanup_raced")
            try:
                _remove_tree_contents(child_fd)
                _require_entry_identity(
                    directory_fd,
                    name,
                    os.fstat(child_fd),
                    "generation_cleanup_raced",
                )
            finally:
                os.close(child_fd)
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except OSError as exc:
                raise GenerationError("generation_cleanup_raced") from exc
        else:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError as exc:
                raise GenerationError("generation_cleanup_raced") from exc


def _read_bounded_fd(descriptor: int, maximum: int, too_large_reason: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ManifestError(too_large_reason)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(payload):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("short_write")
        written += count


def _lexists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _fsync_fd(
    descriptor: int,
    error_type: type[GenerationError],
    reason: str,
) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise error_type(reason) from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ArtifactVerification",
    "ArtifactVerificationRequest",
    "ArtifactVerifier",
    "BuildResult",
    "GenerationError",
    "GenerationManager",
    "GenerationManifest",
    "GenerationSpec",
    "INDEX_MATERIAL_SCHEMA",
    "ManifestError",
    "PromotionError",
    "QualityReportGenerationIdentity",
    "QUALITY_GATE_POLICY",
    "QUALITY_REPORT_SCHEMA",
    "RECALL_QUALITY_DATASET_SCHEMA",
    "RECALL_QUALITY_REPORT_SCHEMA",
    "adapt_recall_quality_report",
    "index_material_sha256",
    "quality_report_generation_identity",
]

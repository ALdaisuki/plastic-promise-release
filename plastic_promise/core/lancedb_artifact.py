"""Open-only verification for immutable LanceDB generation artifacts."""

from __future__ import annotations

import json
import math
import os
import stat
import sys
import warnings
from typing import TYPE_CHECKING, Any

import lancedb
import pyarrow as pa

from plastic_promise.core.lancedb_generation import (
    ArtifactVerification,
    ArtifactVerificationRequest,
    index_material_sha256,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_TABLE_NAME = "memory_vectors"
_EXPECTED_COLUMNS = ("memory_id", "vector", "text", "tier", "category", "scope")
_VERIFY_CHILD_ARGUMENT = "--verify-pinned-cwd"
_MAX_CHILD_RESULT_BYTES = 64 * 1024
_SCAN_BATCH_SIZE = 256


class LanceDBArtifactError(RuntimeError):
    """The selected generation is not a safe, readable vector artifact."""


def verify_lancedb_artifact(
    request: ArtifactVerificationRequest,
) -> ArtifactVerification:
    """Verify a pinned generation without creating tables or indexes.

    LanceDB accepts filesystem paths rather than directory descriptors. A
    short-lived interpreter is therefore started with its working directory
    pinned to ``request.index_fd`` before ``exec``. The child opens ``.`` only;
    it never reopens the public generation path.

    ``memory_vectors`` does not persist the embedding model or revision. Those
    two identity fields (and ``index_schema``) are deliberately returned from
    the manager-authenticated request. The vector dimension, table schema, row
    count, IDs, and vector values are independently observed from the artifact.
    """

    if not isinstance(request, ArtifactVerificationRequest):
        raise TypeError("artifact_verification_request_required")
    try:
        descriptor_stat = os.fstat(request.index_fd)
    except OSError as exc:
        raise LanceDBArtifactError("artifact_index_descriptor_invalid") from exc
    if not stat.S_ISDIR(descriptor_stat.st_mode):
        raise LanceDBArtifactError("artifact_index_descriptor_not_directory")

    payload = {
        "generation_id": request.generation_id,
        "index_schema": request.index_schema,
        "embedding_model": request.embedding_model,
        "model_revision": request.model_revision,
        "embedding_dimension": request.embedding_dimension,
        "expected_tree_sha256": request.expected_tree_sha256,
        "index_text_policy": request.index_text_policy,
        "expected_index_material_sha256": request.expected_index_material_sha256,
    }
    result = _inspect_in_pinned_child(request.index_fd, payload)
    row_count = result.get("row_count")
    dimension = result.get("embedding_dimension")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise LanceDBArtifactError("artifact_verifier_result_invalid")
    if (
        isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or dimension != request.embedding_dimension
    ):
        raise LanceDBArtifactError("artifact_vector_dimension_mismatch")
    material_sha256 = result.get("index_material_sha256")
    if material_sha256 != request.expected_index_material_sha256:
        raise LanceDBArtifactError("artifact_index_material_mismatch")
    return ArtifactVerification(
        row_count=row_count,
        index_schema=request.index_schema,
        embedding_model=request.embedding_model,
        model_revision=request.model_revision,
        embedding_dimension=dimension,
        index_material_sha256=material_sha256,
    )


def _inspect_in_pinned_child(index_fd: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not hasattr(os, "fork"):
        raise LanceDBArtifactError("artifact_descriptor_verification_unsupported")
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    child_environment = dict(os.environ)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    existing_pythonpath = child_environment.get("PYTHONPATH", "")
    child_environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (project_root, existing_pythonpath) if part
    )
    child_arguments = [
        sys.executable,
        "-m",
        __name__,
        _VERIFY_CHILD_ARGUMENT,
        encoded,
    ]
    read_fd, write_fd = os.pipe()
    try:
        # The child performs only descriptor operations followed by exec. Python
        # 3.12 warns for every multithreaded fork even for this exec-only shape.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:  # pragma: no cover - assertions run against the parent result.
        try:
            os.close(read_fd)
            os.fchdir(index_fd)
            os.dup2(write_fd, 1)
            if write_fd != 1:
                os.close(write_fd)
            os.execve(
                sys.executable,
                child_arguments,
                child_environment,
            )
        except BaseException:
            os._exit(127)

    os.close(write_fd)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(read_fd, 8192)
            if not chunk:
                break
            total += len(chunk)
            if total <= _MAX_CHILD_RESULT_BYTES:
                chunks.append(chunk)
    finally:
        os.close(read_fd)
    _, wait_status = os.waitpid(pid, 0)
    exit_code = os.waitstatus_to_exitcode(wait_status)
    if total > _MAX_CHILD_RESULT_BYTES:
        raise LanceDBArtifactError("artifact_verifier_result_too_large")
    try:
        result = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LanceDBArtifactError("artifact_verifier_process_failed") from exc
    if not isinstance(result, dict):
        raise LanceDBArtifactError("artifact_verifier_result_invalid")
    if exit_code != 0 or result.get("ok") is not True:
        reason = result.get("reason")
        if not isinstance(reason, str) or not reason.startswith("artifact_"):
            reason = "artifact_verification_failed"
        raise LanceDBArtifactError(reason)
    return result


def _inspect_current_directory(payload: Mapping[str, Any]) -> dict[str, Any]:
    expected_dimension = payload.get("embedding_dimension")
    if (
        isinstance(expected_dimension, bool)
        or not isinstance(expected_dimension, int)
        or expected_dimension <= 0
    ):
        raise LanceDBArtifactError("artifact_expected_dimension_invalid")
    index_text_policy = payload.get("index_text_policy")
    expected_material_sha256 = payload.get("expected_index_material_sha256")
    if (index_text_policy is None) != (expected_material_sha256 is None):
        raise LanceDBArtifactError("artifact_expected_index_material_invalid")
    try:
        database = lancedb.connect(os.curdir)
        table = database.open_table(_TABLE_NAME)
    except Exception as exc:
        raise LanceDBArtifactError("artifact_memory_vectors_unavailable") from exc

    _validate_schema(table.schema, expected_dimension)
    try:
        declared_count = int(table.count_rows())
        batches = (
            table.search()
            .select(["memory_id", "vector", "text", "tier", "category", "scope"])
            .to_batches(batch_size=_SCAN_BATCH_SIZE)
        )
    except Exception as exc:
        raise LanceDBArtifactError("artifact_memory_vectors_unreadable") from exc

    observed_count = 0
    memory_ids: set[str] = set()
    material_rows: dict[str, dict[str, str]] = {}
    try:
        for batch in batches:
            id_values = batch.column(batch.schema.get_field_index("memory_id")).to_pylist()
            vector_values = batch.column(batch.schema.get_field_index("vector")).to_pylist()
            text_values = batch.column(batch.schema.get_field_index("text")).to_pylist()
            tier_values = batch.column(batch.schema.get_field_index("tier")).to_pylist()
            category_values = batch.column(batch.schema.get_field_index("category")).to_pylist()
            scope_values = batch.column(batch.schema.get_field_index("scope")).to_pylist()
            if (
                len(
                    {
                        len(id_values),
                        len(vector_values),
                        len(text_values),
                        len(tier_values),
                        len(category_values),
                        len(scope_values),
                    }
                )
                != 1
            ):
                raise LanceDBArtifactError("artifact_column_length_mismatch")
            for memory_id, vector, text, tier, category, scope in zip(
                id_values,
                vector_values,
                text_values,
                tier_values,
                category_values,
                scope_values,
                strict=True,
            ):
                _validate_memory_id(memory_id, memory_ids)
                _validate_vector(vector, expected_dimension)
                memory_ids.add(memory_id)
                material_rows[memory_id] = {
                    "text": text,
                    "tier": tier,
                    "category": category,
                    "scope": scope,
                }
                observed_count += 1
    except LanceDBArtifactError:
        raise
    except Exception as exc:
        raise LanceDBArtifactError("artifact_memory_vectors_unreadable") from exc

    if observed_count != declared_count:
        raise LanceDBArtifactError("artifact_row_count_inconsistent")
    material_sha256 = None
    if index_text_policy is not None:
        try:
            material_sha256 = index_material_sha256(index_text_policy, material_rows)
        except (TypeError, ValueError) as exc:
            raise LanceDBArtifactError("artifact_index_material_invalid") from exc
        if material_sha256 != expected_material_sha256:
            raise LanceDBArtifactError("artifact_index_material_mismatch")
    return {
        "ok": True,
        "row_count": observed_count,
        "embedding_dimension": expected_dimension,
        "index_material_sha256": material_sha256,
    }


def _validate_schema(schema: pa.Schema, expected_dimension: int) -> None:
    if not isinstance(schema, pa.Schema) or tuple(schema.names) != _EXPECTED_COLUMNS:
        raise LanceDBArtifactError("artifact_schema_mismatch")
    if not pa.types.is_string(schema.field("memory_id").type):
        raise LanceDBArtifactError("artifact_schema_mismatch")
    vector_type = schema.field("vector").type
    if (
        not pa.types.is_fixed_size_list(vector_type)
        or vector_type.list_size != expected_dimension
        or not pa.types.is_float32(vector_type.value_type)
    ):
        raise LanceDBArtifactError("artifact_vector_schema_mismatch")
    for column in ("text", "tier", "category", "scope"):
        if not pa.types.is_string(schema.field(column).type):
            raise LanceDBArtifactError("artifact_schema_mismatch")


def _validate_memory_id(memory_id: object, observed: set[str]) -> None:
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise LanceDBArtifactError("artifact_memory_id_empty")
    if memory_id in observed:
        raise LanceDBArtifactError("artifact_memory_id_duplicate")


def _validate_vector(vector: object, expected_dimension: int) -> None:
    if not isinstance(vector, list) or len(vector) != expected_dimension:
        raise LanceDBArtifactError("artifact_vector_dimension_mismatch")
    nonzero = False
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LanceDBArtifactError("artifact_vector_value_invalid")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise LanceDBArtifactError("artifact_vector_nonfinite")
        nonzero = nonzero or numeric != 0.0
    if not nonzero:
        raise LanceDBArtifactError("artifact_vector_zero")


def _child_main(encoded_payload: str) -> int:
    try:
        payload = json.loads(encoded_payload)
        if not isinstance(payload, dict):
            raise LanceDBArtifactError("artifact_verification_payload_invalid")
        result = _inspect_current_directory(payload)
        exit_code = 0
    except LanceDBArtifactError as exc:
        result = {"ok": False, "reason": str(exc)}
        exit_code = 1
    except Exception:
        result = {"ok": False, "reason": "artifact_verification_failed"}
        exit_code = 1
    os.write(
        1,
        json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8"),
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover - exercised through the parent verifier.
    if len(sys.argv) == 3 and sys.argv[1] == _VERIFY_CHILD_ARGUMENT:
        raise SystemExit(_child_main(sys.argv[2]))
    raise SystemExit(2)


__all__ = ["LanceDBArtifactError", "verify_lancedb_artifact"]

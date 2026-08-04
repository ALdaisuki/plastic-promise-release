import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _load_validator():
    path = Path("scripts/validate_release_variant.py")
    spec = importlib.util.spec_from_file_location("validate_release_variant", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _standard_payload() -> dict:
    return json.loads(Path("release/variants/standard.json").read_text(encoding="utf-8"))


def _write_variant(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "variant.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_standard_release_variant_is_a_distribution_contract():
    validator = _load_validator()
    path = Path("release/variants/standard.json")

    payload = validator.validate_release_variant(path, repo_root=Path.cwd())

    assert payload["variant"] == {
        "id": "standard",
        "kind": "distribution",
        "status": "active",
    }
    assert payload["storage"]["bundled_runtime_state"] is False
    assert payload["configuration"]["secret_values"] == "forbidden"
    assert payload["deployment"]["default_profile"] == "split-async"
    assert set(payload["deployment"]["profiles"]) == {
        "local-all-in-one",
        "split-async",
    }
    assert payload["content_policy"]["engineering_pattern_allowlist"]
    assert {
        "rust-release-import",
        "artifact-build",
        "artifact-metadata",
        "twine-check",
        "external-release-evidence",
        "atomic-push",
    } <= set(payload["release"]["required_gates"])


def test_release_variant_rejects_secret_values(tmp_path):
    validator = _load_validator()
    payload = _standard_payload()
    payload["distribution"]["license"] = "sk-" + ("x" * 32)
    path = _write_variant(tmp_path, payload)

    with pytest.raises(validator.ReleaseVariantError, match="secret_value_detected"):
        validator.validate_release_variant(path, repo_root=tmp_path)


def test_release_variant_rejects_knowledge_edition_kind(tmp_path):
    validator = _load_validator()
    payload = _standard_payload()
    payload["variant"]["kind"] = "knowledge-base"
    path = _write_variant(tmp_path, payload)

    with pytest.raises(validator.ReleaseVariantError, match="kind_not_distribution"):
        validator.validate_release_variant(path, repo_root=tmp_path)


def test_release_variant_requires_public_runtime_state_exclusions(tmp_path):
    validator = _load_validator()
    payload = _standard_payload()
    payload["content_policy"]["exclude"].remove("runtime-databases")
    path = _write_variant(tmp_path, payload)

    with pytest.raises(validator.ReleaseVariantError, match="exclusion_missing"):
        validator.validate_release_variant(path, repo_root=tmp_path)


def test_release_variant_rejects_unknown_fields(tmp_path):
    validator = _load_validator()
    payload = copy.deepcopy(_standard_payload())
    payload["variant"]["notes"] = "not part of the versioned schema"
    path = _write_variant(tmp_path, payload)

    with pytest.raises(validator.ReleaseVariantError, match="unknown_field:variant:notes"):
        validator.validate_release_variant(path, repo_root=tmp_path)


def test_release_variant_requires_server_only_canonical_state_for_split_async(tmp_path):
    validator = _load_validator()
    payload = _standard_payload()
    payload["deployment"]["profiles"]["split-async"]["canonical_state_location"] = (
        "client-and-server"
    )
    path = _write_variant(tmp_path, payload)

    with pytest.raises(validator.ReleaseVariantError, match="split_profile_invalid"):
        validator.validate_release_variant(path, repo_root=tmp_path)


def test_release_variant_requires_all_in_one_state_to_remain_local(tmp_path):
    validator = _load_validator()
    payload = _standard_payload()
    payload["deployment"]["profiles"]["local-all-in-one"]["async_worker_location"] = "server"
    path = _write_variant(tmp_path, payload)

    with pytest.raises(validator.ReleaseVariantError, match="local_profile_invalid"):
        validator.validate_release_variant(path, repo_root=tmp_path)


def test_release_variant_requires_durable_reconcilable_async_pipeline(tmp_path):
    validator = _load_validator()
    payload = _standard_payload()
    payload["deployment"]["async_pipeline"]["queue"] = "in-memory"
    path = _write_variant(tmp_path, payload)

    with pytest.raises(validator.ReleaseVariantError, match="async_pipeline_invalid"):
        validator.validate_release_variant(path, repo_root=tmp_path)

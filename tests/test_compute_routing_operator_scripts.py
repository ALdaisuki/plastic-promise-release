from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _cutover_args(tmp_path: Path, *, phase: str) -> SimpleNamespace:
    state_root = tmp_path / "state"
    control_root = state_root / "control"
    control_root.mkdir(parents=True, exist_ok=True)
    runtime_env = state_root / "plastic-promise.env"
    managed_env = control_root / "managed.env"
    runtime_env.write_text(
        f"PLASTIC_DB_PATH={state_root / 'canonical' / 'memory.db'}\n"
        f"PLASTIC_LANCEDB_GENERATION_ROOT={state_root / 'generations'}\n"
    )
    managed_env.write_text("PLASTIC_PROJECT_ID=project:test\n")
    quality = tmp_path / "quality.json"
    quality.write_text("{}\n")
    token = tmp_path / "control.token"
    token.write_text("token-value\n")
    token.chmod(0o600)
    prepare_receipt = tmp_path / "generation-a.prepare.json"
    return SimpleNamespace(
        phase=phase,
        python="python3",
        state_root=state_root,
        control_root=None,
        generation_root=None,
        live_root=None,
        runtime_env=None,
        managed_env=None,
        revision_env=None,
        owner_reference=None,
        source_db=None,
        generation_id="generation-a",
        project_id="project:test",
        quality_report=quality if phase == "prepare" else None,
        revision_id=None,
        evidence_file=None,
        prepare_receipt=prepare_receipt,
        token_file=token if phase == "cutover" else None,
    )


def test_control_token_file_must_be_private(tmp_path):
    import scripts.control_api_client as client

    token = tmp_path / "control.token"
    token.write_text("secret-token\n")
    token.chmod(0o644)

    if os.name == "posix":
        with pytest.raises(client.ControlApiError, match="control_token_file_permissions_invalid"):
            client.read_bearer_token(token)


def test_private_http_client_rejects_non_loopback_base_url():
    import scripts.private_http_client as client

    with pytest.raises(client.PrivateHttpError, match="private_http_loopback_required"):
        client.validate_loopback_base_url("https://api.example.test/control")


def test_authenticated_routing_stage_binds_model_identity(monkeypatch, tmp_path, capsys):
    import scripts.activate_compute_node_routing as operator

    token = tmp_path / "control.token"
    token.write_text("secret-token\n")
    token.chmod(0o600)
    calls: list[tuple[str, str, object, str | None]] = []

    def fake_request(base, path, bearer, *, method="GET", body=None, etag=None, **kwargs):
        del base, bearer, kwargs
        calls.append((path, method, body, etag))
        if path == "/config/safe":
            return {
                "etag": '"base"',
                "config": {"node_routing": {"enabled": False, "allowed_node_ids": []}},
            }
        if path == "/config/stage":
            return {"revision_id": "cfg-test"}
        return {"status": "ok"}

    monkeypatch.setattr(operator, "request_json", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "activate_compute_node_routing.py",
            "--token-file",
            str(token),
            "--embedding-identity",
            "sha256:" + "a" * 64,
            "--rerank-identity",
            "sha256:" + "b" * 64,
            "--structured-json-identity",
            "sha256:" + "c" * 64,
            "--embedding-model",
            "embedding-model",
            "--embedding-revision",
            "a" * 40,
            "--embedding-dimension",
            "2560",
            "--rerank-model",
            "rerank-model",
            "--rerank-revision",
            "b" * 40,
            "--structured-json-model",
            "structured-json-model",
            "--structured-json-revision",
            "sha256:" + "d" * 64,
            "--stage-only",
        ],
    )

    assert operator.main() == 0
    stage_body = next(body for path, _, body, _ in calls if path == "/config/stage")
    assert isinstance(stage_body, dict)
    config = stage_body["config"]
    assert config["embedding"]["dimension"] == 2560
    assert config["node_routing"]["embedding_required_identity"] == "sha256:" + "a" * 64
    assert config["node_routing"]["rerank_required_identity"] == "sha256:" + "b" * 64
    assert config["node_routing"]["structured_json_required_identity"] == "sha256:" + "c" * 64
    assert config["node_routing"]["inference_mode"] == "hybrid"
    assert config["node_routing"]["embedding_policy"] == "pinned-node"
    assert config["node_routing"]["rerank_policy"] == "pinned-node"
    assert config["node_routing"]["structured_json_policy"] == "pinned-node"
    assert config["node_routing"]["embedding_pinned_node_id"] == "inference-node"
    assert config["node_routing"]["rerank_pinned_node_id"] == "inference-node"
    assert config["node_routing"]["structured_json_pinned_node_id"] == "inference-node"
    assert config["chunk_inference"]["model"] == "structured-json-model"
    assert config["chunk_inference"]["model_revision"] == "sha256:" + "d" * 64
    assert json.loads(capsys.readouterr().out)["activation"] == "deferred"


def test_control_activation_uses_authenticated_cas(monkeypatch, tmp_path, capsys):
    import scripts.activate_control_revision as operator

    token = tmp_path / "control.token"
    token.write_text("secret-token\n")
    token.chmod(0o600)
    captured: dict[str, object] = {}
    monkeypatch.setattr(operator, "safe_config", lambda base, bearer: ({}, '"current"'))

    def fake_request(base, path, bearer, **kwargs):
        del base, bearer
        captured.update(path=path, **kwargs)
        return {"revision_id": "cfg-test", "activated": True}

    monkeypatch.setattr(operator, "request_json", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "activate_control_revision.py",
            "--token-file",
            str(token),
            "--revision",
            "cfg-test",
        ],
    )

    assert operator.main() == 0
    assert captured["path"] == "/config/revisions/cfg-test/activate"
    assert captured["etag"] == '"current"'
    assert captured["body"] == {}
    assert captured["idempotency_key"]
    assert json.loads(capsys.readouterr().out)["activated"] is True


def test_generation_retarget_uses_authenticated_cas(monkeypatch, tmp_path, capsys):
    import scripts.retarget_current_generation as operator

    token = tmp_path / "control.token"
    token.write_text("secret-token\n")
    token.chmod(0o600)
    captured: dict[str, object] = {}
    monkeypatch.setattr(operator, "safe_config", lambda base, bearer: ({}, '"current"'))
    monkeypatch.setattr(
        operator,
        "_current_generation_payload",
        lambda root: {
            "generation_id": "generation-a",
            "manifest_sha256": "a" * 64,
        },
    )

    def fake_request(base, path, bearer, **kwargs):
        del base, bearer
        captured.update(path=path, **kwargs)
        return {"status": "retargeted"}

    monkeypatch.setattr(operator, "request_json", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "retarget_current_generation.py",
            "--token-file",
            str(token),
            "--generation-root",
            str(tmp_path / "generations"),
        ],
    )

    assert operator.main() == 0
    assert captured["path"] == "/generation/retarget-current"
    assert captured["etag"] == '"current"'
    assert captured["body"] == {
        "generation_id": "generation-a",
        "manifest_sha256": "a" * 64,
    }
    assert captured["idempotency_key"]
    assert json.loads(capsys.readouterr().out)["status"] == "retargeted"


def test_server_compute_transport_verification_is_identity_bound(monkeypatch, tmp_path, capsys):
    import scripts.verify_server_compute_transport as operator

    env_file = tmp_path / "transport.env"
    env_file.write_text("PP_NODE_AUTH_TEST=Bearer private-token\n")
    env_file.chmod(0o600)
    embedding = {
        "model": "embedding-model",
        "revision": "a" * 40,
        "dimension": 2,
        "normalization": "l2",
        "artifact_sha256": "sha256:" + "c" * 64,
    }
    rerank = {
        "model": "rerank-model",
        "revision": "b" * 40,
        "artifact_sha256": "sha256:" + "d" * 64,
    }

    def fake_request(base, path, authorization, *, body=None):
        del base, authorization, body
        if path == "/health":
            return {"status": "ok"}
        if path == "/v1/identity":
            return {
                "node_id": "inference-node",
                "provider_class": "local",
                "capabilities": ["embeddings", "rerank"],
                "embedding": embedding,
                "rerank": rerank,
            }
        if path == "/v1/embeddings":
            return {"data": [{"embedding": [0.6, 0.8]}]}
        if path == "/v1/rerank":
            return {
                "results": [
                    {"index": 0, "score": 0.9},
                    {"index": 1, "score": 0.1},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(operator, "request_json", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_server_compute_transport.py",
            "--env-file",
            str(env_file),
            "--auth-name",
            "PP_NODE_AUTH_TEST",
            "--base-url",
            "http://127.0.0.1:39160",
            "--expected-node-id",
            "inference-node",
            "--expected-embedding-model",
            "embedding-model",
            "--expected-embedding-revision",
            "a" * 40,
            "--expected-embedding-dimension",
            "2",
            "--expected-embedding-normalization",
            "l2",
            "--expected-embedding-identity",
            operator.identity_digest(embedding),
            "--expected-rerank-model",
            "rerank-model",
            "--expected-rerank-revision",
            "b" * 40,
            "--expected-rerank-identity",
            operator.identity_digest(rerank),
        ],
    )

    assert operator.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["health_status"] == "ok"
    assert output["embedding_l2"] == 1.0
    assert output["rerank_directional_probe"] is True


def test_server_embedding_probe_allows_non_l2_profile(monkeypatch):
    import scripts.verify_server_compute_transport as operator

    monkeypatch.setattr(
        operator,
        "request_json",
        lambda *args, **kwargs: {"data": [{"embedding": [3.0, 4.0]}]},
    )

    assert (
        operator._embedding_probe(
            "http://127.0.0.1:39160",
            "Bearer private-token",
            model="embedding-model",
            dimension=2,
            normalization="none",
        )
        == 5.0
    )


def test_prepare_uses_runtime_paths_and_safe_order(tmp_path):
    import scripts.cutover_lancedb_generation as operator

    args = _cutover_args(tmp_path, phase="prepare")
    steps = operator.build_steps(args, tmp_path / "repo")

    assert [step.name for step in steps] == ["build", "reconcile", "verify-candidate"]
    command = " ".join(steps[0].command)
    assert str(args.state_root / "canonical" / "memory.db") in command
    assert str(args.state_root / "generations") in command


def test_prepare_refuses_to_guess_canonical_paths(tmp_path):
    import scripts.cutover_lancedb_generation as operator

    args = _cutover_args(tmp_path, phase="prepare")
    (args.state_root / "plastic-promise.env").write_text("PLASTIC_PROJECT_ID=project:test\n")

    with pytest.raises(SystemExit, match="generation_root_missing_from_runtime_environment"):
        operator.build_steps(args, tmp_path / "repo")


def test_prepare_receipt_binds_generation_and_revision_environment(tmp_path):
    import scripts.cutover_lancedb_generation as operator

    prepare = _cutover_args(tmp_path, phase="prepare")
    revision_env = tmp_path / "revision.env"
    revision_env.write_text("PP_EMBEDDING_MODEL=test\n")
    manifest_path = (
        prepare.state_root
        / "generations"
        / "generations"
        / prepare.generation_id
        / "manifest.json"
    )
    manifest_path.parent.mkdir(parents=True)
    manifest_payload = {
        "generation_id": prepare.generation_id,
        "manifest_sha256": "a" * 64,
        "index_tree_sha256": "b" * 64,
    }
    manifest_path.write_text(json.dumps(manifest_payload) + "\n")
    prepare.revision_id = "cfg-test"
    prepare.revision_env = revision_env
    payload = operator._prepare_receipt_payload(prepare)
    operator._write_prepare_receipt(prepare.prepare_receipt, payload)

    cutover = _cutover_args(tmp_path, phase="cutover")
    cutover.revision_id = "cfg-test"
    cutover.revision_env = revision_env
    cutover.prepare_receipt = prepare.prepare_receipt
    assert operator._verify_prepare_receipt(cutover.prepare_receipt, cutover) == payload

    revision_env.write_text("PP_EMBEDDING_MODEL=changed\n")
    with pytest.raises(SystemExit, match="prepare_receipt_identity_mismatch"):
        operator._verify_prepare_receipt(cutover.prepare_receipt, cutover)

    revision_env.write_text("PP_EMBEDDING_MODEL=test\n")
    manifest_payload["index_tree_sha256"] = "c" * 64
    manifest_path.write_text(json.dumps(manifest_payload) + "\n")
    with pytest.raises(SystemExit, match="prepare_receipt_identity_mismatch"):
        operator._verify_prepare_receipt(cutover.prepare_receipt, cutover)

    manifest_payload["index_tree_sha256"] = "b" * 64
    manifest_path.write_text(json.dumps(manifest_payload) + "\n")
    prepare.quality_report.write_text('{"changed":true}\n')
    with pytest.raises(SystemExit, match="prepare_receipt_identity_mismatch"):
        operator._verify_prepare_receipt(cutover.prepare_receipt, cutover)


def test_cutover_has_separate_activation_and_no_maintenance_mutation(tmp_path):
    import scripts.cutover_lancedb_generation as operator

    args = _cutover_args(tmp_path, phase="cutover")
    steps = operator.build_steps(args, tmp_path / "repo")

    assert [step.name for step in steps] == [
        "promote",
        "control-retarget",
        "bootstrap-live-root",
        "verify-live-root",
        "activate-runtime-env",
    ]
    rendered = "\n".join(" ".join(step.command) for step in steps)
    assert "PP_MAINTENANCE_ENABLED" not in rendered
    assert "systemctl" not in rendered


def test_active_service_blocks_cutover():
    import scripts.cutover_lancedb_generation as operator

    def fake_runner(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(returncode=0, stdout="active\n")

    with pytest.raises(SystemExit, match="cutover_service_still_active"):
        operator._assert_services_stopped(runner=fake_runner)


def test_run_with_env_files_drops_privileges_before_exec(monkeypatch, tmp_path):
    import scripts.run_with_env_files as operator

    owner_reference = tmp_path / "control"
    owner_reference.mkdir()
    env_file = tmp_path / "runtime.env"
    env_file.write_text("PP_TEST=value\n")
    metadata = owner_reference.stat()
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(operator.os, "name", "posix")
    monkeypatch.setattr(operator.os, "geteuid", lambda: 0)
    monkeypatch.setattr(operator.os, "setgroups", lambda value: calls.append(("setgroups", value)))
    monkeypatch.setattr(operator.os, "setgid", lambda value: calls.append(("setgid", value)))
    monkeypatch.setattr(operator.os, "setuid", lambda value: calls.append(("setuid", value)))

    def fake_exec(command, argv, environ):
        calls.append(("exec", command, argv, environ["PP_TEST"]))
        raise RuntimeError("exec-called")

    monkeypatch.setattr(operator.os, "execvpe", fake_exec)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_with_env_files.py",
            "--env-file",
            str(env_file),
            "--owner-reference",
            str(owner_reference),
            "--",
            "python3",
            "-V",
        ],
    )

    with pytest.raises(RuntimeError, match="exec-called"):
        operator.main()
    assert calls == [
        ("setgroups", []),
        ("setgid", metadata.st_gid),
        ("setuid", metadata.st_uid),
        ("exec", "python3", ["python3", "-V"], "value"),
    ]


def test_environment_file_parser_decodes_systemd_quoted_paths(tmp_path):
    import scripts.run_with_env_files as operator

    env_file = tmp_path / "runtime.env"
    env_file.write_text('PLASTIC_DB_PATH="/srv/plastic promise/db.sqlite"\nEMPTY=""\n')

    assert operator.parse_env_file(env_file) == {
        "PLASTIC_DB_PATH": "/srv/plastic promise/db.sqlite",
        "EMPTY": "",
    }


def test_runtime_env_update_is_atomic_and_deduplicates_keys(tmp_path):
    import scripts.update_runtime_env_file as operator

    env_file = tmp_path / "plastic-promise.env"
    env_file.write_text("# runtime\nKEEP=value\nKEEP=duplicate\n")
    updated = operator.update_lines(
        env_file.read_text().splitlines(),
        [("KEEP", "updated"), ("PLASTIC_LANCEDB_LIVE_ROOT", "/srv/live/generation-a")],
    )
    assert updated == [
        "# runtime",
        "KEEP=updated",
        "PLASTIC_LANCEDB_LIVE_ROOT=/srv/live/generation-a",
    ]

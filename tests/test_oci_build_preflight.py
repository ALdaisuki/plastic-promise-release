from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.prepare_oci_build import _inspect_images, build_cleanup_plan, main


def _reports(output: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.getvalue().splitlines() if line]


def _image(
    image_id: str,
    *,
    repository: str,
    created_at: datetime,
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "id": image_id,
        "repository": repository,
        "created_at": created_at.isoformat(),
        "labels": labels or {},
    }


def test_cleanup_plan_removes_only_old_unreferenced_unprotected_project_images():
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    plan = build_cleanup_plan(
        [
            _image(
                "sha256:old",
                repository="plastic-promise-server",
                created_at=now - timedelta(days=8),
            ),
            _image(
                "sha256:running",
                repository="plastic-promise-local-inference-node",
                created_at=now - timedelta(days=8),
            ),
            _image(
                "sha256:protected",
                repository="ghcr.io/aldaisuki/plastic-promise-server",
                created_at=now - timedelta(days=8),
                labels={"com.plastic-promise.retention": "protect"},
            ),
            _image(
                "sha256:recent",
                repository="plastic-promise-inference-node",
                created_at=now - timedelta(hours=24),
            ),
            _image(
                "sha256:unrelated",
                repository="another-project",
                created_at=now - timedelta(days=90),
            ),
            {
                **_image(
                    "sha256:mixed-tags",
                    repository="plastic-promise-server",
                    created_at=now - timedelta(days=90),
                ),
                "repositories": ["plastic-promise-server", "another-project"],
            },
            {
                "id": "sha256:unreadable-labels",
                "repository": "plastic-promise-server",
                "created_at": (now - timedelta(days=90)).isoformat(),
                "labels": None,
            },
        ],
        container_image_ids={"sha256:running"},
        now=now,
        retention_hours=168,
    )

    assert [candidate.image_id for candidate in plan.removable_images] == ["sha256:old"]
    assert plan.protected_image_ids == {
        "sha256:running": "container_reference",
        "sha256:protected": "retention_label",
        "sha256:unreadable-labels": "image_labels_unreadable",
    }
    assert plan.retained_image_ids == {
        "sha256:recent": "within_retention_window",
        "sha256:unrelated": "outside_project_scope",
        "sha256:mixed-tags": "outside_project_scope",
    }


def test_docker_null_labels_are_preserved_as_unreadable_and_fail_closed():
    def runner(command: list[str]) -> str:
        if command == [
            "docker",
            "image",
            "ls",
            "--all",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]:
            return '{"ID":"sha256:no-labels","Repository":"plastic-promise-server"}\n'
        if command == ["docker", "image", "inspect", "sha256:no-labels"]:
            return '[{"Id":"sha256:no-labels","Created":"2026-07-01T00:00:00Z","Config":{"Labels":null}}]'
        raise AssertionError(command)

    images = _inspect_images(runner)

    assert images == [
        {
            "id": "sha256:no-labels",
            "repository": "plastic-promise-server",
            "repositories": ["plastic-promise-server"],
            "created_at": "2026-07-01T00:00:00Z",
            "labels": None,
        }
    ]


def test_cleanup_cli_is_dry_run_by_default_and_executes_only_scoped_removals():
    calls: list[tuple[str, ...]] = []
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)

    def runner(command: list[str]) -> str:
        calls.append(tuple(command))
        if command == [
            "docker",
            "image",
            "ls",
            "--all",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]:
            return '{"ID":"sha256:old","Repository":"plastic-promise-server"}\n'
        if command == ["docker", "container", "ls", "-aq"]:
            return ""
        if command == ["docker", "image", "inspect", "sha256:old"]:
            return (
                '[{"Id":"sha256:old","Created":"2026-07-01T00:00:00Z",'
                '"Config":{"Labels":{"org.opencontainers.image.title":'
                '"Plastic Promise MCP runtime"}}}]'
            )
        raise AssertionError(command)

    dry_output = io.StringIO()
    assert main([], runner=runner, output=dry_output, now=now) == 0
    dry_report = json.loads(dry_output.getvalue())
    assert dry_report["retention_hours"] == 24
    assert dry_report["builder"] == "plastic-promise-oci"
    assert dry_report["event"] == "pre_action_plan"
    assert "would_remove_images" in dry_report
    assert not any(command[:3] == ("docker", "image", "rm") for command in calls)
    assert not any(command[:3] == ("docker", "buildx", "prune") for command in calls)

    calls.clear()
    execute_output = io.StringIO()

    def execute_runner(command: list[str]) -> str:
        if command == ["docker", "image", "rm", "--force", "sha256:old"] or command == [
            "docker",
            "buildx",
            "prune",
            "--builder",
            "plastic-promise-oci",
            "--force",
            "--filter",
            "until=24h",
        ]:
            calls.append(tuple(command))
            return ""
        return runner(command)

    assert main(["--execute"], runner=execute_runner, output=execute_output, now=now) == 0
    execute_reports = _reports(execute_output)
    assert [report["event"] for report in execute_reports] == [
        "pre_action_plan",
        "cleanup_result",
    ]
    assert execute_reports[0]["status"] == "planned"
    assert execute_reports[1]["status"] == "cleaned"
    assert ("docker", "image", "rm", "--force", "sha256:old") in calls
    assert (
        "docker",
        "buildx",
        "prune",
        "--builder",
        "plastic-promise-oci",
        "--force",
        "--filter",
        "until=24h",
    ) in calls
    assert all(command[:3] != ("docker", "system", "prune") for command in calls)


def test_cleanup_cli_persists_pre_action_plan_before_first_mutation(tmp_path: Path):
    report_path = tmp_path / "oci-cleanup.jsonl"
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)

    def runner(command: list[str]) -> str:
        if command == [
            "docker",
            "image",
            "ls",
            "--all",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]:
            return '{"ID":"sha256:old","Repository":"plastic-promise-server"}\n'
        if command == ["docker", "container", "ls", "-aq"]:
            return ""
        if command == ["docker", "image", "inspect", "sha256:old"]:
            return (
                '[{"Id":"sha256:old","Created":"2026-07-01T00:00:00Z",'
                '"Config":{"Labels":{"org.opencontainers.image.title":'
                '"Plastic Promise MCP runtime"}}}]'
            )
        if command == ["docker", "image", "rm", "--force", "sha256:old"]:
            pre_action_records = [
                json.loads(line) for line in report_path.read_text(encoding="utf-8").splitlines()
            ]
            assert pre_action_records == [
                {
                    "builder": "plastic-promise-oci",
                    "cache_prune": "would_run",
                    "event": "pre_action_plan",
                    "protected_image_ids": {},
                    "retained_image_ids": {},
                    "retention_hours": 24,
                    "schema_version": "plastic-promise-oci-build-preflight/v1",
                    "status": "planned",
                    "would_remove_images": [
                        {
                            "created_at": "2026-07-01T00:00:00+00:00",
                            "image_id": "sha256:old",
                            "repository": "plastic-promise-server",
                        }
                    ],
                }
            ]
            return ""
        if command == [
            "docker",
            "buildx",
            "prune",
            "--builder",
            "plastic-promise-oci",
            "--force",
            "--filter",
            "until=24h",
        ]:
            return ""
        raise AssertionError(command)

    output = io.StringIO()
    assert (
        main(
            ["--execute", "--report", str(report_path)],
            runner=runner,
            output=output,
            now=now,
        )
        == 0
    )
    assert [record["event"] for record in _reports(output)] == [
        "pre_action_plan",
        "cleanup_result",
    ]
    assert [
        json.loads(line)["event"] for line in report_path.read_text(encoding="utf-8").splitlines()
    ] == ["pre_action_plan", "cleanup_result"]


def test_preflight_uses_python_310_compatible_utc_api():
    source = Path("scripts/prepare_oci_build.py").read_text(encoding="utf-8")

    assert "from datetime import UTC" not in source
    assert "datetime.UTC" not in source
    assert "timezone.utc" in source


def test_cleanup_cli_passes_explicit_docker_config_to_every_command(tmp_path: Path):
    calls: list[tuple[str, ...]] = []
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    docker_config = tmp_path / "docker-config"

    def runner(command: list[str]) -> str:
        calls.append(tuple(command))
        prefix = ["docker", "--config", str(docker_config)]
        if command == [
            *prefix,
            "image",
            "ls",
            "--all",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]:
            return ""
        if command == [*prefix, "container", "ls", "-aq"]:
            return ""
        raise AssertionError(command)

    output = io.StringIO()
    assert (
        main(
            ["--docker-config", str(docker_config)],
            runner=runner,
            output=output,
            now=now,
        )
        == 0
    )
    assert calls
    assert all(command[:3] == ("docker", "--config", str(docker_config)) for command in calls)


def test_cleanup_cli_fails_closed_when_docker_inspection_fails():
    calls: list[tuple[str, ...]] = []

    def runner(command: list[str]) -> str:
        calls.append(tuple(command))
        raise OSError("docker unavailable")

    output = io.StringIO()
    assert main(["--execute"], runner=runner, output=output) == 2
    report = json.loads(output.getvalue())
    assert report["status"] == "failed"
    assert report["reason"] == "oci_cleanup_docker_command_failed"
    assert not any(command[:3] == ("docker", "image", "rm") for command in calls)
    assert not any(command[:3] == ("docker", "buildx", "prune") for command in calls)

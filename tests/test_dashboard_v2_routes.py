from __future__ import annotations

from contextlib import nullcontext

import httpx
import pytest
from starlette.applications import Starlette

from plastic_promise.mcp.dashboard_v2.config import DashboardSettings
from plastic_promise.mcp.dashboard_v2.routes import create_dashboard_v2_routes


class FakeRepository:
    def __init__(self, scope):
        self.scope = scope
        self.calls: list[tuple[str, object]] = []
        self.request_detail = {
            "call_id": "call-a",
            "tool_name": "memory_recall",
            "status": "success",
            "degraded": False,
            "started_at": "2026-07-19T01:00:00Z",
            "ended_at": "2026-07-19T01:00:01.25Z",
            "duration_ms": 1250.0,
            "duration_status": "measured",
            "request_scope_id": "scope-a",
            "project_id": "project:a",
            "metadata": {
                "retrieval_explain_v1": {
                    "schema": "retrieval_explain_v1",
                    "pipeline_stats": {"candidate_count": 2},
                    "items": [{"memory_id": "mem-a", "final_score": 0.91}],
                }
            },
        }

    def overview(self):
        self.calls.append(("overview", None))
        return {
            "data": {"memory_count": 3, "request_count": 2},
            "scope": self.scope.to_dict(),
            "degraded": False,
            "warnings": [],
        }

    def list_requests(self, **kwargs):
        self.calls.append(("list_requests", kwargs))
        return {
            "data": [{"call_id": "call-a", "status": "success"}],
            "scope": self.scope.to_dict(),
            "page": {
                "limit": kwargs["limit"],
                "total": 1,
                "next_cursor": None,
                "has_more": False,
            },
            "degraded": False,
            "warnings": [],
        }

    def get_request(self, call_id):
        self.calls.append(("get_request", call_id))
        if call_id == "missing":
            return None
        return self.request_detail

    def list_memories(self, **kwargs):
        self.calls.append(("list_memories", kwargs))
        return self._empty_page(kwargs["limit"])

    def get_memory(self, memory_id):
        self.calls.append(("get_memory", memory_id))
        return None if memory_id == "missing" else {"id": memory_id, "content": "visible"}

    def get_lineage(self, memory_id, limit=100):
        self.calls.append(("get_lineage", {"memory_id": memory_id, "limit": limit}))
        return {"memory_id": memory_id, "data": []}

    def list_synthesis(self, **kwargs):
        self.calls.append(("list_synthesis", kwargs))
        return self._empty_page(kwargs["limit"])

    def list_operations(self, **kwargs):
        self.calls.append(("list_operations", kwargs))
        return self._empty_page(kwargs["limit"])

    def get_trust(self, target=""):
        self.calls.append(("get_trust", target))
        return {"target": target or "default", "trust": 0.53, "tier": "medium"}

    def _empty_page(self, limit):
        return {
            "data": [],
            "scope": self.scope.to_dict(),
            "page": {"limit": limit, "total": 0, "next_cursor": None, "has_more": False},
            "degraded": False,
            "warnings": [],
        }


def settings(*, enabled="1", explain="1", project="project:a"):
    return DashboardSettings.from_env(
        {
            "PP_DASHBOARD_V2": enabled,
            "PP_RETRIEVAL_EXPLAIN": explain,
            "PP_DASHBOARD_AUTH": "local",
            "PP_DASHBOARD_PROJECT_ID": project,
        },
        bind_host="127.0.0.1",
    )


def build_app(config, *, repository=None, identity_provider=None, issue_provider=None):
    repositories: list[FakeRepository] = []

    def provider(scope):
        value = repository or FakeRepository(scope)
        if getattr(value, "scope", None) is None:
            value.scope = scope
        repositories.append(value)
        return nullcontext(value)

    routes = create_dashboard_v2_routes(
        config,
        repository_provider=provider,
        version="9.9.9",
        identity_provider=identity_provider
        or (lambda: {"status": "ok", "runtime_mode": "normal"}),
        issue_provider=issue_provider,
    )
    return Starlette(routes=routes), repositories


async def request(
    app,
    path,
    *,
    method="GET",
    client_host="127.0.0.1",
    host_header="127.0.0.1:9128",
    headers=None,
):
    transport = httpx.ASGITransport(app=app, client=(client_host, 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9128") as client:
        request_headers = {"host": host_header}
        if headers:
            request_headers.update(headers)
        return await client.request(method, path, headers=request_headers)


@pytest.mark.asyncio
async def test_gate_off_registers_no_v2_routes():
    app, _ = build_app(settings(enabled="0"))

    response = await request(app, "/api/dashboard/v2/overview")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_local_mode_rejects_non_loopback_client():
    app, _ = build_app(settings())

    response = await request(app, "/api/dashboard/v2/overview", client_host="10.20.30.40")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "dashboard_loopback_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("host_header", ["evil.example:9128", "localhost.evil:9128", ""])
async def test_local_mode_rejects_non_loopback_or_missing_host_authority(host_header):
    app, repositories = build_app(settings())

    response = await request(
        app,
        "/api/dashboard/v2/overview",
        host_header=host_header,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "dashboard_loopback_host_required"
    assert repositories == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host_header", ["localhost:9128", "[::1]:9128"])
async def test_local_mode_accepts_loopback_host_authority(host_header):
    app, _ = build_app(settings())

    response = await request(
        app,
        "/api/dashboard/v2/overview",
        host_header=host_header,
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_requested_project_can_only_narrow_server_owned_scope():
    app, repositories = build_app(settings())

    allowed = await request(app, "/api/dashboard/v2/overview?project_id=project:a")
    denied = await request(app, "/api/dashboard/v2/overview?project_id=project:b")

    assert allowed.status_code == 200
    assert allowed.json()["scope"]["project_id"] == "project:a"
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "dashboard_scope_denied"
    assert len(repositories) == 1


@pytest.mark.asyncio
async def test_overview_enriches_repository_counts_without_initializing_retrieval():
    repository = FakeRepository(scope=None)
    repository.overview = lambda: {
        "memory_count": 3,
        "request_count": 2,
        "synthesis_count": 1,
        "operation_count": 4,
        "runtime_event_count": 2,
        "degradation_count": 1,
        "outbox_count": 1,
        "pending_outbox_count": 0,
    }
    app, _ = build_app(settings(), repository=repository)

    response = await request(app, "/api/dashboard/v2/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["runtime_mode"] == "normal"
    assert payload["data"]["trust"]["trust"] == 0.53
    assert payload["data"]["readiness"]["components"][1]["detail"] == "project:a"


@pytest.mark.asyncio
async def test_issue_projection_is_explicitly_system_scoped_and_read_only():
    issue = {"id": "issue-a", "title": "Index needs attention", "state": "open"}
    app, _ = build_app(settings(), issue_provider=lambda: [issue])

    response = await request(app, "/api/dashboard/v2/trust-issues")

    assert response.status_code == 200
    assert response.json()["data"]["issues"] == [issue]
    assert response.json()["data"]["issue_scope"] == {
        "authority_scope": "system_global",
        "source": "process_issue_manager",
        "mode": "read_only_projection",
    }


@pytest.mark.asyncio
async def test_collection_forwards_validated_filters_and_adds_read_headers():
    app, repositories = build_app(settings())

    response = await request(
        app,
        "/api/dashboard/v2/requests?limit=12&status=success&tool_name=memory_recall",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert response.json()["page"]["limit"] == 12
    assert repositories[0].calls == [
        (
            "list_requests",
            {
                "limit": 12,
                "cursor": None,
                "status": "success",
                "tool_name": "memory_recall",
                "degraded": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_request_id_rejects_control_characters():
    app, _ = build_app(settings())

    # ASGI normally rejects a raw CR/LF header before the app sees it, so use
    # a control character that can pass through the test transport.
    response = await request(
        app,
        "/api/dashboard/v2/overview",
        headers={"x-request-id": "bad\x00request"},
    )
    assert response.status_code == 200
    response_id = response.headers["x-request-id"]
    assert "\x00" not in response_id
    assert response_id.startswith("dash_")


@pytest.mark.asyncio
async def test_invalid_limit_returns_structured_400_without_repository_access():
    app, repositories = build_app(settings())

    response = await request(app, "/api/dashboard/v2/memories?limit=101")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_limit"
    assert repositories == []


@pytest.mark.asyncio
async def test_direct_id_missing_and_unauthorized_share_404_shape():
    app, _ = build_app(settings())

    response = await request(app, "/api/dashboard/v2/memories/missing")

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "memory_not_found",
        "message": "Memory not found",
    }


@pytest.mark.asyncio
async def test_memory_detail_wraps_database_scope_field_in_dashboard_envelope():
    repository = FakeRepository(scope=None)
    repository.get_memory = lambda memory_id: {
        "id": memory_id,
        "content": "visible",
        "scope": "global",
    }
    app, _ = build_app(settings(), repository=repository)

    response = await request(app, "/api/dashboard/v2/memories/mem-a")

    assert response.status_code == 200
    assert response.json() == {
        "data": {"id": "mem-a", "content": "visible", "scope": "global"},
        "scope": {"project_id": "project:a", "auth_mode": "local"},
        "degraded": False,
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_explain_projects_stored_snapshot_without_engine_call():
    repository = FakeRepository(scope=None)
    app, _ = build_app(settings(), repository=repository)

    response = await request(app, "/api/dashboard/v2/retrieval-explain?call_id=call-a")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["schema"] == "retrieval_explain_v1"
    assert payload["data"]["call_id"] == "call-a"
    assert payload["data"]["availability"] == "available"
    assert payload["data"]["call"] == {
        "call_id": "call-a",
        "tool_name": "memory_recall",
        "status": "success",
        "degraded": False,
        "started_at": "2026-07-19T01:00:00Z",
        "ended_at": "2026-07-19T01:00:01.25Z",
        "duration_ms": 1250.0,
        "duration_status": "measured",
        "request_scope_id": "scope-a",
        "project_id": "project:a",
    }
    assert repository.calls == [("get_request", "call-a")]


@pytest.mark.asyncio
async def test_synthesis_and_configuration_expose_fail_closed_governance(monkeypatch):
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "shadow")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    monkeypatch.setenv("PP_MEMORY_PROPOSALS", "off")
    app, _ = build_app(settings())

    synthesis = await request(app, "/api/dashboard/v2/synthesis")
    configuration = await request(app, "/api/dashboard/v2/configuration")

    expected = {
        "source_of_truth": "synthesis_artifacts",
        "artifacts_mode": "shadow",
        "creation_enabled": False,
        "retrieval_enabled": True,
        "retrieval_effective": False,
        "proposal_mode": "off",
    }
    assert synthesis.status_code == 200
    assert synthesis.json()["governance"] == expected
    assert configuration.status_code == 200
    assert configuration.json()["data"]["memory_governance"] == expected


@pytest.mark.asyncio
async def test_configuration_promotes_runtime_identity_failure_to_envelope():
    def unavailable_identity():
        raise RuntimeError("identity unavailable")

    app, _ = build_app(settings(), identity_provider=unavailable_identity)

    response = await request(app, "/api/dashboard/v2/configuration")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["runtime"] == {"status": "unavailable"}
    assert payload["degraded"] is True
    assert payload["warnings"] == ["runtime_identity_unavailable"]


@pytest.mark.asyncio
async def test_explain_reprojects_untrusted_stored_snapshot_fields():
    repository = FakeRepository(scope=None)
    repository.request_detail["metadata"]["retrieval_explain_v1"] = {
        "schema": "retrieval_explain_v1",
        "content": "ROUTE_CONTENT_SECRET",
        "query": "ROUTE_QUERY_SECRET",
        "items": [
            {
                "id": "mem-a",
                "rank": 1,
                "final_score": 0.91,
                "prompt": "ROUTE_PROMPT_SECRET",
            }
        ],
    }
    app, _ = build_app(settings(), repository=repository)

    response = await request(app, "/api/dashboard/v2/retrieval-explain?call_id=call-a")

    assert response.status_code == 200
    assert response.json()["data"]["items"] == [
        {"id": "mem-a", "rank": 1, "final_score": 0.91}
    ]
    rendered = response.text
    assert "ROUTE_CONTENT_SECRET" not in rendered
    assert "ROUTE_QUERY_SECRET" not in rendered
    assert "ROUTE_PROMPT_SECRET" not in rendered


@pytest.mark.asyncio
async def test_explain_schema_only_snapshot_is_not_available():
    repository = FakeRepository(scope=None)
    repository.request_detail["metadata"]["retrieval_explain_v1"] = {
        "schema": "retrieval_explain_v1",
        "channels": [],
        "items": [],
        "pipeline": {},
    }
    app, _ = build_app(settings(), repository=repository)

    response = await request(app, "/api/dashboard/v2/retrieval-explain?call_id=call-a")

    assert response.status_code == 200
    assert response.json()["data"]["availability"] == "unavailable"
    assert response.json()["data"]["reason"] == "snapshot_not_captured"


@pytest.mark.asyncio
async def test_explain_route_is_absent_when_gate_is_off():
    app, _ = build_app(settings(explain="0"))

    response = await request(app, "/api/dashboard/v2/retrieval-explain?call_id=call-a")
    configuration = await request(app, "/api/dashboard/v2/configuration")

    assert response.status_code == 404
    assert configuration.status_code == 200
    assert configuration.json()["data"]["dashboard"]["retrieval_explain_enabled"] is False


@pytest.mark.asyncio
async def test_v2_routes_are_read_only():
    app, _ = build_app(settings())

    response = await request(app, "/api/dashboard/v2/memories", method="POST")

    assert response.status_code == 405


@pytest.mark.asyncio
async def test_dashboard_shell_and_assets_use_a_strict_read_only_surface():
    app, repositories = build_app(settings())

    shell = await request(app, "/dashboard")
    stylesheet = await request(app, "/dashboard/assets/v2/app.css")
    script = await request(app, "/dashboard/assets/v2/app.js")
    unknown = await request(app, "/dashboard/assets/v2/unknown.js")

    assert shell.status_code == 200
    assert "/dashboard/assets/v2/app.js" in shell.text
    assert 'lang="zh-CN"' in shell.text
    for label in (
        "概览",
        "请求",
        "记忆",
        "检索解释",
        "记忆谱系",
        "综合记忆",
        "运行运维",
        "信任与问题",
        "有效配置",
    ):
        assert label in shell.text
    assert "<span>Overview</span>" not in shell.text
    assert "script-src 'self'" in shell.headers["content-security-policy"]
    assert "no-store" in shell.headers["cache-control"]
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert 'title: "概览"' in script.text
    assert 'ready: "就绪"' in script.text
    assert 'text: statusLabel(value)' in script.text
    assert unknown.status_code == 404
    assert repositories == []


@pytest.mark.asyncio
async def test_lineage_keeps_collection_data_at_envelope_root():
    app, _ = build_app(settings())

    response = await request(app, "/api/dashboard/v2/memories/mem-a/lineage")

    assert response.status_code == 200
    assert response.json()["memory_id"] == "mem-a"
    assert response.json()["data"] == []
    assert response.json()["scope"]["project_id"] == "project:a"


@pytest.mark.asyncio
async def test_invalid_operation_kind_returns_structured_400():
    class RejectingRepository(FakeRepository):
        def list_operations(self, **kwargs):
            raise ValueError("operation_kind_invalid")

    repository = RejectingRepository(scope=None)
    app, _ = build_app(settings(), repository=repository)

    response = await request(app, "/api/dashboard/v2/operations?kind=foreign")

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "invalid_filter",
        "message": "operation_kind_invalid",
    }


@pytest.mark.asyncio
async def test_trust_endpoint_is_fixed_to_the_explicit_system_default_target():
    app, repositories = build_app(settings())

    response = await request(app, "/api/dashboard/v2/trust-issues")

    assert response.status_code == 200
    assert response.json()["data"]["trust"]["authority_scope"] == "system_global"
    assert repositories[0].calls == [("get_trust", "")]


@pytest.mark.asyncio
async def test_trust_endpoint_rejects_arbitrary_target_selection():
    app, repositories = build_app(settings())

    response = await request(app, "/api/dashboard/v2/trust-issues?target=codex")

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "trust_target_not_supported",
        "message": "Trust target selection requires an ownership model",
    }
    assert repositories == []

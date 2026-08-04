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

    def passive_memory_overview(self, **kwargs):
        self.calls.append(("passive_memory_overview", kwargs))
        return {
            "summary": {"proposals": {"status_counts": {"pending": 2}}},
            "events": [],
            "quality_cases": [],
        }

    def list_memory_proposals(self, **kwargs):
        self.calls.append(("list_memory_proposals", kwargs))
        return self._empty_page(kwargs["limit"])

    def get_memory_proposal(self, proposal_id):
        self.calls.append(("get_memory_proposal", proposal_id))
        if proposal_id == "missing":
            return None
        return {
            "proposal_id": proposal_id,
            "project_id": self.scope.project_id,
            "status": "pending",
        }

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


def settings(*, enabled="1", explain="1", review="0", project="project:a"):
    return DashboardSettings.from_env(
        {
            "PP_DASHBOARD_V2": enabled,
            "PP_RETRIEVAL_EXPLAIN": explain,
            "PP_DASHBOARD_REVIEW_ACTIONS": review,
            "PP_DASHBOARD_AUTH": "local",
            "PP_DASHBOARD_PROJECT_ID": project,
        },
        bind_host="127.0.0.1",
    )


def build_app(
    config,
    *,
    repository=None,
    identity_provider=None,
    issue_provider=None,
    proposal_review_provider=None,
    project_scope_provider=None,
):
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
        identity_provider=identity_provider or (lambda: {"status": "ok", "runtime_mode": "normal"}),
        issue_provider=issue_provider,
        proposal_review_provider=proposal_review_provider,
        project_scope_provider=project_scope_provider
        or (
            lambda: [
                {
                    "project_id": "project:a",
                    "latest_at": "2026-07-27T00:00:00Z",
                    "event_count": 3,
                },
                {
                    "project_id": "project:b",
                    "latest_at": "2026-07-29T12:33:21Z",
                    "event_count": 7,
                },
            ]
        ),
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
    json_body=None,
):
    transport = httpx.ASGITransport(app=app, client=(client_host, 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9128") as client:
        request_headers = {"host": host_header}
        if headers:
            request_headers.update(headers)
        return await client.request(
            method,
            path,
            headers=request_headers,
            json=json_body,
        )


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
    selected = await request(app, "/api/dashboard/v2/overview?project_id=project:b")
    denied = await request(app, "/api/dashboard/v2/overview?project_id=project:c")

    assert allowed.status_code == 200
    assert allowed.json()["scope"]["project_id"] == "project:a"
    assert selected.status_code == 200
    assert selected.json()["scope"]["project_id"] == "project:b"
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "dashboard_scope_denied"
    assert len(repositories) == 2


@pytest.mark.asyncio
async def test_scope_route_returns_server_owned_project_options_without_memory_rows():
    app, repositories = build_app(settings())

    response = await request(app, "/api/dashboard/v2/scopes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"]["project_id"] == "project:a"
    assert payload["data"]["default_project_id"] == "project:a"
    assert payload["data"]["recommended_project_id"] == "project:b"
    assert [item["project_id"] for item in payload["data"]["scopes"]] == [
        "project:b",
        "project:a",
    ]
    assert repositories == []


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
    assert response.json()["data"]["items"] == [{"id": "mem-a", "rank": 1, "final_score": 0.91}]
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
    review = await request(
        app,
        "/api/dashboard/v2/memory-proposals/proposal-a/review",
        method="POST",
        headers={"x-pp-dashboard-action": "proposal-review-v1"},
        json_body={"feedback_type": "adopted"},
    )

    assert response.status_code == 405
    assert review.status_code == 404


@pytest.mark.asyncio
async def test_passive_memory_and_proposal_routes_forward_scoped_filters():
    app, repositories = build_app(settings())

    passive = await request(app, "/api/dashboard/v2/passive-memory?limit=7")
    proposals = await request(
        app,
        "/api/dashboard/v2/memory-proposals?limit=9&status=pending&category=fact",
    )

    assert passive.status_code == 200
    assert passive.json()["data"]["summary"]["proposals"]["status_counts"] == {"pending": 2}
    assert proposals.status_code == 200
    assert repositories[0].calls == [("passive_memory_overview", {"limit": 7})]
    assert repositories[1].calls == [
        (
            "list_memory_proposals",
            {
                "limit": 9,
                "cursor": None,
                "status": "pending",
                "category": "fact",
            },
        )
    ]


@pytest.mark.asyncio
async def test_proposal_review_gate_requires_provider_and_reports_configuration():
    app, _ = build_app(settings(review="1"))

    configuration = await request(app, "/api/dashboard/v2/configuration")
    review = await request(
        app,
        "/api/dashboard/v2/memory-proposals/proposal-a/review",
        method="POST",
        headers={"x-pp-dashboard-action": "proposal-review-v1"},
        json_body={"feedback_type": "adopted"},
    )

    dashboard = configuration.json()["data"]["dashboard"]
    assert dashboard["proposal_review_requested"] is True
    assert dashboard["proposal_review_enabled"] is False
    assert dashboard["read_only"] is True
    assert configuration.json()["degraded"] is True
    assert configuration.json()["warnings"] == ["proposal_review_provider_unavailable"]
    assert review.status_code == 404


@pytest.mark.asyncio
async def test_proposal_review_requires_same_origin_confirmation_and_project_scope():
    calls = []

    async def reviewer(proposal_id, feedback_type, reason, project_id):
        calls.append((proposal_id, feedback_type, reason, project_id))
        return {
            "updated": True,
            "item_id": proposal_id,
            "feedback_type": feedback_type,
            "status": "adopted",
            "memory_id": "memory-a",
        }

    app, repositories = build_app(
        settings(review="1"),
        proposal_review_provider=reviewer,
    )

    missing_header = await request(
        app,
        "/api/dashboard/v2/memory-proposals/proposal-a/review",
        method="POST",
        json_body={"feedback_type": "adopted"},
    )
    cross_site = await request(
        app,
        "/api/dashboard/v2/memory-proposals/proposal-a/review",
        method="POST",
        headers={
            "x-pp-dashboard-action": "proposal-review-v1",
            "sec-fetch-site": "cross-site",
        },
        json_body={"feedback_type": "adopted"},
    )
    missing = await request(
        app,
        "/api/dashboard/v2/memory-proposals/missing/review",
        method="POST",
        headers={"x-pp-dashboard-action": "proposal-review-v1"},
        json_body={"feedback_type": "adopted"},
    )
    adopted = await request(
        app,
        "/api/dashboard/v2/memory-proposals/proposal-a/review",
        method="POST",
        headers={"x-pp-dashboard-action": "proposal-review-v1"},
        json_body={"feedback_type": "adopted"},
    )

    assert missing_header.status_code == 403
    assert cross_site.status_code == 403
    assert missing.status_code == 404
    assert adopted.status_code == 200
    assert adopted.json()["data"]["memory_id"] == "memory-a"
    assert calls == [("proposal-a", "adopted", "reviewer_rejected", "project:a")]
    assert ("get_memory_proposal", "missing") in repositories[0].calls


@pytest.mark.asyncio
async def test_proposal_review_maps_governance_denial_without_leaking_authority():
    async def reviewer(_proposal_id, _feedback_type, _reason, _project_id):
        return {
            "updated": False,
            "reason": "feedback_runtime_authorization_denied",
        }

    app, _ = build_app(
        settings(review="1"),
        proposal_review_provider=reviewer,
    )

    response = await request(
        app,
        "/api/dashboard/v2/memory-proposals/proposal-a/review",
        method="POST",
        headers={"x-pp-dashboard-action": "proposal-review-v1"},
        json_body={"feedback_type": "rejected", "rejection_reason": "incorrect"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == {
        "code": "feedback_runtime_authorization_denied",
        "message": "Proposal review was not applied",
    }


@pytest.mark.asyncio
async def test_proposal_review_passes_selected_non_default_project_to_governance():
    calls = []

    async def reviewer(proposal_id, feedback_type, reason, project_id):
        calls.append((proposal_id, feedback_type, reason, project_id))
        return {
            "updated": True,
            "item_id": proposal_id,
            "feedback_type": feedback_type,
            "status": "adopted",
            "memory_id": "memory-b",
        }

    app, repositories = build_app(
        settings(review="1"),
        proposal_review_provider=reviewer,
    )

    response = await request(
        app,
        "/api/dashboard/v2/memory-proposals/proposal-b/review?project_id=project%3Ab",
        method="POST",
        headers={"x-pp-dashboard-action": "proposal-review-v1"},
        json_body={"feedback_type": "adopted"},
    )

    assert response.status_code == 200
    assert response.json()["scope"]["project_id"] == "project:b"
    assert calls == [("proposal-b", "adopted", "reviewer_rejected", "project:b")]
    assert repositories[0].scope.project_id == "project:b"


@pytest.mark.asyncio
async def test_dashboard_shell_and_assets_use_a_strict_read_only_surface():
    app, repositories = build_app(settings())

    shell = await request(app, "/dashboard")
    stylesheet = await request(app, "/dashboard/assets/v2/app.css")
    script = await request(app, "/dashboard/assets/v2/app.js")
    unknown = await request(app, "/dashboard/assets/v2/unknown.js")

    assert shell.status_code == 200
    assert "/dashboard/assets/v2/app.js?v=20260803-passive-jobs-v1" in shell.text
    assert "/dashboard/assets/v2/app.css?v=20260803-passive-jobs-v1" in shell.text
    assert 'lang="zh-CN"' in shell.text
    for label in (
        "概览",
        "请求",
        "记忆",
        "被动记忆",
        "记忆提案",
        "检索解释",
        "记忆谱系",
        "综合记忆",
        "运行运维",
        "信任与问题",
        "有效配置",
        "服务器状态",
        "云端期望配置",
        "配置修订",
        "控制审计",
    ):
        assert label in shell.text
    assert "<span>Overview</span>" not in shell.text
    assert "script-src 'self'" in shell.headers["content-security-policy"]
    assert (
        "connect-src 'self' http://127.0.0.1:9040 http://127.0.0.1:19040"
        in shell.headers["content-security-policy"]
    )
    assert "http://127.0.0.1:*" not in shell.headers["content-security-policy"]
    assert "no-store" in shell.headers["cache-control"]
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    app_shell_css = stylesheet.text.split(".app-shell {", 1)[1].split("}", 1)[0]
    assert "height: 100vh;" in app_shell_css
    assert "height: 100dvh;" in app_shell_css
    assert "min-height: 100vh;" not in app_shell_css
    assert "min-height: 100dvh;" not in app_shell_css
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert 'title: "概览"' in script.text
    assert 'ready: "就绪"' in script.text
    assert "text: statusLabel(value)" in script.text
    assert 'sectionHeader("异步记忆管道"' in script.text
    assert "data.derived_jobs" in script.text
    assert 'passive_semantic: "语义识别"' in script.text
    assert 'proposal_promotion: "提案晋升"' in script.text
    assert unknown.status_code == 404
    assert repositories == []


@pytest.mark.asyncio
async def test_dashboard_control_views_keep_credentials_memory_only_and_api_headless():
    app, repositories = build_app(settings())

    shell = await request(app, "/dashboard")
    script = await request(app, "/dashboard/assets/v2/app.js")
    control_script = await request(app, "/dashboard/assets/v2/control.js")
    control_stylesheet = await request(app, "/dashboard/assets/v2/control.css")

    assert shell.status_code == 200
    for route in (
        "#/control-status",
        "#/control-config",
        "#/control-revisions",
        "#/control-audit",
    ):
        assert route in shell.text
    assert control_script.status_code == 404
    assert control_stylesheet.status_code == 404
    assert script.headers["content-type"].startswith("text/javascript")
    for marker in (
        'return "http://127.0.0.1:19040/api/control/v1"',
        'return "http://127.0.0.1:9040/api/control/v1"',
        'Authorization: "Bearer " + token',
        'credentials: "omit"',
        'cache: "no-store"',
        'referrerPolicy: "no-referrer"',
        'headers["If-Match"]',
        'headers["Idempotency-Key"]',
        'window.crypto.subtle.digest("SHA-256"',
        'case "control-status"',
        'case "control-config"',
        'case "control-revisions"',
        'case "control-audit"',
        "resetControlSession();",
        'data-control-secret": "token"',
        'textButton("载入推荐云配置"',
        'base_url: "https://api.syuan.org"',
        'path: "/v1/embeddings"',
        'model: "Qwen3-Embedding-8B"',
        "cost_per_million_tokens: null",
        'cost_currency: ""',
        'pricing_revision: ""',
        'model: "deepseek-v4-flash"',
        "temperature: 0",
        "json_mode: true",
    ):
        assert marker in script.text
    assert "console.log" not in script.text
    assert "document.cookie" not in script.text
    assert "sessionStorage" not in script.text
    assert 'localStorage.setItem("control' not in script.text
    assert 'localStorage.setItem("token' not in script.text
    assert repositories == []


@pytest.mark.asyncio
async def test_dashboard_control_secret_retry_lifecycle_is_fail_closed():
    app, _ = build_app(settings())

    script = (await request(app, "/dashboard/assets/v2/app.js")).text
    reader = script.split("function readControlCandidate(form)", 1)[1].split(
        "function runControlConfigAction", 1
    )[0]
    action = script.split("function runControlConfigAction", 1)[1].split(
        "function renderControlConfig", 1
    )[0]
    success = action.split("}).then(function (result)", 1)[1].split("}).catch(function (error)", 1)[
        0
    ]
    failure = action.split("}).catch(function (error)", 1)[1].split("}).finally(function ()", 1)[0]

    assert "clearControlSecretInputs()" not in reader
    assert 'if (kind === "stage")' in success
    assert "resetControlSecretEditors(form);" in success
    assert "resetControlSecretEditors" not in failure
    assert 'serialized = "";' in action
    assert "setControlEditorLocked(form, true);" in action
    assert "controlEditorMatches(form, serialized)" in success
    assert "setControlEditorLocked(form, false);" in action
    assert 'refs.mainNav.setAttribute("inert", "");' in action
    assert 'refs.mainNav.removeAttribute("inert");' in action
    resetter = script.split("function resetControlSecretEditors(scope)", 1)[1].split(
        "function clearControlSecretInputs", 1
    )[0]
    assert 'operation.value = "";' in resetter
    assert 'input.value = "";' in resetter
    assert "input.disabled = true;" in resetter


@pytest.mark.asyncio
async def test_dashboard_control_mutation_navigation_and_session_guards_are_explicit():
    app, _ = build_app(settings())

    script = (await request(app, "/dashboard/assets/v2/app.js")).text
    disconnect = script.split("function disconnectControlSession()", 1)[1].split(
        "function controlRequest", 1
    )[0]
    route_handler = script.split("function handleRoute()", 1)[1].split(
        "function initializeSidebar", 1
    )[0]
    route_key = script.split("function canonicalRouteKey(view, params)", 1)[1].split(
        "function navigate", 1
    )[0]
    initializer = script.split("function initialize()", 1)[1].split("initialize();", 1)[0]
    session_reset = script.split("function resetControlSession()", 1)[1].split(
        "function disconnectControlSession", 1
    )[0]

    assert "if (state.control.busy)" in disconnect
    assert "resetControlSession();" in disconnect
    assert "if (state.control.busy)" in route_handler
    assert "canonicalRouteKey(route.view, route.params)" in route_handler
    assert "canonicalRouteKey(state.currentView, state.routeParams)" in route_handler
    assert "replaceRouteParams(state.currentView, state.routeParams);" in route_handler
    assert "query.sort();" in route_key
    assert 'refs.detailDialog.addEventListener("cancel"' in initializer
    cancel_handler = initializer.split('refs.detailDialog.addEventListener("cancel"', 1)[1].split(
        'refs.detailDialog.addEventListener("close"', 1
    )[0]
    assert "event.preventDefault();" in cancel_handler
    assert "closeDialog();" in cancel_handler
    assert 'window.addEventListener("beforeunload"' in initializer
    assert 'event.returnValue = "";' in initializer
    assert 'window.addEventListener("pageshow"' in initializer
    pageshow_handler = initializer.split('window.addEventListener("pageshow"', 1)[1].split(
        'window.addEventListener("resize"', 1
    )[0]
    assert "if (!event.persisted)" in pageshow_handler
    assert "resetControlSession();" in pageshow_handler
    assert "closeDialog(true);" in pageshow_handler
    assert "refs.detailBody.replaceChildren();" in pageshow_handler
    assert 'refs.detailEyebrow.textContent = "详情";' in pageshow_handler
    assert 'refs.detailTitle.textContent = "";' in pageshow_handler
    assert "loadCurrentView();" in pageshow_handler
    assert 'state.control.configDraft = "{}";' in session_reset


@pytest.mark.asyncio
async def test_dashboard_control_refreshes_cas_and_projects_revision_eligibility():
    app, _ = build_app(settings())

    script = (await request(app, "/dashboard/assets/v2/app.js")).text
    loader = script.split("function loadControlCurrentView()", 1)[1].split(
        "function renderPayload", 1
    )[0]
    revisions = script.split("function renderControlRevisions(payload)", 1)[1].split(
        "function renderControlAudit", 1
    )[0]
    activation = script.split("function openControlActivation(revision, trigger)", 1)[1].split(
        "function renderControlRevisions", 1
    )[0]

    assert 'if (definition.endpoint === "/config/safe")' in loader
    assert 'controlRequest("/config/safe").then(applyControlSafeSnapshot)' in loader
    assert "state.control.etag || definition.endpoint" not in loader
    assert "state.control.activeRevisionId" in revisions
    assert 'String(row.base_etag || "") === state.control.etag' in revisions
    assert "var disabled = active || stale || insufficient;" in revisions
    assert 'label = active ? "已激活" : stale ? "已过期" : "激活"' in revisions
    assert 'revision.revision_id || "") === state.control.activeRevisionId' in activation
    assert 'revision.base_etag || "") !== state.control.etag' in activation


@pytest.mark.asyncio
async def test_dashboard_project_selector_propagates_scope_to_reads_and_reviews():
    app, _ = build_app(settings())

    page = (await request(app, "/dashboard")).text
    script = (await request(app, "/dashboard/assets/v2/app.js")).text
    request_helper = script.split("function apiRequest", 1)[1].split("function apiMutation", 1)[0]
    mutation_helper = script.split("function apiMutation", 1)[1].split("function pageState", 1)[0]

    assert 'id="project-scope-select"' in page
    assert 'apiRequest("/scopes"' in script
    assert "activeProjectId" in script
    assert "applyProjectScopeToUrl" in request_helper
    assert "applyProjectScopeToUrl" in mutation_helper
    assert "pp_dashboard_project_id" not in script


@pytest.mark.asyncio
async def test_dashboard_control_error_and_embedding_evidence_match_backend_contract():
    app, _ = build_app(settings())

    script = (await request(app, "/dashboard/assets/v2/app.js")).text
    evidence = script.split("var evidenceTemplate = needsEvidence", 1)[1].split(
        "var confirmation", 1
    )[0]

    assert "control_idempotency_key_conflict" in script
    assert "control_idempotency_conflict" not in script
    assert "control_embedding_cost_policy_incomplete" in script
    assert "control_embedding_cost_currency_invalid" in script
    for field in (
        "revision_id",
        "embedding_identity",
        "provider_smoke",
        "shadow_generation",
        "quality_gate",
        "evidence_id",
        "generation_id",
        "passed",
    ):
        assert field in evidence
    assert "单独提供 generation_id 不会通过校验" in script


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

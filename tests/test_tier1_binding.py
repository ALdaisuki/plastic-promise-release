"""Tier-1 binding tests: reviewer authority gate on six mutation handlers.

每个 handler 直接 import + asyncio.run 调用，断言三种 _runtime_context：
  - 公开派发 (actor=pi_fixer/server_dispatch) -> error == "reviewer_authority_required"
  - claude 派发 / None (直连)               -> error != "reviewer_authority_required"

args 均为最小无效输入，让处理器过门后立即自行报错，避免副作用。
"""

import asyncio
import json

import pytest
from mcp.types import TextContent

from plastic_promise.mcp.tools.management import handle_pack_import
from plastic_promise.mcp.tools.market import (
    handle_market_disable,
    handle_market_enable,
    handle_market_install,
    handle_market_remove,
)
from plastic_promise.mcp.tools.review import handle_review_run

CTX_PI = {"actor": "pi_fixer", "authority_source": "server_dispatch"}
CTX_CLAUDE = {
    "actor": "claude",
    "authority_source": "server_dispatch",
}

REJECTION_ERROR = "reviewer_authority_required"


def _payload(response: list[TextContent]) -> dict:
    assert isinstance(response, list) and response, response
    return json.loads(response[0].text)


# handler 名 -> (可调用, 最小无效 args)
HANDLERS = {
    "handle_pack_import": (handle_pack_import, {}),
    "handle_market_install": (handle_market_install, {}),
    "handle_market_remove": (handle_market_remove, {}),
    "handle_market_enable": (handle_market_enable, {}),
    "handle_market_disable": (handle_market_disable, {}),
    "handle_review_run": (
        handle_review_run,
        # prepare 且缺 project_id -> 过门后立即被 project guard 拒绝，无副作用
        {"action": "prepare"},
    ),
}


@pytest.mark.parametrize("handler_name", sorted(HANDLERS))
@pytest.mark.parametrize(
    "ctx, expect_rejected",
    [
        (CTX_PI, True),
        (CTX_CLAUDE, False),
        (None, False),
    ],
    ids=["pi_dispatch_rejected", "claude_passes", "no_ctx_passes"],
)
def test_reviewer_authority_gate(handler_name, ctx, expect_rejected):
    handler, args = HANDLERS[handler_name]
    payload = _payload(asyncio.run(handler(None, dict(args), _runtime_context=ctx)))
    if expect_rejected:
        assert payload.get("error") == REJECTION_ERROR, payload
        assert payload.get("success") is False, payload
    else:
        assert payload.get("error") != REJECTION_ERROR, payload

"""Pi Agent — Headless 后台 Agent 主循环

每个 Pi Agent 是一个独立进程，通过环境变量 AGENT_OWNER 注册身份。
启动: AGENT_OWNER=pi_builder python -m plastic_promise.agent
"""

import os
import sys
import json
import asyncio
import logging
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

# Agent 身份由环境变量决定
ROLE = os.environ.get("AGENT_OWNER", "")
DOMAIN_MAP = {
    "pi_builder":  "building",
    "pi_fixer":    "fixing",
    "pi_reviewer": "reflecting",
}
DOMAIN = DOMAIN_MAP.get(ROLE, "uncategorized")

# 轮询间隔（秒）
POLL_INTERVAL = int(os.environ.get("AGENT_POLL_INTERVAL", "15"))

# MCP SSE 服务地址
MCP_URL = os.environ.get("PLASTIC_MCP_URL", "http://127.0.0.1:9020/sse")


class McpClient:
    """Thin MCP client wrapper — bridges async tool calls to the SSE endpoint.

    Uses the mcp library's sse_client transport + ClientSession for full
    JSON-RPC protocol support.  Callers get back plain dicts parsed from
    the first TextContent blob the server returns.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url
        self._session: Optional[ClientSession] = None
        self._read = None
        self._write = None
        self._stack = None

    async def connect(self):
        """Open SSE transport, create session, and initialise."""
        self._stack = sse_client(self.base_url)
        self._read, self._write = await self._stack.__aenter__()
        self._session = ClientSession(self._read, self._write)
        await self._session.initialize()

    async def call(self, tool_name: str, args: dict | None = None) -> dict:
        """Invoke an MCP tool and return the first text result as a dict."""
        if self._session is None:
            raise RuntimeError("McpClient not connected — call connect() first")
        args = args or {}
        result = await self._session.call_tool(tool_name, args)
        for content in result.content:
            if hasattr(content, "text"):
                try:
                    return json.loads(content.text)
                except json.JSONDecodeError:
                    return {"text": content.text}
        return {}

    async def close(self):
        """Tear down session and SSE transport."""
        if self._stack is not None:
            await self._stack.__aexit__(None, None, None)
            self._session = None
            self._stack = None


class PiAgent:
    """Headless Pi Agent — 轮询 Issue 表 → 执行任务 → 交付。

    每个 Agent:
      1. 宪法注入 — principle_activate
      2. 权限检查 — defense(action="get") + trust-freedom
      3. 主循环 — issue_list → transition → execute → store → signal
    """

    def __init__(self, mcp: McpClient):
        self.mcp = mcp
        self.role = ROLE
        self.domain = DOMAIN
        self.trust_score = 0.5
        self.tier = "standard"
        self._running = False

    async def start(self):
        """Agent 启动序列。"""
        logger.info(f"Agent {self.role} 启动，域 {self.domain}")

        # 1. 宪法注入
        logger.info("宪法注入...")
        princ = await self.mcp.call("principle_activate", {"task_type": "general"})
        activated = princ.get("activated", [])
        logger.info(f"激活原则: {len(activated)} 条")

        # 2. 注册身份
        logger.info("注册身份...")
        await self.mcp.call("memory_store", {
            "content": f"Agent {self.role} 启动，域 {self.domain}",
            "memory_type": "experience",
        })

        # 3. 权限检查
        logger.info("权限检查...")
        trust = await self.mcp.call("defense", {"action": "get"})
        self.trust_score = trust.get("score", 0.5)
        tier_info = get_tier_info(self.trust_score)
        self.tier = tier_info["tier"]
        logger.info(f"信任分: {self.trust_score}, 自由度: {self.tier}")

        self._running = True
        await self._main_loop()

    async def _main_loop(self):
        """主循环: 轮询 → 认领 → 执行 → 交付。"""
        from plastic_promise.core.issue_validator import get_tier, check_permission

        while self._running:
            try:
                # 1. 拉取分配给自己的 open 任务
                logger.debug(f"轮询 Issue (owner={self.role}, state=open)...")
                issues = await self.mcp.call("issue_list", {
                    "owner": self.role, "state": "open"
                })

                # 2. 过滤信任门槛 → 认领 → 执行 → 交付
                for issue in issues.get("items", []):
                    min_trust = issue.get("context", {}).get("min_trust_level", "standard")
                    if get_tier(self.trust_score) != min_trust and \
                       _tier_rank(get_tier(self.trust_score)) < _tier_rank(min_trust):
                        logger.info(
                            f"跳过 Issue {issue['id']}: 需要 {min_trust}, 当前 {self.tier}"
                        )
                        continue

                    # 3. 认领
                    await self.mcp.call("issue_transition", {
                        "issue_id": issue["id"], "state": "in_progress",
                        "reason": f"{self.role} 已认领",
                    })

                    # 4. 检查权限 → 高风险操作发审批信号
                    for action in issue.get("actions", []):
                        perm = check_permission(self.tier, action)
                        if perm == "needs_review":
                            await self.mcp.call("domain", {"action": "merge"})

                    # 5. 执行任务
                    result = await self._execute(issue)

                    # 6. 交付
                    await self.mcp.call("memory_store", {
                        "content": result["summary"],
                        "memory_type": "experience",
                    })
                    await self.mcp.call("issue_transition", {
                        "issue_id": issue["id"], "state": "resolved",
                        "reason": f"交付: {result['files']}",
                    })

                await asyncio.sleep(POLL_INTERVAL)

            except Exception as e:
                logger.error(f"主循环异常: {e}")
                await asyncio.sleep(POLL_INTERVAL)

    async def _execute(self, issue: dict) -> dict:
        """执行任务 — 子类或配置决定具体行为。

        实际部署时，此处应调用 Claude API 或其他 LLM，
        传入 issue.context 作为完整上下文。
        """
        context = issue.get("context", {})
        return {
            "summary": f"Agent {self.role} 在域 {self.domain} 处理 Issue",
            "tags": [self.domain, self.role],
            "files": context.get("files", []),
        }

    async def stop(self):
        """优雅关闭。"""
        logger.info(f"Agent {self.role} 关闭中...")
        self._running = False


def get_tier_info(trust_score: float) -> dict:
    """返回完整的自由度信息（含 motto）。"""
    TRUST_TIERS = [
        (0.80, "autonomous", "放手干，结果负责"),
        (0.60, "standard",   "常规操作，需周知"),
        (0.30, "restricted", "关键操作需审批"),
        (0.00, "readonly",   "只能看，不能动"),
    ]
    for threshold, name, motto in TRUST_TIERS:
        if trust_score >= threshold:
            return {"tier": name, "threshold": threshold, "motto": motto}
    return {"tier": "readonly", "threshold": 0.0, "motto": "只能看，不能动"}


def _tier_rank(tier: str) -> int:
    """Lower number = higher trust."""
    _order = {"autonomous": 0, "standard": 1, "restricted": 2, "readonly": 3}
    return _order.get(tier, 99)


async def main():
    """入口: python -m plastic_promise.agent"""
    if not ROLE:
        logger.error("AGENT_OWNER 环境变量未设置。请设置后重试。")
        sys.exit(1)

    mcp = McpClient(MCP_URL)
    try:
        await mcp.connect()
    except Exception as e:
        logger.error(f"无法连接 MCP 服务 ({MCP_URL}): {e}")
        sys.exit(1)

    agent = PiAgent(mcp)
    try:
        await agent.start()
    except KeyboardInterrupt:
        await agent.stop()
    finally:
        await mcp.close()


if __name__ == "__main__":
    asyncio.run(main())

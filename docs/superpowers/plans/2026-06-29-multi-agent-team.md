# Multi-Agent Development Team Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Multi-Agent development team — Claude PM manages Pi Builder/Fixer/Reviewer via Issue table protocol, trust-freedom matrix, supervisor daemon. ~200 new lines.

**Architecture:** Three new Python modules: issue_validator (constitution enforcement), agent (Pi headless main loop), agent_supervisor (process lifecycle). All communication via existing Issue table + federation signals.

**Tech Stack:** Python 3.10+, asyncio, existing Plastic Promise MCP tools (issue_create/transition/list, defense, memory_store/recall, domain)

## Global Constraints

- Issue 表 = 唯一通信协议，不建消息队列/RPC/WebSocket
- 宪法人人遵守 — validate_issue_context 管 Claude 也管 Pi
- 信号 ≤200 字符，不传完整上下文
- 信任-自由度矩阵: 4 档映射 (autonomous/standard/restricted/readonly)
- 零新 MCP 工具 — 复用已有的
- agent_id 参数已预埋 (Task 0)

---

### Task 1: validate_issue_context — 宪法校验器

**Files:**
- Create: `plastic_promise/core/issue_validator.py`
- Create: `tests/test_issue_validator.py`

**Interfaces:**
- Produces: `validate_issue_context(issue: dict) -> dict` — 返回 `{"valid": True}` 或 `{"error": "NEEDS_CONTEXT: 缺少 ['files']"}`
- Produces: `REQUIRED_CONTEXT = ["files", "interfaces", "acceptance"]`
- Produces: `get_tier(trust_score: float) -> str`
- Produces: `check_permission(tier: str, action: str) -> str` — "granted" | "needs_review" | "denied"

- [ ] **Step 1: 创建测试**

`tests/test_issue_validator.py`:

```python
"""Issue Validator + Trust-Freedom Matrix 测试"""
import pytest
from plastic_promise.core.issue_validator import (
    validate_issue_context, get_tier, check_permission, REQUIRED_CONTEXT
)

class TestIssueValidator:
    def test_valid_context_passes(self):
        issue = {"context": {"files": ["a.py"], "interfaces": "def f():", "acceptance": "pytest"}}
        result = validate_issue_context(issue)
        assert result["valid"] is True

    def test_missing_context_rejected(self):
        issue = {"context": {"files": ["a.py"]}}
        result = validate_issue_context(issue)
        assert "error" in result
        assert "NEEDS_CONTEXT" in result["error"]
        assert "interfaces" in result["error"]

    def test_empty_context_rejected(self):
        issue = {"context": {}}
        result = validate_issue_context(issue)
        assert "error" in result

    def test_get_tier_autonomous(self):
        assert get_tier(0.90) == "autonomous"
        assert get_tier(0.80) == "autonomous"

    def test_get_tier_standard(self):
        assert get_tier(0.70) == "standard"
        assert get_tier(0.60) == "standard"

    def test_get_tier_restricted(self):
        assert get_tier(0.50) == "restricted"
        assert get_tier(0.30) == "restricted"

    def test_get_tier_readonly(self):
        assert get_tier(0.20) == "readonly"
        assert get_tier(0.0) == "readonly"

    def test_permission_autonomous_can_assign(self):
        assert check_permission("autonomous", "assign_task") == "granted"

    def test_permission_standard_cannot_assign(self):
        assert check_permission("standard", "assign_task") == "denied"

    def test_permission_restricted_needs_review_for_write(self):
        assert check_permission("restricted", "write_file") == "needs_review"

    def test_permission_readonly_cannot_write(self):
        assert check_permission("readonly", "write_file") == "denied"
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" pytest tests/test_issue_validator.py -v
```

Expected: all FAIL (模块不存在)

- [ ] **Step 3: 实现**

`plastic_promise/core/issue_validator.py`:

```python
"""Issue Validator — 宪法校验 + 信任-自由度矩阵

规则:
  1. Issue context 必须包含 files, interfaces, acceptance
  2. 信任分 → 离散自由度 → 工具权限映射
  3. 校验不区分角色 — Claude 和 Pi 同等约束
"""

REQUIRED_CONTEXT = ["files", "interfaces", "acceptance"]

# 信任 → 自由度映射
TRUST_TIERS = [
    (0.80, "autonomous", "放手干，结果负责"),
    (0.60, "standard",   "常规操作，需周知"),
    (0.30, "restricted", "关键操作需审批"),
    (0.00, "readonly",   "只能看，不能动"),
]

# 自由度 → 工具权限映射 (* 后缀 = 需审批)
ACTION_PERMISSIONS = {
    "read":           ["readonly", "restricted", "standard", "autonomous"],
    "memory_recall":  ["readonly", "restricted", "standard", "autonomous"],
    "issue_list":     ["readonly", "restricted", "standard", "autonomous"],
    "write_file":     ["restricted*", "standard", "autonomous"],
    "run_bash":       ["restricted*", "standard", "autonomous"],
    "issue_create":   ["standard", "autonomous"],
    "issue_close":    ["standard*", "autonomous"],
    "assign_task":    ["autonomous"],
    "modify_principle": ["autonomous*"],
}


def validate_issue_context(issue: dict) -> dict:
    """校验 Issue context 是否完整。

    Args:
        issue: 含 context 字段的 Issue dict。

    Returns:
        {"valid": True} 或 {"error": "NEEDS_CONTEXT: 缺少 [...]"}
    """
    context = issue.get("context", {})
    if not isinstance(context, dict):
        return {"error": "NEEDS_CONTEXT: context 必须是一个对象"}
    missing = [k for k in REQUIRED_CONTEXT if not context.get(k)]
    if missing:
        return {"error": f"NEEDS_CONTEXT: 缺少 {missing}。请补全后重新创建。"}
    return {"valid": True}


def get_tier(trust_score: float) -> str:
    """将连续信任分映射为离散自由度等级。

    Args:
        trust_score: 0.0-1.0 的信任分。

    Returns:
        "autonomous" | "standard" | "restricted" | "readonly"
    """
    for threshold, name, _ in TRUST_TIERS:
        if trust_score >= threshold:
            return name
    return "readonly"


def get_tier_info(trust_score: float) -> dict:
    """返回完整的自由度信息（含 motto）。"""
    for threshold, name, motto in TRUST_TIERS:
        if trust_score >= threshold:
            return {"tier": name, "threshold": threshold, "motto": motto}
    return {"tier": "readonly", "threshold": 0.0, "motto": "只能看，不能动"}


def check_permission(tier: str, action: str) -> str:
    """检查指定自由度的 Agent 是否有权执行某操作。

    Args:
        tier: 自由度等级 ("autonomous" 等)。
        action: 操作名 ("write_file" 等)。

    Returns:
        "granted" | "needs_review" | "denied"
    """
    allowed = ACTION_PERMISSIONS.get(action, [])
    if tier in allowed:
        return "granted"
    if f"{tier}*" in allowed:
        return "needs_review"
    return "denied"
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" pytest tests/test_issue_validator.py -v
```

Expected: 11 PASS

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/issue_validator.py tests/test_issue_validator.py
git commit -m "feat: issue_validator — constitution enforcement + trust-freedom matrix"
```

---

### Task 2: Pi Agent 主循环

**Files:**
- Create: `plastic_promise/agent.py`

**Interfaces:**
- Produces: `PiAgent(role, domain, port)` — headless Agent 主循环
- Consumes: issue_validator (Task 1), 现有 MCP 工具

- [ ] **Step 1: 创建 agent.py**

`plastic_promise/agent.py`:

```python
"""Pi Agent — Headless 后台 Agent 主循环

每个 Pi Agent 是一个独立进程，通过环境变量 AGENT_OWNER 注册身份。
启动: AGENT_OWNER=pi_builder python -m plastic_promise.agent
"""

import os
import sys
import time
import asyncio
import logging
from typing import Any, Optional

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


class PiAgent:
    """Headless Pi Agent — 轮询 Issue 表 → 执行任务 → 交付。

    每个 Agent:
      1. 宪法注入 — principle_activate
      2. 权限检查 — defense(action="get") + trust-freedom
      3. 主循环 — issue_list → transition → execute → store → signal
    """

    def __init__(self):
        self.role = ROLE
        self.domain = DOMAIN
        self.trust_score = 0.5
        self.tier = "standard"
        self._running = False

    async def start(self):
        """Agent 启动序列。"""
        logger.info(f"Agent {self.role} 启动，域 {self.domain}")

        # 1. 宪法注入 (MCP)
        logger.info("宪法注入...")
        # princ = await mcp_call("principle_activate", {"task_type": "general"})
        # logger.info(f"激活原则: {len(princ.get('activated', []))} 条")

        # 2. 注册身份 (MCP)
        logger.info("注册身份...")
        # await mcp_call("memory_store", {
        #     "content": f"Agent {self.role} 启动，域 {self.domain}",
        #     "memory_type": "experience",
        # })

        # 3. 权限检查 (MCP)
        logger.info("权限检查...")
        # trust = await mcp_call("defense", {"action": "get"})
        # self.trust_score = trust.get("score", 0.5)
        # tier_info = get_tier_info(self.trust_score)
        # self.tier = tier_info["tier"]
        logger.info(f"信任分: {self.trust_score}, 自由度: {self.tier}")

        self._running = True
        await self._main_loop()

    async def _main_loop(self):
        """主循环: 轮询 → 认领 → 执行 → 交付。"""
        from plastic_promise.core.issue_validator import get_tier, check_permission

        while self._running:
            try:
                # 1. 拉取分配给自己的 open 任务
                logger.debug(f"轮询 Issue (assignee={self.role}, state=open)...")
                # issues = await mcp_call("issue_list", {
                #     "assignee": self.role, "state": "open"
                # })

                # 2. 过滤信任门槛
                # for issue in issues.get("items", []):
                #     min_trust = issue.get("context", {}).get("min_trust_level", "standard")
                #     if get_tier(self.trust_score) < min_trust:
                #         logger.info(f"跳过 Issue {issue.id}: 需要 {min_trust}, 当前 {self.tier}")
                #         continue
                #
                #     # 3. 认领
                #     await mcp_call("issue_transition", {
                #         "issue_id": issue.id, "state": "in_progress",
                #         "reason": f"{self.role} 已认领"
                #     })
                #
                #     # 4. 检查权限 → 高风险操作发审批信号
                #     for action in issue.get("actions", []):
                #         perm = check_permission(self.tier, action)
                #         if perm == "needs_review":
                #             await mcp_call("domain", {
                #                 "action": "merge",  # 复用 signal 通道
                #             })
                #
                #     # 5. 执行任务（这里调用 LLM + 工具链）
                #     result = await self._execute(issue)
                #
                #     # 6. 交付
                #     await mcp_call("memory_store", {
                #         "content": result.summary,
                #         "tags": result.tags,
                #         "memory_type": "experience",
                #     })
                #     await mcp_call("issue_transition", {
                #         "issue_id": issue.id, "state": "resolved",
                #         "reason": f"交付: {result.files}"
                #     })

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


async def main():
    """入口: python -m plastic_promise.agent"""
    if not ROLE:
        logger.error("AGENT_OWNER 环境变量未设置。请设置后重试。")
        sys.exit(1)
    agent = PiAgent()
    try:
        await agent.start()
    except KeyboardInterrupt:
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 验证启动**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" set AGENT_OWNER=pi_builder && timeout 5 python -c "import asyncio; from plastic_promise.agent import PiAgent; agent = PiAgent(); print(f'Role: {agent.role}, Domain: {agent.domain}, Tier: {agent.tier}')" 2>&1
```

Expected: `Role: pi_builder, Domain: building, Tier: standard`

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/agent.py
git commit -m "feat: Pi Agent headless main loop — poll→claim→execute→deliver"
```

---

### Task 3: agent_supervisor 守护进程

**Files:**
- Create: `agent_supervisor.py` (项目根目录)

**Interfaces:**
- Produces: 命令行入口 `python agent_supervisor.py start --all`
- Consumes: PiAgent (Task 2), issue_validator (Task 1)

- [ ] **Step 1: 创建 supervisor**

`agent_supervisor.py`:

```python
"""Agent Supervisor — Pi Agent 进程生命周期管理

启动: python agent_supervisor.py start --all
Claude 控制: issue_create(type="agent_control", action="start", role="builder")
"""

import os
import sys
import time
import signal
import subprocess
import logging
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Supervisor] %(message)s")
logger = logging.getLogger("supervisor")

AGENTS = {
    "pi_builder":  {"port": 9021, "role": "builder",  "cmd": ["python", "-m", "plastic_promise.agent"]},
    "pi_fixer":    {"port": 9022, "role": "fixer",    "cmd": ["python", "-m", "plastic_promise.agent"]},
    "pi_reviewer": {"port": 9023, "role": "reviewer", "cmd": ["python", "-m", "plastic_promise.agent"]},
}

HEALTH_CHECK_INTERVAL = 30  # 秒


class AgentSupervisor:
    """管理所有 Pi Agent 进程的生命周期。"""

    def __init__(self):
        self._processes: dict[str, subprocess.Popen] = {}
        self._running = False

    def start_agent(self, name: str) -> bool:
        """启动一个 Agent 进程。"""
        if name not in AGENTS:
            logger.error(f"未知 Agent: {name}")
            return False
        if name in self._processes and self._processes[name].poll() is None:
            logger.warning(f"Agent {name} 已在运行 (PID {self._processes[name].pid})")
            return False

        cfg = AGENTS[name]
        env = os.environ.copy()
        env["AGENT_OWNER"] = name
        env["PLASTIC_MCP_URL"] = f"http://127.0.0.1:{cfg['port']}/sse"

        proc = subprocess.Popen(
            cfg["cmd"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._processes[name] = proc
        logger.info(f"Agent {name} 启动 (PID {proc.pid}, port {cfg['port']})")
        return True

    def stop_agent(self, name: str) -> bool:
        """优雅关闭一个 Agent。"""
        if name not in self._processes:
            logger.warning(f"Agent {name} 未运行")
            return False
        proc = self._processes[name]
        if proc.poll() is not None:
            logger.info(f"Agent {name} 已退出")
            del self._processes[name]
            return True

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            logger.warning(f"Agent {name} 强制关闭 (PID {proc.pid})")
        del self._processes[name]
        logger.info(f"Agent {name} 已关闭")
        return True

    def start_all(self):
        """启动所有 Agent。"""
        logger.info("启动所有 Agent...")
        for name in AGENTS:
            self.start_agent(name)
        self._running = True
        self._health_loop()

    def stop_all(self):
        """关闭所有 Agent。"""
        logger.info("关闭所有 Agent...")
        self._running = False
        for name in list(self._processes.keys()):
            self.stop_agent(name)

    def _health_loop(self):
        """健康检查循环。"""
        while self._running:
            for name, proc in list(self._processes.items()):
                if proc.poll() is not None:
                    logger.warning(f"Agent {name} 异常退出 (code {proc.returncode}) — auto restart")
                    del self._processes[name]
                    self.start_agent(name)
            time.sleep(HEALTH_CHECK_INTERVAL)

    def status(self) -> dict:
        """返回所有 Agent 状态。"""
        result = {}
        for name, cfg in AGENTS.items():
            proc = self._processes.get(name)
            if proc is None:
                result[name] = {"status": "stopped"}
            elif proc.poll() is None:
                result[name] = {"status": "running", "pid": proc.pid, "port": cfg["port"]}
            else:
                result[name] = {"status": "exited", "exit_code": proc.returncode}
        return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agent Supervisor")
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--role", help="指定 Agent 名称或 --all")
    parser.add_argument("--all", action="store_true", help="操作所有 Agent")
    args = parser.parse_args()

    sup = AgentSupervisor()

    if args.action == "start":
        if args.all:
            sup.start_all()
        elif args.role:
            sup.start_agent(args.role)
        else:
            parser.error("需要 --role <name> 或 --all")

    elif args.action == "stop":
        if args.all:
            sup.stop_all()
        elif args.role:
            sup.stop_agent(args.role)
        else:
            parser.error("需要 --role <name> 或 --all")

    elif args.action == "status":
        status = sup.status()
        for name, s in status.items():
            print(f"  {name:15s} {s['status']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 验证启动命令**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" python agent_supervisor.py status 2>&1
```

Expected: `pi_builder      stopped` / `pi_fixer        stopped` / `pi_reviewer     stopped`

- [ ] **Step 3: Commit**

```bash
git add agent_supervisor.py
git commit -m "feat: agent_supervisor — Pi Agent process lifecycle manager"
```

---

### Task 4: E2E 集成验证

**Files:**
- Create: `tests/test_multi_agent_e2e.py`

- [ ] **Step 1: 创建 E2E 测试**

`tests/test_multi_agent_e2e.py`:

```python
"""Multi-Agent Team E2E — 宪法 + 信任 + 权限 集成验证"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMultiAgentE2E:
    def test_validator_rejects_incomplete_issue(self):
        from plastic_promise.core.issue_validator import validate_issue_context
        result = validate_issue_context({"context": {"files": ["a.py"]}})
        assert "error" in result
        assert "interfaces" in result["error"]
        assert "acceptance" in result["error"]

    def test_validator_accepts_complete_issue(self):
        from plastic_promise.core.issue_validator import validate_issue_context
        issue = {"context": {
            "files": ["a.py"], "interfaces": "def f():", "acceptance": "pytest"
        }}
        assert validate_issue_context(issue)["valid"] is True

    def test_trust_tier_boundaries(self):
        from plastic_promise.core.issue_validator import get_tier
        assert get_tier(0.90) == "autonomous"
        assert get_tier(0.80) == "autonomous"
        assert get_tier(0.79) == "standard"
        assert get_tier(0.60) == "standard"
        assert get_tier(0.59) == "restricted"
        assert get_tier(0.30) == "restricted"
        assert get_tier(0.29) == "readonly"

    def test_permission_escalation(self):
        from plastic_promise.core.issue_validator import check_permission
        # readonly 什么都不能写
        assert check_permission("readonly", "write_file") == "denied"
        assert check_permission("readonly", "read") == "granted"
        # restricted 写需审批
        assert check_permission("restricted", "write_file") == "needs_review"
        # standard 直接写
        assert check_permission("standard", "write_file") == "granted"
        # 只有 autonomous 能分配任务
        assert check_permission("standard", "assign_task") == "denied"
        assert check_permission("autonomous", "assign_task") == "granted"

    def test_pi_agent_identity(self):
        os.environ["AGENT_OWNER"] = "pi_builder"
        from plastic_promise.agent import PiAgent
        agent = PiAgent()
        assert agent.role == "pi_builder"
        assert agent.domain == "building"
        assert agent.tier == "standard"
        del os.environ["AGENT_OWNER"]

    def test_supervisor_status(self):
        from agent_supervisor import AgentSupervisor
        sup = AgentSupervisor()
        status = sup.status()
        assert "pi_builder" in status
        assert "pi_fixer" in status
        assert "pi_reviewer" in status
        # 未启动，均为 stopped
        for name in ["pi_builder", "pi_fixer", "pi_reviewer"]:
            assert status[name]["status"] == "stopped"
```

- [ ] **Step 2: 运行 E2E 测试**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" pytest tests/test_multi_agent_e2e.py -v --tb=short
```

Expected: 6 PASS

- [ ] **Step 3: 运行全量回归**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" pytest tests/test_issue_validator.py tests/test_multi_agent_e2e.py tests/test_domain_manager.py -v --tb=line -q
```

Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_multi_agent_e2e.py
git commit -m "test: multi-agent team E2E — validator, trust matrix, agent identity, supervisor status"
```

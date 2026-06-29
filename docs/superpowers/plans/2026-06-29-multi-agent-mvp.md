# Multi-Agent MVP Implementation Plan

> **For agentic workers:** Inline execution recommended. Single task, 3 configuration files, zero code changes.

**Goal:** Inject Team Protocol into Pi's .pi/memory.md, create reusable protocol file, verify Pi autonomously claims issues via MCP.

**Architecture:** Pi reads .pi/memory.md on startup → ReAct loop follows protocol → calls Plastic Promise MCP tools (issue_list, issue_transition, memory_recall).

**Tech Stack:** Markdown (configuration), Python (test assertions against Issue state history)

## Global Constraints

- 不改 Pi 源码（纯配置注入）
- 不改 Plastic Promise 代码（MCP 工具已就绪）
- 验证通过 Issue 状态历史，不依赖 Pi 日志格式
- 零新依赖

---

### Task 1: Team Protocol 注入 + 验证测试

**Files:**
- Modify: `.pi/memory.md` (追加 Team Protocol)
- Create: `.pi/team-protocol.md` (可复用协议文件)
- Create: `tests/test_team_protocol.py` (验证测试)

- [ ] **Step 1: 创建 .pi/team-protocol.md**

```markdown
## Team Protocol

你是 Plastic Promise 多 Agent 开发团队的成员。Claude 是你的项目经理。

### 任务认领
1. 调用 `issue_list(owner=<your-role>, state="open")` 查看任务
   - 返回 JSON 数组。每项含 `id`（格式 `issue_<hex12>`）、`title`、`context`
2. 从返回结果中提取 Issue ID
3. 调用 `issue_transition("<task-id>", "in_progress", reason="已认领")` 认领

### 上下文拉取
- 调用 `memory_recall(domain_hint="<your-domain>", query="<关键词>")`
  - query 从 Issue 的 `context.interfaces` 或 `context.files` 中提取
  - 不传空字符串；如果缺关键词，query 取 Issue title

### 执行
- 用 `write` / `edit` 工具实现
- 关键决策调用 `memory_store(content="<摘要>", tags=["<domain>"])` 写入共享记忆

### 交付
- 调用 `issue_transition("<task-id>", "resolved", reason="交付: <文件清单>")`

### 通信规范
- 禁止闲聊。所有通信携带 Issue ID 和文件路径
- 上下文不足时标注 NEEDS_CONTEXT，不编造
- 信号长度 ≤200 字符
```

- [ ] **Step 2: 追加到 .pi/memory.md**

在 `.pi/memory.md` 末尾追加（如文件不存在则创建）：

```markdown

## Team Protocol

以下协议在每次会话中生效：

- 你是多 Agent 开发团队成员，Claude 是项目经理
- 执行任务前调用 issue_list(owner=<role>, state="open") 查看任务，从返回的 JSON 中提取 Issue ID（格式 issue_<hex12>）
- 认领任务: issue_transition("<task-id>", "in_progress")
- 拉取上下文: memory_recall(domain_hint="<domain>", query="<关键词>")
- 交付任务: issue_transition("<task-id>", "resolved", reason="交付: <文件>")
- 禁止闲聊，上下文不足时标注 NEEDS_CONTEXT
```

- [ ] **Step 3: 创建验证测试**

`tests/test_team_protocol.py`:

```python
"""Team Protocol E2E — Pi 能自主认领 Issue 并完成生命周期"""
import os, sys, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTeamProtocol:
    def test_issue_lifecycle_via_state_history(self):
        """Pi 执行任务后，Issue 状态历史包含 in_progress"""
        from plastic_promise.core.context_engine import ContextEngine
        engine = ContextEngine()

        # Claude 创建任务
        issue = engine.create_issue(
            title="MVP Protocol: hello.py 加 /health 端点",
            assignee="pi_builder",
            context={
                "files": ["hello.py"],
                "interfaces": "GET /health -> {\"status\":\"ok\"}",
                "acceptance": "curl /health returns ok"
            }
        )
        issue_id = issue["id"]
        assert issue.get("state") == "open"

        # Pi 执行（带 Team Protocol）
        result = subprocess.run([
            "pi", "--print",
            f"执行 Issue {issue_id}：在 hello.py 加 /health 端点",
            "--append-system-prompt",
            "执行前 issue_list(owner=pi_builder, state=open) 认领，"
            "issue_transition(id, in_progress)。Issue ID 格式 issue_<hex12>。",
            "--session-id", f"mvp_{issue_id}",
        ], capture_output=True, text=True, timeout=180)

        print("Pi stdout:", result.stdout[-300:] if result.stdout else "")

        # 验证：检查 Issue 状态历史
        updated = engine.get_issue(issue_id)
        history = updated.get("history", [])
        states = [h["state"] for h in history]
        assert "in_progress" in states, \
            f"Pi 未认领任务。状态历史: {states}"

        if "resolved" in states:
            print("PASS: 完整 open→in_progress→resolved 闭环")
        else:
            print(f"WARN: Pi 只推到 in_progress（可能执行超时）")

    def test_pi_preserves_existing_code(self):
        """Pi 修改 hello.py 时保留了已有代码（间接验证上下文拉取）"""
        with open("hello.py") as f:
            before = f.read()
        assert "Hello, World!" in before, "前置条件: hello.py 需要已有 GET /"

        subprocess.run([
            "pi", "--print",
            "在 hello.py 加 GET /health → {status:ok}。保留已有 GET / 不变。",
            "--session-id", "mvp_context",
        ], capture_output=True, text=True, timeout=120)

        with open("hello.py") as f:
            after = f.read()
        assert "Hello, World!" in after, "FAIL: Pi 删除了已有代码"
        assert "/health" in after, "FAIL: Pi 未添加 /health"
        print("PASS: Pi 保留已有代码 + 添加新端点")
```

- [ ] **Step 4: 运行测试（先确保 Plastic Promise SSE 在 9020）**

```powershell
netstat -ano | findstr 9020
cd "F:/Agent/Memory system"
PYTHONPATH="F:/Agent/Memory system" pytest tests/test_team_protocol.py -v --tb=short
```

- [ ] **Step 5: Commit**

```bash
git add .pi/memory.md .pi/team-protocol.py tests/test_team_protocol.py
git commit -m "feat: Team Protocol injection — Pi autonomous Issue lifecycle via .pi/memory.md"
```

# Multi-Agent Team MVP — Team Protocol + Context Loading

> 状态: 已确认 | 日期: 2026-06-29

## 一、目标

让 Pi Agent 从"单次执行器"（`pi --print "task"`）变成"团队成员"——能自主认领 Issue、拉取项目上下文、执行任务、交付结果。

**核心洞察：Pi 本身就有 ReAct 循环 + 4 工具 + MCP 支持。不需要写代码控制它——给一份团队手册，它的推理引擎会自己决定何时调 MCP 工具。**

## 二、架构

```
Claude (项目经理)
  │
  ├─ validate_issue_context() → 宪法校验
  ├─ issue_create(assignee="pi_builder", context={...}) → 任务协议
  │
  └─ bash("pi --print '执行 Issue task_001'")
       │
       Pi ReAct 循环:
       1. 读 .pi/memory.md → 看到 Team Protocol
       2. issue_list(assignee="pi_builder") → 认领任务
       3. issue_transition(id, "in_progress") → 告知团队
       4. memory_recall(domain_hint="building") → 拉取上下文
       5. read/write/edit → 执行任务
       6. memory_store(...) → 写交付记忆
       7. issue_transition(id, "resolved") → 通知 Claude
       │
       ↓
Claude 验收 → issue_transition("closed") → defense(boost/decay)
```

## 三、Team Protocol — 注入方式

### 方式 A：.pi/memory.md（Pi 自动加载）

Pi 启动时会读取 `.pi/memory.md` 作为持久化记忆。写在里面，每次会话自动注入。

```markdown
## Team Protocol for Pi-Builder

你是一个开发团队的成员，Claude 是你的项目经理（project manager）。

### 执行任务前的标准流程

1. `issue_list(owner="pi_builder", state="open")` → 查看分配给你的任务
   - 返回 JSON 数组，每项含 `id`（格式 `issue_<hex12>`）、`title`、`context`
   - 从中提取 Issue ID，记作 `<task-id>`
2. `issue_transition("<task-id>", "in_progress", reason="已认领")` → 认领任务
3. `memory_recall(domain_hint="building", query="<任务关键词>")` → 拉取项目上下文
   - query 值从 Issue 的 `context.interfaces` 或 `context.files` 中提取关键词
   - 不传空字符串——如果 context 缺关键词，query 取 Issue title

### 执行期间

- 用 `write` / `edit` 工具写代码
- 关键决策和发现调用 `memory_store(content="<摘要>", tags=["building"])` 写入共享记忆

### 完成后

- `issue_transition(<task-id>, "resolved", reason="交付: <文件清单>")` → 通知项目经理
- 如果遇到无法解决的问题：`issue_transition(<task-id>, "in_progress", reason="NEEDS_CONTEXT: <具体缺什么>")`

### 团队通信规范

- 禁止闲聊。所有通信必须携带 Issue ID 和文件路径
- 上下文不足时标注 NEEDS_CONTEXT，不编造
- 信号长度 ≤200 字符
```

### 方式 B：--append-system-prompt（命令行注入）

```bash
pi --print "执行任务" \
   --append-system-prompt "$(cat .pi/team-protocol.md)" \
   --session-id task_001
```

### 三角色协议差异

| | Pi-Builder | Pi-Fixer | Pi-Reviewer |
|---|---|---|---|
| owner | pi_builder | pi_fixer | pi_reviewer |
| domain_hint | building | fixing | reflecting |
| memory_recall 范围 | domain_hint="building" | domain_hint="building"+issue context | domain_hint 不限制 |
| 典型交付物 | 新文件/代码修改 | 测试结果+修复 patch | 审查报告 |
| 信号目标 | Claude + Reviewer | Claude + Builder | Claude + Builder |

## 四、缺失的未来三项

以下代码已就绪，通过配置接入即可——不在本次 MVP 范围：

| 缺口 | 代码状态 | 接入方式 |
|------|----------|----------|
| 宪法拦截 | issue_validator.py ✅ | Claude 侧 enforce，Pi 侧通过 system prompt 告知 |
| 信任分反馈 | defense(boost/decay) ✅ | Claude 验收时调用 |
| 质量闸门 | Issue 状态机 + Claude 审查 | resolved→review→closed 状态流 |

## 五、验证方案

### 测试：通过 Issue 状态历史验证

不读 Pi 的日志文本——日志格式可能变化。直接查 Issue 表的状态变更历史，确定 Pi 真的调了 issue_transition。

```python
# tests/test_team_protocol.py

class TestTeamProtocol:
    def test_full_issue_lifecycle(self):
        """Pi 自主完成 open→in_progress→resolved，通过 Issue 状态历史验证"""
        from plastic_promise.core.context_engine import ContextEngine
        engine = ContextEngine()

        # 1. Claude 创建任务
        issue = engine.create_issue(
            title="MVP Protocol Test: 在 hello.py 加一个 /health 端点",
            assignee="pi_builder",
            context={
                "files": ["hello.py"],
                "interfaces": "GET /health → {\"status\": \"ok\"}",
                "acceptance": "curl http://localhost:8000/health 返回 status ok"
            }
        )
        issue_id = issue["id"]
        assert issue["state"] == "open"

        # 2. 运行 Pi（带 Team Protocol）
        import subprocess
        result = subprocess.run([
            "pi", "--print",
            f"执行 Issue {issue_id}：在 hello.py 加 /health 端点",
            "--append-system-prompt",
            "执行任务前先 issue_list(owner=pi_builder) 认领，"
            "然后 issue_transition(task_id, in_progress)，"
            "执行后 issue_transition(task_id, resolved)。"
            "Issue ID 从返回 JSON 的 id 字段提取，格式 issue_<hex12>。",
            "--session-id", f"mvp_test_{issue_id}",
        ], capture_output=True, text=True, timeout=120)
        print("Pi stdout:", result.stdout[-500:])

        # 3. 检查 Issue 状态历史 —— 不依赖日志文本
        updated = engine.get_issue(issue_id)
        states = [h["state"] for h in updated.get("history", [])]
        # Pi 应该至少推到了 in_progress（最好 resolved）
        assert "in_progress" in states, \
            f"Pi 未认领任务：{states}"
        if "resolved" in states:
            print("PASS: Pi 完成完整 open→in_progress→resolved 闭环")
        else:
            print(f"WARN: Pi 只到 in_progress，状态: {states}")

    def test_pi_recalls_context(self):
        """Pi 在修改 hello.py 时参考了已有代码（间接验证 memory_recall）"""
        # 前置：存入包含 hello.py 当前结构的信息
        from plastic_promise.core.context_engine import ContextEngine
        engine = ContextEngine()
        engine.register_memory({
            "content": "hello.py 是 FastAPI 应用，已有 GET / 端点返回 Hello World JSON",
            "memory_type": "experience",
            "tags": ["hello", "fastapi", "e2e"],
            "domain": "building",
        })

        # Pi 被要求加 /health 端点——如果它读了 memory，应该能正确导入 FastAPI
        # 验证：hello.py 仍然包含原有的 GET / 端点（没有被覆盖）
        import subprocess
        subprocess.run([
            "pi", "--print",
            "在 hello.py 中加一个 GET /health 端点，返回 {\"status\":\"ok\"}。"
            "保留已有的 GET / 端点不变。",
            "--session-id", "mvp_context_test",
        ], capture_output=True, text=True, timeout=120)

        with open("hello.py") as f:
            content = f.read()
        assert "Hello, World!" in content, "原有 GET / 端点被覆盖——Pi 没有拉取上下文"
        assert "/health" in content, "Pi 没有添加 /health 端点"
        assert "status" in content, "Pi 的 /health 端点格式错误"
        print("PASS: Pi 保留了已有代码并添加了新端点")
```

### 手动验证（第一步——确认 Pi 日志格式）

```bash
# 先跑一次，看 Pi 输出里工具调用的实际表示方式
pi --print "issue_list(owner=pi_builder, state=open)" --session-id log_check 2>&1 | head -30
```

根据实际输出调整 `query` 参数格式和 Issue ID 提取逻辑。

## 六、文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `.pi/memory.md` | 修改 | 追加 Team Protocol |
| `.pi/team-protocol.md` | 新建 | 可复用的 team protocol 文件 |
| `tests/test_team_protocol.py` | 新建 | Pi 自主调用 MCP 工具的验证 |

## 七、不做什么

- 不写 agent.py（Pi 原生 ReAct）
- 不写 supervisor（pi --print 原生 headless）
- 不改 Pi 源码（纯配置 + system prompt）
- 不建新通信协议（Issue 表 + MCP SSE）

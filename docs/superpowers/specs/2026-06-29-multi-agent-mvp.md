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
2. `issue_transition(<task-id>, "in_progress", reason="已认领")` → 认领任务
3. `memory_recall(domain_hint="building", query="<任务关键词>")` → 拉取项目上下文

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

### 测试：Pi 是否自主调用了 MCP 工具

```python
# tests/test_team_protocol.py

class TestTeamProtocol:
    def test_pi_can_list_issues(self):
        """Pi 能通过 MCP 调用 issue_list"""
        # 创建测试 Issue
        issue_create(title="E2E Protocol Test", assignee="pi_builder", ...)
        
        # 运行 Pi with team protocol
        result = run_pi("执行 Issue task_proto_001")
        
        # Pi 的 ReAct 日志应包含 issue_list / issue_transition 调用
        assert "issue_list" in result.log
        assert "in_progress" in result.log

    def test_pi_loads_context_before_execution(self):
        """Pi 执行前调用了 memory_recall"""
        memory_store("测试上下文：项目中已有 auth 模块")
        
        result = run_pi("实现 JWT 登录")
        
        # Pi 应在执行前拉取上下文
        assert "memory_recall" in result.log
        # 执行应参考已有 auth 模块
        assert "auth" in result.output.lower()

    def test_full_issue_lifecycle(self):
        """完整生命周期: open→in_progress→resolved"""
        issue = issue_create(...)
        
        run_pi(f"执行 Issue {issue.id}")
        
        # 检查 Issue 状态变更历史
        history = issue_get(issue.id).history
        states = [h["state"] for h in history]
        assert states == ["open", "in_progress", "resolved"]
```

### 手动验证

```bash
# 1. Plastic Promise SSE 就绪
netstat -ano | findstr 9020

# 2. Claude 创建测试 Issue
# (via MCP) issue_create(title="Protocol Test", assignee="pi_builder", ...)

# 3. 启动 Pi（带 Team Protocol）
pi --print "执行分配给 pi_builder 的任务" \
   --append-system-prompt "$(cat .pi/team-protocol.md)" \
   --session-id mvp_test

# 4. 检查 Issue 状态
# (via MCP) issue_list(owner="pi_builder") → state 应为 "resolved"
```

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

# Multi-Agent Development Team — 完整设计

> 状态: 已验证 (Pi 0.80.2 原生闭环跑通) | 日期: 2026-06-29

## 一、核心理念

Claude Code 从"操作者"升级为"项目经理"——不再亲自写代码，而是通过**验证、分配、监控、验收**调度 Pi Agent 团队。所有 Agent 通过共享的 Plastic Promise 记忆框架协作，Issue 表是唯一的通信协议。

**设计约束：**
- ~40 行新代码（仅 issue_validator），其余全部复用
- Pi Agent 使用原生 CLI (`pi --print`)，不重写执行层
- 零新通信协议——Issue 表 + 联邦信号已足够
- 宪法人人遵守——Claude 发的 Issue 也需要通过 context 校验

---

## 二、团队结构

```
Claude Code (项目经理)
  owner=claude, trust > 0.80 (autonomous)
  责任: 拆解→分配→不干预→验收→信任反馈
  域: governing + designing + all

┌──────────────┬──────────────┬──────────────┐
│ Pi-Builder   │ Pi-Fixer     │ Pi-Reviewer  │
│ building     │ fixing       │ reflecting   │
│ 写代码/文件   │ 测试/Bug修复  │ 审查/检查    │
│ trust: 0.85  │ trust: 0.72  │ trust: 0.91  │
│ autonomous   │ standard     │ autonomous   │
└──────────────┴──────────────┴──────────────┘
        │                             │
  未来: Pi-Scribe          未来: Pi-Guardian
  (文档/记录)              (监控/自治)
```

每个 Pi 通过 `AGENT_OWNER` 环境变量注册身份。一个物理进程 = 一个 team member。

---

## 三、通信协议 — Issue 表即一切

不建消息队列、不建 RPC、不建 WebSocket。Issue 生命周期就是任务状态机：

```
open → in_progress → resolved → closed
  ↑                               │
  └── NEEDS_CONTEXT (打回) ←──────┘
```

### Claude 发任务

```python
issue_create(
    title="实现 JWT 登录模块",
    description="...",
    assignee="pi_builder",
    domain="building",
    context={
        "files": ["auth/__init__.py", "tests/test_auth.py"],
        "interfaces": "def create_jwt(user_id: str) -> str",
        "acceptance": "pytest tests/test_auth.py -v 全部通过",
        "min_trust_level": "standard"  # 任务门槛
    }
)
```

### Pi 认领任务

```python
issues = issue_list(assignee="pi_builder", state="open")
for issue in issues:
    if get_tier(trust_score) >= issue.context["min_trust_level"]:
        issue_transition(issue.id, "in_progress")
        # 执行...
        issue_transition(issue.id, "resolved")
```

### Issue Context 校验（前置，管所有人）

```python
REQUIRED_CONTEXT = {"files", "interfaces", "acceptance"}

def validate_issue_context(issue):
    missing = [k for k in REQUIRED_CONTEXT if not issue.context.get(k)]
    if missing:
        return {"error": f"NEEDS_CONTEXT: 缺少 {missing}"}
    return {"valid": True}
```

Claude 自己发的 Issue 缺 context → 拒绝创建，返回 NEEDS_CONTEXT。强迫拆解时就精确到文件路径和接口签名。

### 负载格式

```json
{
  "assignee": "pi_builder",
  "domain": "building",
  "context": {
    "files": ["auth/jwt.py"],
    "interfaces": "def create_jwt(user_id: str) -> str",
    "acceptance": "pytest tests/test_auth.py -v 全部通过",
    "min_trust_level": "standard"
  },
  "deadline": "2026-06-30T18:00:00",
  "parent_issue": "#10"
}
```

---

## 四、信任-自由度矩阵

连续信任分 → 离散自由度 → 工具权限。复用现有 `defense(action="get")` 。

### 四档自由度

| 信任分 | 等级 | 代号 | 理念 |
|--------|------|------|------|
| 0.80-1.00 | 自主 | Autonomous | "放手干，结果负责" |
| 0.60-0.80 | 标准 | Standard | "常规操作，需周知" |
| 0.30-0.60 | 受限 | Restricted | "关键操作需审批" |
| 0.00-0.30 | 只读 | ReadOnly | "只能看，不能动" |

### 工具权限映射

| 权限 | ReadOnly | Restricted | Standard | Autonomous |
|------|----------|------------|----------|------------|
| Read/Glob/Grep | ✅ | ✅ | ✅ | ✅ |
| memory_recall | ✅ | ✅ | ✅ | ✅ |
| issue_list | ✅ | ✅ | ✅ | ✅ |
| Write/Edit | ❌ | ⚠️ 需审批 | ✅ | ✅ |
| Bash | ❌ | ⚠️ 需审批 | ✅ | ✅ |
| issue_create | ❌ | ❌ | ✅ | ✅ |
| issue_close | ❌ | ❌ | ⚠️ 需复核 | ✅ |
| assign_task | ❌ | ❌ | ❌ | ✅ |
| modify_principle | ❌ | ❌ | ❌ | ⚠️ 需复核 |

⚠️ 需审批 = 执行前必须发送联邦信号请求 Claude 确认

### 实现

```python
def get_tier(trust_score: float) -> str:
    if trust_score >= 0.80: return "autonomous"
    if trust_score >= 0.60: return "standard"
    if trust_score >= 0.30: return "restricted"
    return "readonly"

def check_permission(tier: str, action: str) -> str:
    """granted | needs_review | denied"""
    if tier in PERMS[action]: return "granted"
    if f"{tier}*" in PERMS[action]: return "needs_review"
    return "denied"
```

### 信任分反馈闭环

```
Builder 代码通过验收 → defense(action="boost", delta=0.02, reason="PR #12 通过审查")
Builder 代码被拒绝   → defense(action="decay", delta=0.02, reason="PR #12 缺少异常处理")
Reviewer 审查准确    → boost
Reviewer 误判        → decay
```

信任分变化 → 自由度自动调整 → 无需人工干预。

---

## 五、团队宪法 — "沉默专业主义"

| 规则 | 落地 |
|------|------|
| **无事可闲聊** | 信号 ≤200 字符，禁止 "好的收到马上开始" 类空信息。格式不合规 → 不传递 |
| **上下文不足不瞎猜** | validate_issue_context() 拦截缺字段任务。Pi 遇到歧义 → NEEDS_CONTEXT 打回，不编造 |
| **可以多讨论** | 讨论限定在 issue context 线程内，不广播。Claude 是唯一仲裁者 |
| **宪法人人遵守** | validate_issue_context() 校验 Claude 和 Pi，不区分角色 |

映射到已有原则：#1 奥卡姆剃刀 + #4 上下文不足标注而非猜测。

### 合法/非法通信示例

```
✅ "Issue #12 resolved。文件 auth/jwt.py:23-67。pytest 全部通过。请 Review。"
❌ "好的收到！我马上开始处理！"
✅ "NEEDS_CONTEXT: jwt.py 缺少 token 过期时间。当前 5 分钟？24 小时？"
❌ "这个接口应该大概也许可以工作吧"
✅ "Issue #12 审查通过。auth/jwt.py:45 缺少类型注解，其余 OK。建议 close。"
```

---

## 六、Headless 运行 — 原生 Pi CLI

Pi Agent (v0.80.2) 自带 ReAct 循环 + 4 工具。不需要自己写执行层。

### Claude 启动 Pi

```bash
# Claude 通过 Bash 工具调用:
pi --print "实现 JWT 登录模块" --session task_001 --provider claude
```

Pi 自动完成：read 上下文 → write 代码 → bash 测试 → 返回结果。

### Pi 的 MCP 连接

`.pi/mcp.json` 已配置 Plastic Promise SSE 端点。Pi 启动时自动连接，获得以下工具：

```
memory_store    → 交付物写入共享记忆
memory_recall   → 拉取项目上下文
issue_list      → 轮询分配给自己的任务
issue_transition → 认领/交付任务
```

### Claude 生命周期管理

```bash
# 启动: subprocess.Popen("pi --print 'task' --session id")
# 状态: issue_list(owner="pi_builder", state="in_progress")
# 终止: subprocess.terminate()
```

不需守护进程——Pi 本身是无状态 CLI，session 持久化到文件。

---

## 七、Claude 运行时流程

```
用户需求 "实现 JWT 登录"
  │
  ├─ 1. 能力分析
  │     code_generation → building 域 → Pi-Builder
  │     architecture → designing 域 → Claude 自己
  │
  ├─ 2. 拆解任务
  │     validate_issue_context() → 通过
  │     issue_create(assignee="pi_builder", context={...})
  │
  ├─ 3. 不干预 — Claude 继续处理用户其他需求
  │     Pi 在后台工作，15s 后 Claude 才收到信号
  │
  ├─ 4. 并行操作
  │     Claude: designing 域 — 设计数据库 schema
  │     Pi: building 域 — 写 JWT 代码
  │
  ├─ 5. 验收 (分水岭)
  │     memory_recall(domain_hint="building") → 拉取交付物
  │     通过 → issue_transition("closed")
  │     不通过 → issue_create(给 Fixer)
  │
  └─ 6. 信任反馈
        defense(action="boost"|"decay") → 影响下次分配
```

### Claude 状态面板

```
/agents dashboard
┌──────────────┬─────────┬───────────┬──────────────┬─────────┐
│ Agent        │ Status  │ Current   │ Last Signal  │ Trust   │
├──────────────┼─────────┼───────────┼──────────────┼─────────┤
│ pi_builder   │ busy    │ Issue #12 │ 代码已提交    │ 0.85 ▲  │
│ pi_fixer     │ idle    │ -         │ -            │ 0.72    │
│ pi_reviewer  │ idle    │ -         │ 上次: #11 审查│ 0.91 ▲  │
└──────────────┴─────────┴───────────┴──────────────┴─────────┘
```

不新建 MCP 工具——`domain(stats) + issue_list + defense(get)` 组合拼表。

---

## 八、实施范围

```
新建 (~40 行):
└── plastic_promise/core/issue_validator.py  ~40 行

复用 (零新代码):
├── pi CLI (--print --session)           ← Pi 原生 headless 执行层
├── issue_create/transition/list         ← 任务协议
├── domain(stats) + signals              ← 身份 + 通知
├── memory_store/recall                  ← 上下文 + 交付
├── defense(boost/decay/get)            ← 信任分 + 权限
├── principle_activate                   ← 宪法注入
├── pack_export/import                   ← 跨 Agent 知识
├── schema_version + _dm_ok             ← 韧性
└── agent_id 参数 (已预埋)               ← 多 Agent 追溯

已废止 (Pi 原生替代):
├── agent.py (Pi 自带 ReAct 循环)
└── agent_supervisor.py (pi --print 原生 headless)
```

---

## 九、演进路径

| 阶段 | 角色 | 目标 |
|------|------|------|
| **MVP** (当前) | Claude + Builder + Fixer + Reviewer | 核心闭环跑通 |
| **1 个月后** | + Pi-Scribe | 文档自动生成 |
| **3 个月后** | + Pi-Guardian (可选) | 系统自治 |

---

## 十、不做什么

- 不建消息队列 / RPC / WebSocket
- 不加新原则（4 条宪法全映射已有原则）
- 不加闲聊检测（格式合规即过滤，不合规不传递）
- 不建 Agent 注册表（`AGENT_OWNER` 环境变量即注册）
- 不建视觉 UI（Claude 终端拼表）
- 不拆前后端分离 Agent（域标签 `tags=["frontend"]` 已够用）
- 不加 issue comment 系统（memory_store 关联 issue_id 即为注释）

# Multi-Agent → 约定工程融入设计

> 状态: 已确认(代码验证) | 日期: 2026-06-29 | 审查轮次: 1

## 一、目标

多 Agent 开发组操作当前在约定工程框架外裸跑。让每次委派、交付、审查、验收都自动纳入 post_task 六联闭环和 PrincipleTracker 量化追踪。

**核心原则：约定工程记录的不是 Claude 做了什么，而是整个团队是否遵守了约定。**

## 二、post_task 双层调用

### Light 模式 — Claude 委派时

触发: `issue_create` 后立即调用

```python
post_task(
    task_description="委派 Issue #N 给 pi_builder: 实现 JWT 登录",
    mode="light",
    issue_id="issue_xxx",
    assignee="pi_builder",
    context_validated=True  # validate_issue_context 结果
)
```

记录:
- alignment: 校验了宪法（原则 #1, #4）
- memory_store: "委派 Issue #N 给 pi_builder，domain=building"
- trust: 不变

### Full 模式 — Claude 验收关闭时

触发: `issue_transition("closed")` 后调用

```python
post_task(
    task_description="验收 Issue #N: pi_builder 实现 JWT 登录，Reviewer 审查通过",
    mode="full",
    issue_id="issue_xxx",
    git_commit="abc1234",
    # 从 Reviewer 报告中提取:
    principle_observations={
        "#7": "test_runner.py 未被修改，下游安全",
        "#12": "auth/jwt.py:45 缺少类型注解",
        "#5": "审查建议具体且可执行"
    },
    trust_delta=+0.02,
    trust_reason="代码可用，审查通过"
)
```

记录: 完整六联 + trust 联动

## 三、Pi-Reviewer 结构化报告

在 `team-protocol-reviewer.md` 中定义交付格式：

```markdown
### 审查报告格式

审查完成后，输出结构化 JSON：

```json
{
  "status": "pass" | "fail",
  "principle_observations": {
    "#1": "<是否最简方案>",
    "#5": "<审查建议是否可操作>",
    "#7": "<下游模块是否受影响>",
    "#12": "<命名/类型是否自解释>"
  },
  "findings": ["<具体发现1>", "<具体发现2>"],
  "recommendation": "approve" | "revise"
}
```

Claude 读取 `principle_observations` 字段直接注入 post_task(full)，无需二次提取。
```

## 四、映射矩阵 — 每个多 Agent 动作对原则的负责

| 动作 | 角色 | 记录的原则 | 谁记录 |
|------|------|-----------|--------|
| 委派任务 | Claude | #1(拆解最简), #4(context完整) | post_task(light) |
| 执行代码 | Pi-Builder | #7(器官互保), #12(代码即文档) | Reviewer 观察 |
| 审查代码 | Pi-Reviewer | #3(审计闭环), #5(约定>约束) | 结构化报告 |
| 验收关闭 | Claude | #9(信任驱动), #10(自演化) | post_task(full) |
| 报修打回 | Pi-Reviewer | #3(根因分析), #4(NEEDS_CONTEXT) | Reviewer 结构报告 |
| 修复交付 | Pi-Fixer | #7(修复不引入新问题) | Reviewer 观察 |

## 五、Trust 联邦扩展

### 当前状态（需扩展）

`defense/soul_enforcer.py` — TrustManager 是单值 `self._trust: float`。不区分 Agent。

### 扩展为多 Agent 维度

```python
# TrustManager: _trust → _trusts dict
class TrustManager:
    _trusts: Dict[str, float] = {}  # key=agent_id, "" = 默认(Claude)
    
    def get(self, target: str = "") -> float:
        return self._trusts.get(target, TRUST_INITIAL)  # 默认 0.60
    
    def boost(self, delta: float, reason: str = "", target: str = "") -> float:
        self._trusts[target] = min(1.0, self.get(target) + delta)
        return self._trusts[target]
    
    def decay(self, delta: float, reason: str = "", target: str = "") -> float:
        self._trusts[target] = max(0.0, self.get(target) - delta)
        return self._trusts[target]

# MCP 工具层: defense 加 target 参数
# audit_defense.py — handle_defense_trust:
target = args.get("target", "")  # 空串 = 当前 Agent(Claude)
```

**向后兼容：** target="" 默认值 = 调整 Claude 自己的信任分。现有调用者零影响。

### Trust 联动规则
- Pi 交付被验收通过 → defense(adjust, +0.02, target="pi_builder")；Reviewer 准确 → defense(adjust, +0.01, target="pi_reviewer")
- Pi 交付被拒绝 → defense(adjust, -0.02, target="pi_builder")
- Trust-Freedom 矩阵自动生效：降到 0.59 → standard→restricted 降级

## 六、CLAUDE.md 工作流更新

```markdown
## 多 Agent 委派工作流

委派任务时:
1. validate_issue_context(issue) → 通过则继续，不通过则补全
2. issue_create(assignee="pi_builder", context={...})
3. post_task(task_description="委派 Issue #N", mode="light")

验收任务时:
1. memory_recall(domain_hint="building") → 拉取交付物
2. 读取 Reviewer 的 principle_observations
3. issue_transition(id, "closed")
4. defense(action="adjust", delta=+0.02, target="pi_builder")
5. post_task(task_description="验收 Issue #N", mode="full",
             principle_observations=reviewer_report,
             trust_delta=+0.02)
```

## 七、代码确认

### 7.1 post_task — 向后兼容 ✅

当前签名 `post_task(task_description="", git_commit="")`。加参数完全向后兼容：

```python
def post_task(task_description="", git_commit="", mode="full", issue_id=None):
```
`mode="full"` 默认值保持现有行为。

### 7.2 defense target — 需要扩展 ❌

TrustManager 当前 `self._trust: float` 单值。需改为 `self._trusts: Dict[str, float]`。

### 7.3 Reviewer 协议 — 已有文件 ✅

`.pi/team-protocol-reviewer.md` 已存在，只需追加结构化报告格式。

### 7.4 改动面完整版

| 文件 | 改动 | 说明 |
|------|------|------|
| `loop/soul_loop.py` | post_task 加 `mode` + `issue_id` | light 模式 skip SCARF/hormone/reflection |
| `defense/soul_enforcer.py` | TrustManager `_trust`→`_trusts` dict | 加 `target` 参数 |
| `mcp/tools/audit_defense.py` | handle_defense_trust 加 `target` | 透传参数 |
| `.pi/team-protocol-reviewer.md` | 追加结构化报告格式 | principle_observations JSON |
| `CLAUDE.md` | 加多 Agent 委派工作流 | 委派/验收流程 |
| `tests/test_commitment_integration.py` | 新测试 | post_task mode + trust target |

## 八、不做什么

- 不改 post_task 的六联闭环内部逻辑（只是加参数）
- 不加新的 MCP 工具（post_task、defense 已存在）
- 不给 Pi 加 post_task 调用（Pi 通过 Team Protocol 行为，约定记录是 Claude 的职责）
- 不加 center coordinator（域联邦已去中心化）

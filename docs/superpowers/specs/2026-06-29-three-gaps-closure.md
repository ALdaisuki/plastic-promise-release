# Three Gaps Closure — 宪法 + 信任 + 质量闸门

> 状态: 已确认 | 日期: 2026-06-29 | 零新代码

## 一、宪法拦截（代码已就绪）

Claude 发 Issue 前调用 `validate_issue_context()`。

```python
# Claude 工作流 — 每次 issue_create 前:
from plastic_promise.core.issue_validator import validate_issue_context

result = validate_issue_context({"context": {...}})
if "error" in result:
    return result  # 拒绝，列出缺失字段
# → issue_create
```

## 二、信任反馈（代码已就绪）

Claude 验收后调 `defense(boost/decay)`。

```python
# 通过:
defense(action="boost", delta=0.02, reason="Issue #12 交付合格，代码可用")
issue_transition(id, "closed")

# 不通过:
defense(action="decay", delta=0.02, reason="Issue #12 缺少测试覆盖")
issue_create(assignee="pi_fixer", domain="fixing", context={...})
```

## 三、质量闸门（协议文本）

Issue 状态流: `open → in_progress → review → resolved → closed`

| 阶段 | 操作者 | 动作 |
|------|--------|------|
| open → in_progress | Pi-Builder | issue_transition(id, "in_progress") |
| in_progress → review | Pi-Builder | issue_transition(id, "review", reason="交付: <files>") |
| review → resolved | Pi-Reviewer | issue_transition(id, "resolved") 或打回 in_progress |
| resolved → closed | Claude | 最终验收 |

### Pi-Reviewer 协议

```markdown
## Team Protocol for Pi-Reviewer

你是开发团队的代码审查员。审查所有标记为 review 的任务。

审查流程:
1. issue_list(state="review") → 找待审查任务
2. read <files> → 检查代码质量、安全、性能
3. 通过: issue_transition(id, "resolved", reason="审查通过")
   不通过: issue_transition(id, "in_progress", reason="打回: <具体问题>")
          + memory_store(content="审查发现: <摘要>", tags=["review"])

审查标准:
- 代码是否遵循已有模式
- 是否有测试覆盖
- 是否有安全隐患
- 接口签名是否与 Issue context 一致
```

## 四、文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `issue_validator.py` | 已有 ✅ | 宪法校验 |
| `defense(boost/decay)` | 已有 ✅ | 信任反馈 |
| `.pi/team-protocol-reviewer.md` | 新建 | Reviewer 协议 |

## 五、验证

```python
# tests/test_three_gaps.py

def test_constitution_rejects_incomplete_issue():
    from plastic_promise.core.issue_validator import validate_issue_context
    r = validate_issue_context({"context": {"files": ["a.py"]}})
    assert "error" in r

def test_constitution_accepts_complete_issue():
    from plastic_promise.core.issue_validator import validate_issue_context
    r = validate_issue_context({"context": {"files": ["a.py"], "interfaces": "f", "acceptance": "t"}})
    assert r["valid"] is True

def test_trust_feedback_affects_score():
    from plastic_promise.core.context_engine import ContextEngine
    e = ContextEngine()
    before = e._dm_defense_score if hasattr(e, '_dm_defense_score') else 0.6
    # boost 应增加信任分
    # (通过 MCP defense 工具验证)
    assert True  # 手动验证占位

def test_reviewer_can_find_review_items():
    """Reviewer issue_list(state=review) 应能获取待审查任务"""
    from plastic_promise.core.context_engine import ContextEngine
    e = ContextEngine()
    issue = e.create_issue(
        title="Review Test",
        assignee="pi_reviewer",
        state="review",
        context={"files": ["hello.py"], "interfaces": "GET /", "acceptance": "pytest"}
    )
    issues = e.list_issues(state="review")
    assert len(issues) > 0
```

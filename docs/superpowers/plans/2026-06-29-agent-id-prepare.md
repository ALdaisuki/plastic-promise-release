# agent_id 预埋 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans or inline execution. Single task.

**Goal:** Add `agent_id=""` parameter to 7 DomainManager methods + 3 call sites. Zero behavior change.

**Architecture:** Default parameter injection — all existing callers work unchanged. MCP layer auto-injects from engine owner.

**Tech Stack:** Python 3.10+

## Global Constraints

- 原则 #1: 不加 if agent_id 分支（死代码）
- 向后兼容: 默认值 "" 保证现有调用者不传参也能运行
- 零行为变化: 29 个已有测试全部通过

---

### Task 1: agent_id 参数预埋（15 行）

**Files:**
- Modify: `plastic_promise/core/domain_manager.py`
- Modify: `plastic_promise/memory/pipeline.py`
- Modify: `plastic_promise/mcp/tools/domain.py`
- Modify: `plastic_promise/mcp/tools/memory.py`
- Modify: `plastic_promise/core/step_auditor.py`
- Modify: `tests/test_domain_manager.py`

- [ ] **Step 1: DomainManager 7 个方法签名**

```python
# domain_manager.py — 每行只加 agent_id: str = ""

def assign(self, tags: list[str], agent_id: str = "") -> str:
def merge(self, source: str, target: str, agent_id: str = "") -> bool:
def rename(self, old: str, new: str, agent_id: str = "") -> bool:
def decay(self, agent_id: str = "") -> list[dict]:
def generate_signal(self, from_domain: str, to_domain: str, context: str, agent_id: str = "") -> str:
def stats(self, agent_id: str = "") -> dict:
    # TODO(agent_id): 多 Agent 场景按 agent_id 过滤域可见性
def rebuild_from_memories(self, memories_source=None, agent_id: str = "") -> dict:
```

- [ ] **Step 2: pipeline.py 调用注入**

```python
# _process_tagged_to_classified, 约第204行:
record["domain"] = self._dm.assign(tags, agent_id=getattr(self, '_owner', ''))
```

- [ ] **Step 3: domain.py MCP 注入**

```python
# 文件顶部加辅助函数:
import os

def _get_agent_id(engine) -> str:
    return getattr(engine, '_agent_owner', '') or os.environ.get("AGENT_OWNER", "")

# handle_domain 中每次 dm 调用加 agent_id:
dm.stats(agent_id=_get_agent_id(engine))
dm.merge(args["source"], args["target"], agent_id=_get_agent_id(engine))
dm.unmerge(args["source"], agent_id=_get_agent_id(engine))  # unmerge 也加上保持一致
dm.rename(args["old_name"], args["new_name"], agent_id=_get_agent_id(engine))
dm.rebuild_from_memories(agent_id=_get_agent_id(engine))
```

- [ ] **Step 4: memory.py federation signals 注入**

```python
# _generate_federation_signals 中:
"signal": dm.generate_signal(item_domain, domain_hint,
                              getattr(item, 'id', '?'),
                              agent_id=getattr(engine, '_agent_owner', '')
                                or os.environ.get("AGENT_OWNER", ""))
```

- [ ] **Step 5: step_auditor.py decay 注入**

```python
decayed = dm.decay(agent_id="")
```

- [ ] **Step 6: 测试**

```python
# test_domain_manager.py 加:
def test_agent_id_param_accepted(self):
    """agent_id 参数接受非空值不抛异常"""
    dm = DomainManager(db_path=":memory:")
    result = dm.stats(agent_id="agent_pi")
    assert "building" in result  # 行为不变
    r = dm.assign(["debug", "fix"], agent_id="agent_pi")
    assert r == "fixing"  # 行为不变
```

- [ ] **Step 7: 运行全部测试**

```powershell
cd "F:/Agent/Memory system" && PYTHONPATH="F:/Agent/Memory system" pytest tests/test_domain_manager.py tests/test_rebuild.py tests/test_resilience_e2e.py tests/test_domain_e2e.py -v --tb=line -q
```

Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add plastic_promise/core/domain_manager.py plastic_promise/memory/pipeline.py plastic_promise/mcp/tools/domain.py plastic_promise/mcp/tools/memory.py plastic_promise/core/step_auditor.py tests/test_domain_manager.py
git commit -m "feat: agent_id parameter pre-wired on DomainManager API + MCP injection (zero behavior change)"
```

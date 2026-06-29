# agent_id 预埋 — 多 Agent 参数准备

> 状态: 已评审 | 日期: 2026-06-29

## 一、目标

为 DomainManager 的 7 个公开 API 添加 `agent_id` 参数（默认空串），MCP 工具层自动从 engine owner 注入。当前不改变任何逻辑——空串 = 单 Agent 模式。未来多 Agent 激活时传实值即可。

## 二、DomainManager 签名变更

```python
# 7 个方法，每行加一个参数，默认 ""
def assign(self, tags: list[str], agent_id: str = "") -> str:
def merge(self, source: str, target: str, agent_id: str = "") -> bool:
def rename(self, old: str, new: str, agent_id: str = "") -> bool:
def decay(self, agent_id: str = "") -> list[dict]:
def generate_signal(self, from_domain: str, to_domain: str, context: str, agent_id: str = "") -> str:
def stats(self, agent_id: str = "") -> dict:
def rebuild_from_memories(self, memories_source=None, agent_id: str = "") -> dict:
```

`unmerge` 不需要——它接收 `source` 域名字符串，agent 身份不改变解绑逻辑。

## 三、调用方变更

| 文件 | 位置 | 当前 | 改为 |
|------|------|------|------|
| `memory/pipeline.py` | `_process_tagged_to_classified` | `self._dm.assign(tags)` | `self._dm.assign(tags, agent_id=getattr(self, '_owner', ''))` |
| `mcp/tools/domain.py` | `handle_domain` | `dm.stats()` / `dm.merge()` / `dm.rename()` / `dm.rebuild_from_memories()` | 全部加 `agent_id=_get_agent_id(engine)` |
| `mcp/tools/memory.py` | `_generate_federation_signals` | `dm.generate_signal(...)` | 加 `agent_id` 参数 |
| `core/step_auditor.py` | audit_step | `dm.decay()` | `dm.decay(agent_id="")` |
| `core/context_engine.py` | `_text_retrieval` | 域加权逻辑不调 DomainManager | 不变 |

## 四、MCP 层注入

```python
# domain.py 新增辅助函数
def _get_agent_id(engine) -> str:
    return getattr(engine, '_agent_owner', '') or os.environ.get("AGENT_OWNER", "")

# 每次 DomainManager 调用时自动注入
dm.stats(agent_id=_get_agent_id(engine))
```

## 五、现有 owner vs agent_id

```
owner (MemoryRecord)  — 记忆归属，用于多 Agent 记忆隔离
agent_id (DomainManager) — 操作归属，用于多 Agent 域操作追溯

当前: owner = agent_id（语义相同，来源相同：AGENT_OWNER 环境变量）
未来: owner 不变，agent_id 作为域操作的独立标识；如需映射，通过 alias_map 桥接
```

## 六、当前行为保证

- 所有 `agent_id=""` → 所有逻辑路径不变（无 `if agent_id` 分支）
- 向后兼容：现有调用者不传参数，默认空串
- audit_log 可选记录 agent_id（`_write_audit_log` 的 detail dict 加 `"agent_id": agent_id`）

## 七、测试

现有 29 个测试全部应通过（参数默认值确保零行为变化）。

新增测试：`test_agent_id_param_accepts_value` — 验证传非空 agent_id 不抛异常。

## 八、改动面

| 文件 | 改动 |
|------|------|
| `core/domain_manager.py` | 7 个方法签名 + `_write_audit_log` 注入 |
| `memory/pipeline.py` | `assign` 调用加 agent_id |
| `mcp/tools/domain.py` | 加 `_get_agent_id()` + 注入 |
| `mcp/tools/memory.py` | `generate_signal` 调用加 agent_id |
| `core/step_auditor.py` | `decay` 调用加 agent_id |
| `tests/test_domain_manager.py` | 加 1 个参数测试 |

## 九、不做什么

- 不加 if agent_id 分支（死代码）
- 不改 MemoryRecord.owner 命名
- 不改 principle_activate
- 不建 agent 注册表

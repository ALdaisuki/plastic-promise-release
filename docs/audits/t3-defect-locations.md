# T3 静默缺陷定位（只读定位报告）

> 仓库 main `8850113`；症状来源：分支 `docs/tool-surface-audit` 提交 `8db3c36` 的 `docs/audits/2026-10-02-tool-surface-audit.md:62-69`（F6/F7）。
> 触发链：`daemons/maintenance_daemon.py:2218` 注册 → `:2171-2175 _run_data_quality_job` → `:2098-2101` 每轮新建 `ContextEngine()` → `plastic_promise/cron/scan_data_quality.py:18`。
> 生产库：`~/.local/share/plastic-promise/mac-server/data/plastic-promise.sqlite3`（33 MB，WAL）。

## D1 [high] principle_injection —— 是扫描器契约错，不是注入链错

- **告警点**：`plastic_promise/cron/scan_data_quality.py:146`（消息 :151，误导性修复提示 :152）
- **探针调用**：同文件 `:134` `engine.activate_principles("code_generation", "test probe")`

数据流（真实契约 = `str`）：
1. 生产 `core/context_engine.py:6632 _activate_principles(...) -> list[str]`；`:6696-6699` `result.append(p["name"])` —— 只输出**名字**。
2. 公开包装 `:6100-6111 activate_principles(...) -> list[str]`（本仓从无 dict 版本；`plastic-promise-dev:6515-6521` 同签名）。
3. 消费 A `:5222 pack = ContextPack(activated_principles=activated)`；`:384-398 to_prompt()` 在 `:392` 按名字回查 `CORE_PRINCIPLES`，`:395` 打印 `content`、`:396` 打印 `consequence`。
4. 消费 B `:6584-6626 _inject_activated_to_graph_locked()` 按 name 建 `principle:{id}` 节点。
5. 消费 C `loop/soul_loop.py:159-169` 按 name 记 `PrincipleTracker`。

**根因**：`:146` `isinstance(p, str)` 命中即 `return`，`:156` 的 `p.get("content")` 永远不可达；该分支体按"激活返回 dict"的旧设计写成，与生产者 `list[str]` 不符。**因此"原则注入是摆设"不成立**——`mcp/tools/context.py:475` 会渲染 `pack.to_prompt()`，原则段确实进 prompt（`to_prompt` 仅此一处被调用）。
**最小修法**（只改扫描器）：断言非空 `list[str]`、每个名字可在 `CORE_PRINCIPLES` 解析且 `content` 非空，再补端到端探针 `ContextPack(activated_principles=principles).to_prompt()` 必含 `"## [PRINCIPLES]"`；`:135-143` 的"空激活"分支升为 high（那才是真静默失效）。
**影响面**：仅告警真实性（每轮误报 high，掩盖真信号）。若照 `:152` 提示把生产者改成 dict，将同时打断上述 3 处消费点与 8 处测试桩（`tests/test_recall_pipeline_upgrade.py:17,674,807`、`test_fusion_policy.py:409,903`、`test_cloud_reranker.py:71`、`test_rust_python_parity.py:563`、`test_synthesis_retrieval_gate.py:1255`、`test_governed_retrieval_embedding.py:207`）。
**修复风险：低** ｜ **需迁移：否**

## D2 [low] rust_engine_health —— 调用名与实现名不对齐

- **调用点**：`plastic_promise/cron/scan_data_quality.py:179` `healthy = engine.check_rust_health()`
- **异常吞点**：同文件 `:189-196` → 产出报告里的 `'ContextEngine' object has no attribute 'check_rust_health'`
- **实现**：`core/context_engine.py:6264 _check_rust_health(self) -> bool | None`（私有，TTL 缓存 + `_rust_lock`）
- **缺失**：无公开 `check_rust_health`——`:6100-6111` 的公开包装是 `activate_principles`；`ContextEngine`（类定义 `:731`）**无 `__getattr__`**（已核），故必然 AttributeError。同类正确调用：`context_engine.py:3541`、`mcp/server.py:283` 均用私有名。

**最小修法**（推荐 A）：A. 在 `:6111` 后加公开包装 `def check_rust_health(self): return self._check_rust_health()`；B. 把扫描器 `:179` 改为 `engine._check_rust_health()`。
**影响面**：低——健康上报失真：Rust 真不可用时给出的不是可执行修复（`:186` 的 `cargo build --release` 文案永远打不出），而是包装器报错。
**修复风险：低** ｜ **需迁移：否**。注意 `plastic-promise-dev` 同段 `:176-196` 与 release **逐字节相同**（已 diff），修复须双仓同步，否则下次 sync 回退。

## D3 schema 漂移 —— 三个概念的真实表名与写入方

生产库实测：无 `memory_records`、无 `issues`、无 `semantic_jobs`；实有 `memories`(84 行)、`task_queue`(259 行)。

| 文档/报表名 | 真实位置 | 写入方 |
|---|---|---|
| `memory_records` | **`memories`**（同库，DDL `core/context_engine.py:7928`，PK `id TEXT`） | `core/context_engine.py:8332/8378/8388`（`create_ordinary_if_absent`）、`core/memory_proposals.py:1081→1100`、`core/synthesis.py:1065→1125` |
| `issues` | **无表**；纯内存 dict `plastic_promise/issue.py:25 self._issues`（类 :12） | `mcp/tools/management.py:408-437` create / `:441-457` transition；单例 `core/context_engine.py:6190-6196`；dashboard `mcp/server.py:6979-6993` 同源 |
| `semantic_jobs` | **`knowledge_semantic_jobs`**，且**不在生产库**：在独立知识库 `knowledge/contracts.py:27-42` → `…/data/knowledge/plastic_knowledge.db`（特性门控 `:53-58`，默认 `off`） | `knowledge/repository.py:948 create_semantic_job()` → INSERT `:974-990`；消费 `knowledge/semantic.py:565/578`；守护汇总键 `daemons/maintenance_daemon.py:1719` |

**关键判读**：
- `issue_create` "成功"只因写内存——**重启即全部丢失**。守护 `scan_unclosed_issues`（`daemons/maintenance_daemon.py:1522-1581`）在 `:2172` 每轮新建引擎，故 `:1541 issues` 恒为空，**永远关不掉任何 issue**；且 `:1560-1567` 传 `to_status`/`comment`，而 handler `mcp/tools/management.py:445-449` 只认 `state`/`reason` ⇒ KeyError 被 `:1580` 吞掉。
- `knowledge_semantic_jobs` 从未建表：`…/mac-server/data/knowledge/` 目录不存在，`init_schema()`（`repository.py:377`）唯一入口是 CLI `cli/__init__.py:233`、`knowledge/worker.py:143`、`knowledge/ingestion.py:89`。报表查它是**查错库**，不是查错名。

**影响面**：前两项纯文档/报表指向错误；`issues` 为高——跨进程不持久、依赖检查（`issue.py:86-99`）仅进程内有效、`/api/issues` 恒空。
**修复风险**：文档改表名 **低**；`issues` 落库 **高**（新表 + `ensure_*_schema` 挂点 + `IssueManager` 改造 + handler 参数对齐）。
**需迁移**：文档两项 **否**（`knowledge_semantic_jobs` 开启知识特性后由幂等 `init_schema()` 自建）；`issues` **是**——需新增建表 + 迁移脚本，无历史数据可迁（现全为内存态）。

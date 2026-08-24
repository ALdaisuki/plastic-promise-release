# 包结构地图（plastic_promise/）

> 勘察基线：发行仓 plastic-promise-release；LOC = 非 \`__pycache__\` 的 .py 行数。

## 一、包清单

| 包 | LOC | 职责一句话 | 关键文件 | 对外核心入口 |
|---|---|---|---|---|
| core | 72,901 | 记忆中枢：上下文引擎、LanceDB 向量层、召回质量、节点治理、推理作业 | context_engine.py(9k), lancedb_generation.py(4.4k), node_governance.py(3k), inference_jobs.py(2.3k), recall_quality.py(1.9k) | ContextEngine / 治理与召回服务类 |
| collaboration | 30,536 | 服务器签发的协作运行时：work board、验收回执、租约绑定、上下文投影 | durable_runtime.py(4.5k), acceptance_receipt.py(2.5k), runtime_binding.py(2.3k) | work_register/review/accept、lease heartbeat |
| mcp | 27,858 | MCP 协议门面：server、工具注册表、推理网关、dashboard v2 | server.py(7.3k), tools/memory.py(3.7k), inference_gateway.py(2.6k) | MCP server + 全部 \`mcp__plastic-promise__*\` handler |
| deployment | 17,146 | 部署与迁移：endpoint 契约、容器产物、collab schema 迁移日志 | endpoint_contract.py(2.3k), container_artifacts.py(2.1k), collaboration_schema_migration.py(1.7k) | 迁移操作器 / 部署契约校验 |
| knowledge | 5,308 | 项目作用域知识库：仓储、语义检索、影子索引、ingestion | repository.py(1.7k), lancedb_shadow.py(0.9k), semantic.py(0.8k) | knowledge_search 后端 Repository |
| control_plane | 4,207 | 控制面状态存储与配置 schema、鉴权 | store.py(2.2k), config_schema.py(1.9k), auth.py | ControlPlane Store |
| local_inference_node | 4,201 | 本地推理节点：provider HTTP 适配、runtime、FastAPI app | adapters.py(1k), provider_http.py(1k), app.py | 节点 HTTP app (:19130-19132 面) |
| memory | 3,143 | soul_memory 持久记忆与写入管线 | soul_memory.py(1.8k), pipeline.py(1.3k) | SoulMemory / MemoryPipeline |
| skills | 3,529 | 技能会话生命周期、工具路由、官方工作流阶段 | tool_routing.py(1k), engine.py(0.8k) | SkillEngine / session trace |
| cron | 2,482 | 定时扫描器：调度健康、记忆衰减、耦合、架构、数据质量 | scan_scheduler_health.py, scan_memory_decay.py | 各 scan_* 任务函数 |
| launcher | 1,646 | 服务启动编排（launchd/进程拉起） | launcher 主模块 | 启动入口 |
| defense | 1,319 | 信任分防线管理（get/history/adjust） | defense 主模块 | Defense API |
| passive_memory | 3,930 | 被动注入/捕获适配层：coordinator、codex hook、语义管线 | coordinator.py(1.5k), codex_hook.py(1.5k) | auto_context_inject / after_invoke 管线 |
| growth | 919 | 成长/经验包增长逻辑 | growth 主模块 | — |
| reflection | 1,142 | LLM 反思生成（经验/优化/根因）、SCARF | reflection 主模块 | step_closure 反思段 |
| release_builder | 1,080 | 发行打包构建器（配合顶层 release_* 模块） | builder 主模块 | 构建入口 |
| client | 1,411 | core 的客户端封装 | client/* | Client 类 |
| loop | 499 | 自演化循环骨架 | loop 主模块 | Loop |
| cli | 371 | CLI 子命令薄封装 | cli/__init__.py | console_scripts 入口 |
| extensions | 807 | 插件扩展挂点 | extensions/* | 扩展注册 |
| principles | 24 | 原则清单占位（主体在数据侧） | __init__.py | — |

顶层散件：endpoint_roles.py(644)、release_manifest.py(538)、smart_extractor.py(283)、issue.py(220)、pack.py(156)、adaptive_retrieval.py(145) 等，合计约 2.8k LOC。仓库总计约 18.6 万行 Python。

## 二、依赖方向（import 粗粒度）

分层视图（下层不感知上层为理想态）：

- **基础层**：client→core；growth/reflection/principles/memory/launcher→core；extensions→defense；defense→{core,mcp}
- **中台层**：knowledge→{core,skills}；skills→{core,extensions,loop,mcp}；passive_memory→{core,knowledge,mcp,skills}；control_plane→core
- **协作层**：collaboration→{core,deployment}；deployment→collaboration（**双向耦合**）；cron→{core,defense,mcp,memory}
- **门面层**：mcp→几乎所有包（client/collaboration/control_plane/core/defense/deployment/extensions/knowledge/launcher/loop/memory/passive_memory/reflection/skills）
- **入口层**：cli→{deployment,knowledge,mcp}

异常点：
1. **core ↔ 下游反向依赖**：core 自己 import 了 control_plane/cron/defense/deployment/growth/loop/mcp/memory/passive_memory/reflection —— 基础包引用全部上层，是最大架构债。
2. **deployment ⇄ collaboration 双向 import**。
3. defense/mcp 被 core 引用，说明存在回调或类型借用绕行。

## 三、深度优化候选

1. **core 巨包拆分（72.9k）**：context_engine.py 单文件 9k 行应先拆（检索/排序/装配分文件）；lancedb_generation 与 recall_quality 可下沉为独立 storage/quality 子包，切断 core 对上层 10 个包的反向 import。
2. **collaboration 过重（30.5k）**：durable_runtime.py 4.5k 行聚合过多职责；建议按 work-board / receipt / lease-binding 三域拆包，并把 deployment 迁移逻辑从 collaboration_schema_migration 移回 deployment，消除双向依赖。
3. **mcp/server.py 7.3k 行工具注册表**：按工具族（memory/skill/work/dashboard）拆分为子路由模块，handler 注册改声明式清单。
4. **mcp→全量依赖收敛**：mcp 依赖 14 个包，经 inference_gateway 与 dashboard_v2 引入隐性耦合；dashboard repository(2.4k) 宜独立成接口包。
5. **小包合并**：principles(24)/loop(499)/cli(371)/extensions(807) 过碎，可并入相邻域（principles→数据侧、cli→release_builder），减少层级噪声。

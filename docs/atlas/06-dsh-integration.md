# 06 · Day-0 DSH 集成契约

> 目标：DSH（Harness）与记忆系统任一侧演进时，另一侧**当天适配、零滞后**。
> 本文件是双向变更的强制检查清单——改清单外的东西不需要走这里，改清单内的必须逐项过。

## 集成面地图（今日实测拓扑）

| # | 触点 | 方向 | 载体 | 今日证据 |
|---|---|---|---|---|
| 1 | 工具暴露面 | DSH→MCP | ~/.dsh/profiles/web/cordis.patch.yml 的 mcp-plastic-promise 客户端插件（:9020/mcp streamable-http） | 58 工具可达 |
| 2 | 周期注入 | DSH→记忆 | 同文件 pp-memory-cadence 插件（every:5，注入+捕获提醒） | 配置在案，生效待用量验证 |
| 3 | 派发遥测 | 记忆→自身 | server.py call_tool 遥测壳 → tool_usage_events（f086962） | 冒烟通过 |
| 4 | 进程环境 | 运维→双方 | launchd plist EnvironmentVariables（PP_MCP_RUNTIME_ACTOR=claude、PP_PASSIVE_SEMANTIC_CAPTURE=shadow 等） | bootout/bootstrap 后 ps eww 实测可见 |
| 5 | 会话生命周期 | DSH↔MCP | :9020 重启使 MCP session 失效 → 客户端需重新握手（session-init/memory_recall 重试即恢复） | 本日两次实测 |
| 6 | 权威身份 | 双方约定 | PP_MCP_RUNTIME_ACTOR + reviewer 集合 {claude,codex}；声明≠权威 | task_verify 授权链两次拦截验证 |
| 7 | 工作区指令 | DSH→Agent | CLAUDE.md / AGENTS.md 经 DSH 注入 agent 上下文 | 本会话实时生效 |

## Day-0 检查清单（双向变更必过）

### 当 DSH 升级/变更时
- [ ] cordis.patch.yml 插件 id 与新版兼容（pp-memory-cadence、mcp-plastic-promise 可加载）
- [ ] 重启后 memory_recall 冒烟通过（会话重握手验证）
- [ ] census 工具复跑：binding 计数与 mismatch=0 不回退
- [ ] tool_usage_events 表 schema 兼容（新列可空）
- [ ] CLAUDE.md/AGENTS.md 中 DSH 机制描述与新版一致

### 当记忆系统升级/变更时
- [ ] 新增 env 开关同步进 launchd plist 并走 bootout/bootstrap（kickstart 不重读配置——已知陷阱）
- [ ] 新增 handler 过 census：绑定状态入册（Tier 分级）
- [ ] SQLite schema 变更附带只读兼容（mode=ro 报表脚本不炸）
- [ ] GOAL.md 追加日期注记；重大语义变更同步 CLAUDE.md 对应节

## 反模式（今日实测教训）
- 双跳 ssh 引号会被 Windows 层吞——用 bash -s + stdin 脚本
- 后台代理超时可能留下半成品——接手前先 git status + diff
- git checkout -b 对已存在分支静默失败——提交前确认当前分支

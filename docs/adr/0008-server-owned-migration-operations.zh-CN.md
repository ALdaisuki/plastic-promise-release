# 将协同迁移保持为服务器拥有的操作

Plastic Promise 只有一个 canonical SQLite writer：`pp-server-backend`。因此
Deployment Center 浏览器界面及其 `ppctl` planning adapter 始终只读；现有跨平台 CLI
仍是 planner 和 operator entry point，而不是通用 command runner。协同 runtime migration
建模为一个 server-owned Migration Operation：它使用新的 Migration Operation Plan、显式
Execution Grant、类型化 runtime/node/derived-index adapter 与无 secret 的 Migration
Receipt。

## 考虑过的方案

- 为 `ppctl` 增加 browser 可调用的 apply command。
- 让 CLI 直接打开并迁移 canonical SQLite。
- 采用 server-owned orchestration seam 与类型化 adapter。

前两个方案会让 browser 或 local operator process 成为竞争性的 canonical writer，并混淆
planning evidence 与 execution authority。第三个方案保持 command surface 狭窄，并把
rehearsal、cutover、rollback、Maintenance 与 receipt persistence 集中到一个 deep module。

## 结果

- Deployment Center 的 `plan_hash` 永远只用于 inspection，不能被接收为 Execution Grant。
- runtime、node 和 derived-index adapter 只能执行固定 migration phase；它们绝不接收任意
  shell、Docker、SSH 或 SQLite command。
- live mutable phase-adapter wiring 仍需单独授权。source contract 和 fake-adapter test 不得宣称
  live migration、listener、tunnel、container 或 production cutover 已发生。
- production composition 必须使用 `SQLiteMigrationExecutionJournal`；其备份门控的版本化
  schema 会持久化 server-issued grant、installation-scoped lease、单调 fence、一次性
  operation state 和无 secret receipt。过期 running work 会进入 `recovery-required`；旧 owner
  在 fence 丢失后不能继续完成或回滚。
- in-memory journal 仅供测试与显式 non-production composition 使用。Deployment Center 与
  `ppctl` 继续保持 inspection-only。

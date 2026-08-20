# 联合六 PR 文档一致性标准

英文对等页：[`documentation-parity.md`](documentation-parity.md)。

> **规范绑定：**本文是机器可读
> [联合六 PR 合同](../../standards/union-six-pr-contract.json) revision `2026-08-18.1`
> 的派生投影，规范源原始字节 SHA-256 为
> `2c7e4a532e17cde229830479712aabe3ca36a13e21fafbe9fcf781cd91305722`。
> 发生分歧时以规范 JSON 为准。
> [派生文档清单](../../standards/union-six-pr-derived-documents.json)定义受跟踪的文档/资产族，
> [证据台账](../../standards/union-six-pr-evidence-ledger.json)定义证据状态。

## 联合规则

文档一致性是每个 PR 的阻塞门禁，不能统一推迟到 PR6 清理。只有每个
`delivery_scope`、`collaboration_scope` 与 `required_evidence` 条目均通过，PR 才算完成。
即使中英文彼此一致，只覆盖部署侧或只覆盖协作侧仍属于不完整。

只有交付范围、协作范围和所需证据全部通过，PR 才算完成；任一单侧完成都不等于 PR 完成。

文档证据只能证明其回执所声明的 documentation/test 层级，不能证明 listener、持久 runtime、
migration、restart、promotion、Maintenance transition、RC、stable publication 或生产验收。

## 单一来源策略

1. 规范范围变化必须修改 canonical JSON、递增 revision、保留上一 revision 的不可变谱系、
   重新生成双语规范视图，并在同一变更集中更新受治理投影。
2. README、roadmap、architecture、TODO 与 release 页面不得重新编写精确 PR requirement；
   它们必须链接规范源并只作不缩减、不替代、不重排的摘要。
3. 状态以类型化 evidence receipt 为准。使用“已实现、已测试、运行中、已部署、已推广、
   已发行、已完成”等词时，必须说明实际证明的 evidence class。
4. 手工 Markdown 中的 `pass` 不是 parity receipt。回执必须绑定不可变 source revision、
   diff digest、contract revision/hash、requirement 集合、changed-file 集合、check 与 UTC 时间。
5. 历史本地 receipt 只作为来源记录；source revision、diff、requirement 集合或联合合同
   revision 变化后不能沿用。

## 跟踪清单

派生文档 manifest 是 inventory 权威。每个受影响 PR 必须评估所有适用 family，包括：

- 中英文 Markdown 对；
- Mermaid 与 ASCII 架构图；
- 中英文 SVG 对；
- badge 及其目标链接；
- 内部相对链接与公开 canonical 链接；
- command、path、port、environment name、profile name、image name、model identity、schema
  revision 与 default；
- price、download size、disk、memory、GPU、concurrency、retention 等资源表。

`enforcement: tracked-drift` 是阻塞失败记录，不是豁免或通过状态。只有修复底层文档/资产，
并从已验证证据重新生成或修订 manifest，drift 才算解决。

## 必需的语义一致性

每个受影响的中英文文档对必须满足：

- topic 与 navigation 暴露等价行为和权威边界；
- default、failure mode、rollback、degradation 与 non-goal 一致；
- command、identifier、path、port、profile、model identity、schema revision、unit、date 与
  resource assumption 一致，除非记录明确平台差异；
- 架构图展示相同 ownership 与 data flow；
- SVG 对使用相同 topology、revision、事实、badge/link 集合与状态；
- 被删除的 feature 或 flag 同时从两种语言中消失；
- target、experimental、source-only、runtime、production 与 released 状态使用等价标签；
- manifest 要求时存在 canonical contract 链接与联合完成规则；
- source/test 声明绝不写成 runtime/production 声明。

## 协作与验收声明

描述 Project Working Set、awareness 或 accepted artifact 的文档必须保持 revision
`2026-08-18.1` 的以下边界：

- `project_for(*, audience: AgentSession, deltas: EventPage)` 是非权威值工厂，不是调用方认证；
- 调用方自报 coordinator/reviewer role 或自行构造 session 不授予 full-work 可见性；
- 可信 feed 必须绑定服务器认证的 active session、当前 policy、source kind/authority、event
  schema/log/factory revision、`cursor_from`/`cursor_to`、source-page/projection digest、
  generated-at UTC 与独立 `AcceptanceReceipt` 谱系；
- `completed + artifact_refs`、reviewer string 或未绑定的 `ResultReceipt` 不是 accepted work；
- peer progress、agreement、finding、semantic capture 与 submitted work 都不会自动成为
  canonical memory。

### PR 5 源码/测试状态（2026-08-17）

双语架构文档族必须把认证 fresh-client Hook continuation、公开有界 `ProjectWorkBoard`
lifecycle、Dashboard Agent topology/work-board/event-timeline projection，以及 Maintenance
协作 composition/lifecycle 表述为：**当前源码已实现 / 聚焦测试已通过 / 真实运行时与生产
证据待验证**。

同一组文档必须把服务器拥有的 `WorkReceipt` issuer、accepted-result-to-pending-only
自动编排、普通 tool-call reconcile，以及 `Stop` 的有界 progress/submitted 发射表述为：
**当前源码已实现 / 聚焦测试已通过 / 真实运行时证据待验证**。真实 browser smoke、认证 runtime/lifecycle E2E、真实 Maintenance
transition、部署/激活与 production 证据仍未验证。文档一致性不能把 source/test 证据提升为
runtime 或 production 证据。

部署、远程 Control、架构、Mermaid 与 ASCII 文档族还必须一致说明：generation 准备与
cutover 是两个阶段。准备阶段构建、reconcile 并验证 inactive candidate；cutover 要求独立
授权且已停止的 runtime；Control activation 与 retarget 只使用认证 CAS API；restart、
health/retrieval smoke 与 Maintenance transition 仍是独立宿主操作。任何文档都不得把直接
修复 Control SQLite、内嵌 restart flag 或自动启用 Maintenance 描述为 operator cutover 工具能力。

## 审查要求

每个正式 PR 都需要三条独立 review channel，并绑定同一不可变 source revision、diff digest、
requirement 集合与 contract revision：

1. Standards conformance；
2. Spec conformance；
3. DeepSec Shield 与代码坏味道审查。

DeepSec 仅允许 repository/diff/web read 与只读 MCP；不得拥有 shell、file、database、release
或 production write 权威，其 finding 绝不自动成为 canonical memory。

## 机器可读回执合同

权威 parity result 必须位于生成式 machine-readable receipt。中英文人类视图可以渲染同一份
receipt，但不得分别维护独立 counter 或 conclusion。

```json
{
  "schema": "plastic-promise/documentation-parity-receipt/v1",
  "contract": {
    "path": "docs/standards/union-six-pr-contract.json",
    "revision": "2026-08-18.1",
    "sha256": "2c7e4a532e17cde229830479712aabe3ca36a13e21fafbe9fcf781cd91305722"
  },
  "source_revision": "<immutable source revision>",
  "diff_sha256": "<sha256>",
  "requirement_ids": ["<affected PRn-Dxx/PRn-Cxx/PRn-Exx>"],
  "changed_files": ["<repository-relative path>"],
  "document_families": ["<manifest id>"],
  "checks": {
    "bilingual_markdown": "pass|fail",
    "diagrams_and_svg": "pass|fail",
    "badges_and_links": "pass|fail",
    "resource_and_pricing_tables": "pass|fail",
    "status_and_evidence_classes": "pass|fail",
    "canonical_contract_binding": "pass|fail"
  },
  "intentional_differences": [],
  "result": "pass|fail",
  "generated_at_utc": "<timezone-aware UTC timestamp>"
}
```

缺失 receipt、可变 source identity、过期 contract revision/hash、changed-file 集合不匹配、
中英文独立 counter、未解释 difference 或任一 check 失败时，门禁保持 not-evidenced 或 failed。

## 聚焦本地检查

优先使用仓库原生 verifier。该文档族的最低聚焦检查为：

```bash
python scripts/render_union_six_pr_contract.py
python scripts/verify_union_six_pr_contract.py --repo-root .
python -m pytest -q -o addopts='' tests/test_union_six_pr_contract.py
git diff --check
```

存在 previous canonical source 时，verifier 还必须传入
`--previous-contract <immutable-path>`，避免当前制品自洽掩盖“源码字节已变化但 revision 未变”。
只有 changed family 需要时才增加 asset、link、resource、pricing 或 render check。

## 门禁结果

本标准不会自行签发 `pass`。当前结果必须在上述检查为精确不可变 source 生成回执后，从
派生文档 manifest 与 evidence ledger 读取。任何 Markdown 编辑都不能单独完成 PR，也不能
证明 runtime/production 状态。

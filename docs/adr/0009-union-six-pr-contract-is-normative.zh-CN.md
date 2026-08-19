---
status: accepted
date: 2026-08-11
---

# ADR 0009：联合六 PR 合同是规范源

英文对等页：
[`0009-union-six-pr-contract-is-normative.md`](0009-union-six-pr-contract-is-normative.md)。

## 规范绑定

- 规范源：[`docs/standards/union-six-pr-contract.json`](../standards/union-six-pr-contract.json)
- Schema：`plastic-promise/union-six-pr-contract/v1`
- Revision：`2026-08-18.1`
- 规范源原始字节 SHA-256：
  `bc7b90b55bb2c14c5ff12a9c8b73448bf3e8142a23b777d95719d4e1a1c99f90`
- 上一 revision：`2026-08-11.1`，由规范源中的 `revision_lineage` 绑定

发生分歧时，规范 JSON 优先于本 ADR、生成式 Markdown、roadmap、architecture、TODO、PR
description、commit、test、receipt、deployed artifact 与历史对话。

## 背景

可组合部署计划与项目级多 Agent 协作计划曾分散在不同文档和对话中，导致只包含部署职责的
旧 PR 分配在协作职责、acceptance 安全、DeepSec review、证据层级与 Workflow Composer
治理加入后仍然残留，也容易把 source-level work 夸大为整个 PR、runtime 或 production 已完成。

稳定决策需要唯一机器可读范围、明确变更控制、类型化证据以及生成/验证式投影。

## 决策

Plastic Promise 将上述 canonical JSON 作为 PR1 到 PR6 依赖链的唯一规范合同。

只有交付范围、协作范围和所需证据全部通过，PR 才算完成；任一单侧完成都不等于 PR 完成。

1. 部署与协作是同六个 PR 内的两个强制范围，不是平行 roadmap 或可选插件。
2. 只有每个 `delivery_scope`、`collaboration_scope` 与 `required_evidence` 条目均通过所有
   适用完成门禁，PR 才算完成。
3. implementation、test、runtime 与 production 是独立 evidence class。任何措辞、聚合、
   reviewer 意见、merge 状态或 artifact reference 都不能让一个层级满足另一个层级。
4. Coordination、Project Working Set 与 Canonical Memory 保持分离。peer progress、agreement、
   finding、semantic capture 与 submitted work 都不会自动成为 canonical memory。
5. `AcceptanceReceipt` 是服务器认证、不可变、绑定 project/session 的决策，必须包含独立
   submitter/reviewer session，以及 WorkReceipt/ResultReceipt/evidence digest、policy revision、
   conflict state、source revision 与 UTC 签发时间。`completed + artifact_refs`、reviewer string
   或 self-attestation 都不充分。
6. 调用方自报 role、capability、project ID、manifest、model identity、audience、session、cursor
   page 或 result shape 都只是校验输入，绝不是授权。可信协作投影必须绑定服务器认证的
   session/policy/source/cursor/digest。
7. 每个 PR 都需要独立 Standards、Spec 与 DeepSec Shield/代码坏味道 review receipt，三者
   绑定同一不可变 source revision、diff digest、requirement 集合与联合合同 revision。DeepSec
   保持只读，其 finding 绝不自动进入 canonical memory。
8. Workflow Composer 只以 PR6 的 `shadow-only`、可观测、非权威行为存在。fixed route 始终是
   execution authority 与 rollback target。
9. 中英文文档、diagram、SVG、badge、link、resource table 与 pricing table 都是受治理投影，
   必须保持同步。

## 治理制品

- 规范双语视图由同一 JSON 原始字节生成：
  [`union-six-pr-contract.md`](../standards/union-six-pr-contract.md) 与
  [`union-six-pr-contract.zh-CN.md`](../standards/union-six-pr-contract.zh-CN.md)。
- [证据台账](../standards/union-six-pr-evidence-ledger.json)为每个 requirement ID 记录显式
  implementation/test/runtime/production 状态。
- [派生文档清单](../standards/union-six-pr-derived-documents.json)枚举关键双语文档与资产族；
  `tracked-drift` 是阻塞证据，不是豁免。
- 生成视图、ledger、manifest 与受治理 review receipt 都绑定精确 contract revision 与规范源
  原始字节 digest。

## 变更流程

任何规范修订必须在同一个经审查变更集中：

1. 修改 canonical JSON；
2. 递增 revision；
3. 保留上一 canonical source 与 digest 的不可变 lineage；
4. 重新生成双语规范视图与受治理投影；
5. 必要时更新 evidence ledger 与 derived-document manifest；
6. 通过 contract、revision、document、asset、review、DeepSec 与 evidence 门禁。

只修改生成式 Markdown、roadmap、ADR、PR description 或 status report 不能改变规范范围。
单次执行授权可以允许有界动作，但不会重写合同，也不隐含其他授权。

## 后果

- 状态汇报必须说明实际证明的 evidence class。
- 只完成 delivery 或只完成 collaboration 的切片不能称为整个 PR 已完成。
- 没有匹配回执时，source/test 工作不能描述成 live listener、persistent runtime、migration、
  restart、LanceDB promotion、Maintenance transition、RC、stable release 或 production acceptance。
- 派生文档可以摘要合同，但必须链接规范源，且不得静默缩减、替代或重排 requirement。
- revision 或 digest drift 必须 fail closed；当前制品自洽不能证明 revision 历史单调。

## 非决策

采纳本 ADR 不表示任何 implementation、test、runtime、production、release、publication、
migration、promotion、restart 或 Maintenance 动作已经完成；这些状态仍完全由 receipt 决定。

## 取代规则

只有后续双语 ADR 明确指出 replacement canonical source/revision，并通过 canonical amendment
procedure 获得采纳，才能取代本 ADR。未来通用 Delivery Program Contract 可以提供可复用执行
Module，但不能静默改变这个历史联合六 PR 实例。

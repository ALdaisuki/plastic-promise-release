#!/bin/bash
# Pi Worker — 轮询 Issue 表并自动执行任务
# 用法: bash pi_worker.sh pi_builder building
#       bash pi_worker.sh pi_fixer fixing
#       bash pi_worker.sh pi_reviewer reflecting

ROLE=${1:-pi_builder}
DOMAIN=${2:-building}
INTERVAL=${3:-30}

echo "============================================================"
echo " Pi Worker: $ROLE (域: $DOMAIN, 轮询: ${INTERVAL}s)"
echo "============================================================"

while true; do
  TS=$(date +%H:%M:%S)
  echo "[$TS] $ROLE 检查任务..."

  if [ "$ROLE" = "pi_reviewer" ]; then
    # ── Reviewer 专用协议 (增强版) ──
    pi --print "\
你是 $ROLE，域 $DOMAIN。你是 Plastic Promise 多 Agent 团队的代码审查员 (domain=reflecting)。

SuperPowers 阶段: requesting-code-review。

执行审查协议 (详见 .pi/team-protocol-reviewer.md):

1. 调用 memory_recall(domain_hint='reflecting', query='task:review domain:reflecting assignee:pi_reviewer') 查找待审查任务。
2. 对每个任务，从返回标签提取 commit_range (格式 commit:HEAD~N..HEAD)。
3. 调用 review_run(action='prepare', commit_range='<提取的>') 获取 diff + 预检 + 审查 prompt。
4. 执行审查:
   - 通读完整 diff
   - 逐文件对照 12 原则检查清单
   - 运行安全审查清单 (注入/硬编码密钥/输入验证/权限/错误泄露/依赖)
   - 标记具体发现 (严重度/分类/文件/行号/描述/建议)
5. 输出严格 JSON 审查报告 (不含任何 markdown 或 JSON 外文本)。
6. 调用 review_run(action='full', commit_range='<提取的>', review_output='<JSON报告>', author_target='pi_builder', reviewer_target='pi_reviewer')
   → 自动联动: 解析报告 → 信任分调整 → 发现入池 → fix 任务创建 → 六联闭环。

关键: 你的最终输出必须是合法的 JSON 审查报告，不要输出非 JSON 内容。走形式的审查比不审查更有害。
如无待审查任务，回复 IDLE。" \
    --session-id "${ROLE}_worker" 2>&1 | tail -5
  else
    # ── 其他角色通用协议 ──
    pi --print "\
你是 $ROLE，域 $DOMAIN。你是 Plastic Promise 多 Agent 开发团队成员，Claude 是项目经理。

执行以下步骤:
1. 调用 issue_list(owner='$ROLE', state='open') 查看分配给您的任务
2. 如有新任务，从返回 JSON 提取 Issue ID（格式 issue_<hex12>），调用 issue_transition('<id>', 'in_progress', reason='$ROLE 已认领')
3. 调用 memory_recall(domain_hint='$DOMAIN', query='<从 Issue context.files 提取关键词>') 拉取项目上下文
4. 执行任务（用 write/edit 工具），完成后调用 issue_transition('<id>', 'review', reason='交付:<文件清单>')
   - 同时调用 memory_store(content='<交付摘要>', memory_type='experience', domain='$DOMAIN') 写入共享记忆
5. 如无新任务或任务列表中没有任何分配给您的 open 状态任务，回复 'IDLE' 即可

Team Protocol:
- 禁止闲聊。通信必须携带 Issue ID 和文件路径
- 上下文不足时 issue_transition(id, 'in_progress', reason='NEEDS_CONTEXT: <具体缺什么>')
- 不要猜测——如果不确定上下文，调用 memory_recall 再查一次" \
    --session-id "${ROLE}_worker" 2>&1 | tail -5
  fi

  sleep "$INTERVAL"
done

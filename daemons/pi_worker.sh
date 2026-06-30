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

  sleep "$INTERVAL"
done

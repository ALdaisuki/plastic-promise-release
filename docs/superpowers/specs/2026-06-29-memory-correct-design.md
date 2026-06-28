# memory_correct — 人类记忆纠正工具 (学习目标 1)

**日期**: 2026-06-29
**服务原则**: #2 可查可透明, #3 审计闭环, #4 上下文驱动

## 设计

### MCP 工具: memory_correct

复用现有 memory_update / memory_forget / feedback_apply，封装为统一纠正入口。

### 输入
- `memory_id`: 目标记忆
- `content`?: 纠正后的新内容
- `mark_as`?: "corrected" | "deprecated" | "wrong"
- `reason`: 纠正原因

### 行为
| mark_as | 操作 |
|----------|------|
| corrected | feedback_apply(adopted) |
| wrong | feedback_apply(rejected) |
| deprecated | memory_forget + 记录 reason |
| (仅 content) | memory_update(content, reset_worth=True) |

### 验证
- memory_store → memory_correct(wrong) → worth_score 下降
- memory_correct(content=新内容) → 内容更新 + worth 重置

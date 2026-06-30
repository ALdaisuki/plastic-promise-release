# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

Plastic Promise 处理敏感数据（记忆、审计日志、Agent 信任分）。如果你发现安全漏洞，请：

1. **不要公开披露**。通过 GitHub Security Advisory 或邮件私密报告。
2. 提供复现步骤、受影响版本、潜在影响。
3. 我们会在 48 小时内确认，7 天内发布修复。

## Security Design

### 数据隔离
- 记忆池按 scope 隔离，多 Agent 场景不会交叉污染
- 信任分和审计日志存储于独立数据库表

### 输入验证
- 所有 MCP 工具参数通过 JSON Schema 验证
- 记忆内容经噪声过滤器（`noise_filter.is_noise()`）检测
- SQL 注入防护：所有查询通过参数化语句

### 防线层级
- **L0 硬边界**：绝对不可逾越的规则，pre_check 拦截
- **L1 约束衰减**：信任分驱动，高分放宽/低分收紧
- **L2 免疫巡检**：24 小时周期扫描，自动修复

### 审计追溯
- 每步操作生成 audit trail（工具名、时间戳、参数摘要）
- 关键决策有完整 git 痕迹
- 审计日志写入 `step_audit_log.jsonl`（gitignore 排除）

## Best Practices for Users

- 不要将 `.env` 文件提交到版本控制（已在 `.gitignore` 中排除）
- 定期运行 `audit_run(action="full")` 检查系统健康
- 信任分低于 0.30 时，Agent 操作需人工审批
- 使用 `pack_export` 定期备份记忆
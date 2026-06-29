# Autonomous Pipeline — Daemon 多角色 + 流水线自治 + 信任执行

> 状态: 已确认 | 日期: 2026-06-29 | 改动: single file (pi_daemon.py)

## 一、目标

将三个手动修复合并为自治流水线——Daemon 同时盯多角色，流水线阶段自动衔接，信任分控制执行权限。

**原则映射：** #1 奥卡姆剃刀（一个文件）、#7 器官互保（上游完成→下游自动触发）、#9 信任驱动（信任分控制 spawn）。

## 二、单一 Daemon，多角色扫描

```python
# 角色注册表 — 一个 Daemon 管所有
AGENT_ROLES = {
    "pi_builder":  {"domain": "building",   "trigger": ["task:pending", "task:spec"],       "output": "task:active"},
    "pi_fixer":    {"domain": "fixing",     "trigger": ["task:rejected"],                  "output": "task:fixed"},
    "pi_reviewer": {"domain": "reflecting", "trigger": ["task:active"],                    "output": "task:review"},
}

def get_pending_task():
    """扫描所有角色，返回 (role, content, task_id) 或 None"""
    for role, cfg in AGENT_ROLES.items():
        task = sqlite_get_task(role, cfg["trigger"])
        if task:
            return role, cfg, task
    return None
```

## 三、流水线自动衔接

```
Builder 完成 → mark task:active
               → /notify broadcast: {type:"tag_transition", to_tag:"task:active"}
               → Reviewer 扫描到 task:active → 自动 spawn → 审查

Reviewer 完成 → mark task:review  
               → Claude 验收 → task:reviewed 或 task:rejected
               → task:rejected → Fixer 自动 spawn → 修复

Claude 唯一手动操作: 最终验收 (task:reviewed / task:rejected)
其余全部自动——Daemon 根据标签阶段自动触发下一角色。
```

## 四、信任分控制执行权限

```python
from plastic_promise.core.issue_validator import get_tier, check_permission

def can_execute(role: str, action: str = "write_file") -> bool:
    """读取 Agent 信任分，检查是否有权执行。"""
    trust = sqlite_get_trust(role)  # 从 defense 表读
    tier = get_tier(trust)
    return check_permission(tier, action) != "denied"
```

在 spawn Pi 之前：
```python
if not can_execute(role):
    print(f"BLOCKED: {role} trust too low for {action}")
    return  # 跳过，等待信任分恢复
```

## 五、改动面

| 改动 | 行数 |
|------|------|
| AGENT_ROLES 注册表 | 8 |
| get_pending_task 多角色扫描 | 10 |
| can_execute 信任检查 | 8 |
| main loop 角色调度 | 5 |
| **总计** | **~30** |

## 六、启动

```bash
# 一个终端替代三个:
python pi_daemon.py
# 自动管理 Builder/Fixer/Reviewer 三个角色的任务
```

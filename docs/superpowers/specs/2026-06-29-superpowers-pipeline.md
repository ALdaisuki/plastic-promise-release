# SuperPowers Pipeline — 多 Agent 标准化流程

> 状态: 已确认 | 日期: 2026-06-29

## 一、核心设计

SuperPowers 标准化流程 (brainstorming→spec→plans→execute→review→acceptance) 映射到多 Agent 标签状态机。同一个 Pi 进程通过 `--mode` 参数扮演不同角色。

**设计约束：零新代码——纯配置 + prompt + 标签。**

## 二、阶段映射

| SuperPowers | 现有映射 | Tag | 谁做 | Mode | 输出物 |
|------------|---------|-----|------|------|--------|
| Brainstorming | 内存对话 + memory_store | 无 | Claude | - | 需求澄清 |
| Spec | designing 域 | `task:spec` | Claude (未来 Pi-Designer) | - | 技术规格 |
| Writing Plans | building 域 | `task:plan` | Pi-Planner | `planner` | 原子任务列表 |
| Execute | building/fixing | `task:active` | Pi-Builder/Fixer | `builder`/`fixer` | 代码/修复 |
| Review | reflecting 域 | `task:review` | Pi-Reviewer | `reviewer` | 审查报告 |
| Acceptance | governing 域 | `task:reviewed` | Claude | - | 验收/打回 |

**生命周期：brainstorming → spec → plan → active → review → reviewed（或 rejected → fixed → review 循环）**

## 三、Pi Mode 语义

| Mode | 输入查询 | 输出标签 | 域 |
|------|---------|---------|-----|
| `planner` | `tag:spec domain:designing` | `task:plan` | building |
| `builder` | `tag:plan domain:building` | `task:active` | building |
| `fixer` | `tag:rejected domain:fixing` | `task:fixed` | fixing |
| `reviewer` | `tag:active domain:building` | `task:review` | reflecting |

## 四、pi_worker.ps1 mode 参数

```powershell
param(
    [string]$mode = "builder",
    [int]$interval = 30
)

$modeMap = @{
    "planner"  = @{ role="pi_planner";  domain="designing";  query="tag:spec domain:designing" }
    "builder"  = @{ role="pi_builder";  domain="building";   query="tag:plan domain:building" }
    "fixer"    = @{ role="pi_fixer";    domain="fixing";     query="tag:rejected domain:fixing" }
    "reviewer" = @{ role="pi_reviewer"; domain="reflecting"; query="tag:active domain:building" }
}
```

## 五、示例：JWT 登录模块全流程

```
1. Claude brainstorming → memory_store("JWT 登录需求", tags=[])
2. Claude spec        → memory_store("规格: auth/jwt.py, login(email, password)", tags=["task:spec","assignee:pi_builder","domain:designing"])
3. Pi-Planner          → 读取 spec → memory_store("Task 1: create auth/jwt.py\nTask 2: login()\nTask 3: tests", tags=["task:plan","assignee:pi_builder","domain:building"])
4. Pi-Builder          → 执行 → auth/jwt.py + memory_store("done", tags=["task:active","owner:pi_builder","domain:building"])
5. Pi-Reviewer         → 审查 → memory_store("pass, add type hint at :45", tags=["task:review","domain:reflecting"])
6. Claude acceptance   → memory_store("accepted", tags=["task:reviewed","reviewer:claude","domain:governing"])
                       → defense(adjust, +0.02, target="pi_builder")
```

## 六、启动方式

```powershell
# Planner + Builder + Reviewer 并行
.\pi_worker.ps1 -mode planner
.\pi_worker.ps1 -mode builder
.\pi_worker.ps1 -mode reviewer

# Fixer 按需
.\pi_worker.ps1 -mode fixer
```

## 七、未来扩展

```powershell
# Pi-Designer (选择 2 接口已预留)
$modeMap["designer"] = @{ role="pi_designer"; domain="designing"; query="tag:brainstorm domain:designing" }
```

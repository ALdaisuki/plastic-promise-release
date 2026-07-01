# One-Click Launcher — 一键初始化与启动

> 状态: 设计完成 | 日期: 2026-07-02 | 作者: claude

## 一、目标

为 Plastic Promise 记忆系统提供一键初始化和启动入口，替代当前需要手动分别启 MCP Server + Daemon 的多终端操作。要求：
- 全组件一键启动
- 智能环境检测（首次运行自动 bootstrap）
- 崩溃自愈（子进程死亡自动重启）
- 用户关闭即全停（优雅关闭，不留孤儿进程）

## 二、文件结构

```
scripts/
  init_and_start.py              # 一键启动入口（thin wrapper）
plastic_promise/
  launcher/
    __init__.py
    watchdog.py                  # Watchdog 主循环 + 信号处理
    service_manager.py           # ServiceManager — 启动/停止/重启编排
    service_definition.py        # ServiceDefinition + RestartPolicy dataclass
    env_checker.py               # 环境预检 (Python, Ollama, LanceDB, port, DB)
    bootstrap_checker.py         # 首次运行检测 + 自动 bootstrap
```

入口调用方式：
```bash
# 一键启动（默认）
python scripts/init_and_start.py

# 跳过 Ollama 检查（降级运行——无 LLM 分类，但核心记忆功能可用）
python scripts/init_and_start.py --skip-ollama-check

# 仅检查环境，不启动
python scripts/init_and_start.py --check-only

# 优雅停止所有服务
python scripts/init_and_start.py --stop
```

## 三、核心数据类型

### 3.1 RestartPolicy

```python
@dataclass
class RestartPolicy:
    max_retries: int = 5         # 窗口内最大连续重启次数
    window_seconds: float = 60.0 # 滚动窗口（秒）
    backoff_base: float = 1.0    # 初始退避（秒）
    backoff_multiplier: float = 2.0  # 指数退避乘数
    max_backoff: float = 30.0    # 退避上限（秒）
```

**unrecoverable 规则**: 窗口内连续重启次数超过 `max_retries` → 标记为 `unrecoverable`，停止重启，控制台红色告警，日志写入 `init_and_start.log`，提示用户手动介入。

### 3.2 ServiceDefinition

```python
@dataclass
class ServiceDefinition:
    name: str                         # "mcp-server"
    command: list[str]                # ["python", "-m", "plastic_promise", "--sse", "9020"]
    health_url: str | None            # "http://127.0.0.1:9020/health"
    startup_timeout: float = 30.0     # 启动等待超时（秒）
    health_check_interval: float = 5.0  # 健康检查周期（秒）
    depends_on: list[str] = field(default_factory=list)  # 依赖的服务名称列表
    pre_start: list[str] = field(default_factory=list)   # 启动前执行的命令
    restart_policy: RestartPolicy = field(default_factory=RestartPolicy)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = "."                    # 工作目录（相对于项目根）
```

### 3.3 ServiceStatus (运行时状态)

```python
class ServiceStatus(Enum):
    PENDING = "pending"           # 等待依赖就绪
    STARTING = "starting"         # 进程已启动，等待健康检查
    HEALTHY = "healthy"           # 健康检查通过
    FAILED = "failed"             # 启动失败或健康检查失败
    UNRECOVERABLE = "unrecoverable"  # 窗口内耗尽重启次数
    STOPPED = "stopped"           # 已主动停止
```

## 四、组件设计

### 4.1 env_checker.py — 环境预检

检查项（按顺序，首次失败即停止）：

| 检查项 | 方法 | 失败动作 |
|--------|------|---------|
| Python >= 3.10 | `sys.version_info` | 报错退出 |
| Ollama 可用 | `GET http://127.0.0.1:11434` (3s timeout) | 报错退出 (或 `--skip-ollama-check` 时 warn) |
| LanceDB 可导入 | `import lancedb` | 报错退出 |
| 端口 9020 空闲 | `socket.bind(127.0.0.1, 9020)` | 报错退出（提示已有实例运行） |
| plastic_memory.db | `os.path.exists(DB_PATH)` | 仅 warn（首次运行无 DB 正常） |

Ollama 视为**环境依赖**，不纳入看门狗托管——看门狗不管理 Ollama 进程的启停。

### 4.2 bootstrap_checker.py — 首次运行检测

检测逻辑：
1. 如果 `plastic_memory.db` 不存在 → 需要 bootstrap
2. 如果 DB 存在但 `SELECT COUNT(*) FROM memories WHERE tags LIKE '%seed:true%'` = 0 → 需要 bootstrap
3. 否则 → 跳过

Bootstrap 执行：
```bash
python scripts/bootstrap.py
```
输出写入 `init_and_start.log`。失败则报错退出，提示用户检查环境。

### 4.3 service_manager.py — 服务生命周期管理

#### 启动流程

```
start_all():
  for svc in dependency_order:
    1. 执行 svc.pre_start（如有，阻塞等待完成）
    2. subprocess.Popen(svc.command, cwd=svc.cwd, env={**os.environ, **svc.env})
    3. 循环发送健康检查（间隔 0.5s），持续整个 startup_timeout 窗口
    4. 窗口内任意一次检查通过 → HEALTHY，立即继续下一个服务
    5. 整个 startup_timeout 窗口耗尽仍未通过:
       - process.terminate() (等 5s grace period)
       - 仍未退出 → process.kill() (强制)
       - 标记 FAILED → 触发 RestartPolicy
    6. 依赖项启动失败的后续服务: PENDING → FAILED（级联失败）

#### 依赖恢复通知

当某个服务通过 reset_service(name) 手动重置后（如从 UNRECOVERABLE 恢复），
ServiceManager 遍历所有服务，对 depends_on 包含该服务的条目自动触发启动:

```
reset_service(name):
  svc = find(name)
  svc.status = STOPPED
  svc.restart_history.clear()
  # 级联恢复依赖方
  for dependent in find_dependents(name):
    if dependent.status == FAILED:
      start_service(dependent)  # 重新尝试启动
```

依赖方启动失败不影响已恢复的服务。

#### 停止流程

```
stop_all():
  for svc in reversed(dependency_order):   # 反向：先停 daemon，再停 mcp-server
    1. process.terminate() (SIGTERM / CTRL_BREAK_EVENT on Windows)
    2. 等待 5s
    3. 未退出 → process.kill()
    4. 标记 STOPPED
```

### 4.4 watchdog.py — 看门狗主循环

```
watchdog_loop():
  while not shutdown_flag:
    for svc in running_services:
      if svc.status in [HEALTHY, STARTING]:
        ok = health_check(svc)   # GET health_url, timeout=3s
        if not ok:
          svc.consecutive_failures += 1
          if svc.consecutive_failures >= 3:  # 需要连续 3 次失败才确认
            handle_crash(svc)
        else:
          svc.consecutive_failures = 0

    sleep(1.0)  # 主循环粒度 1 秒
```

#### 崩溃处理

```
handle_crash(svc):
  1. 终止残留进程（terminate → kill 升级）
  2. 检查 RestartPolicy:
     recent_restarts = count_restarts_in_window(svc, window_seconds)
     if recent_restarts >= max_retries:
       svc.status = UNRECOVERABLE
       log_error(f"{svc.name} unrecoverable: {recent_restarts} restarts in {window_seconds}s")
       trigger_alert(svc)  # 控制台红色输出 + 日志
       return
  3. backoff = min(backoff_base * (backoff_multiplier ** recent_restarts), max_backoff)
  4. sleep(backoff)
  5. start_service(svc)  # 重新启动
```

#### 信号处理

```python
# SIGINT (Ctrl+C) / SIGTERM
def handle_shutdown(signum, frame):
    shutdown_flag = True
    print("[launcher] Shutting down...")
    stop_all()        # 优雅停止所有子进程
    cleanup_pidfiles()
    print("[launcher] All services stopped. Goodbye.")
    sys.exit(0)
```

Windows 上使用 `signal.signal(signal.SIGINT, ...)` 和 `signal.signal(signal.SIGBREAK, ...)`。

### 4.5 服务定义清单

```python
SERVICES = [
    ServiceDefinition(
        name="mcp-server",
        command=[sys.executable, "-m", "plastic_promise", "--sse", "9020"],
        health_url="http://127.0.0.1:9020/health",
        startup_timeout=15.0,
        health_check_interval=5.0,
        depends_on=[],
        pre_start=[],
        restart_policy=RestartPolicy(max_retries=5, window_seconds=60.0),
    ),
    ServiceDefinition(
        name="maintenance-daemon",
        command=[sys.executable, "daemons/maintenance_daemon.py"],
        health_url=None,   # daemon 无 HTTP health endpoint，通过 PID + 心跳文件检测
        startup_timeout=10.0,
        health_check_interval=10.0,  # daemon 低频检查即可
        depends_on=["mcp-server"],
        pre_start=[],
        restart_policy=RestartPolicy(max_retries=5, window_seconds=120.0),
    ),
]
```

**daemon 健康检测方式**: 因为没有 HTTP endpoint，通过心跳文件判断存活。

**前置条件**: 在 `maintenance_daemon.py` 主循环中添加心跳写操作（实施任务之一）：
```python
# 在主循环中，每 10s tick 时更新心跳文件
_heartbeat_path = os.path.join(_project_root, "maintenance_daemon.heartbeat")
with open(_heartbeat_path, "w") as f:
    f.write(datetime.now().isoformat())
```

**launcher 端检测**（按优先级降级）：
1. PID 文件 `maintenance_daemon.pid` 存在且进程存活
2. 心跳文件 `maintenance_daemon.heartbeat` 最近 2 分钟内更新过（检测僵死）
3. 两者都失败 → 判定为不健康

进程存活检测: `psutil.pid_exists(pid)` 优先，不可用时降级到 `subprocess.run(["tasklist", "/FI", ...])` (Windows) 或 `os.kill(pid, 0)` (Unix)。

## 五、控制台输出规范

```
╔══════════════════════════════════════════════════════════════╗
║  Plastic Promise — One-Click Launcher v0.1.0               ║
╚══════════════════════════════════════════════════════════════╝

[ENV]   Python 3.12.0 ................ ✅ OK
[ENV]   Ollama (127.0.0.1:11434) ..... ✅ OK
[ENV]   LanceDB ....................... ✅ OK
[ENV]   Port 9020 ..................... ✅ free
[ENV]   plastic_memory.db ............. ⚠️ not found (first run)

[INIT]  Bootstrap ..................... 🔄 running...
[INIT]  Bootstrap ..................... ✅ done (18 seed memories)

[START] mcp-server .................... 🔄 starting...
[START] mcp-server .................... ✅ healthy (pid=12345, port=9020)
[START] maintenance-daemon ............ 🔄 starting...
[START] maintenance-daemon ............ ✅ healthy (pid=12346)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  All services running. Dashboard: http://127.0.0.1:9020/dashboard
  Press Ctrl+C to stop all services.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[WATCH] 10:30:15  all healthy | mcp-server pid=12345 daemon pid=12346
[WATCH] 10:35:15  all healthy | mcp-server pid=12345 daemon pid=12346
[RESTART] 10:37:02 maintenance-daemon crashed — restarting (attempt 1/5, backoff 1.0s)
[START] maintenance-daemon ............. ✅ healthy (pid=12890)
[ALERT] 10:42:00 maintenance-daemon UNRECOVERABLE — 5 restarts in 60s, manual intervention needed
```

## 六、错误处理矩阵

| 场景 | 行为 |
|------|------|
| Ollama 不可用 | 报错退出（除非 `--skip-ollama-check`） |
| Port 9020 被占用 | 报错退出（提示已有实例运行或手动释放端口） |
| Bootstrap 失败 | 报错退出 |
| MCP Server 启动超时 | terminate → kill → 重试（最多 5 次/60s 窗口） |
| Daemon 启动超时 | terminate → kill → 重试（最多 5 次/120s 窗口） |
| 运行时 MCP Server 崩溃 | 自动重启（含退避） |
| 运行时 Daemon 崩溃 | 自动重启（含退避） |
| 窗口内耗尽重试 | UNRECOVERABLE → 告警，其他服务继续运行 |
| 所有服务 UNRECOVERABLE | 看门狗退出，exit code 1 |
| 用户 Ctrl+C | 优雅关闭所有子进程，清理 PID 文件 |
| 看门狗自身崩溃 | 子进程可能成为孤儿——通过 PID 文件检测 + `--stop` 清理 |
| 子进程僵死（进程存活但无响应） | 连续 3 次健康检查失败 → 视为崩溃 |

## 七、日志

所有输出同时写入 `init_and_start.log`（项目根目录），格式：
```
[2026-07-02T10:30:15] [WATCH] all healthy | mcp-server pid=12345 daemon pid=12346
[2026-07-02T10:37:02] [RESTART] maintenance-daemon crashed (exit_code=1) — restarting (1/5)
[2026-07-02T10:37:05] [START] maintenance-daemon healthy (pid=12890)
```

## 八、不做什么（YAGNI / Phase 2 候选人）

- **不注册系统服务**（Windows Service / systemd）— Phase 2 考虑
- **不提供 CLI 控制面板** — 通过已有 `/dashboard` + `/health` HTTP API 查看状态
- **不管理 Ollama 进程** — 视为环境依赖
- **不启动 Bridge 服务**（event-bus / neko-adapter）— 它们属于 interop 子系统，不在记忆系统核心范围内。`scripts/start-all.bat` 已覆盖

### Phase 2 候选（已记录，本次不实施）

| 候选 | 说明 |
|------|------|
| 服务定义 YAML 化 | 将 `SERVICES` 列表移到 `scripts/services.yaml`，便于用户自定义新增服务 |
| 看门狗心跳文件 | 看门狗自身每 5s 写心跳，启动时检测孤儿子进程并提示 `--stop` 清理 |
| 自定义健康检查脚本 | `health_check_cmd: list[str]` 字段，支持脚本返回码判断健康（替代 HTTP） |

## 九、实施顺序

1. `service_definition.py` — 纯数据类，无依赖
2. `env_checker.py` — 环境预检，依赖 service_definition (Ollama check config)
3. `bootstrap_checker.py` — 首次运行检测
4. `service_manager.py` — 服务生命周期，依赖 service_definition
5. `watchdog.py` — 主循环 + 信号处理
6. `scripts/init_and_start.py` — 入口，组装所有组件

## 十、测试策略

- `env_checker` 单元测试：mock HTTP/lanceDB/port 的各种状态
- `bootstrap_checker` 单元测试：空 DB、有 DB 无种子、正常 DB 三种场景
- `service_manager` 集成测试：启动→健康检查→停止 完整链路
- `watchdog` 集成测试：模拟子进程崩溃，验证自动重启和 unrecoverable 逻辑
- 手动 E2E：实际运行 `python scripts/init_and_start.py`，确认仪表盘可访问

# 运行时拓扑全景（Runtime Topology Atlas）

> 勘察基线：server v0.2.16 @ fce7269（runtime-checkout）· 发行仓 main 已到 1b15d0f

## 1. Mac 本机（launchd：org.plastic-promise.*）

| 服务 | PID | 职责 |
|---|---|---|
| mac-canonical-runtime | 13901 | canonical 权威/MCP Server，run_canonical_runtime.py 监听 **127.0.0.1:9020**；KeepAlive+RunAtLoad。health：status=ok、identity_valid=true、bm25/graph ready；**degraded=true**（vector_ready=false，embedding 探测失败——GPU 容器今日已停）；fusion_attestation：requested/effective runtime 均为 **python**（runtime_forced:python），rust_runtime=null |
| mac-control-plane | 29162 | 控制面 run_control_plane.py（deployment.json / private-node-endpoints / control.sqlite3 运行时面） |
| mac-maintenance-daemon | 71289 | 维护守护；直接跑发行仓 .venv 的 daemons/maintenance_daemon.py（唯一绑定发行仓工作区的常驻进程） |
| node-tunnel | 24468（上次退出码 **255**） | SSH 转发 127.0.0.1:29130 → 192.168.5.6:19130（专用密钥 id_ed25519_plastic_promise，ServerAlive 保活）；255 + 直连 ssh 超时 ⇒ 计算节点链路当前中断/重启中 |
| mac-loopback-forward | 38691 | 本地环回转发辅助 local_loopback_forward.py |
| codex-hook-cleanup | 无（触发式） | passive_memory.codex_hook --cleanup-states 被动记忆钩子清理 |
| （mcp-tunnel） | 未加载 | plist 存在但不在 launchctl 列表：预留 19020→9020、19040→9040 至公网 plastic@8.133.197.86 |

**部署方式**：MCP Server 不直接跑发行仓，而从 ~/.local/share/plastic-promise/mac-server/runtime-checkout 运行；该 checkout 由发行管线跟随 main 发布（当前 fce7269，落后 main 一个提交）。运行模式为 **Python 运行时**（runtime-venv/bin/python），rust-full 为候选档位，attestation 显示 runtime 被 config 强制为 python。

**DSH Web App :3080**：宿主为 node 进程（PID 11653，DSH harness web shell）；上层宿主环境是 ChatGPT.app 内嵌 Codex Framework（codex app-server 托管形态）。

## 2. 计算节点 WSL（ssh ALdai@192.168.5.6 → wsl -d Ubuntu-22.04）

- **local-inference-node** — governed API **:19130**（经 node-tunnel 映射为 Mac 侧 :29130）；节点身份 windows-5080-llamacpp-4b-v2（RTX 5080，max_concurrency=1）
- **pp-llama-embedding / pp-llama-rerank** — 今日人为停止、让渡显存；这是 health 中 embedding probe failed / vector_ready=false 的直接原因
- **pp-docker-tcp-bridge** — Docker TCP 桥接容器；**mihomo** — 代理容器
- ⚠ 本次勘察两次直连 ssh :22 超时，容器现状以任务给定快照为准；node-tunnel 退出码 255 与之一致

## 3. 边缘（pp-local-edge :19021）

**未部署**：本机 curl :19021 连接失败，Mac 亦无 Docker。发行镜像已发布 **v0.2.17**（见 CHANGELOG.md），待一键部署。

## 4. 数据流箭头图

```
┌────────────────────── Mac ──────────────────────┐        ┌──── WSL (Ubuntu-22.04, RTX 5080) ────┐
│  Codex app-server ──► DSH Web :3080             │        │  [pp-docker-tcp-bridge]   [mihomo]   │
│        │ MCP 调用                                │        │        │                             │
│        ▼                                        │        │        ▼                             │
│  mac-canonical-runtime                          │  ssh   │  local-inference-node                │
│    MCP Server :9020 ◄─ launchd KeepAlive        │  ═✗═►  │    governed API :19130               │
│        │                                        │ node-  │        │                             │
│        ├─ SQLite（控制面 control.sqlite3）       │ tunnel │        ├─ pp-llama-embedding  [停]    │
│        ├─ LanceDB（vector_ready=false 待GPU）    │ :29130 │        └─ pp-llama-rerank     [停]    │
│        │                                        │        │                                      │
│        └─► 127.0.0.1:29130 ─(期望转发:19130)────┼──────► │  GPU 显存已让渡（embedding/rerank停） │
│  maintenance-daemon ──► 巡检/回收                │        │                                      │
│  pp-local-edge :19021 ✗ 未部署（镜像 v0.2.17）  │        │                                      │
└─────────────────────────────────────────────────┘        └──────────────────────────────────────┘

正常路径: agent → :9020 → 向量检索 → :29130/:19130 → GPU 容器
当前实况: agent → :9020 → text-only 降级（BM25+图谱可用，向量不可用）
```

## 深度优化候选

1. **llama 容器按需启停脚本化**：embedding/rerank 手工停启导致 server 长期 degraded；提供 `pp-gpu on/off` 一键脚本 + server 侧探测自动恢复，按显存需求秒级切换。
2. **watchdog 对 compute node 的覆盖缺口**：node-tunnel 退出码 255 仅被 launchd 盲重启，无端到端健康闭环；应让 watchdog/maintenance-daemon 定期探 :19130，联动隧道重建与告警。
3. **edge 部署条件**：镜像 v0.2.17 已就绪，阻塞点是 Mac 无 Docker；条件＝「任一常驻 Docker 宿主（WSL/NAS）+ :19021 环回暴露」，建议由 release_pipeline edge 子命令做自动验收。
4. **runtime-checkout 滞后 main 自动化**：checkout 停在 fce7269 而 main 到 1b15d0f，发布后缺自动滚动；可在 publish 后钩子触发 launchd 重载对齐。

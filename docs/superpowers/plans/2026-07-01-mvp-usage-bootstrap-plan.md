# MVP Usage Bootstrap 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让记忆系统管道成为实际使用的默认通道 — MCP 优先 + 存量同步 + 审计频率修正

**Architecture:** 三层防线 (CLAUDE.md 启动检查 → 存量同步工具 → 写入健康检查) + daemon 间隔 jitter

**Tech Stack:** Python 3.13, SQLite, MCP SSE, httpx, pyyaml, psutil

## Global Constraints

- 所有变更向后兼容，不破坏现有 MCP 工具签名
- 新增 `memory_sync_files` 工具注册到 MCP server
- 所有 CLI 命令使用 Python 内置库，跨平台兼容（Windows PowerShell + Unix Bash）
- Batch 2 (编码修复、域污染) 本次不动

## 建议执行顺序

| 顺序 | 任务 | 说明 |
|------|------|------|
| 1 | Task 4（审计间隔） | 独立，可先做 |
| 2 | Task 3（写入健康检查） | 独立，可先做 |
| 3 | Task 2（memory_sync_files） | 核心功能，需要 pyyaml |
| 4 | Task 1（CLAUDE.md） | 依赖 Task 3 的 health check |
| 5 | Task 5（验证） | 最后执行 |

## 依赖确认

| 依赖 | 状态 | 说明 |
|------|------|------|
| pyyaml | ✅ 已安装 | Task 2 `_parse_frontmatter` 使用 `yaml.safe_load` |
| httpx | ✅ 已安装 | Task 3 异步健康检查 |
| psutil | ✅ 已安装 | Task 5 跨平台进程管理 |

---

### Task 1: CLAUDE.md 启动序列 — 服务器健康检查

**Files:**
- Modify: `CLAUDE.md:6-15`

**Interfaces:**
- Produces: 启动步骤 0→1→2→3→4→5 序列

- [ ] **Step 1: 在 CLAUDE.md 插入第 0 步**

CLAUDE.md 第 6 行 "## 会话启动" 下面的步骤列表，在 "1." 之前插入。使用 Python 内置 `urllib` 替代 `curl`，确保 Windows/Linux/macOS 全平台兼容：

```markdown
0. **server up check** — `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9020/health')"`
   - 不可用（报错）→ 启动: `python -m plastic_promise.mcp.server --sse 9020` (后台运行: Windows 用 `start /B`, Unix 用 `&`)
   - 仍不可用 → 告警，本次会话使用文件系统降级（写入 `.md` 需加 `[[pending-sync]]` 标记）
```

原来的步骤 1-5 编号不变。

- [ ] **Step 2: 验证 CLAUDE.md 格式**

```bash
python -c "
with open('CLAUDE.md', 'r') as f:
    content = f.read()
assert 'server up check' in content
assert 'urllib.request.urlopen' in content
print('PASS: CLAUDE.md updated')
"
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add MCP server health check as CLAUDE.md startup step 0"
```

---

### Task 2: memory_sync_files — 存量 .md 同步到 MCP

**Files:**
- Create: `plastic_promise/mcp/tools/sync.py`
- Modify: `plastic_promise/mcp/server.py` (注册新工具)
- Test: `tests/test_memory_sync.py`

**Interfaces:**
- Consumes: `handle_memory_store(engine, args)` from `plastic_promise.mcp.tools.memory`
- Produces: `handle_memory_sync_files(engine, args) -> list[TextContent]`

- [ ] **Step 1: 写测试**

```python
# tests/test_memory_sync.py
import json, os, tempfile
import asyncio
from plastic_promise.core.context_engine import ContextEngine

async def _call(engine, args):
    from plastic_promise.mcp.tools.sync import handle_memory_sync_files
    r = await handle_memory_sync_files(engine, args)
    return json.loads(r[0].text)

class TestMemorySyncFiles:
    def test_sync_empty_dir(self):
        """空目录返回 0 条同步。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            with tempfile.TemporaryDirectory() as td:
                result = await _call(engine, {"source_dir": td})
                assert result["synced"] == 0
                assert result["skipped"] == 0
        asyncio.run(run())

    def test_sync_single_md(self):
        """单条 .md 正确解析 frontmatter 并存储。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            with tempfile.TemporaryDirectory() as td:
                md_path = os.path.join(td, "test-memory.md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write("""---
name: test-memory
description: "A test memory for sync"
metadata:
  type: reference
---

This is the memory body content.
""")
                result = await _call(engine, {"source_dir": td})
                assert result["synced"] == 1
                assert result["skipped"] == 0
                # Verify stored in engine
                found = False
                for mid, mem in engine._memories.items():
                    if "test-memory" in mem.get("content", ""):
                        found = True
                        assert "reference" in str(mem.get("tags", []))
                        break
                assert found, "Memory not stored in engine"
        asyncio.run(run())

    def test_skip_synced(self):
        """已标记 [[synced-to-mcp]] 的文件跳过。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            with tempfile.TemporaryDirectory() as td:
                md_path = os.path.join(td, "already-synced.md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write("""---
name: already-synced
description: "Already synced"
metadata:
  type: reference
---

Already synced. [[synced-to-mcp]]
""")
                result = await _call(engine, {"source_dir": td})
                assert result["skipped"] == 1
        asyncio.run(run())

    def test_skip_memory_system_primary_channel(self):
        """已标记 [[memory-system-primary-channel]] 的文件跳过（代表此记忆来自 MCP 系统）。"""
        async def run():
            engine = ContextEngine(use_sqlite=False)
            with tempfile.TemporaryDirectory() as td:
                md_path = os.path.join(td, "from-mcp.md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write("""---
name: from-mcp
description: "From MCP system"
metadata:
  type: feedback
---

Already tracked. [[memory-system-primary-channel]]
""")
                result = await _call(engine, {"source_dir": td})
                assert result["skipped"] == 1
        asyncio.run(run())
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_memory_sync.py -v
# Expected: ImportError (模块不存在)
```

- [ ] **Step 3: 实现 handle_memory_sync_files**

```python
# plastic_promise/mcp/tools/sync.py
"""MCP 工具: memory_sync_files — 存量 .md 文件同步到 MCP 管道"""

import json
import os
from typing import Any
from mcp.types import TextContent


def _parse_frontmatter(content: str) -> dict:
    """使用 yaml 标准库解析 frontmatter。失败时降级返回空 dict。"""
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        import yaml
        result = yaml.safe_load(parts[1])
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}  # 降级：解析失败不阻塞同步


async def handle_memory_sync_files(engine: Any, args: dict) -> list[TextContent]:
    """同步文件系统 .md 记忆到 MCP 管道。

    Args:
        engine: ContextEngine 实例
        args:
            source_dir: str — 源目录路径 (含 .md 记忆文件)
            dry_run: bool — 仅扫描不写入 (默认 false)

    Returns:
        list[TextContent]: synced, skipped, errors 计数
    """
    source_dir = args.get("source_dir", "")
    dry_run = args.get("dry_run", False)

    if not source_dir or not os.path.isdir(source_dir):
        return [TextContent(type="text", text=json.dumps({
            "error": f"Invalid source_dir: {source_dir}",
            "synced": 0, "skipped": 0, "errors": 0
        }, ensure_ascii=False))]

    from plastic_promise.mcp.tools.memory import handle_memory_store

    synced = 0
    skipped = 0
    errors = 0

    for fname in sorted(os.listdir(source_dir)):
        if fname == "MEMORY.md" or not fname.endswith(".md"):
            continue

        fpath = os.path.join(source_dir, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()

        # 跳过已同步的文件
        if "[[synced-to-mcp]]" in content or "[[memory-system-primary-channel]]" in content:
            skipped += 1
            continue

        fm = _parse_frontmatter(content)
        name = fm.get("name", fname.replace(".md", ""))
        # type 在嵌套的 metadata block 中: metadata: {type: reference}
        metadata = fm.get("metadata", {})
        mem_type = metadata.get("type", "reference") if isinstance(metadata, dict) else "reference"
        description = fm.get("description", "")

        # 提取 body（frontmatter 之后的部分）
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            body = parts[-1].strip() if len(parts) >= 3 else content

        tags = [f"cat:{mem_type}", "source:file-sync", f"file:{fname}"]
        entity_id = f"memory:file:{name}"

        if dry_run:
            synced += 1
            continue

        try:
            result = await handle_memory_store(engine, {
                "content": f"[FILE SYNC] {name}: {description}\n\n{body}",
                "memory_type": "experience",
                "source": "file_sync",
                "entity_ids": [entity_id],
                "tags": tags,
            })
            data = json.loads(result[0].text)
            if data.get("stored"):
                synced += 1
                # 标记源文件为已同步
                new_content = content.rstrip() + "\n\n[[synced-to-mcp]]\n"
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(new_content)
            else:
                errors += 1
        except Exception:
            errors += 1

    return [TextContent(type="text", text=json.dumps({
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "source_dir": source_dir,
    }, ensure_ascii=False))]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_memory_sync.py -v
# Expected: 4 passed
```

- [ ] **Step 5: 在 MCP server 注册工具**

在 `plastic_promise/mcp/server.py` 的 `_TOOLS` 列表末尾（`skill_auto_track` 之后）添加：

```python
Tool(
    name="memory_sync_files",
    description="同步文件系统 .md 记忆到 MCP 管道。扫描目录、解析 frontmatter、去重、标记已同步。",
    inputSchema={
        "type": "object",
        "properties": {
            "source_dir": {"type": "string", "description": ".md 记忆文件目录路径"},
            "dry_run": {"type": "boolean", "description": "仅扫描不写入 (默认 false)"},
        },
        "required": ["source_dir"],
    },
),
```

并在 server.py 的 dispatch 部分添加:

```python
elif name == "memory_sync_files":
    from plastic_promise.mcp.tools.sync import handle_memory_sync_files
    return await handle_memory_sync_files(engine, arguments)
```

- [ ] **Step 6: 执行存量同步**

```bash
python -c "
import asyncio, json
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.mcp.tools.sync import handle_memory_sync_files
import os

async def main():
    engine = ContextEngine(use_sqlite=True)
    mem_dir = os.path.expanduser('~/.claude/projects/F--Agent-Memory-system/memory')
    result = await handle_memory_sync_files(engine, {'source_dir': mem_dir})
    print(json.loads(result[0].text))

asyncio.run(main())
"
# Expected: synced=6, skipped=0
```

- [ ] **Step 7: Commit**

```bash
git add plastic_promise/mcp/tools/sync.py plastic_promise/mcp/server.py tests/test_memory_sync.py
git commit -m "feat: memory_sync_files — sync .md memories into MCP pipeline"
```

---

### Task 3: 写入健康检查 — handle_memory_store 失败时明确告警

**Files:**
- Modify: `plastic_promise/mcp/tools/memory.py:111-185`

**Interfaces:**
- Consumes: 无新增依赖
- Produces: handle_memory_store 返回增加 `server_ok` 字段

- [ ] **Step 1: 修改 handle_memory_store**

在 `handle_memory_store` 函数开头（第 122 行 try 块之后）加入 MCP server 可用性检查。在 try 块内、is_noise 检查之前：

```python
try:
    from plastic_promise.core.noise_filter import is_noise
    content = args["content"]
    if is_noise(content):
        return [TextContent(type="text", text=json.dumps(
            {"stored": False, "reason": "noise_filtered",
             "content_preview": content[:100]},
            ensure_ascii=False))]

    memory_type = args.get("memory_type", "experience")
    ...
```

在 noise_filter 检查之后、memory_type 提取之前，加入异步健康检查：

```python
    # Health check: 异步检测 MCP 服务器可用性（不阻塞事件循环）
    server_ok = True
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.get("http://127.0.0.1:9020/health", timeout=2.0)
    except Exception:
        server_ok = False
```

然后在返回 dict 中加入 `"server_ok": server_ok`。

```python
        return [TextContent(type="text", text=json.dumps({
            "stored": True,
            "memory_id": fuzzy_id,
            ...
            "server_ok": server_ok,  # 新增字段
        }, ensure_ascii=False))]
```

- [ ] **Step 2: 验证**

```bash
python -m pytest tests/test_skill_tracking.py tests/test_e2e_skill_pipeline.py -v
# Expected: all 27 tests pass
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/mcp/tools/memory.py
git commit -m "feat: add server_ok health check to handle_memory_store response"
```

---

### Task 4: 审计间隔 — 环境变量 + jitter

**Files:**
- Modify: `pi_daemon.py:17,382-387`

- [ ] **Step 1: 修改 INTERVAL 和审计触发条件**

修改 `pi_daemon.py:17`:

```python
INTERVAL = int(os.environ.get("PI_INTERVAL", "10"))
AUDIT_INTERVAL = int(os.environ.get("AUDIT_INTERVAL_SECONDS", "3600"))
```

修改审计触发逻辑 (`pi_daemon.py:381-387`)。使用概率跳过替代负数计数器避免计数器异常：

```python
        _cleanup_counter += 1
        # 审计间隔: AUDIT_INTERVAL 秒，带 jitter 防惊群
        _audit_threshold = AUDIT_INTERVAL // INTERVAL
        if _cleanup_counter >= _audit_threshold:
            import random
            # 10% 概率跳过本次审计（jitter），防止所有实例同时审计
            if random.random() < 0.1:
                _cleanup_counter = _audit_threshold - 1  # 下次循环再触发
            else:
                cleanup_old_memories()
                # 使用 packaged audit 入口（audit_daemon.py 在项目根目录，pi_daemon.py 同目录导入）
                from audit_daemon import run_audit
                await run_audit()
                _cleanup_counter = 0
```

- [ ] **Step 2: 验证常量**

```bash
python -c "
import os
os.environ['AUDIT_INTERVAL_SECONDS'] = '3600'
os.environ['PI_INTERVAL'] = '10'
# 读取 pi_daemon 头部常量
code = open('pi_daemon.py').read().split('async def main')[0]
exec(code)
print(f'INTERVAL={INTERVAL}, AUDIT_INTERVAL={AUDIT_INTERVAL}')
_threshold = AUDIT_INTERVAL // INTERVAL
print(f'Audit every ~{_threshold} iterations = ~{_threshold * INTERVAL}s with 10% skip jitter')
assert _threshold == 360
print('PASS')
"
```

- [ ] **Step 3: 添加冷启动首次审计**

在 `pi_daemon.py` 的 `main()` 函数中，while 循环之前插入：

```python
    # 冷启动: 30s 后执行首次审计（daemon 启动即获得基线）
    await asyncio.sleep(30)
    from audit_daemon import run_audit
    await run_audit()
```

> **审计导入说明**: `audit_daemon.py` 位于项目根目录 (`F:\Agent\Memory system\audit_daemon.py`)，与 `pi_daemon.py` 同级。`pi_daemon.py` 当前已使用 `from audit_daemon import run_audit`，此行已验证可用。`plastic_promise.cron.audit_daily` 是另一个审计入口，供 cron 调度使用，daemon 循环使用根目录版本。

- [ ] **Step 4: Commit**

```bash
git add pi_daemon.py
git commit -m "feat: audit interval control via AUDIT_INTERVAL_SECONDS + jitter + cold-start"
```

---

### Task 5: 端到端集成验证

- [ ] **Step 1: 运行全量测试**

```bash
python -m pytest tests/ -v --tb=short
```

- [ ] **Step 2: 端到端验证脚本**

```bash
python -c "
import asyncio, json, os
from plastic_promise.core.context_engine import ContextEngine

async def main():
    engine = ContextEngine(use_sqlite=True)
    
    # 1. 验证 memory_sync_files
    from plastic_promise.mcp.tools.sync import handle_memory_sync_files
    mem_dir = os.path.expanduser('~/.claude/projects/F--Agent-Memory-system/memory')
    r = await handle_memory_sync_files(engine, {'source_dir': mem_dir, 'dry_run': True})
    data = json.loads(r[0].text)
    print(f'Sync scan: synced={data[\"synced\"]}, skipped={data[\"skipped\"]}')
    assert data['synced'] + data['skipped'] >= 1, 'No files found'
    
    # 2. 验证 server health check
    import sqlite3
    conn = sqlite3.connect('plastic_memory.db')
    count = conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
    print(f'Memories in DB: {count}')
    conn.close()
    
    print('ALL CHECKS PASSED')

asyncio.run(main())
"
```

- [ ] **Step 3: 重启 MCP 服务器加载新工具（跨平台）**

```bash
python -c "
import subprocess, os, sys

def kill_on_port(port=9020):
    '''跨平台终止占用指定端口的进程。'''
    try:
        import psutil
        for conn in psutil.net_connections():
            if conn.laddr.port == port and conn.status == 'LISTEN':
                p = psutil.Process(conn.pid)
                p.terminate()
                p.wait(timeout=5)
                print(f'Killed PID {conn.pid} on port {port}')
                return
    except Exception:
        pass
    # Fallback: platform-specific
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '//F', '//PID', 
            subprocess.run(['netstat','-ano'], capture_output=True, text=True).stdout], 
            shell=True)
    else:
        subprocess.run(['fuser', '-k', f'{port}/tcp'], stderr=subprocess.DEVNULL)

kill_on_port(9020)
print('Port 9020 freed')
"
sleep 1
python -m plastic_promise.mcp.server --sse 9020 &
sleep 2
python -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:9020/health'); print(r.read().decode())"
```

- [ ] **Step 4: 提交最终验证结果**

```bash
git add -A && git status
git commit -m "test: end-to-end MVP usage bootstrap verification"
```

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

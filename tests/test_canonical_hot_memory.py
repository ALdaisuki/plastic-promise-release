from pathlib import Path

from plastic_promise.core.canonical_hot_memory import lookup_canonical_hot
from plastic_promise.core.code_memory import build_code_index


def test_canonical_hot_lookup_principle_alias_hits():
    hits = lookup_canonical_hot("需要先查 context_supply，避免无上下文行动")

    assert any(hit.kind == "principle" and hit.target_id.startswith("principle:") for hit in hits)


def test_canonical_hot_lookup_mcp_tool_hits(tmp_path: Path):
    tool_dir = tmp_path / "plastic_promise" / "mcp" / "tools"
    tool_dir.mkdir(parents=True)
    (tool_dir / "context.py").write_text(
        """
async def handle_context_supply(engine, args):
    '''Return task context.'''
    return []
""".lstrip(),
        encoding="utf-8",
    )
    index = build_code_index(tmp_path, max_files=10)

    hits = lookup_canonical_hot("please call context-supply before acting", code_index=index)

    assert any(hit.key == "mcp_tool:context_supply" for hit in hits)
    assert any(hit.target_id == "mcp_tool:context_supply" for hit in hits)


def test_canonical_hot_lookup_bilingual_synonym_hits():
    hits = lookup_canonical_hot("这次要复盘一下经验", domain_hint="reflecting")

    assert any(hit.kind == "bilingual_synonym" for hit in hits)

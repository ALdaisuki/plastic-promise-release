from plastic_promise.core.chunking import (
    build_chunk_manifest,
    legacy_character_chunks,
    shadow_chunking_diagnostics,
    structure_aware_chunks,
)


def test_legacy_character_chunks_preserve_bounded_request_contract():
    assert legacy_character_chunks("aaaabbbbcccc", 4, 2) == ["aaaa", "bbbb"]


def test_shadow_diagnostics_share_legacy_limit_and_find_tail():
    text = "# Topic\n\n" + ("First topic. " * 8) + "\n\nTAIL-EVIDENCE"

    diagnostics = shadow_chunking_diagnostics(
        text,
        target_chars=24,
        hard_chars=48,
        max_chunks=2,
    )

    assert diagnostics["legacy"]["chunk_count"] == 2
    assert diagnostics["legacy"]["truncated"] is True
    assert diagnostics["candidate"]["last_source_end"] == len(text)
    assert diagnostics["candidate"]["truncated"] is False


def test_structure_chunks_carry_heading_context_and_preserve_tail():
    text = "# Retrieval\n\nFirst paragraph about indexing.\n\nSecond paragraph about recall."

    chunks = structure_aware_chunks(text, target_chars=40, hard_chars=80)

    assert len(chunks) >= 2
    assert all(chunk.heading_path == ("Retrieval",) for chunk in chunks)
    assert all("Retrieval" in chunk.text for chunk in chunks)
    assert "recall" in chunks[-1].text
    assert chunks[0].source_start == text.index("First paragraph")
    assert chunks[-1].source_end <= len(text)


def test_structure_chunk_source_spans_match_verbatim_oversized_text():
    body = "Sentence one. Sentence two. Sentence three. Sentence four."
    text = f"# Topic\n\n{body}"

    chunks = structure_aware_chunks(text, target_chars=20, hard_chars=28)

    assert len(chunks) >= 2
    for chunk in chunks:
        source_slice = text[chunk.source_start : chunk.source_end]
        contextual_body = chunk.text.split("\n", 1)[-1]
        assert source_slice == contextual_body
    assert chunks[-1].source_end == len(text)


def test_structure_chunks_keep_fenced_code_atomic_until_hard_limit():
    text = "# API\n\n```python\nfirst = 1\n\nsecond = 2\n```\n\nUse the endpoint."

    chunks = structure_aware_chunks(text, target_chars=200, hard_chars=200)

    code_chunks = [chunk for chunk in chunks if chunk.kind == "code"]
    assert len(code_chunks) == 1
    assert "first = 1" in code_chunks[0].text
    assert "second = 2" in code_chunks[0].text


def test_structure_chunks_isolate_tables_from_paragraphs():
    text = "Intro.\n\n| name | value |\n| --- | --- |\n| a | 1 |\n\nConclusion."

    chunks = structure_aware_chunks(text, target_chars=200, hard_chars=200)

    assert [chunk.kind for chunk in chunks] == ["paragraph", "table", "paragraph"]
    assert "| name | value |" in chunks[1].text


def test_structure_chunks_merge_peer_paragraphs_without_repeating_heading():
    text = "# Retrieval\n\nFirst paragraph.\n\nSecond paragraph."

    chunks = structure_aware_chunks(text, target_chars=200, hard_chars=200)

    assert len(chunks) == 1
    assert chunks[0].text.count("Retrieval") == 1
    assert "First paragraph.\n\nSecond paragraph." in chunks[0].text


def test_structure_chunks_split_list_transition_without_blank_line():
    text = "Intro paragraph.\n- first item\n- second item"

    chunks = structure_aware_chunks(text, target_chars=200, hard_chars=200)

    assert [chunk.kind for chunk in chunks] == ["paragraph", "list"]


def test_structure_chunks_keep_heading_only_and_consecutive_headings():
    text = "# API\n## Methods"

    chunks = structure_aware_chunks(text, target_chars=200, hard_chars=200)

    assert [chunk.kind for chunk in chunks] == ["heading", "heading"]
    assert chunks[-1].source_end == len(text)
    assert "# API" in chunks[0].text
    assert "## Methods" in chunks[1].text


def test_structure_chunks_bounded_plan_keeps_tail_and_reports_gap():
    text = "\n\n".join(f"# Section {index}\n\nBody {index}" for index in range(8))

    chunks = structure_aware_chunks(text, target_chars=30, hard_chars=60, max_chunks=3)

    assert len(chunks) == 3
    assert "Body 7" in chunks[-1].text


def test_chunk_manifest_is_stable_parent_projection_for_chinese_markdown():
    text = (
        "# 检索\n\n这是第一段，用来验证中文边界。\n\n"
        "## 代码\n\n```python\nvalue = '记忆'\n```\n\n"
        "- 保留标题路径\n- 保留来源跨度"
    )

    first = build_chunk_manifest(text, target_chars=32, hard_chars=72, max_chunks=16)
    second = build_chunk_manifest(text, target_chars=32, hard_chars=72, max_chunks=16)

    assert first == second
    assert first["schema_version"] == "structure-v1"
    assert first["source_chars"] == len(text)
    assert first["chunk_count"] == len(first["chunks"])
    assert first["truncated"] is False
    assert {chunk["kind"] for chunk in first["chunks"]} >= {"paragraph", "code", "list"}
    assert all(chunk["source_hash"] == first["source_hash"] for chunk in first["chunks"])
    assert all(chunk["chunk_id"].startswith("chunk_") for chunk in first["chunks"])
    assert all(len(chunk["text_hash"]) == 64 for chunk in first["chunks"])
    assert any(chunk["header_path"] == ["检索", "代码"] for chunk in first["chunks"])
    assert any("来源跨度" in chunk["text"] for chunk in first["chunks"])


def test_chunk_manifest_marks_bounded_middle_omission_as_resource_limited():
    text = "\n\n".join(f"# Section {index}\n\nBody {index}" for index in range(10))

    manifest = build_chunk_manifest(text, target_chars=20, hard_chars=40, max_chunks=3)

    assert manifest["chunk_count"] == 3
    assert manifest["resource_limited"] is True
    assert manifest["truncated"] is True
    assert "Body 9" in manifest["chunks"][-1]["text"]

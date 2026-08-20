from plastic_promise.core.context_engine import ContextEngine, ContextItem
from plastic_promise.core.noise_filter import is_noise
from plastic_promise.smart_extractor import _split_memory_sentences, extract_memories


def test_sentence_split_preserves_urls_and_repo_paths():
    text = "学习项目 https://github.com/CortexReach/memory-lancedb-pro。然后修复 recall 质量。"

    sentences = _split_memory_sentences(text)

    assert sentences[0] == "学习项目 https://github.com/CortexReach/memory-lancedb-pro"
    assert not any(s == "https://github" for s in sentences)
    assert not any(s == "com/CortexReach/memory-lancedb-pro" for s in sentences)


def test_extraction_keeps_reference_url_in_one_memory():
    text = "学习参考项目 https://github.com/CortexReach/memory-lancedb-pro 的 memory retrieval 设计"

    memories = extract_memories(text, max_llm_calls=0)

    assert len(memories) == 1
    assert memories[0].l2_content == text
    assert memories[0].l0_abstract == text[:80]


def test_server_backend_uses_rule_only_extraction(monkeypatch):
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-server-backend")

    def fail_network(*args, **kwargs):
        raise AssertionError("server backend must not call Ollama")

    monkeypatch.setattr("plastic_promise.smart_extractor.requests.post", fail_network)

    memories = extract_memories(
        "用户决定采用 compute node 负责推理并且服务器只负责持久化",
        max_llm_calls=3,
    )

    assert memories
    assert memories[0].category == "decision"


def test_noise_filter_rejects_low_information_and_partial_urls():
    assert is_noise("No file edits")
    assert is_noise("md files only")
    assert is_noise("https://github")
    assert is_noise("com/CortexReach/memory-lancedb-pro")
    assert is_noise("AUDIT trust=0.60 pipeline=1.00 domain=0.80")
    assert not is_noise(
        "学习参考项目 https://github.com/CortexReach/memory-lancedb-pro 的 recall 设计"
    )


def test_noise_filter_rejects_emoji_reactions_but_keeps_meaningful_text():
    assert is_noise("👍")
    assert is_noise("  👍 👍 \n")
    assert is_noise("[reaction: 👍]")
    assert is_noise("[like: shipped]")
    assert not is_noise("release sync passed 👍 next verify startup recovery")


def test_context_item_defaults_to_nonzero_worth_when_supplied():
    item = ContextItem(
        id="m1",
        content="useful memory",
        relevance=0.8,
        source="vector",
        worth_score=0.5,
    )

    assert item.worth_score == 0.5


def test_worth_score_is_computed_from_success_failure_counters():
    assert (
        ContextEngine._calc_worth_score_from_memory({"worth_success": 5, "worth_failure": 0})
        == 6 / 7
    )
    assert (
        ContextEngine._calc_worth_score_from_memory({"worth_success": 0, "worth_failure": 0}) == 0.5
    )
    assert ContextEngine._calc_worth_score_from_memory({"worth_score": 0.75}) == 0.75


def test_recall_noise_helper_filters_legacy_low_information_memories():
    assert ContextEngine._is_recall_noise("No file edits")
    assert ContextEngine._is_recall_noise("AUDIT trust=0.60 pipeline=1.00")
    assert not ContextEngine._is_recall_noise("useful recall quality finding with enough context")

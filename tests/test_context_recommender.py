import asyncio
import json

from plastic_promise.core.context_engine import ContextItem, ContextPack


def test_context_recommender_scores_with_explainable_reasons():
    from plastic_promise.core.context_recommender import recommend_context_items

    strong = ContextItem(
        id="mem_strong",
        content="Architecture task should use runtime_events and audit trace",
        relevance=0.91,
        source="project:plastic-promise",
        layer="core",
        worth_score=0.8,
        adoption_count=3,
        rejection_count=0,
    )
    weak = ContextItem(
        id="mem_weak",
        content="Unrelated note",
        relevance=0.21,
        source="global",
        layer="divergent",
        worth_score=0.2,
    )

    recs = recommend_context_items([weak, strong], task_type="architecture")

    assert recs[0]["id"] == "mem_strong"
    assert {"high_relevance", "positive_worth", "project_scope_match"}.issubset(
        recs[0]["reasons"]
    )
    assert recs[0]["score"] > recs[1]["score"]


def test_context_recommender_does_not_reintroduce_hard_exclusions():
    from plastic_promise.core.context_recommender import recommend_context_items

    hidden = ContextItem(
        id="mem_hidden",
        content="High scoring but hard excluded",
        relevance=1.0,
        source="project:other",
        layer="core",
        worth_score=1.0,
    )
    visible = ContextItem(
        id="mem_visible",
        content="Allowed memory",
        relevance=0.5,
        source="global",
        layer="related",
        worth_score=0.5,
    )

    recs = recommend_context_items(
        [hidden, visible],
        task_type="architecture",
        hard_excluded_ids={"mem_hidden"},
    )

    assert [rec["id"] for rec in recs] == ["mem_visible"]


def test_context_pack_prompt_renders_context_recommender_section():
    from plastic_promise.core.context_recommender import attach_context_recommendations

    pack = ContextPack(
        core=[
            ContextItem(
                id="mem_prompt",
                content="Use context recommender reasons",
                relevance=0.88,
                source="project:plastic-promise",
                layer="core",
                worth_score=0.7,
            )
        ]
    )
    attach_context_recommendations(pack, task_type="architecture")

    prompt = pack.to_prompt()

    assert "## [CONTEXT_RECOMMENDER]" in prompt
    assert "mem_prompt" in prompt
    assert "high_relevance" in prompt


def test_memory_recall_payload_includes_context_recommendations(monkeypatch):
    from mcp.types import TextContent

    from plastic_promise.core import embedder
    from plastic_promise.mcp.tools import memory as memory_tools

    class FakePack(ContextPack):
        pass

    class FakeEngine:
        def supply(self, *args, **kwargs):
            pack = FakePack(
                core=[
                    ContextItem(
                        id="mem_recall",
                        content="P1 architecture recall",
                        relevance=0.9,
                        source="project:plastic-promise",
                        layer="core",
                        worth_score=0.8,
                    )
                ],
                audit_metadata={"mode": "mix", "budget": {"core": 1}},
            )
            pack.core[0].metadata = {
                "project_id": "project:plastic-promise",
                "visibility": "project",
            }
            return pack

        def get_memory_dict(self, mid):
            return {"project_id": "project:plastic-promise", "visibility": "project"}

    class FakeEmbedder:
        async def aembed(self, _text):
            return [0.0]

    monkeypatch.setattr(embedder, "get_embedder", lambda fallback_on_error=False: FakeEmbedder())
    result = asyncio.run(
        memory_tools.handle_memory_recall(
            FakeEngine(),
            {
                "query": "architecture",
                "task_type": "architecture",
                "project_id": "project:plastic-promise",
                "project_policy": "balanced",
            },
        )
    )

    assert isinstance(result[0], TextContent)
    payload = json.loads(result[0].text)
    assert payload["context_recommendations"][0]["id"] == "mem_recall"
    assert "context_recommender" in payload["audit"]

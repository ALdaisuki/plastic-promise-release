"""Quick recall quality test."""
import sys
sys.path.insert(0, '.')
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.embedder import get_embedder

engine = ContextEngine()
engine._ensure_heavy_init()
embedder = get_embedder(fallback_on_error=True)
print(f"Embedder: {embedder.model_name}")

queries = [
    ("enterprise git governance branch strategy commit convention", "general"),
    ("memory pipeline LanceDB worth score decay quality retrieval", "general"),
    ("Hunter Guild delegation trust merge permission review", "general"),
    ("write code to store memory records in the pipeline", "code_generation"),
]

for q, tt in queries:
    vec = embedder.embed(q)
    pack = engine.supply(q, vec, tt, "global")
    audit = pack.audit_metadata
    print(f"\n=== {q[:55]}... ===")
    print(f"  engine={audit.get('engine_version','?')} vs={audit.get('vector_search','?')}")
    print(f"  core={len(pack.core)}:")
    for item in pack.core[:4]:
        print(f"    [{item.relevance:.3f}] {item.content[:80]}")
    if not pack.core:
        print("    (empty)")
    print(f"  related={len(pack.related)} (sample):")
    for item in pack.related[:2]:
        print(f"    [{item.relevance:.3f}] {item.content[:80]}")

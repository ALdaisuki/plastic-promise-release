"""Rebuild LanceDB index with Ollama embeddings.

Run this once to replace all zero-vector entries with real mxbai-embed-large vectors.
Requires Ollama running (http://127.0.0.1:11434) with mxbai-embed-large model.
"""
import sys
import time
sys.path.insert(0, '.')

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.embedder import get_embedder

engine = ContextEngine()
engine._ensure_heavy_init()

total = len(engine._memories)
embedder = get_embedder(fallback_on_error=False)
print(f"Embedder: {embedder.model_name}")
print(f"Memories to re-index: {total}")

# Clear old LanceDB and recreate
import shutil
import os
ldb_path = "plastic_memory.lancedb"
if os.path.exists(ldb_path):
    shutil.rmtree(ldb_path)
    print(f"Deleted old LanceDB at {ldb_path}")

# Force re-init to create fresh table
engine._ldb = None
engine._heavy_init_done = False
engine._ensure_heavy_init()

count = 0
errors = 0
for mid, mem in engine._memories.items():
    content = mem.get('content', '') if isinstance(mem, dict) else getattr(mem, 'content', '')
    if not content or len(content.strip()) < 3:
        continue
    tier = (mem.get('tier', 'L2') if isinstance(mem, dict) else getattr(mem, 'tier', 'L2')) or 'L2'
    category = (mem.get('category', 'other') if isinstance(mem, dict) else getattr(mem, 'category', 'other')) or 'other'
    try:
        vec = embedder.embed(content)
        engine._ldb.insert(mid, vec, content, tier=tier, category=category)
        count += 1
        if count % 50 == 0:
            print(f"  {count}/{total}...")
    except Exception as e:
        errors += 1
        if errors <= 3:
            print(f"  SKIP {str(mid)[:30]}: {e}")

print(f"\nRe-indexed: {count}/{total} (errors: {errors})")
print(f"LanceDB rows: {engine._ldb.count_rows()}")

# Verify
test_vec = embedder.embed("test query")
results = engine._ldb._table.search(test_vec).limit(3).to_list()
print("\nVerification — stored vectors:")
for r in results:
    vec = r.get('vector', [])
    nz = sum(1 for v in vec if abs(v) > 0.0001)
    print(f"  non_zero={nz}/{len(vec)} text={r.get('text','')[:60]}")

if all(sum(1 for v in r.get('vector',[]) if abs(v)>0.0001) > 0 for r in results):
    print("\nSUCCESS: All vectors are real, no zero vectors.")
else:
    print("\nWARNING: Some vectors are still zero.")

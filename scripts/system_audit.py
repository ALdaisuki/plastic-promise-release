"""Full system audit — what's alive vs dead."""
import sys
sys.path.insert(0, '.')

print('=' * 60)
print('1. SCARF REFLECTION')
print('=' * 60)
try:
    from plastic_promise.reflection.soul_scarf import SCARFReflector
    sr = SCARFReflector()
    r = sr.reflect("system audit check")
    print(f'  overall={r["summary"]["overall_score"]:.3f}')
    for dim in ['Status','Certainty','Autonomy','Relatedness','Fairness']:
        s = r[dim]['score']
        print(f'  {dim}: {s:.3f}')
except Exception as e:
    print(f'  FAIL: {e}')

print()
print('=' * 60)
print('2. TRUST SYSTEM')
print('=' * 60)
try:
    from plastic_promise.defense.trust_store import TrustStore
    ts = TrustStore()
    score = ts.get_trust('default')
    history = ts.get_history('default', limit=5)
    print(f'  trust(default)={score:.4f}')
    print(f'  history entries: {len(history)}')
    for h in history[:3]:
        print(f'    delta={h[1]:+.3f} reason={str(h[2])[:60]}')
except Exception as e:
    print(f'  FAIL: {e}')

print()
try:
    from plastic_promise.defense.soul_enforcer import TrustManager
    tm = TrustManager()
    print(f'  TrustManager tier: {tm.get_tier()}')
    print(f'  autonomy: {tm.get_autonomy_level()}')
    print(f'  retrieval_boost: {tm.get_retrieval_boost()}')
except Exception as e:
    print(f'  TrustManager FAIL: {e}')

print()
print('=' * 60)
print('3. STEP-CLOSURE / CEI')
print('=' * 60)
try:
    from plastic_promise.loop.soul_loop import SoulLoop
    cei = SoulLoop.get_cached_cei()
    print(f'  Cached CEI: {cei}')
except Exception as e:
    print(f'  FAIL: {e}')

print()
print('=' * 60)
print('4. HORMONES')
print('=' * 60)
try:
    from plastic_promise.growth.soul_hormone import HormoneEngine
    he = HormoneEngine()
    h = he.get_hormones()
    for k, v in h.items():
        print(f'  {k}: {v}')
except Exception as e:
    print(f'  FAIL: {e}')

print()
print('=' * 60)
print('5. DEFENSE — SoulEnforcer')
print('=' * 60)
try:
    from plastic_promise.defense.soul_enforcer import SoulEnforcer
    se = SoulEnforcer()
    print(f'  OK')
except Exception as e:
    print(f'  FAIL: {e}')

print()
print('=' * 60)
print('6. PRINCIPLES + GRAPH')
print('=' * 60)
from plastic_promise.core.context_engine import ContextEngine
engine = ContextEngine()
engine._ensure_heavy_init()
print(f'  enable_principles: {engine.enable_principles}')
print(f'  graph_nodes: {len(engine._graph_nodes)}')
print(f'  graph_edges: {len(engine._graph_edges)}')

print()
print('=' * 60)
print('7. Weibull DECAY — real state')
print('=' * 60)
from plastic_promise.memory.soul_memory import RecMem
rm = RecMem()
stats = rm.stats()
print(f'  total: {stats["total"]}')
print(f'  health: {stats["health_ratio"]}')
print(f'  L1/L2/L3: {stats["l1_count"]}/???/{stats["l3_count"]}')

# Check actual decay values
samples = list(rm._records.values())[:5] if hasattr(rm, '_records') else []
for r in samples:
    print(f'  tier={r.tier} decay={r.decay_multiplier:.6f} hl={r.effective_half_life} worth={r.worth_success}/{r.worth_failure}')

print()
print('=' * 60)
print('8. DAEMON — running?')
print('=' * 60)
import subprocess
result = subprocess.run(['tasklist', '/fi', 'IMAGENAME eq python.exe'], capture_output=True, text=True)
lines = [l for l in result.stdout.split('\n') if 'python' in l.lower()]
print(f'  Python processes: {len(lines)}')
for l in lines:
    print(f'  {l.strip()[:100]}')

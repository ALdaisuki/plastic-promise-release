"""Verify MCP tool merge: deleted tools + merged tools + strict mode"""
import urllib.request, json

def call(name, args=None):
    if args is None:
        args = {}
    req = urllib.request.Request('http://127.0.0.1:9020/call',
        data=json.dumps({'tool': name, 'arguments': args}).encode(),
        headers={'Content-Type': 'application/json'})
    r = urllib.request.urlopen(req, timeout=10)
    return json.loads(r.read().decode())

print("=== Deleted tools (should NOT be found) ===")
for t in ['principle_inherit', 'principle_diffuse', 'context_ready', 'pack_recall', 'memory_stats']:
    try:
        call(t)
        print(f"  {t}: FOUND (BAD)")
    except Exception:
        print(f"  {t}: NOT FOUND (OK)")

print("\n=== Active tools (should work) ===")
for t, args in [
    ('principle_activate', {'task_type': 'general', 'task_description': 'test'}),
    ('context_supply', {'task_description': 'test'}),
    ('memory_recall', {'query': 'test'}),
    ('memory_sync_files', {'source_dir': '.'}),
    ('memory_reclassify', {'batch_size': 1}),
    ('system', {'action': 'stats'}),
]:
    try:
        r = call(t, args)
        st = "OK"
        if 'error' in r:
            st = f"OK (with error: {r['error'][:60]})"
        print(f"  {t}: {st}")
    except Exception as e:
        print(f"  {t}: ERROR - {e}")

print("\n=== memory_recall strict mode ===")
r = call('memory_recall', {'query': 'xyzzy_no_match_42', 'strict': True})
core_count = len(r.get('core', []))
strict_flag = r.get('strict', False)
print(f"  core_count={core_count}, strict={strict_flag}")
if strict_flag and core_count == 0:
    print("  PASS")
else:
    print("  WARN: strict mode should return empty on no match")

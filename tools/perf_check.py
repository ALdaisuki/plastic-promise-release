"""Post-fix benchmark: lightweight hook + /api/skill-track speed"""
import urllib.request, json, time, subprocess, sys

MCP = "http://127.0.0.1:9020"

def post(url, body, timeout=30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def run_hook(event_name, stage="brainstorming"):
    hook_input = json.dumps({
        "hook_event_name": event_name,
        "tool_name": "run_mcp",
        "tool_input": {
            "server_name": "mcp_plastic-promise",
            "tool_name": "sp-stage",
            "args": {"stage": stage, "task_description": "perf test"}
        }
    })
    t0 = time.time()
    result = subprocess.run([sys.executable, ".trae/hooks/sp_hook.py"], 
        input=hook_input, capture_output=True, text=True, 
        cwd=r"f:\Agent\Memory system", timeout=5)
    return time.time() - t0, result.stdout.strip()

# Warm up (load embedder)
print("Warmup...")
post(f"{MCP}/api/skill-track", {"phase":"start","skill_name":"brainstorming"}, 10)
post(f"{MCP}/api/skill-track", {"phase":"complete","skill_name":"brainstorming"}, 10)
print("Warmup done.\n")

# Test 1: Hook PreToolUse
print("=== Hook PreToolUse ===")
for i in range(3):
    dt, out = run_hook("PreToolUse")
    print(f"  call {i+1}: {dt:.2f}s")

# Clean up session
post(f"{MCP}/api/skill-track", {"phase":"complete","skill_name":"brainstorming"}, 5)

# Test 2: Hook PostToolUse
print("\n=== Hook PostToolUse ===")
for i in range(3):
    # Need a start first
    post(f"{MCP}/api/skill-track", {"phase":"start","skill_name":"brainstorming"}, 5)
    dt, out = run_hook("PostToolUse")
    print(f"  call {i+1}: {dt:.2f}s")

# Test 3: API direct
print("\n=== /api/skill-track direct ===")
for i in range(3):
    t0 = time.time()
    r = post(f"{MCP}/api/skill-track", {"phase":"start","skill_name":"brainstorming"}, 5)
    dt = time.time() - t0
    print(f"  call {i+1}: {dt:.2f}s, eid={str(r.get('entity_id',''))[:40]}")
    post(f"{MCP}/api/skill-track", {"phase":"complete","skill_name":"brainstorming"}, 5)

print("\nDone.")

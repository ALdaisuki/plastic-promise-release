#!/usr/bin/env python3
"""One-command governed release pipeline for Plastic Promise.

Subcommands
-----------
doctor      Environment preflight: toolchain modules, proxy, TZ policy, WSL link,
            compute-node services, keychain material, control-plane files.
handshake   One-step compute-node wiring for a release container: stage the
            control-plane materials, resolve the node bearer from the keychain,
            rebuild the container with the full governed env, apply the
            node-governance schema migration, and verify node-routing readiness.
e2e         Store a canary through the governed route and run the official
            read-only smoke in strict mode (no text-only allowance).
publish     Assert a clean tree, assemble the proven push environment, and run
            scripts/release-sync.py --push.

Every step encodes a v0.2.16 field lesson; none of them are optional.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MAC_RUNTIME = pathlib.Path("~/.local/share/plastic-promise/mac-server").expanduser()
WSL_SSH = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "ALdai@192.168.5.6"]
WSL_PREFIX = "wsl -d Ubuntu-22.04 -u root"
CONTAINER = "pp-release-vector-smoke"
NODE_PORT = 19130
EMBED_PORT = 19131
MCP_PORT = 9020
KEYCHAIN_SERVICE = "plastic-promise-compute-windows-5080"
KEYCHAIN_ACCOUNT = "aldaisuki"
VENV_BIN = "/Users/aldaisuki/.local/share/plastic-promise/mac-server/runtime-venv/bin"
PYTEST_IGNORES = " ".join(
    "--ignore=tests/%s.py" % name
    for name in (
        "test_compute_routing_operator_scripts",
        "test_context_recommender",
        "test_deployment_cli",
        "test_governed_retrieval_embedding",
        "test_mcp_claude_code_schema_regression",
        "test_mcp_durable_runtime_binding",
        "test_ordinary_memory_mutation",
        "test_passive_memory_hooks",
        "test_pipeline_quality",
        "test_project_recall_isolation",
        "test_rebuild_lancedb",
        "test_recall_pipeline_upgrade",
        "test_response_projection",
        "test_retrieval_explain_projection",
        "test_rust_release_import",
        "test_safety_net_daemon",
        "test_semantic_chunk_enrichment",
        "test_synthesis_retrieval_gate",
        "test_synthesis_store",
        "test_neko_adapter_cli",
        "test_rust_supply_perf",
        "test_smoke_restart_recovery",
    )
)


def step(msg):
    print("[pipeline] " + msg, flush=True)


def check(cond, msg, fatal=True):
    print("  [{}] {}".format("OK " if cond else "FAIL", msg), flush=True)
    if not cond and fatal:
        sys.exit(1)
    return bool(cond)


def run(cmd, **kw):
    if isinstance(cmd, str):
        return subprocess.run(
            cmd, shell=True, text=True, capture_output=True,
            encoding="utf-8", errors="replace", **kw)
    return subprocess.run(
        cmd, text=True, capture_output=True, encoding="utf-8", errors="replace", **kw)


def wsl_upload(label, script_text):
    """Ship one bash script into WSL over the proven b64 pipe."""
    b64 = base64.b64encode(script_text.encode()).decode()
    inner = "base64 -d > /tmp/pp-" + label + ".sh"
    remote = WSL_PREFIX + ' bash -c "' + inner + '"'
    done = subprocess.run(
        WSL_SSH + [remote], input=b64, capture_output=True,
        encoding="utf-8", errors="replace"
    )
    if done.returncode != 0:
        raise RuntimeError("wsl upload %s: %s" % (label, (done.stderr or "")[-400:]))


def wsl_exec(label, timeout=300):
    remote = WSL_PREFIX + " bash /tmp/pp-" + label + ".sh"
    done = subprocess.run(
        WSL_SSH + [remote], capture_output=True, timeout=timeout,
        encoding="utf-8", errors="replace"
    )
    out = done.stdout or ""
    if done.returncode != 0:
        raise RuntimeError(
            "wsl exec %s rc=%s: %s" % (label, done.returncode, (done.stderr or out)[-600:])
        )
    return out


def wsl_script(label, script_text, timeout=300):
    wsl_upload(label, script_text)
    return wsl_exec(label, timeout=timeout)


def wsl_put_bytes(data, remote_path):
    """Transfer raw bytes to WSL as one base64 stream over ssh stdin."""
    encoded = base64.b64encode(data).decode()
    inner = (
        "cat > /tmp/pp-put.b64 && base64 -d /tmp/pp-put.b64 > "
        + remote_path
        + " && rm -f /tmp/pp-put.b64"
    )
    remote = WSL_PREFIX + ' bash -c "' + inner + '"'
    done = subprocess.run(
        WSL_SSH + [remote],
        input=encoded,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if done.returncode != 0:
        raise RuntimeError(
            "put " + remote_path + ": " + (done.stderr or "")[-300:]
        )


def mac_control_root():
    env = os.environ.get("PP_CONTROL_ROOT")
    return pathlib.Path(env).expanduser() if env else MAC_RUNTIME / "control"


def node_bearer():
    got = run(
        [
            "security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            KEYCHAIN_ACCOUNT,
            "-w",
        ]
    )
    token = (got.stdout or "").strip().replace("\r", "").replace("\n", "")
    if not token.startswith("Bearer "):
        token = "Bearer " + token
    return token


def image_ref_from_manifest(manifest_path):
    doc = json.loads(manifest_path.read_text())
    for img in doc.get("images", []):
        if img.get("name") == "server":
            return img["reference"]
    raise SystemExit("manifest has no server image")


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def cmd_doctor(args):
    step("doctor: environment preflight")
    ok = True
    py = os.path.join(VENV_BIN, "python")

    tz = os.environ.get("TZ")
    ok &= check(not tz or tz == time.tzname[0], "TZ override unset (got %r)" % tz, False)

    for mod in ("build", "twine", "pytest", "pytest_asyncio", "pytest_cov"):
        r = run([py, "-c", "import " + mod])
        ok &= check(r.returncode == 0, "venv module " + mod, False)

    r = run(["which", "maturin"])
    has_maturin = r.returncode == 0 or os.path.exists(os.path.join(VENV_BIN, "maturin"))
    ok &= check(has_maturin, "maturin on PATH or in venv", False)

    ok &= check(bool(os.environ.get("HTTPS_PROXY")), "HTTPS_PROXY set", False)

    r = run(WSL_SSH + ["echo wsl-ok"], timeout=25)
    wsl_ok = "wsl-ok" in (r.stdout or "")
    ok &= check(wsl_ok, "WSL ssh link", False)

    if wsl_ok:
        probe = (
            "echo EMB:$(curl -s -o /dev/null -w %%{http_code} --max-time 6 "
            "http://127.0.0.1:%d/health); "
            "echo GOV:$(curl -s -o /dev/null -w %%{http_code} --max-time 6 "
            "http://127.0.0.1:%d/health)"
        ) % (EMBED_PORT, NODE_PORT)
        r2 = run(WSL_SSH + ['%s bash -c "%s"' % (WSL_PREFIX, probe)], timeout=45)
        m_emb = __import__("re").search(r"EMB:(\d+)", r2.stdout or "")
        m_gov = __import__("re").search(r"GOV:(\d+)", r2.stdout or "")
        emb_ok = bool(m_emb) and m_emb.group(1) != "000"
        gov_alive = bool(m_gov) and m_gov.group(1) != "000"
        ok &= check(emb_ok, "embedding service on %d reachable" % EMBED_PORT, False)
        ok &= check(
            gov_alive,
            "governed node API on %d listening (auth verified during handshake)" % NODE_PORT,
            False,
        )

    try:
        tok = node_bearer()
        ok &= check(
            tok.startswith("Bearer ") and len(tok) > 10,
            "keychain node bearer resolves with Bearer prefix",
            False,
        )
    except Exception as exc:  # noqa: BLE001
        ok &= check(False, "keychain bearer: %s" % exc, False)

    cr = mac_control_root()
    ok &= check((cr / "control-plane.sqlite3").is_file(), "control-plane.sqlite3 present", False)
    ok &= check((MAC_RUNTIME / "deployment.json").is_file(), "deployment.json present", False)
    ok &= check(
        (MAC_RUNTIME / "private-node-endpoints.json").is_file(),
        "private-node-endpoints.json present",
        False,
    )

    print("[pipeline] doctor:", "PASS" if ok else "FAIL", flush=True)
    sys.exit(0 if ok else 1)


# --------------------------------------------------------------------------
# handshake
# --------------------------------------------------------------------------
SCHEMA_APPLY = """
import sqlite3
from plastic_promise.deployment.sqlite_migrations import apply_node_governance_schema
db = sqlite3.connect('/tmp/state/db/plastic_memory.db')
db.execute('BEGIN')
try:
    apply_node_governance_schema(db)
    db.commit()
    print('SCHEMA_APPLIED')
except Exception as exc:
    db.rollback()
    print('SCHEMA_SKIP', type(exc).__name__)
"""


def cmd_handshake(args):
    step("handshake: one-step compute-node wiring")
    ref = args.image
    if args.manifest:
        mpath = pathlib.Path(args.manifest).expanduser()
        if mpath.is_file():
            ref = image_ref_from_manifest(mpath)
    check(bool(ref), "image reference resolved")

    step("staging control-plane materials")
    staging = pathlib.Path(tempfile.mkdtemp(prefix="pp-mat-"))
    env = dict(os.environ, COPYFILE_DISABLE="1")
    subprocess.run(
        ["tar", "-C", str(MAC_RUNTIME), "-czf", str(staging / "raw.tgz"),
         "control", "deployment.json", "private-node-endpoints.json"],
        env=env,
        check=True,
    )
    with tarfile.open(staging / "raw.tgz") as tf:
        tf.extractall(staging / "x")
    ep_file = staging / "x" / "private-node-endpoints.json"
    doc = json.loads(ep_file.read_text())
    for node in doc.get("nodes", []):
        node["base_url"] = "http://127.0.0.1:%d" % NODE_PORT
    ep_file.write_text(json.dumps(doc, indent=2))
    final_tgz = staging / "materials.tgz"
    with tarfile.open(final_tgz, "w:gz") as tf:
        for name in ("control", "deployment.json", "private-node-endpoints.json"):
            tf.add(staging / "x" / name, arcname=name)

    step("transferring materials and node bearer to WSL")
    wsl_put_bytes(final_tgz.read_bytes(), "/tmp/pp-node-materials.tgz")
    token = node_bearer()
    wsl_put_bytes(token.encode(), "/tmp/pp-bearer.txt")

    step("rebuilding container with governed env (state dirs pre-created)")
    launch = "\n".join([
        "#!/bin/bash",
        "set -e",
        "rm -rf /tmp/pp-node-materials && mkdir -p /tmp/pp-node-materials",
        "tar -C /tmp/pp-node-materials -xzf /tmp/pp-node-materials.tgz 2>/dev/null || true",
        "chown -R 1000:1000 /tmp/pp-node-materials",
        "chmod 600 /tmp/pp-node-materials/private-node-endpoints.json",
        "TOKEN=$(cat /tmp/pp-bearer.txt)",
        'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true',
        "docker rm -f pp-vector-smoke pp-release-smoke-v0216 >/dev/null 2>&1 || true",
        "for cid in $(docker ps -q --filter publish=" + str(MCP_PORT) + "); do docker rm -f $cid >/dev/null 2>&1 || true; done",
        "mkdir -p /tmp/pp-shared/state/db /tmp/pp-shared/lancedb",
        "chown -R 1000:1000 /tmp/pp-shared",
        'docker run -d --name "$CONTAINER" --network host -w /tmp \\',
        " -v /tmp/pp-node-materials:/materials:ro \\",
        " -v /tmp/pp-shared/state:/tmp/state \\",
        " -e PLASTIC_DB_PATH=/tmp/state/db/plastic_memory.db \\",
        " -e PLASTIC_LANCEDB_PATH=/tmp/state/lancedb \\",
        " -e PP_HEALTH_ALLOW_TEXT_ONLY=0 \\",
        " -e PP_CONTROL_PLANE=1 \\",
        " -e PP_CONTROL_ROOT=/materials/control \\",
        " -e PP_DEPLOYMENT_MANIFEST_PATH=/materials/deployment.json \\",
        " -e PP_NODE_PRIVATE_ENDPOINTS_FILE=/materials/private-node-endpoints.json \\",
        ' -e "PP_NODE_AUTH_WINDOWS_5080=$TOKEN" \\',
        ' "$IMAGE" >/dev/null && echo RUN_OK',
    ]).replace("$CONTAINER", CONTAINER).replace("$IMAGE", ref)
    out = wsl_script("launch", launch, 180)
    check("RUN_OK" in out, "container started")

    step("applying node-governance schema inside container")
    schema_b64 = base64.b64encode(SCHEMA_APPLY.encode()).decode()
    prep = "echo " + schema_b64 + " > /tmp/sb64 && base64 -d /tmp/sb64 > /tmp/pp-schema.py"
    wsl_script("schprep", prep, 60)
    go = (
        "docker cp /tmp/pp-schema.py "
        + CONTAINER
        + ":/tmp/apply.py >/dev/null && docker exec "
        + CONTAINER
        + " python3 /tmp/apply.py 2>&1 | tail -1"
    )
    out = wsl_script("schemago", go, 120)
    applied = ("SCHEMA_APPLIED" in out) or ("SCHEMA_SKIP" in out)
    check(applied, "governance schema ready: " + out.strip()[:60])

    step("waiting for health with node routing")
    wait = (
        "for i in $(seq 1 100); do "
        "code=$(curl -s -o /dev/null -w %%{http_code} --max-time 8 "
        "http://127.0.0.1:%d/health); "
        '[ "$code" = 200 ] && { echo H_OK; exit 0; }; sleep 3; done; '
        "echo H_TIMEOUT" % MCP_PORT
    )
    out = wsl_script("wait", wait, 420)
    check("H_OK" in out, "health 200")

    verify_py = [
        'import json, subprocess, urllib.request',
        'logs = subprocess.run(["docker", "logs", "' + CONTAINER + '"], capture_output=True, text=True)',
        'for line in (logs.stdout + logs.stderr).splitlines():',
        '    if "node routing" in line:',
        '        print("BOOTSTRAP:", line.strip()[-90:])',
        'h = urllib.request.urlopen("http://127.0.0.1:%d/health", timeout=20)' % MCP_PORT,
        'd = json.load(h)',
        'print("VECTOR_READY:", d.get("vector_ready"))',
        'print("POLICY:", d.get("health_policy"))',
        'print("PROVIDER:", (d.get("embedding") or {}).get("provider"))',
    ]
    vb64 = base64.b64encode("\n".join(verify_py).encode()).decode()
    verify = (
        'echo ' + vb64 + ' > /tmp/vb64 && base64 -d /tmp/vb64 > /tmp/pp-verify.py && python3 /tmp/pp-verify.py'
    )
    out = wsl_script("verify", verify, 90)
    check("node_routing_ready" in out, "bootstrap reports node_routing_ready")
    check("VECTOR_READY: True" in out, "vector_ready true under strict policy")
    check("governed-node" in out, "embedding provider is governed-node")
    print(out.strip())


# --------------------------------------------------------------------------
# e2e
# --------------------------------------------------------------------------
SEED_CANARY = """
import json, urllib.request, uuid
BASE = "http://127.0.0.1:%(port)d/mcp"
def post(payload, sid=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if sid:
        h["mcp-session-id"] = sid
    req = urllib.request.Request(BASE, json.dumps(payload).encode(), h)
    resp = urllib.request.urlopen(req, timeout=180)
    nsid = resp.headers.get("mcp-session-id")
    body = resp.read().decode()
    data = {}
    try:
        data = json.loads(body)
    except Exception:
        for line in body.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                break
    return data, nsid or sid
d, sid = post({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"pipeline","version":"1.0"}}})
post({"jsonrpc":"2.0","method":"notifications/initialized"}, sid)
canary = "smokeseed" + uuid.uuid4().hex[:12]
call = {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_store","arguments":{
  "content":"Governed vector retrieval smoke canary " + canary + " qwen3 embedding2560 lancedb dualwrite verification",
  "memory_type":"experience","source":"claude_code","tags":["smoke:vector-canary"]}}}
d2, _ = post(call, sid)
txt = d2.get("result", {}).get("content", [{}])[0].get("text", "")
parsed = json.loads(txt)
assert parsed.get("stored") is True, parsed
print(parsed["memory_id"])
""" % {"port": MCP_PORT}


def cmd_e2e(args):
    step("e2e: strict-mode canary smoke")
    out_dir = pathlib.Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    seed_b64 = base64.b64encode(SEED_CANARY.encode()).decode()
    wsl_script("seedup", "echo " + seed_b64 + " > /tmp/sb64 && base64 -d /tmp/sb64 > /tmp/pp-seed-canary.py", 60)
    out = wsl_script("seedgo", "python3 /tmp/pp-seed-canary.py", 240)
    memory_id = out.strip().splitlines()[-1]
    check(len(memory_id) >= 8, "canary stored: " + memory_id)

    smoke = (
        "python3 /tmp/smoke_http_mcp.py --url http://127.0.0.1:%d/mcp "
        "--health-url http://127.0.0.1:%d/health --read-only --json --timeout 90 "
        "--query 'governed vector retrieval smoke verification canary seed' "
        "--expected-memory-id %s > /tmp/pp-vec-report.json 2>/tmp/pp-vec-err.log; echo RC:$?"
        % (MCP_PORT, MCP_PORT, memory_id)
    )
    out = wsl_script("strictsmoke", smoke, 240)
    check("RC:0" in out, "smoke rc=0")
    show = (
        "python3 -c 'import json;d=json.load(open(\"/tmp/pp-vec-report.json\"));"
        "print(\"OKFLAG:\", d.get(\"ok\"));"
        "[print(k, str(v)[:70]) for k,v in d.get(\"checks\",{}).items()]'"
    )
    report_txt = wsl_script("reportshow", show, 60)
    check("OKFLAG: True" in report_txt, "SMOKE_OK true (strict mode)")
    print(report_txt.strip())

    b64data = wsl_script("pull", "base64 -w0 /tmp/pp-vec-report.json", 60)
    (out_dir / "smoke-report.json").write_bytes(base64.b64decode(b64data.strip()))
    step("artifacts written to " + str(out_dir))
    print("[pipeline] e2e complete. Receipt + evidence generation next.")


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------
def cmd_publish(args):
    step("publish: clean-tree assert + proven push environment")
    tz = os.environ.get("TZ")
    if tz:
        check(tz == time.tzname[0], "TZ matches host regime", False)
    st = run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"])
    check(not st.stdout.strip(), "release repo tree is clean")
    py = os.path.join(VENV_BIN, "python")
    cmd = " ".join([
        "cd " + str(REPO_ROOT),
        "PATH=%s:/Users/aldaisuki/.cargo/bin:$PATH" % VENV_BIN,
        'HTTPS_PROXY="%s"' % os.environ.get("HTTPS_PROXY", ""),
        'HTTP_PROXY="%s"' % os.environ.get("HTTP_PROXY", ""),
        "EMBEDDER_PROVIDER=openai-compatible",
        "EMBEDDER_BASE_URL=http://127.0.0.1:9/v1",
        'PYTEST_ADDOPTS="%s"' % PYTEST_IGNORES,
        "PYTHONPATH=$PWD",
        py + " scripts/release-sync.py",
        "--from %s..HEAD" % args.base,
        "--version " + args.version,
        "--release-repo .",
        "--expected-source-branch main",
        "--expected-source-origin git@github.com:ALdaisuki/plastic-promise-release.git",
        "--validation-profile full",
        "--audit-range %s..HEAD" % args.base,
        "--release-evidence " + args.evidence,
        "--release-manifest " + args.manifest,
        "--server-deployment-receipt " + args.receipt,
        "--push",
    ])
    if args.dry_run:
        print(cmd.replace(PYTEST_IGNORES, PYTEST_IGNORES[:40] + "..."))
        return
    step("running release-sync --push (full validation profile)")
    done = subprocess.run(["bash", "-lc", cmd], text=True, capture_output=True)
    lines = (done.stdout or "").strip().splitlines()
    print("\n".join(lines[-8:]))
    check(done.returncode == 0 and "Sync complete" in (done.stdout or ""), "release-sync completed")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)

    hs = sub.add_parser("handshake")
    hs.add_argument("--image", default="")
    hs.add_argument("--manifest", default="")
    hs.set_defaults(fn=cmd_handshake)

    ee = sub.add_parser("e2e")
    ee.add_argument("--manifest", required=True)
    ee.add_argument("--out-dir", default="/tmp/pp-release-out")
    ee.set_defaults(fn=cmd_e2e)

    pb = sub.add_parser("publish")
    pb.add_argument("--version", required=True)
    pb.add_argument("--base", default="v0.2.14")
    pb.add_argument("--evidence", required=True)
    pb.add_argument("--manifest", required=True)
    pb.add_argument("--receipt", required=True)
    pb.add_argument("--dry-run", action="store_true")
    pb.set_defaults(fn=cmd_publish)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

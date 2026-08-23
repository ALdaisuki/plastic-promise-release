#!/usr/bin/env python3
"""Handler runtime-binding census.

Mechanically lists every MCP handler dispatched by server.py and whether the
handler signature accepts _runtime_context and/or the dispatch site passes it.
Output feeds the declared-fields-as-authority audit (see PR #15 review notes).

Usage: python scripts/handler_binding_census.py [repo_root]
"""

import json
import re
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path.cwd()).resolve()
    tools = root / "plastic_promise" / "mcp" / "tools"
    server = (root / "plastic_promise" / "mcp" / "server.py").read_text()

    sig: dict = {}
    for f in sorted(tools.rglob("*.py")):
        src = f.read_text()
        rel = f.relative_to(root).as_posix()
        for m in re.finditer(r"async def (handle_\w+)\(([^)]*)\)", src):
            sig.setdefault(m.group(1), []).append((rel, "_runtime_context" in m.group(2)))

    guards = [(m.start(), m.group(1)) for m in re.finditer(r'name == "([^"]+)"', server)]
    rows = []
    for m in re.finditer(r"return await (handle_\w+)\(([^)]*)\)", server):
        fn, argexpr = m.group(1), m.group(2)
        tool = None
        for pos, g in guards:
            if pos < m.start():
                tool = g
            else:
                break
        rows.append({"tool": tool, "fn": fn, "passes_ctx": "_runtime_context" in argexpr})

    out, seen = [], set()
    for r in rows:
        entries = sig.get(r["fn"])
        if not entries or r["fn"] in seen:
            continue
        seen.add(r["fn"])
        out.append(
            {
                "tool": r["tool"],
                "handler": r["fn"],
                "file": entries[0][0],
                "sig_has_ctx": any(c for _, c in entries),
                "dispatch_passes_ctx": r["passes_ctx"],
            }
        )

    no_ctx = [o for o in out if not o["sig_has_ctx"] and not o["dispatch_passes_ctx"]]
    mismatch = [o for o in out if o["sig_has_ctx"] != o["dispatch_passes_ctx"]]
    print(
        json.dumps(
            {
                "total_handlers_dispatched": len(out),
                "no_runtime_binding": len(no_ctx),
                "signature_vs_dispatch_mismatch": len(mismatch),
            }
        )
    )
    print("--- NO BINDING ---")
    for o in no_ctx:
        print(o["tool"], "|", o["handler"], "|", o["file"])
    print("--- MISMATCH ---")
    for o in mismatch:
        print(
            o["tool"],
            "|",
            o["handler"],
            "|",
            o["file"],
            "| sig:",
            o["sig_has_ctx"],
            "| dispatch:",
            o["dispatch_passes_ctx"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

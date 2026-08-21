#!/usr/bin/env python3
"""One-click compute-node private-transport handshake.

Establishes (or verifies) the SSH local forward that carries the governed
node retrieval route, probes the node health endpoint through the exact
private path the canonical runtime consumes, and optionally restarts the
launchd-managed canonical runtime so node routing re-bootstraps.

Design constraints:
- Standard library only: safe to run from any deployment checkout without
  importing MCP or service runtime code (mirrors sqlite_migrations.py).
- Never prints bearer material; tokens are resolved from the process
  environment or the macOS Keychain and reported only by source label.
- Idempotent: an existing healthy listener short-circuits to success.

The endpoint contract (transport_id/base_url/authorization_env) is read from
the deployment private-node-endpoints.json referenced by
PP_NODE_PRIVATE_ENDPOINTS_FILE; --endpoints overrides.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

DEFAULT_KEYCHAIN_SERVICE = "plastic-promise-compute-windows-5080"
DEFAULT_RUNTIME_SERVICE = "org.plastic-promise.mac-canonical-runtime"
DEFAULT_SSH_USER = "plastic"
DEFAULT_SSH_KEY = "~/.ssh/id_ed25519_plastic_promise"
COMMON_REMOTE_PORTS = (8080, 8000, 1337, 5000, 11434, 7860, 3000, 9000)
SSH_FORWARDED_PREFIX = "ssh-local-forward-"


# ---------------------------------------------------------------- pure helpers


def parse_local_port(base_url: str) -> int:
    """Extract the loopback listen port from a node base_url."""

    port = urlparse(base_url).port
    if port is None:
        raise ValueError("endpoint_base_url_port_missing")
    return int(port)

    tail = base_url.rsplit(":", 1)[-1]
    digits = "".join(ch for ch in tail if ch.isdigit())
    if not digits:
        raise ValueError("endpoint_base_url_port_missing")
    return int(digits)


def is_ssh_local_forward(transport_id: str) -> bool:
    return transport_id.startswith(SSH_FORWARDED_PREFIX)


def load_nodes(endpoints_path: Path) -> list[dict[str, Any]]:
    with endpoints_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("endpoints_file_nodes_missing")
    return [node for node in nodes if isinstance(node, dict)]


def select_node(nodes: list[dict[str, Any]], node_id: str | None) -> dict[str, Any]:
    for node in nodes:
        if node_id is None or node.get("node_id") == node_id:
            return node
    raise ValueError("endpoint_node_not_found")


def build_ssh_command(
    *, host: str, user: str, key: Path, local_port: int, remote_port: int
) -> list[str]:
    return [
        "/usr/bin/ssh",
        "-F", "/dev/null",
        "-f", "-N", "-T",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "IdentitiesOnly=yes",
        "-o", "AddKeysToAgent=yes",
        "-o", "UseKeychain=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-i", str(key),
        "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        f"{user}@{host}",
    ]


def bearer_token(authorization_env: str, keychain_service: str) -> tuple[str | None, str]:
    """Resolve the node bearer from env, then Keychain. Never log the value."""

    value = os.environ.get(authorization_env, "").strip()
    if value:
        return value, "environment"
    try:
        completed = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", keychain_service, "-w"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "keychain_error"
    if completed.returncode == 0:
        token = completed.stdout.strip()
        if token:
            return token, "keychain"
    return None, "unavailable"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------- probe I/O


def probe_health(base_url: str, token: str | None, timeout: float) -> dict[str, Any]:
    """GET {base_url}/health through the private path, mirroring the runtime."""

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        base_url.rstrip("/") + "/health", headers=headers, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return {"status_code": response.status, "body_head": body[:200], "ok": True}
    except urllib.error.HTTPError as exc:
        return {
            "status_code": exc.code,
            "body_head": exc.read(200).decode("utf-8", errors="replace"),
            "ok": False,
        }
    except Exception as exc:
        return {"status_code": None, "body_head": str(exc)[:200], "ok": False}


def discover_remote_ports(host: str, user: str, key: Path) -> list[int]:
    """Best-effort remote listener discovery via Windows netstat, then fallbacks."""

    candidates: list[int] = []
    try:
        completed = subprocess.run(
            [
                "/usr/bin/ssh",
                "-F", "/dev/null",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=accept-new",
                "-i", str(key),
                user + "@" + host,
                "netstat -an | findstr LISTENING",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        for line in completed.stdout.splitlines():
            for part in line.split():
                if ":" in part:
                    tail = part.rsplit(":", 1)[-1]
                    if tail.isdigit():
                        port = int(tail)
                        if port in COMMON_REMOTE_PORTS and port not in candidates:
                            candidates.append(port)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for port in COMMON_REMOTE_PORTS:
        if port not in candidates:
            candidates.append(port)
    return candidates


# --------------------------------------------------------------------- actions


def establish_tunnel(
    *, host: str, user: str, key: Path, local_port: int, remote_port: int
) -> dict[str, Any]:
    if port_open(local_port):
        return {"state": "already_listening", "ok": True}
    command = build_ssh_command(
        host=host, user=user, key=key, local_port=local_port, remote_port=remote_port
    )
    errpath = tempfile.mkstemp(suffix=".err")[1]
    try:
        with open(errpath, "w+t") as errfile:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=errfile,
                text=True,
                check=False,
                timeout=30,
            )
        if completed.returncode != 0:
            with open(errpath, "r+t") as errfile:
                detail = (errfile.read() or completed.stderr or "").strip().splitlines()
            reason = detail[-1] if detail else "ssh_forward_failed"
            return {"state": "ssh_failed", "ok": False, "reason": reason[:200]}
    finally:
        try:
            os.unlink(errpath)
        except OSError:
            pass
    deadline = time.time() + 10
    while time.time() < deadline:
        if port_open(local_port):
            return {"state": "established", "ok": True}
        time.sleep(0.5)
    return {"state": "listener_timeout", "ok": False, "reason": "tunnel_listener_timeout"}


def restart_runtime(service: str) -> dict[str, Any]:
    target = "gui/" + str(os.getuid()) + "/" + service
    try:
        completed = subprocess.run(
            ["/bin/launchctl", "kickstart", "-k", target],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        return {
            "performed": completed.returncode == 0,
            "service": service,
            "reason": None if completed.returncode == 0 else "kickstart_failed",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"performed": False, "service": service, "reason": str(exc)[:120]}


# ------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compute_node_handshake",
        description="One-click compute-node private-transport handshake.",
    )
    parser.add_argument("--endpoints", type=Path, default=None)
    parser.add_argument("--node-id", default=None)
    parser.add_argument("--node", dest="host", default=None, help="physical node host")
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER)
    parser.add_argument("--ssh-key", type=Path, default=Path(DEFAULT_SSH_KEY).expanduser())
    parser.add_argument("--remote-port", type=int, default=None)
    parser.add_argument("--mode", choices=("establish", "verify"), default="establish")
    parser.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE)
    parser.add_argument("--restart-runtime", action="store_true")
    parser.add_argument("--runtime-service", default=DEFAULT_RUNTIME_SERVICE)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)

    verdict: dict[str, Any] = {"tool": "compute_node_handshake", "ok": False}
    try:
        endpoints_path = args.endpoints or os.environ.get("PP_NODE_PRIVATE_ENDPOINTS_FILE")
        if not endpoints_path:
            raise ValueError("endpoints_source_unspecified")
        nodes = load_nodes(Path(endpoints_path).expanduser())
        node = select_node(nodes, args.node_id)
        node_id = str(node.get("node_id"))
        transport_id = str(node.get("transport_id"))
        base_url = str(node.get("base_url"))
        verdict.update({"node_id": node_id, "transport_id": transport_id, "base_url": base_url})

        if not is_ssh_local_forward(transport_id):
            raise ValueError("transport_not_ssh_local_forward")
        local_port = parse_local_port(base_url)
        verdict["local_port"] = local_port

        authorization_env = str(node.get("authorization_env") or "")
        token, auth_source = bearer_token(authorization_env, args.keychain_service)
        verdict["auth_source"] = auth_source

        if args.mode == "verify":
            tunnel = {"state": "verify_only", "ok": port_open(local_port)}
        else:
            if not args.host:
                raise ValueError("node_host_required_for_establish")
            remote_port = args.remote_port
            if remote_port is None and not port_open(local_port):
                discovered = discover_remote_ports(args.host, args.ssh_user, args.ssh_key)
                remote_port = discovered[0] if discovered else None
                verdict["discovered_remote_ports"] = discovered[:5]
            if remote_port is None:
                raise ValueError("remote_port_unresolved")
            verdict["remote_port"] = remote_port
            tunnel = establish_tunnel(
                host=args.host,
                user=args.ssh_user,
                key=args.ssh_key,
                local_port=local_port,
                remote_port=remote_port,
            )
        verdict["tunnel"] = tunnel

        probe = probe_health(base_url, token, args.timeout)
        verdict["probe"] = probe

        ok = bool(tunnel.get("ok")) and bool(probe.get("ok"))
        verdict["ok"] = ok

        if ok and args.restart_runtime:
            verdict["restart"] = restart_runtime(args.runtime_service)
        elif args.restart_runtime:
            verdict["restart"] = {"performed": False, "reason": "handshake_not_ok"}
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        verdict["ok"] = False
        verdict["reason"] = str(exc)[:200]

    print(json.dumps(verdict, ensure_ascii=False))
    return 0 if verdict.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

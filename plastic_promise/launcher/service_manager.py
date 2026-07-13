"""Service lifecycle manager for One-Click Launcher.

Orchestrates start/stop of MCP Server and Maintenance Daemon with
dependency ordering, health checks, and crash recovery support.
"""

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from plastic_promise.core.fusion_policy import canonical_fusion_config_hash
from plastic_promise.launcher.service_definition import (
    ServiceDefinition,
    ServiceStatus,
)
from plastic_promise.launcher.subprocess_utils import hidden_subprocess_kwargs

MAINTENANCE_HEARTBEAT_SCHEMA = "maintenance-heartbeat/v1"
MCP_FUSION_IDENTITY_SCHEMA = "retrieval-fusion-identity/v1"


def canonical_source_root(path: str | Path) -> str:
    """Return a stable, platform-aware source-root identity."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def resolve_source_revision(source_root: str | Path) -> str | None:
    """Resolve the checked-out Git revision when source control is available."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=canonical_source_root(source_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-fA-F]{40,64}", revision) is None:
        return None
    return revision.lower()


def validate_mcp_health_identity(
    payload: object,
    *,
    expected_pid: int | None = None,
    expected_source_root: str | Path,
    expected_source_revision: str | None = None,
) -> tuple[bool, str]:
    """Validate that an MCP health response belongs to the expected checkout/process."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False, "health_not_ok"
    pid = payload.get("pid")
    if type(pid) is not int or pid <= 0:
        return False, "health_pid_invalid"
    if expected_pid is not None and pid != expected_pid:
        return False, "health_pid_mismatch"

    source_root = payload.get("source_root")
    if not isinstance(source_root, str) or not source_root.strip():
        return False, "health_source_root_missing"
    if canonical_source_root(source_root) != canonical_source_root(expected_source_root):
        return False, "health_source_root_mismatch"

    if expected_source_revision is None:
        return False, "expected_source_revision_missing"
    source_revision = payload.get("source_revision")
    if (
        not isinstance(source_revision, str)
        or re.fullmatch(r"[0-9a-f]{40,64}", source_revision) is None
    ):
        return False, "health_source_revision_missing"
    if source_revision != expected_source_revision:
        return False, "health_source_revision_mismatch"

    fusion_policy = payload.get("fusion_policy")
    if (
        not isinstance(fusion_policy, str)
        or re.fullmatch(r"(?:legacy-auto|max-v1|wrrf-v1:[0-9a-f]{64})", fusion_policy) is None
    ):
        return False, "health_fusion_policy_invalid"
    attestation = payload.get("fusion_attestation")
    if not isinstance(attestation, dict):
        return False, "health_fusion_attestation_missing"
    if (
        attestation.get("schema") != MCP_FUSION_IDENTITY_SCHEMA
        or attestation.get("requested_policy") != fusion_policy
        or attestation.get("effective_policy") != fusion_policy
    ):
        return False, "health_fusion_attestation_mismatch"
    requested_runtime = attestation.get("requested_runtime")
    effective_runtime = attestation.get("effective_runtime")
    capability_reason = attestation.get("capability_reason")
    if (
        requested_runtime not in {"python", "rust"}
        or effective_runtime not in {"python", "rust"}
        or not isinstance(capability_reason, str)
    ):
        return False, "health_fusion_runtime_invalid"
    valid_runtime_attestations = {
        ("python", "python", "runtime_forced:python"),
        ("python", "python", "runtime_preferred:python"),
    }
    if fusion_policy == "max-v1":
        valid_runtime_attestations.add(("rust", "python", "policy_requires_python:max-v1"))
    else:
        valid_runtime_attestations.update(
            {
                ("rust", "rust", "rust_capability_satisfied"),
                ("rust", "python", "rust_unavailable_or_failed"),
            }
        )
        if fusion_policy.startswith("wrrf-v1:"):
            valid_runtime_attestations.add(("rust", "python", "rust_capability_missing:fts"))
    if (requested_runtime, effective_runtime, capability_reason) not in valid_runtime_attestations:
        return False, "health_fusion_capability_mismatch"
    candidate_id = fusion_policy if fusion_policy.startswith("wrrf-v1:") else ""
    config_hash = fusion_policy.partition(":")[2] if candidate_id else ""
    if (
        attestation.get("candidate_id") != candidate_id
        or attestation.get("config_hash") != config_hash
    ):
        return False, "health_fusion_attestation_mismatch"
    config = attestation.get("config")
    if candidate_id:
        if not isinstance(config, dict):
            return False, "health_fusion_config_missing"
        try:
            canonical_hash = canonical_fusion_config_hash(
                {
                    "k": config["k"],
                    "channels": config["channels"],
                    "weights": config["weights"],
                    "windows": config["windows"],
                }
            )
        except (KeyError, TypeError, ValueError):
            return False, "health_fusion_config_invalid"
        if canonical_hash != config_hash or config.get("config_hash") != config_hash:
            return False, "health_fusion_config_hash_mismatch"
    elif config is not None:
        return False, "health_fusion_config_unexpected"
    return True, "ok"


def pid_is_alive(pid: int) -> bool:
    """Return whether a PID exists, without trusting heartbeat freshness."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def write_maintenance_heartbeat(
    path: str | Path,
    *,
    pid: int,
    startup_replay_cycle_id: str,
    process_generation: str | None = None,
    updated_at: datetime | str | None = None,
) -> None:
    """Atomically publish structured daemon liveness evidence."""
    timestamp = updated_at or datetime.now(timezone.utc)
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp_text = timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        timestamp_text = str(timestamp)
    generation = str(process_generation or os.environ.get("PLASTIC_PROCESS_GENERATION") or "")
    if re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise ValueError("maintenance_process_generation_invalid")
    payload = {
        "schema": MAINTENANCE_HEARTBEAT_SCHEMA,
        "pid": pid,
        "updated_at": timestamp_text,
        "startup_replay_cycle_id": str(startup_replay_cycle_id or ""),
        "startup_replay_owner_pid": pid,
        "process_generation": generation,
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def read_maintenance_health(
    path: str | Path,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 120.0,
    expected_pid: int | None = None,
    expected_process_generation: str | None = None,
) -> dict[str, object]:
    """Validate daemon heartbeat schema, PID liveness, and then freshness."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return {"healthy": False, "reason": "maintenance_heartbeat_invalid"}
    if not isinstance(payload, dict) or payload.get("schema") != MAINTENANCE_HEARTBEAT_SCHEMA:
        return {"healthy": False, "reason": "maintenance_heartbeat_invalid"}
    pid = payload.get("pid")
    if type(pid) is not int or pid <= 0:
        return {"healthy": False, "reason": "maintenance_heartbeat_invalid"}
    if not pid_is_alive(pid):
        return {"healthy": False, "reason": "maintenance_pid_not_alive", "pid": pid}
    if expected_pid is not None and pid != expected_pid:
        return {"healthy": False, "reason": "maintenance_pid_mismatch", "pid": pid}
    startup_cycle = str(payload.get("startup_replay_cycle_id") or "")
    if not startup_cycle:
        return {
            "healthy": False,
            "reason": "maintenance_startup_replay_incomplete",
            "pid": pid,
        }
    startup_owner_pid = payload.get("startup_replay_owner_pid")
    if type(startup_owner_pid) is not int or startup_owner_pid != pid:
        return {
            "healthy": False,
            "reason": "maintenance_startup_replay_owner_mismatch",
            "pid": pid,
        }
    process_generation = payload.get("process_generation")
    if (
        not isinstance(process_generation, str)
        or re.fullmatch(r"[0-9a-f]{32}", process_generation) is None
        or expected_process_generation is None
        or process_generation != expected_process_generation
    ):
        return {
            "healthy": False,
            "reason": "maintenance_process_generation_mismatch",
            "pid": pid,
        }
    try:
        updated_at = datetime.fromisoformat(str(payload["updated_at"]).replace("Z", "+00:00"))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return {"healthy": False, "reason": "maintenance_heartbeat_invalid", "pid": pid}
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=timezone.utc)
    age = (
        observed_now.astimezone(timezone.utc) - updated_at.astimezone(timezone.utc)
    ).total_seconds()
    if age < 0 or age >= max_age_seconds:
        return {
            "healthy": False,
            "reason": "maintenance_heartbeat_stale",
            "pid": pid,
            "age_seconds": age,
        }
    return {
        "healthy": True,
        "reason": "ok",
        "pid": pid,
        "age_seconds": age,
        "startup_replay_cycle_id": startup_cycle,
        "startup_replay_owner_pid": startup_owner_pid,
        "process_generation": process_generation,
    }


class ServiceRuntime:
    """Runtime state for one service instance."""

    def __init__(self, definition: ServiceDefinition):
        self.definition = definition
        self.status = ServiceStatus.PENDING
        self.process: subprocess.Popen | None = None
        self.pid: int | None = None
        self.restart_timestamps: list[float] = []
        self.consecutive_failures = 0
        self.first_unhealthy_at: float | None = None
        self.process_generation: str | None = None

    def record_restart(self):
        now = time.time()
        self.restart_timestamps.append(now)
        policy = self.definition.restart_policy
        cutoff = now - policy.window_seconds
        self.restart_timestamps = [t for t in self.restart_timestamps if t > cutoff]

    def recent_restarts(self) -> int:
        return len(self.restart_timestamps)

    def is_unrecoverable(self) -> bool:
        return self.recent_restarts() >= self.definition.restart_policy.max_retries

    def backoff_seconds(self) -> float:
        policy = self.definition.restart_policy
        count = self.recent_restarts()
        return min(
            policy.backoff_base * (policy.backoff_multiplier ** max(0, count - 1)),
            policy.max_backoff,
        )


class ServiceManager:
    """Manages lifecycle of multiple services with dependency ordering."""

    def __init__(
        self,
        services: list[ServiceDefinition],
        project_root: str,
        *,
        expected_source_revision: str | None = None,
    ):
        self._runtimes: dict[str, ServiceRuntime] = {
            svc.name: ServiceRuntime(svc) for svc in services
        }
        self._project_root = os.path.realpath(os.path.abspath(project_root))
        self._expected_source_revision = expected_source_revision

    def _mcp_pid_path(self) -> Path:
        return Path(self._project_root) / "var" / "run" / "mcp_server.pid"

    def _write_mcp_pid(self, pid: int) -> None:
        destination = self._mcp_pid_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".pid.tmp")
        temporary.write_text(str(pid), encoding="ascii")
        os.replace(temporary, destination)

    def _remove_mcp_pid(self, pid: int | None) -> None:
        path = self._mcp_pid_path()
        try:
            recorded = int(path.read_text(encoding="ascii").strip())
        except (OSError, TypeError, ValueError):
            return
        if pid is not None and recorded != pid:
            return
        with suppress(OSError):
            path.unlink()

    def get_status(self) -> dict[str, ServiceStatus]:
        return {name: rt.status for name, rt in self._runtimes.items()}

    def _topological_order(self) -> list[ServiceRuntime]:
        """Return runtimes in dependency order (dependencies first).
        Raises ValueError if a dependency cycle is detected.
        """
        white, gray, black = 0, 1, 2
        color = dict.fromkeys(self._runtimes, white)
        order = []

        def visit(name: str, path: list[str]):
            color[name] = gray
            path.append(name)
            rt = self._runtimes[name]
            for dep in rt.definition.depends_on:
                if dep not in self._runtimes:
                    continue
                if color[dep] == gray:
                    cycle_start = path.index(dep)
                    cycle = " -> ".join(path[cycle_start:] + [dep])
                    raise ValueError(f"Circular dependency detected: {cycle}")
                if color[dep] == white:
                    visit(dep, path)
            path.pop()
            color[name] = black
            order.append(rt)

        for name in self._runtimes:
            if color[name] == white:
                visit(name, [])

        return order  # post-order = dependencies before dependents

    async def start_all(self, log_file: str | None = None):
        """Start all services in dependency order."""
        for rt in self._topological_order():
            await self._start_service(rt, log_file)
            # Cascade failure
            if rt.status == ServiceStatus.FAILED:
                for other_rt in self._runtimes.values():
                    if (
                        rt.definition.name in other_rt.definition.depends_on
                        and other_rt.status == ServiceStatus.PENDING
                    ):
                        other_rt.status = ServiceStatus.FAILED
                        self._log(
                            f"[START] {other_rt.definition.name} ..... FAILED"
                            f" (dependency {rt.definition.name} failed)",
                            log_file,
                        )

    async def _start_service(self, rt: ServiceRuntime, log_file: str | None = None):
        """Start a single service and wait for health check."""
        svc = rt.definition
        rt.status = ServiceStatus.STARTING
        self._log(f"[START] {svc.name} .................... starting...", log_file)

        # Run pre-start commands
        for cmd in svc.pre_start:
            try:
                subprocess.run(
                    cmd,
                    shell=True,
                    cwd=self._project_root,
                    timeout=30,
                    check=True,
                    **hidden_subprocess_kwargs(),
                )
            except Exception as e:
                self._log(
                    f"[START] {svc.name} .................... FAILED (pre_start: {e})", log_file
                )
                rt.status = ServiceStatus.FAILED
                return

        # Build environment
        env = os.environ.copy()
        env.update(svc.env)
        rt.process_generation = secrets.token_hex(16)
        env["PLASTIC_PROCESS_GENERATION"] = rt.process_generation
        existing_pythonpath = env.get("PYTHONPATH")
        pythonpath_parts = [self._project_root]
        if existing_pythonpath:
            pythonpath_parts.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

        # Launch process
        try:
            cwd = self._project_root
            kwargs: dict = {"env": env, "cwd": cwd}
            if sys.platform == "win32":
                kwargs.update(hidden_subprocess_kwargs(new_process_group=True))
            else:
                kwargs["preexec_fn"] = os.setsid
            rt.process = subprocess.Popen(svc.command, **kwargs)
            rt.pid = rt.process.pid
            if svc.name == "mcp-server":
                self._write_mcp_pid(rt.pid)
        except Exception as e:
            self._log(f"[START] {svc.name} .................... FAILED (spawn: {e})", log_file)
            if rt.process is not None:
                self._stop_service(rt)
            rt.status = ServiceStatus.FAILED
            return

        # Health check loop
        deadline = time.time() + svc.startup_timeout
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            healthy = await self._health_check(rt)
            if healthy:
                rt.status = ServiceStatus.HEALTHY
                rt.consecutive_failures = 0
                self._log(
                    f"[START] {svc.name} .................... healthy (pid={rt.pid})", log_file
                )
                return

            # Check if process died during startup
            if rt.process and rt.process.poll() is not None:
                self._log(
                    f"[START] {svc.name} .................... FAILED"
                    f" (exit code={rt.process.returncode})",
                    log_file,
                )
                rt.status = ServiceStatus.FAILED
                return

        # Timeout
        self._log(
            f"[START] {svc.name} .................... FAILED (timeout {svc.startup_timeout}s)",
            log_file,
        )
        self.stop_service(rt.definition.name)
        rt.status = ServiceStatus.FAILED

    async def _health_check(self, rt: ServiceRuntime) -> bool:
        """Check if a service is healthy.

        Priority:
        1. HTTP health endpoint (MCP also requires checkout/process identity)
        2. Heartbeat file freshness (for daemon without HTTP endpoint)
        3. PID alive check (fallback)
        """
        svc = rt.definition

        # 1. HTTP health check
        if svc.health_url:
            try:
                req = urllib.request.Request(svc.health_url)
                with urllib.request.urlopen(req, timeout=3) as response:
                    if response.status != 200:
                        return False
                    if svc.name != "mcp-server":
                        return True
                    payload = json.loads(response.read().decode("utf-8"))
                valid, _reason = validate_mcp_health_identity(
                    payload,
                    expected_pid=rt.pid,
                    expected_source_root=self._project_root,
                    expected_source_revision=self._expected_source_revision,
                )
                return valid
            except Exception:
                return False

        # 2. Heartbeat file check (for daemon)
        if svc.name == "maintenance-daemon":
            heartbeat_path = os.path.join(
                self._project_root, "var", "run", "maintenance_daemon.heartbeat"
            )
            health = read_maintenance_health(
                heartbeat_path,
                expected_pid=rt.pid,
                expected_process_generation=rt.process_generation,
            )
            if health.get("healthy") is not True:
                return False
            return (
                rt.pid is not None
                and health.get("startup_replay_owner_pid") == rt.pid
                and health.get("process_generation") == rt.process_generation
                and self._pid_alive(rt.pid)
            )

        # 3. PID alive check
        if rt.pid is not None:
            return self._pid_alive(rt.pid)

        return False

    def _pid_alive(self, pid: int) -> bool:
        """Check if a PID is alive. psutil preferred, fallback to platform tools."""
        return pid_is_alive(pid)

    def stop_service(self, name: str):
        """Public method: stop a single service by name."""
        rt = self._runtimes.get(name)
        if rt is not None:
            self._stop_service(rt)

    def _stop_service(self, rt: ServiceRuntime):
        """Stop a single service. Terminate (5s grace) -> kill."""
        stopped_pid = rt.pid
        if rt.process is None or rt.process.poll() is not None:
            if rt.definition.name == "mcp-server":
                self._remove_mcp_pid(stopped_pid)
            rt.status = ServiceStatus.STOPPED
            rt.pid = None
            rt.process_generation = None
            return

        try:
            if sys.platform == "win32":
                rt.process.terminate()
            else:
                rt.process.terminate()

            try:
                rt.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rt.process.kill()
                with suppress(subprocess.TimeoutExpired):
                    rt.process.wait(timeout=2)
        except Exception:
            with suppress(Exception):
                rt.process.kill()

        rt.status = ServiceStatus.STOPPED
        if rt.definition.name == "mcp-server":
            self._remove_mcp_pid(stopped_pid)
        rt.pid = None
        rt.process_generation = None

    async def stop_all(self, log_file: str | None = None):
        """Stop all services in reverse dependency order."""
        for rt in reversed(self._topological_order()):
            if rt.status in (ServiceStatus.HEALTHY, ServiceStatus.STARTING, ServiceStatus.FAILED):
                self._log(
                    f"[STOP]  {rt.definition.name} .................... stopping...", log_file
                )
                self._stop_service(rt)
                self._log(f"[STOP]  {rt.definition.name} .................... stopped", log_file)

    def reset_service(self, name: str, log_file: str | None = None):
        """Reset a service from UNRECOVERABLE back to STOPPED, clearing restart history."""
        rt = self._runtimes.get(name)
        if rt is None:
            return
        rt.status = ServiceStatus.STOPPED
        rt.restart_timestamps.clear()
        rt.consecutive_failures = 0
        self._log(f"[RESET] {name} .................... reset (manual)", log_file)

        # Cascade: flag dependents as eligible for restart
        for other_name, other_rt in self._runtimes.items():
            if name in other_rt.definition.depends_on and other_rt.status == ServiceStatus.FAILED:
                self._log(
                    f"[RESET] {other_name} .................... eligible for restart", log_file
                )

    @staticmethod
    def _log(message: str, log_file: str | None = None):
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        full_msg = f"[{timestamp}] {message}"
        print(full_msg)
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(full_msg + "\n")
            except Exception:
                pass

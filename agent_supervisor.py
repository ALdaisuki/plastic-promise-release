"""Agent Supervisor — Pi Agent 进程生命周期管理

启动: python agent_supervisor.py start --all
Claude 控制: issue_create(type="agent_control", action="start", role="builder")
"""

import os
import sys
import time
import signal
import subprocess
import logging
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Supervisor] %(message)s")
logger = logging.getLogger("supervisor")

AGENTS = {
    "pi_builder":  {"port": 9021, "role": "builder",  "cmd": ["python", "-m", "plastic_promise.agent"]},
    "pi_fixer":    {"port": 9022, "role": "fixer",    "cmd": ["python", "-m", "plastic_promise.agent"]},
    "pi_reviewer": {"port": 9023, "role": "reviewer", "cmd": ["python", "-m", "plastic_promise.agent"]},
}

HEALTH_CHECK_INTERVAL = 30  # 秒


class AgentSupervisor:
    """管理所有 Pi Agent 进程的生命周期。"""

    def __init__(self):
        self._processes: dict[str, subprocess.Popen] = {}
        self._running = False

    def start_agent(self, name: str) -> bool:
        """启动一个 Agent 进程。"""
        if name not in AGENTS:
            logger.error(f"未知 Agent: {name}")
            return False
        if name in self._processes and self._processes[name].poll() is None:
            logger.warning(f"Agent {name} 已在运行 (PID {self._processes[name].pid})")
            return False

        cfg = AGENTS[name]
        env = os.environ.copy()
        env["AGENT_OWNER"] = name
        env["PLASTIC_MCP_URL"] = f"http://127.0.0.1:{cfg['port']}/sse"

        proc = subprocess.Popen(
            cfg["cmd"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._processes[name] = proc
        logger.info(f"Agent {name} 启动 (PID {proc.pid}, port {cfg['port']})")
        return True

    def stop_agent(self, name: str) -> bool:
        """优雅关闭一个 Agent。"""
        if name not in self._processes:
            logger.warning(f"Agent {name} 未运行")
            return False
        proc = self._processes[name]
        if proc.poll() is not None:
            logger.info(f"Agent {name} 已退出")
            del self._processes[name]
            return True

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            logger.warning(f"Agent {name} 强制关闭 (PID {proc.pid})")
        del self._processes[name]
        logger.info(f"Agent {name} 已关闭")
        return True

    def start_all(self):
        """启动所有 Agent。"""
        logger.info("启动所有 Agent...")
        for name in AGENTS:
            self.start_agent(name)
        self._running = True
        self._health_loop()

    def stop_all(self):
        """关闭所有 Agent。"""
        logger.info("关闭所有 Agent...")
        self._running = False
        for name in list(self._processes.keys()):
            self.stop_agent(name)

    def _health_loop(self):
        """健康检查循环。"""
        while self._running:
            for name, proc in list(self._processes.items()):
                if proc.poll() is not None:
                    logger.warning(f"Agent {name} 异常退出 (code {proc.returncode}) — auto restart")
                    del self._processes[name]
                    self.start_agent(name)
            time.sleep(HEALTH_CHECK_INTERVAL)

    def status(self) -> dict:
        """返回所有 Agent 状态。"""
        result = {}
        for name, cfg in AGENTS.items():
            proc = self._processes.get(name)
            if proc is None:
                result[name] = {"status": "stopped"}
            elif proc.poll() is None:
                result[name] = {"status": "running", "pid": proc.pid, "port": cfg["port"]}
            else:
                result[name] = {"status": "exited", "exit_code": proc.returncode}
        return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agent Supervisor")
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--role", help="指定 Agent 名称或 --all")
    parser.add_argument("--all", action="store_true", help="操作所有 Agent")
    args = parser.parse_args()

    sup = AgentSupervisor()

    if args.action == "start":
        if args.all:
            sup.start_all()
        elif args.role:
            sup.start_agent(args.role)
        else:
            parser.error("需要 --role <name> 或 --all")

    elif args.action == "stop":
        if args.all:
            sup.stop_all()
        elif args.role:
            sup.stop_agent(args.role)
        else:
            parser.error("需要 --role <name> 或 --all")

    elif args.action == "status":
        status = sup.status()
        for name, s in status.items():
            print(f"  {name:15s} {s['status']}")


if __name__ == "__main__":
    main()

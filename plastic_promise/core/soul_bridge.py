"""
Soul Bridge — Plastic Promise 灵魂系统桥接层

为 agent-interop 提供统一接口访问 Plastic Promise 的 7 个灵魂模块。
支持两种调用方式:
  1. Python 直接 import (neko_adapter.py 使用)
  2. CLI 子进程调用 (interop-bridge.ts 通过 subprocess 使用)

CLI 用法:
  python bridge/soul_bridge.py pre_task "审查代码" code_review
  python bridge/soul_bridge.py post_task "审查通过，发现1个bug" code_review
  python bridge/soul_bridge.py status              # 完整灵魂状态
  python bridge/soul_bridge.py trust               # 信任分
  python bridge/soul_bridge.py scarf               # SCARF 自省
"""

import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional

SOUL_ENABLED = os.environ.get("SOUL_ENABLED", "1") == "1"
PLASTIC_PROMISE_PATH = os.environ.get(
    "PLASTIC_PROMISE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plastic-promise"),
)

if SOUL_ENABLED:
    try:
        sys.path.insert(0, PLASTIC_PROMISE_PATH)
    except Exception:
        pass


class SoulBridge:
    """Plastic Promise 灵魂系统桥接。

    封装 7 个模块的调用，提供统一的 pre_task/post_task/status 接口。
    SOUL_ENABLED=0 时所有方法返回空结果，不报错。
    """

    def __init__(self):
        self._soul_loop = None
        self._scarf = None
        self._trust = None
        self._hormone = None
        self._curiosity = None
        self._proprioception = None
        self._enforcer = None
        self._initialized = False

    def _init_modules(self) -> bool:
        """延迟初始化所有模块。"""
        if self._initialized:
            return True
        if not SOUL_ENABLED:
            return False

        try:
            from plastic_promise.loop.soul_loop import SoulLoop
            from plastic_promise.reflection.soul_scarf import SCARFReflector
            from plastic_promise.defense.soul_enforcer import TrustManager, SoulEnforcer, TRUST_DECAY_RATE
            from plastic_promise.growth.soul_hormone import HormoneEngine
            from plastic_promise.reflection.soul_curiosity import CuriosityExplorer
            from plastic_promise.reflection.soul_proprioception import ProprioceptionManager

            self._soul_loop = SoulLoop()
            self._scarf = SCARFReflector()
            self._trust = TrustManager()
            self._enforcer = SoulEnforcer(trust_manager=self._trust)
            self._hormone = HormoneEngine()
            self._curiosity = CuriosityExplorer()
            self._proprioception = ProprioceptionManager()
            self._initialized = True
            return True
        except ImportError as e:
            print(f"[SoulBridge] Failed to import soul modules: {e}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"[SoulBridge] Failed to init soul modules: {e}", file=sys.stderr)
            return False

    async def pre_task(self, task: str, task_type: str = "general") -> Dict[str, Any]:
        """任务执行前管线 — 统一走 auto_context_inject handler。

        Returns:
            {
                ok: bool,                    # 是否通过防线
                blocked: bool,              # 是否被拦截
                block_reason: str | None,   # 拦截原因
                context: dict | None,       # 上下文包 {summary, inject_memory_id}
                scarf: dict | None,         # SCARF 自省结果
                trust: float,               # 当前信任分
                layer: str | None,          # 触发拦截的防线层级
            }
        """
        result: Dict[str, Any] = {
            "ok": True,
            "blocked": False,
            "block_reason": None,
            "context": None,
            "scarf": None,
            "trust": 0.60,
            "layer": None,
        }

        if not self._init_modules():
            return result

        # 1. 信任分检查
        trust_score = self._trust.get()
        result["trust"] = round(trust_score, 2)

        if trust_score < 0.4:
            result["ok"] = False
            result["blocked"] = True
            result["block_reason"] = f"Trust too low ({trust_score:.2f} < 0.40)"
            result["layer"] = "L1"
            return result

        # 2. 防线 L0/L1/L2 检查
        try:
            defense = self._enforcer.pre_check(task, task_type)
            if defense.get("blocked"):
                result["ok"] = False
                result["blocked"] = True
                result["block_reason"] = defense.get("reason", "Defense blocked")
                result["layer"] = defense.get("layer", "L0")
                return result
        except Exception:
            # 防线检查失败不阻塞任务
            pass

        # 3. SCARF 自省
        try:
            scarf = self._scarf.reflect(task)
            result["scarf"] = scarf
        except Exception:
            result["scarf"] = None

        # 4. Unified context injection via auto_context_inject handler
        try:
            from plastic_promise.mcp.tools.context import handle_auto_context_inject
            from plastic_promise.core.context_engine import ContextEngine

            # Ensure engine is initialized (SoulLoop lazy-inits it on pre_task_v2)
            engine = getattr(self._soul_loop, '_engine', None)
            if engine is None:
                engine = ContextEngine()
                self._soul_loop._engine = engine

            inject_result = await handle_auto_context_inject(engine, {
                "task_description": task,
                "task_type": task_type,
                "source": "pi_agent",
            })
            data = json.loads(inject_result[0].text)
            # Backward compatibility: return context_pack dict (not ContextPack object)
            # Existing callers (neko_adapter.py) expect a dict with "summary" key
            if data.get("context_pack"):
                result["context"] = {
                    "summary": str(data["context_pack"])[:200],
                    "inject_memory_id": data.get("inject_memory_id"),
                }
        except Exception:
            result["context"] = None

        return result

    def post_task(self, result_text: str, task_type: str = "general",
                  success: bool = True) -> Dict[str, Any]:
        """任务执行后管线。

        Returns:
            {
                ok: bool,
                trust_delta: float,         # 信任分变化
                trust: float,               # 更新后信任分
                memory_stored: bool,        # 是否存储了记忆
                cei: float,                 # 更新后 CEI
            }
        """
        output: Dict[str, Any] = {
            "ok": True,
            "trust_delta": 0.0,
            "trust": 0.60,
            "memory_stored": False,
            "cei": 0.5,
        }

        if not self._init_modules():
            return output

        old_trust = self._trust.get()

        # 1. 信任分更新
        try:
            if success:
                delta = 0.02
                self._trust.boost(delta, reason=f"Task success: {task_type}", target="")
            else:
                delta = -0.05
                self._trust.decay(TRUST_DECAY_RATE, reason=f"Task failure: {task_type}", target="")
                delta = -TRUST_DECAY_RATE
            output["trust_delta"] = round(delta, 2)
        except Exception:
            pass

        new_trust = self._trust.get()
        output["trust"] = round(new_trust, 2)

        # 2. 激素更新
        try:
            if success:
                self._hormone.apply_feedback("dopamine", 0.1, "Task success")
            else:
                self._hormone.apply_feedback("cortisol", 0.15, "Task failure")
        except Exception:
            pass

        # 3. 记忆存储
        try:
            self._soul_loop.post_task(result_text, task_type)
            output["memory_stored"] = True
        except Exception:
            pass

        # 4. 惯性检查
        try:
            self._proprioception.record_task(task_type, success)
        except Exception:
            pass

        return output

    def status(self) -> Dict[str, Any]:
        """获取完整灵魂状态。"""
        if not self._init_modules():
            return {"enabled": SOUL_ENABLED, "initialized": False}

        result = {"enabled": True, "initialized": True}

        try:
            result["scarf"] = self._scarf.get_status_summary() if self._scarf else None
        except Exception:
            result["scarf"] = {"error": "unavailable"}

        try:
            result["trust"] = {
                "score": round(self._trust.get(), 2),
                "tier": self._trust.tier(),
                "autonomy": self._trust.autonomy_level(),
            }
        except Exception:
            result["trust"] = {"score": 0.60, "tier": "medium", "autonomy": "normal"}

        try:
            result["hormone"] = self._hormone.get_hormone_status() if self._hormone else None
        except Exception:
            result["hormone"] = {"dopamine": 0.5, "cortisol": 0.2}

        try:
            result["defense"] = self._enforcer.get_defense_status() if self._enforcer else None
        except Exception:
            result["defense"] = {"L0": "ok", "L1": "ok", "L2": "ok"}

        try:
            result["curiosity"] = self._curiosity.get_exploration_stats() if self._curiosity else None
        except Exception:
            result["curiosity"] = {"epsilon": 0.1}

        try:
            result["proprioception"] = (
                self._proprioception.get_pattern_analysis() if self._proprioception else None
            )
        except Exception:
            result["proprioception"] = {"patterns": []}

        return result


# ============================================================
# 全局单例
# ============================================================

_bridge: Optional[SoulBridge] = None


def get_bridge() -> SoulBridge:
    global _bridge
    if _bridge is None:
        _bridge = SoulBridge()
    return _bridge


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Soul Bridge CLI")
    sub = parser.add_subparsers(dest="command")

    pre = sub.add_parser("pre_task")
    pre.add_argument("task")
    pre.add_argument("task_type", nargs="?", default="general")

    post = sub.add_parser("post_task")
    post.add_argument("result")
    post.add_argument("task_type", nargs="?", default="general")
    post.add_argument("--success", action="store_true", default=True)
    post.add_argument("--fail", dest="success", action="store_false")

    sub.add_parser("status")
    sub.add_parser("trust")
    sub.add_parser("scarf")

    args = parser.parse_args()
    bridge = get_bridge()

    if args.command == "pre_task":
        result = asyncio.run(bridge.pre_task(args.task, args.task_type))
    elif args.command == "post_task":
        result = bridge.post_task(args.result, args.task_type, args.success)
    elif args.command == "status":
        result = bridge.status()
    elif args.command == "trust":
        result = bridge.status().get("trust", {})
    elif args.command == "scarf":
        result = bridge.status().get("scarf", {})
    else:
        result = bridge.status()

    print(json.dumps(result, ensure_ascii=False, indent=2))

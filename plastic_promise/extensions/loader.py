"""PluginLoader — discover, validate, and activate plugins from packs.

Usage:
    loader = PluginLoader()
    loader.discover()            # scan plugins/ for packs
    loader.activate_all()        # validate + load all plugins
    results = loader.trigger_hooks("on_before_dispatch", context)
"""

import importlib
import importlib.util
import json
import logging
import subprocess
from pathlib import Path

from plastic_promise import __version__
from plastic_promise.extensions.registry import PackInfo, PackRegistry

PLASTIC_PROMISE_VERSION = __version__
logger = logging.getLogger("plastic-promise.extensions.loader")

HOOK_MERGE_STRATEGIES = {
    "on_before_dispatch": "concat",
    "on_after_dispatch": "concat",
    "on_transition_write_execute": "last_wins",
    "on_after_verify": "all_or_nothing",
}


class PluginLoader:
    """Loads plugins from packs and dispatches them at extension points."""

    def __init__(self, plugins_dir: str = "plugins"):
        self._registry = PackRegistry(plugins_dir)
        self._hooks: dict[str, list[dict]] = {}
        self._tools: dict[str, dict] = {}
        self._dispatch_providers: list = []
        self._activated: list[str] = []
        self._core_version = PLASTIC_PROMISE_VERSION

    # ── Discovery ──

    def discover(self) -> list[PackInfo]:
        """Scan plugins/ for installed packs."""
        return self._registry.discover()

    # ── Activation ──

    def activate_all(self) -> int:
        """Activate all discovered plugins. Returns count of activated plugins."""
        count = 0
        for pack in self._registry.list_packs():
            try:
                if self._activate_one(pack):
                    count += 1
            except Exception as e:
                logger.warning("Failed to activate plugin %s: %s", pack.name, e)
        return count

    def _activate_one(self, pack: PackInfo) -> bool:
        """Activate a single plugin. Returns True on success.

        Security gates (in order, NO code execution before all pass):
          1. Static validation — no import or __init__ called
          2. min_core_version check
          3. Trust score gate
        """
        if pack.name in self._activated:
            return True

        # Skip disabled plugins
        if (Path(pack.path) / ".disabled").exists():
            logger.info("Plugin %s is disabled, skipping", pack.name)
            return False

        # ── Security Gate 1: Static validation (no code execution) ──
        if not self._validate_pack(pack):
            logger.warning("Plugin %s rejected at security gate", pack.name)
            return False

        # ── Security Gate 2: min_core_version ──
        if not self._check_core_version(pack):
            logger.warning(
                "Plugin %s requires core >= %s, but running %s",
                pack.name,
                pack.min_core_version,
                self._core_version,
            )
            return False

        # ── Security Gate 3: Trust score gate ──
        if not self._check_trust(pack):
            logger.warning("Plugin %s: trust score too low for activation", pack.name)
            return False

        # Install pip dependencies if declared
        for pip_spec in pack.install_pip:
            self._pip_install(pip_spec)

        # Register hooks from pack.hooks
        for slot_name, hook_cfg in pack.hooks.items():
            if slot_name not in self._hooks:
                self._hooks[slot_name] = []
            self._hooks[slot_name].append(
                {
                    "plugin": pack.name,
                    "method": hook_cfg.get("method", "mcp"),
                    "command": hook_cfg.get("command", ""),
                    "tool": hook_cfg.get("tool", ""),
                    "path": pack.path,
                    "timeout": hook_cfg.get("timeout", 30),
                }
            )

        # Register tools from pack.tools
        for tname in pack.tools.get("provides", []):
            self._tools[tname] = {
                "plugin": pack.name,
                "method": pack.tools.get("method", "mcp"),
                "path": pack.path,
            }

        self._activated.append(pack.name)
        self._write_installed(pack)
        logger.info(
            "Plugin activated: %s (type=%s, v%s)",
            pack.name,
            pack.pack_type,
            pack.version,
        )
        return True

    # ── Deactivation ──

    def _deactivate_one(self, pack_name: str) -> None:
        """Fully remove all registrations for a plugin. Idempotent."""
        # Remove from hooks
        for slot in list(self._hooks.keys()):
            self._hooks[slot] = [h for h in self._hooks[slot] if h["plugin"] != pack_name]
            if not self._hooks[slot]:
                del self._hooks[slot]
        # Remove from tools
        self._tools = {k: v for k, v in self._tools.items() if v["plugin"] != pack_name}
        # Remove from activated list
        self._activated = [p for p in self._activated if p != pack_name]

    def disable_plugin(self, name: str) -> bool:
        """Disable plugin at runtime. Writes .disabled marker, deactivates."""
        pack = self._registry.get(name)
        if not pack:
            return False
        (Path(pack.path) / ".disabled").touch()
        self._deactivate_one(name)
        return True

    def enable_plugin(self, name: str) -> bool:
        """Re-enable a disabled plugin."""
        pack = self._registry.get(name)
        if not pack:
            return False
        disabled_marker = Path(pack.path) / ".disabled"
        if disabled_marker.exists():
            disabled_marker.unlink()
        self.discover()  # re-read pack info
        return self._activate_one(pack)

    # ── Security Validation ──

    def _validate_pack(self, pack: PackInfo) -> bool:
        """Static validation — NO code execution. RCE-safe.

        Only checks module importability via find_spec.
        Does NOT import, instantiate, or call any plugin code.
        """
        # knowledge/workflow/adapter packs are data-only, inherently safe
        if pack.pack_type != "capability":
            return True

        for hook_cfg in pack.hooks.values():
            if hook_cfg.get("method") == "python":
                mod_path = hook_cfg.get("module", "")
                if mod_path:
                    if importlib.util.find_spec(mod_path) is None:
                        logger.warning("Module %s not found", mod_path)
                        return False
        return True

    def _check_core_version(self, pack: PackInfo) -> bool:
        """Gate: plugin declares min_core_version, engine checks compatibility."""
        if not pack.min_core_version or pack.min_core_version == "0.0.0":
            return True
        try:
            from packaging.version import Version

            return Version(self._core_version) >= Version(pack.min_core_version)
        except ImportError:
            return True  # packaging not installed, skip version check

    def _check_trust(self, pack: PackInfo) -> bool:
        """Gate: trust score must meet threshold for plugin activation."""
        if pack.author == "plastic-promise":
            min_trust = 0.35  # D-tier minimum for official
        else:
            min_trust = 0.50  # B-tier required for community/third-party
        try:
            from plastic_promise.defense.trust_store import TrustStore

            store = TrustStore()
            result = store.get("claude")
            trust = result.get("trust", 0.5) if isinstance(result, dict) else 0.5
            return trust >= min_trust
        except ImportError:
            return True  # no TrustStore, allow

    def _write_installed(self, pack: PackInfo) -> None:
        """Write version lock file for installed plugin."""
        import datetime

        installed_path = Path(pack.path) / ".installed"
        installed_path.write_text(
            json.dumps(
                {
                    "name": pack.name,
                    "version": pack.version,
                    "installed_at": datetime.datetime.now().isoformat(),
                    "source": pack.path,
                }
            )
        )

    def _pip_install(self, spec: str) -> None:
        """Install a pip dependency. Gracefully skips if already installed."""
        try:
            subprocess.run(
                ["pip", "install", spec],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except Exception as e:
            logger.warning("pip install %s failed: %s", spec, e)

    # ── Hook Dispatch ──

    def get_hooks(self, slot: str) -> list[dict]:
        """Get all plugins registered for a specific slot."""
        return self._hooks.get(slot, [])

    def trigger_hooks(self, slot: str, context: dict) -> list[dict]:
        """Execute all hooks for a slot and collect results.

        Returns list of result dicts from each hook. Failed hooks return {"error": ...}.
        """
        results = []
        for hook in self.get_hooks(slot):
            try:
                if hook["method"] == "mcp":
                    result = self._exec_mcp(hook, context)
                elif hook["method"] == "cli":
                    result = self._exec_cli(hook, context)
                else:
                    result = self._exec_python(hook, context)
                results.append(result if result else {})
            except Exception as e:
                logger.warning("Hook %s/%s failed: %s", slot, hook["plugin"], e)
                results.append({"error": str(e), "plugin": hook["plugin"]})
        return results

    def trigger_hooks_merged(self, slot: str, context: dict) -> dict:
        """Execute hooks and merge results according to strategy."""
        results = self.trigger_hooks(slot, context)
        return self._merge_results(slot, results)

    def _merge_results(self, slot: str, results: list[dict]) -> dict:
        """Merge hook results according to declared strategy."""
        strategy = HOOK_MERGE_STRATEGIES.get(slot, "concat")
        if strategy == "concat":
            merged: dict = {}
            for r in results:
                if isinstance(r, dict):
                    merged.update(r)
            return merged
        elif strategy == "last_wins":
            ok = [r for r in results if "error" not in r]
            return ok[-1] if ok else {}
        elif strategy == "all_or_nothing":
            errors = [r for r in results if "error" in r]
            if errors:
                return {"_hook_errors": errors}
            merged = {}
            for r in results:
                if isinstance(r, dict):
                    merged.update(r)
            return merged
        return {}

    def _exec_mcp(self, hook: dict, context: dict) -> dict:
        """Execute an MCP-based hook via subprocess stdio."""
        command_str = hook.get("command", "")
        if not command_str:
            return {"error": "No MCP command specified"}

        try:
            from plastic_promise.extensions.mcp_subprocess import McpSubprocessPlugin
        except ImportError:
            return {"error": "McpSubprocessPlugin not available"}

        mcp = McpSubprocessPlugin(command_str.split(), timeout=hook.get("timeout", 30))
        if not mcp.start():
            return {"error": f"MCP server failed to start: {command_str}"}

        try:
            mcp.discover_tools()
            tool_name = hook.get("tool", "")
            task_desc = context.get("task_description", "")
            if tool_name and tool_name in mcp._tools:
                result = mcp.call_tool(
                    tool_name,
                    {
                        "task_description": task_desc,
                    },
                )
                return {"result": result} if result else {}
            return {}
        finally:
            mcp.shutdown()

    def _exec_cli(self, hook: dict, context: dict) -> dict:
        """Execute a CLI-based hook via subprocess."""
        task_desc = context.get("task_description", "")
        payload = {
            "task_description": task_desc,
            "from_stage": context.get("from_stage", ""),
            "to_stage": context.get("to_stage", ""),
        }
        try:
            result = subprocess.run(
                [hook["command"], json.dumps(payload)],
                capture_output=True,
                text=True,
                timeout=hook.get("timeout", 30),
            )
            if result.returncode != 0:
                return {"error": result.stderr.strip()[:200]}
            if not result.stdout.strip():
                return {}
            return json.loads(result.stdout)
        except FileNotFoundError:
            return {"error": f"Binary not found: {hook['command']}"}
        except subprocess.TimeoutExpired:
            return {"error": "Timeout"}
        except json.JSONDecodeError:
            return {"error": "Invalid JSON output"}
        except Exception as e:
            return {"error": str(e)}

    def _exec_python(self, hook: dict, context: dict) -> dict:
        """Execute a Python-based hook by importing its module."""
        mod_path = hook.get("module", "")
        if not mod_path:
            return {}
        try:
            mod = importlib.import_module(mod_path)
            if hasattr(mod, "execute"):
                return mod.execute(context)
        except ImportError as e:
            logger.warning("Python hook %s import failed: %s", hook["plugin"], e)
        except Exception as e:
            logger.warning("Python hook %s execution failed: %s", hook["plugin"], e)
        return {}

    # ── Tool Dispatch ──

    def get_tools(self) -> dict[str, dict]:
        """Get all registered tools from plugins."""
        return dict(self._tools)

    def call_plugin_tool(self, tool_name: str, arguments: dict) -> dict | None:
        """Dispatch a tool call to the plugin that provides it.

        Returns the tool result as a dict, or None if the tool is unknown
        or the plugin fails to execute it.
        """
        tool_info = self._tools.get(tool_name)
        if not tool_info:
            return None

        # Re-activate if needed so the providing plugin is loaded
        plugin_name = tool_info["plugin"]
        if plugin_name not in self._activated:
            pack = self._registry.get(plugin_name)
            if pack and not self._activate_one(pack):
                return None

        method = tool_info.get("method", "mcp")
        if method == "mcp":
            pack = self._registry.get(plugin_name)
            if not pack:
                return None
            command_str = pack.raw.get("tools", {}).get("command", "")
            if not command_str:
                return None
            try:
                from plastic_promise.extensions.mcp_subprocess import McpSubprocessPlugin
            except ImportError:
                return None
            mcp = McpSubprocessPlugin(command_str.split(), timeout=30)
            if not mcp.start():
                return None
            try:
                mcp.discover_tools()
                result = mcp.call_tool(tool_name, arguments)
                return {"result": result} if result else {}
            finally:
                mcp.shutdown()
        elif method == "cli":
            import json as _json
            import subprocess as _sp

            command = tool_info.get("command", "")
            if not command:
                return None
            try:
                proc = _sp.run(
                    [command, _json.dumps(arguments)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode != 0:
                    return {"error": proc.stderr.strip()[:200]}
                return _json.loads(proc.stdout) if proc.stdout.strip() else {}
            except Exception as e:
                return {"error": str(e)}
        else:
            return None

"""MCP tools for the Plastic Promise plugin market."""

import json
import re
from typing import Any

from mcp.types import TextContent


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a semantic version string into a comparable tuple."""
    try:
        from packaging.version import Version

        return Version(version_str).release  # type: ignore[attr-defined]
    except ImportError:
        # Fallback: strip leading 'v', split on '.', convert to ints
        ver = version_str.lstrip("v")
        parts = re.split(r"[.\-+]", ver)
        return tuple(int(p) for p in parts[:3] if p.isdigit())


def _safe_name(name: str) -> str:
    """Reject names that could escape the plugins directory."""
    if not name or not re.fullmatch(r"[a-zA-Z0-9][-a-zA-Z0-9_.]*", name):
        raise ValueError(f"Invalid pack name: {name!r}")
    return name


async def handle_market_list(engine: Any, args: dict) -> list[TextContent]:
    """List available packs — merges installed (local) + remote index."""
    try:
        from plastic_promise.extensions.registry import PackRegistry

        registry = PackRegistry()
        local_packs = {p.name: p for p in registry.discover()}
        remote_entries = registry.fetch_remote_index()

        pack_type = args.get("type")
        upgradable_only = args.get("upgradable", False)

        merged = []
        seen: set[str] = set()
        for entry in remote_entries:
            name = entry["name"]
            seen.add(name)
            installed = name in local_packs
            if pack_type and entry.get("type") != pack_type:
                continue
            if upgradable_only and not installed:
                continue
            entry["installed"] = installed
            if installed:
                local = local_packs[name]
                entry["installed_version"] = local.version
                entry["upgradable"] = _parse_version(local.version) < _parse_version(
                    entry.get("version", "0.0.0")
                )
            merged.append(entry)

        # Add local-only packs not in remote index
        for name, pack in local_packs.items():
            if name not in seen:
                if pack_type and pack.pack_type != pack_type:
                    continue
                merged.append(
                    {
                        "name": name,
                        "version": pack.version,
                        "type": pack.pack_type,
                        "author": pack.author,
                        "description": pack.description,
                        "installed": True,
                        "installed_version": pack.version,
                        "upgradable": False,
                        "source": "local",
                    }
                )

        result = {"packs": merged, "count": len(merged)}
        return [
            TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_list"}, ensure_ascii=False),
            )
        ]


async def handle_market_install(engine: Any, args: dict) -> list[TextContent]:
    """Install a pack from the market."""
    name = args.get("name", "")
    if not name:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "name is required"}, ensure_ascii=False),
            )
        ]
    name = _safe_name(name)

    try:
        from plastic_promise.extensions.loader import PluginLoader

        loader = PluginLoader()
        loader.discover()

        pack = loader._registry.get(name)
        if not pack:
            # Try remote index as fallback
            remote = loader._registry.fetch_remote_index()
            entry = next((e for e in remote if e["name"] == name), None)
            if entry:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": f"Pack '{name}' is available remotely but "
                                "git-based install is not yet implemented. "
                                "Clone the source repo into plugins/{name}/ and "
                                "re-run market install.",
                                "source": entry.get("source", "unknown"),
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": f"Pack '{name}' not found"},
                        ensure_ascii=False,
                    ),
                )
            ]

        success = loader._activate_one(pack)
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "installed": name,
                        "type": pack.pack_type,
                        "version": pack.version,
                        "activated": success,
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_install"}, ensure_ascii=False),
            )
        ]


async def handle_market_upgrade(engine: Any, args: dict) -> list[TextContent]:
    """Upgrade a plugin to the latest version from remote index."""
    name = args.get("name", "")
    if not name:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "name is required"}, ensure_ascii=False),
            )
        ]
    name = _safe_name(name)

    try:
        from plastic_promise.extensions.registry import PackRegistry

        registry = PackRegistry()
        registry.discover()
        remote = registry.fetch_remote_index()

        target = next((e for e in remote if e["name"] == name), None)
        if not target:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"'{name}' not in remote market"}),
                )
            ]

        local = registry.get(name)
        if local and _parse_version(local.version) >= _parse_version(
            target.get("version", "0.0.0")
        ):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"status": "up-to-date", "version": local.version},
                        ensure_ascii=False,
                    ),
                )
            ]

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "status": "upgrade-available",
                        "current": local.version if local else "none",
                        "latest": target["version"],
                        "source": target.get("source", ""),
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_upgrade"}, ensure_ascii=False),
            )
        ]


async def handle_market_remove(engine: Any, args: dict) -> list[TextContent]:
    """Remove an installed pack."""
    name = args.get("name", "")
    if not name:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "name is required"}, ensure_ascii=False),
            )
        ]
    name = _safe_name(name)

    try:
        import shutil
        from pathlib import Path

        from plastic_promise.extensions.loader import PluginLoader

        # Deactivate first (clean hooks/tools/activated list)
        loader = PluginLoader()
        loader.discover()
        loader._deactivate_one(name)

        # Then remove directory
        plugins_dir = Path("plugins")
        pack_dir = plugins_dir / name
        if pack_dir.exists():
            shutil.rmtree(pack_dir)
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"removed": name}, ensure_ascii=False),
                )
            ]

        return [
            TextContent(
                type="text",
                text=json.dumps({"error": f"Pack '{name}' not installed"}, ensure_ascii=False),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_remove"}, ensure_ascii=False),
            )
        ]


async def handle_market_enable(engine: Any, args: dict) -> list[TextContent]:
    """Enable a disabled plugin."""
    name = args.get("name", "")
    if not name:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "name is required"}, ensure_ascii=False),
            )
        ]
    name = _safe_name(name)

    try:
        from plastic_promise.extensions.loader import PluginLoader

        loader = PluginLoader()
        loader.discover()
        ok = loader.enable_plugin(name)
        return [
            TextContent(
                type="text",
                text=json.dumps({"enabled": name, "success": ok}, ensure_ascii=False),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_enable"}, ensure_ascii=False),
            )
        ]


async def handle_market_disable(engine: Any, args: dict) -> list[TextContent]:
    """Disable a plugin at runtime."""
    name = args.get("name", "")
    if not name:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "name is required"}, ensure_ascii=False),
            )
        ]
    name = _safe_name(name)

    try:
        from plastic_promise.extensions.loader import PluginLoader

        loader = PluginLoader()
        loader.discover()
        ok = loader.disable_plugin(name)
        return [
            TextContent(
                type="text",
                text=json.dumps({"disabled": name, "success": ok}, ensure_ascii=False),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_disable"}, ensure_ascii=False),
            )
        ]


async def handle_market_status(engine: Any, args: dict) -> list[TextContent]:
    """Show status of all plugins."""
    try:
        from pathlib import Path

        from plastic_promise.extensions.loader import PluginLoader

        loader = PluginLoader()
        loader.discover()

        statuses = []
        for pack in loader._registry.list_packs():
            disabled = (Path(pack.path) / ".disabled").exists()
            activated = pack.name in loader._activated
            statuses.append(
                {
                    "name": pack.name,
                    "version": pack.version,
                    "type": pack.pack_type,
                    "activated": activated,
                    "disabled": disabled,
                }
            )

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"plugins": statuses, "count": len(statuses)},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_status"}, ensure_ascii=False),
            )
        ]

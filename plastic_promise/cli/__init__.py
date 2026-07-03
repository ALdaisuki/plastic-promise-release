"""CLI entry points for plastic-promise commands."""

import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(prog="plastic-promise")
    sub = parser.add_subparsers(dest="command")

    # market subcommands
    market = sub.add_parser("market", help="Plugin market operations")
    market_sub = market.add_subparsers(dest="market_command")

    market_sub.add_parser("list", help="List available packs")
    install = market_sub.add_parser("install", help="Install a pack")
    install.add_argument("name", help="Pack name or GitHub URL")
    upgrade = market_sub.add_parser("upgrade", help="Upgrade an installed pack")
    upgrade.add_argument("name", help="Pack name")
    remove = market_sub.add_parser("remove", help="Remove an installed pack")
    remove.add_argument("name", help="Pack name")
    market_sub.add_parser("status", help="Show plugin status")
    enable = market_sub.add_parser("enable", help="Enable a disabled plugin")
    enable.add_argument("name", help="Pack name")
    disable = market_sub.add_parser("disable", help="Disable a plugin")
    disable.add_argument("name", help="Pack name")

    # start subcommand
    start = sub.add_parser("start", help="Start Plastic Promise services")
    start.add_argument(
        "--skip-ollama-check", action="store_true", help="Skip Ollama check"
    )

    args = parser.parse_args()

    if args.command == "market":
        _handle_market(args)
    elif args.command == "start":
        _handle_start(args)
    else:
        parser.print_help()
        sys.exit(1)


def _handle_market(args):
    """Route market subcommands to MCP tool handlers."""
    from plastic_promise.mcp.tools.market import (
        handle_market_disable,
        handle_market_enable,
        handle_market_install,
        handle_market_list,
        handle_market_remove,
        handle_market_status,
        handle_market_upgrade,
    )

    async def _run():
        cmd = args.market_command
        if cmd == "list":
            return await handle_market_list(None, {"upgradable": False})
        elif cmd == "install":
            return await handle_market_install(None, {"name": args.name})
        elif cmd == "upgrade":
            return await handle_market_upgrade(None, {"name": args.name})
        elif cmd == "remove":
            return await handle_market_remove(None, {"name": args.name})
        elif cmd == "status":
            return await handle_market_status(None, {})
        elif cmd == "enable":
            return await handle_market_enable(None, {"name": args.name})
        elif cmd == "disable":
            return await handle_market_disable(None, {"name": args.name})
        else:
            print("Unknown market command. Try: list, install, upgrade, remove, status, enable, disable")
            return []

    results = asyncio.run(_run())
    for r in results:
        print(r.text)


def _handle_start(args):
    """Start Plastic Promise services."""
    skip_ollama = getattr(args, "skip_ollama_check", False)
    print(f"Starting Plastic Promise... (skip_ollama_check={skip_ollama})")
    # Delegate to existing init_and_start script
    import subprocess
    cmd = [sys.executable, "scripts/init_and_start.py"]
    if skip_ollama:
        cmd.append("--skip-ollama-check")
    subprocess.run(cmd)


if __name__ == "__main__":
    main()

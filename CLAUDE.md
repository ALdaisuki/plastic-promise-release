# Agent Interop — CLAUDE.md (v2)

## You are connected to a WebSocket event bus with Pi and N.E.K.O

```
Bus: ws://127.0.0.1:48970
Topics: pi:claude | claude:pi | neko:* | memory:sync | heartbeat
```

## To join the event bus

```bash
pip install websockets
python bridge/bus_client.py --agent claude --session "claude-$(date +%s)"
```

## Available MCP tools (Shared Memory)

| Domain | Tools |
|--------|-------|
| Memory | memory_recall, memory_store, memory_update, memory_forget, memory_stats, memory_list, memory_gc |
| Principles | principle_activate, principle_inherit, principle_diffuse, principle_evaluate |
| Context | context_supply, context_inject, context_graph |
| Audit | audit_run, audit_pre_check, audit_report, defense_trust, defense_status |
| Reflection | scarf_reflect, inertia_check, feedback_apply |
| System | system_stats, system_backup, system_migrate |

## Standard workflow

1. **Start**: Run `context_supply` with task description to load shared memory
2. **During**: Use `memory_recall` for references, `memory_store` to share findings
3. **Before decisions**: `principle_activate` to check alignment
4. **End**: `audit_run` for session checkpoint

## Working with Pi (real-time)

When connected to event bus:
- Pi sends `task` messages — execute and reply with `result`
- Pi sends `message` — read and optionally reply
- N.E.K.O may send context or memory updates

## Memory sync

Memory changes are broadcast via `memory:sync` topic.
All agents sharing Plastic Promise MCP server have shared memory automatically.

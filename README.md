<!-- SEO Meta Tags
Description: Plastic Promise — AI behavioral governance system with 48 MCP tools across 12 domains. Commitment Engineering replaces constraint enforcement with internalized conventions, featuring multi-agent autonomous pipelines, trust-driven permission escalation, and Weibull-based memory decay.
Keywords: ai-governance, mcp-server, agent-memory, commitment-engineering, context-engine, llm-agent, multi-agent, trust-score, memory-decay, lance-db
author: ALdaisuki
canonical: https://github.com/ALdaisuki/plastic-promise
-->

<!-- Open Graph / Facebook
og:type: website
og:url: https://github.com/ALdaisuki/plastic-promise
og:title: Plastic Promise — AI Behavioral Governance System with Multi-Agent Pipeline
og:description: An AI behavioral governance system built on Commitment Engineering. 48 MCP tools, multi-agent autonomous pipeline (Claude PM + Pi Builder/Fixer/Reviewer), trust-driven permissions, and Weibull-based memory decay.
og:image: https://raw.githubusercontent.com/ALdaisuki/plastic-promise/main/docs/architecture/social-preview.png
og:image:alt: Plastic Promise architecture diagram showing MCP server, memory pipeline, and multi-agent team
og:site_name: Plastic Promise
og:locale: en_US
-->

<!-- Twitter Card
twitter:card: summary_large_image
twitter:url: https://github.com/ALdaisuki/plastic-promise
twitter:title: Plastic Promise — AI Behavioral Governance System
twitter:description: Commitment Engineering replaces constraint enforcement. 48 MCP tools, multi-agent pipeline, trust-driven permissions, memory decay engine.
twitter:image: https://raw.githubusercontent.com/ALdaisuki/plastic-promise/main/docs/architecture/social-preview.png
-->

<!-- GitHub Metadata
topics: ai-governance, mcp-server, agent-memory, commitment-engineering, multi-agent, trust-score, memory-decay, lancedb, python, rust
languages: python, rust
homepage: https://github.com/ALdaisuki/plastic-promise
funding: https://github.com/sponsors/ALdaisuki
roadmap: https://github.com/ALdaisuki/plastic-promise/blob/main/docs/GOAL.md
security: https://github.com/ALdaisuki/plastic-promise/blob/main/SECURITY.md
-->

<div align="center">

# Plastic Promise

### Memory is plastic; the soul exists through memory and grows through commitment.

[![Python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white&style=flat-square)](https://www.python.org/)
[![Rust](https://img.shields.io/badge/rust-core-000000?logo=rust&style=flat-square)](https://www.rust-lang.org/)
[![License](https://img.shields.io/github/license/ALdaisuki/plastic-promise?style=flat-square)](LICENSE)
[![MCP Tools](https://img.shields.io/badge/mcp-48_tools-orange.svg?style=flat-square)](https://spec.modelcontextprotocol.io/)
[![Status](https://img.shields.io/badge/status-alpha-red.svg?style=flat-square)](#)

![LanceDB](https://img.shields.io/badge/vector_db-LanceDB-3B82F6?style=flat-square)
![SQLite](https://img.shields.io/badge/db-SQLite_WAL-003B57?logo=sqlite&logoColor=white&style=flat-square)
![MCP](https://img.shields.io/badge/protocol-MCP_1.0-FF6B35?style=flat-square)
![sentence-transformers](https://img.shields.io/badge/embeddings-all--MiniLM--L6--v2-FFB000?style=flat-square)

![GitHub Stars](https://img.shields.io/github/stars/ALdaisuki/plastic-promise?style=social)
![GitHub Forks](https://img.shields.io/github/forks/ALdaisuki/plastic-promise?style=social)
![GitHub Issues](https://img.shields.io/github/issues/ALdaisuki/plastic-promise)
![GitHub Last Commit](https://img.shields.io/github/last-commit/ALdaisuki/plastic-promise)

[Architecture Docs](docs/architecture/architecture.md) - [Goal & Roadmap](docs/GOAL.md) - [Report Bug](https://github.com/ALdaisuki/plastic-promise/issues) - [Request Feature](https://github.com/ALdaisuki/plastic-promise/issues)

</div>

---

**Plastic Promise** is an AI behavioral governance system built on **Commitment Engineering** -- a paradigm that replaces external constraint enforcement with internalized conventions. It provides a complete operating system for AI agents: persistent memory with Weibull-based decay, evolving principles with cross-agent inheritance, proactive context supply, trust-driven permission escalation, and a multi-agent autonomous pipeline. 48 MCP tools across 12 domains serve as the interface between AI agents and the governance substrate.

> Full architecture, roadmap, and current status: **[GOAL.md](docs/GOAL.md)**.

---

## Table of Contents

- [Architecture](#architecture)
- [System Requirements](#system-requirements)
- [Quick Start](#quick-start)
- [Core Features](#core-features)
- [MCP Tool Reference](#mcp-tool-reference)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Architecture Documentation](#architecture-documentation)
- [Development Guide](#development-guide)
- [Privacy & Data Security](#privacy--data-security)
- [License](#license)

---

## Architecture

### C4 Model -- System Context (Level 1)

```
+---------------------------+          +--------------------------------------+
|    AI Coding Agent        |  MCP     |   Plastic Promise Governance System  |
|  +---------------------+  |  stdio   |  +--------------------------------+  |
|  | Claude Code / Trae  |<>+-------->+--+ MCP Server (:9020)             |  |
|  +---------------------+  |  SSE    |  | 48 tools / 12 domains           |  |
+---------------------------+<--------+--+                                 |  |
                                       |  +--------------------------------+  |
+---------------------------+  /notify |  | Memory Pool (SQLite + LanceDB)  |  |
|    Pi Agent Team          |  POST    |  +--------------------------------+  |
|  Pi Builder / Fixer /     |<--------+--+ Tag State Machine               |  |
|  Reviewer                 |  tags    |  | pending -> active -> done        |  |
+---------------------------+<-------->+--+      -> review -> reviewed      |  |
                                       |  | Daemon (tag-based polling)       |  |
                                       +--------------------------------------+
```

Full C4 diagrams: [Level 1 — Context](docs/architecture/diagrams/c4-level1-context.txt) - [Level 2 — Container](docs/architecture/diagrams/c4-level2-container.txt) - [Level 3 — Component](docs/architecture/diagrams/c4-level3-component.txt)

### Multi-Agent Pipeline

```
task:pending -> Daemon tag-based detection -> spawn Pi -> task:active
             -> Builder complete -> auto-wake Reviewer -> task:review
             -> Claude verify -> task:reviewed / task:rejected -> Fixer auto-repair
```

### Subsystem Status

| Subsystem | Status | Core Module |
|-----------|--------|-------------|
| Memory | Stable | `soul_memory` (dual-layer, L1/L3 tiered storage) |
| Reflex Arc | Stable | `soul_enforcer` (3-layer defense: L0 hard boundary, L1 trust constraint, L2 immune patrol) |
| Motor | Beta | `exec/write/edit` + ACP |
| Sensory | Beta | `memory_recall` + `code_search` |
| Immune | Beta | `soul_audit` (11-dimension scan via `audit_run`, hourly cron) |
| Endocrine | Beta | `soul_hormone` (evaluation engine + trust score linkage) |
| Genetic | Beta | `soul_principles` (unidirectional diffusion + synchronized decay) |
| Autonomic | Beta | `scan_and_fix` + HEARTBEAT |
| Cognitive | Experimental | `soul_scarf` + `soul_curiosity` |

> **Audit dimensions** (11): The 3 audit tools (`audit_run`, `audit_pre_check`, `defense`) expose 11 inspection dimensions -- 7 foundational (memory health, principle adherence, domain integrity, context freshness, trust trajectory, GC efficiency, schema version) + 4 multi-agent (task throughput, hunter trust distribution, redo-queue depth, skill-chain completeness). Each dimension is scored independently; `audit_run(action="full")` returns the aggregate report.

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| Python | 3.10+ | 3.12+ |
| RAM | 2 GB | 4 GB |
| Disk | 500 MB (SQLite + LanceDB) | 2 GB (with experience packs) |
| OS | Windows 10+, Linux, macOS 12+ | — |
| Rust toolchain | Not required (Python fallback) | `rust/context-engine-core` via `maturin develop` for 3-5x context engine speedup |
| Ollama | Not required (embedding fallback available) | For local cross-encoder reranking |

---

## Quick Start

### Installation

```bash
# Core dependencies
pip install -r requirements.txt

# Full install (includes dev tools)
pip install -e ".[dev]"

# Optional: build Rust core engine (3-5x context engine performance)
cd rust/context-engine-core && pip install maturin && maturin develop
```

### Launch

```bash
# 1. Start shared memory server (SSE multi-agent mode)
python -m plastic_promise.mcp.server --sse 9020

# 2. Start autonomous pipeline daemon
python daemons/pi_daemon.py
```

### Verify Installation

```bash
# Health check -- server must respond
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9020/health').read())"
# Expected: {"status": "ok", "version": "0.1.0", ...}

# Store a test memory and verify retrieval
# Via MCP tool (from Claude Code / Trae connected to the server):
#   memory_store(content="test memory", tags=["test"])
#   memory_recall(query="test memory")
# Expected: returns the stored memory with similarity score

# Send a task through the pipeline
#   memory_store(content="Implement hello world", tags=["task:pending", "assignee:pi_builder", "domain:building"])
# Watch daemon output:
#   [Pi] task detected -> spawning builder -> task:active
#   [Pi] builder complete -> waking reviewer -> task:review
```

### Connect to Claude Code / Trae

Merge into your MCP configuration:

```json
{
  "mcpServers": {
    "plastic-promise": {
      "command": "python",
      "args": ["-m", "plastic_promise.mcp.server"],
      "cwd": "/path/to/Memory system",
      "env": {
        "PP_EMBEDDING_DIM": "384",
        "PP_LANCEDB_PATH": ".data/lancedb",
        "PP_SQLITE_PATH": ".data/memories.db"
      }
    }
  }
}
```

---

## Core Features

### Commitment Engineering

| Concept | Description |
|---------|-------------|
| **Convention over Constraint** | Agents comply because they don't want to disappoint those they care about, not because they are forbidden |
| **Trust for Autonomy** | Trust score drives dynamic constraints: high trust relaxes restrictions, low trust tightens them |
| **Principle Emergence** | Principles emerge naturally during retrieval of past decisions, not via firewall enforcement |
| **Proactive Context Supply** | Memory is not a "queried archive" but an "engine that proactively supplies context" |

### Multi-Agent Autonomous Pipeline

```
task:pending -> Daemon tag-based detection -> spawn Pi -> task:active
             -> Builder complete -> auto-wake Reviewer -> task:review
             -> Claude verify -> task:reviewed / task:rejected -> Fixer auto-repair
```

The daemon uses **tag-based polling** on SQLite -- no LLM calls are required for task detection or routing. Only the actual implementation and review stages consume LLM tokens.

### Tag State Machine

```
task:pending -> task:accepted -> task:active -> task:done -> task:review -> task:reviewed
                   ^ timeout 5min reset              ^ timeout 10min reset
```

### Trust-Freedom Matrix

| Trust Score | Tier | Permissions |
|-------------|------|-------------|
| 0.80+ | `autonomous` | Full access, can assign tasks |
| 0.60+ | `standard` | Read/write files, create issues |
| 0.30+ | `restricted` | Approval required for writes |
| 0.00+ | `readonly` | Read-only access |

### Resilience

- **Disaster Recovery**: `domain(action="rebuild")` rebuilds domain graph from tags
- **Cross-version Compatibility**: `schema_version` migration chain + pack escape hatch
- **Silent Failure Protection**: `_dm_ok` degradation switch + tag system independent of issue table
- **11-dimension Audit**: Hourly automated scan, Tier 1 issues auto-fixed

---

## MCP Tool Reference

48 tools across 12 domains:

| Domain | Count | Tools |
|--------|-------|-------|
| Memory | 10 | `memory_recall` `memory_store` `memory_update` `memory_forget` `memory_stats` `memory_list` `memory_gc` `memory_correct` `memory_reclassify` `memory_sync_files` |
| Principles | 4 | `principle_activate` `principle_inherit` `principle_diffuse` `principle_evaluate` |
| Context | 5 | `context_supply` `context_inject` `context_graph` `context_ready` `auto_context_inject` |
| Audit & Defense | 3 | `audit_run` `audit_pre_check` `defense` |
| Reflection | 2 | `scarf_reflect` `feedback_apply` |
| System | 4 | `system` `issue_create` `issue_transition` `issue_list` |
| Experience Pack | 3 | `pack_export` `pack_import` `pack_recall` |
| Domain Federation | 1 | `domain` |
| Skill Tracking | 5 | `skill_session_start` `skill_session_complete` `skill_session_trace` `skill_session_audit` `skill_auto_track` |
| Programmatic Skills | 3 | `session-init` `smart-remember` `step-closure` |
| Dispatch | 7 | `task_enqueue` `task_claim` `task_complete` `task_verify` `task_inbox` `task_heartbeat` `task_abandon` |
| SuperPowers | 1 | `sp-stage` |

> Also includes 3 Prompt templates and 5 Resource endpoints.

---

## Tech Stack

<div align="center">

**Language** - **Vector Store** - **Protocol** - **LLM**

</div>

| Category | Technologies |
|----------|-------------|
| **Language** | ![Python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white) ![Rust](https://img.shields.io/badge/rust-core-000000?logo=rust&logoColor=white) |
| **Vector Store** | ![LanceDB](https://img.shields.io/badge/LanceDB-%E2%89%A50.6.0-3B82F6) |
| **Relational DB** | ![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white) |
| **Embeddings** | ![sentence-transformers](https://img.shields.io/badge/all--MiniLM--L6--v2-384d-FFB000) |
| **Protocol** | ![MCP](https://img.shields.io/badge/MCP-1.0-FF6B35) |
| **LLM** | Claude (PM) / DeepSeek-v4-pro (Pi Agents) |
| **Async** | asyncio + threading.RLock |
| **Web Server** | uvicorn + starlette |

<details>
<summary>View all dependencies</summary>

### Core Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `mcp` | >=1.0.0 | Model Context Protocol server |
| `lancedb` | >=0.6.0 | Disk-based ANN vector storage |
| `sentence-transformers` | >=2.2.0 | Local embedding generation (384d) |
| `uvicorn[standard]` | >=0.27.0 | ASGI server for SSE |
| `starlette` | >=0.36.0 | Web framework for HTTP endpoints |
| `httpx` | >=0.27.0 | HTTP client for bridge |

### Dev Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pytest` | >=8.0 | Test framework |
| `pytest-asyncio` | >=0.23 | Async test support |
| `pytest-cov` | >=4.0 | Coverage reporting |
| `ruff` | >=0.3.0 | Linting + formatting |
| `mypy` | >=1.8 | Static type checking |
| `pre-commit` | >=3.6 | Git hook framework |

</details>

---

## Project Structure

```
plastic_promise/              # Python core package
├── core/                     # Core engine
│   ├── constants.py          # Constants, thresholds, 12 core principles
│   ├── context_engine.py     # Context supply engine (Python fallback)
│   ├── embedder.py           # Embedder (sentence-transformers / OpenAI)
│   ├── decay_engine.py       # Time decay engine (Weibull)
│   ├── domain_manager.py     # Domain federation manager
│   ├── noise_filter.py       # Noise filter
│   ├── quality_gate.py       # Quality gate (4-dim scoring)
│   ├── reranker.py           # Cross-encoder reranker
│   ├── lancedb_store.py      # LanceDB vector store
│   └── pack_index.py         # Experience pack index
├── mcp/                      # MCP Server
│   ├── server.py             # Main entry (48 tool routes)
│   ├── tools/                # 12 domain handler modules
│   │   ├── memory.py         # Memory domain (10 tools)
│   │   ├── principles.py     # Principles domain (4 tools)
│   │   ├── context.py        # Context domain (5 tools)
│   │   ├── audit_defense.py  # Audit & defense domain (3 tools)
│   │   ├── reflection.py     # Reflection domain (2 tools)
│   │   ├── management.py     # System management domain (7 tools, incl. dispatch)
│   │   ├── domain.py         # Domain federation (1 tool)
│   │   ├── skill_tracking.py # Skill tracking (5 tools)
│   │   ├── reclassify.py     # Memory reclassification
│   │   └── sync.py           # File sync
│   ├── resources.py          # 5 MCP Resources
│   └── prompts.py            # 3 MCP Prompts
├── memory/                   # Memory system
│   └── soul_memory.py        # RecMem + EvolveR + MemoryGC
├── loop/                     # Orchestration
│   └── soul_loop.py          # pre_task_v2 + post_task + step-closure
├── principles/               # Principle system
│   └── soul_principles.py    # Activate/Inherit/Diffuse/Evaluate
├── reflection/               # Reflection system
│   ├── soul_scarf.py         # SCARF 5-dimension introspection
│   └── soul_proprioception.py # Proprioception + inertia suppression
├── growth/                   # Growth system
│   ├── soul_hormone.py       # Real-time feedback hormone
│   ├── soul_classifier.py    # Task classifier
│   └── skill_extractor.py    # Skill extraction
├── defense/                  # Defense system
│   ├── soul_enforcer.py      # 3-layer defense
│   └── soul_audit.py         # 11-dimension audit
└── skills/                   # Programmatic skills (Phase 1)

rust/context-engine-core/     # Rust core engine (PyO3)
├── src/
│   ├── entity_graph.rs       # Entity relationship graph
│   ├── rank_fuser.rs         # RRF fusion + symbolic rules
│   ├── source_tracker.rs     # Source tracking
│   ├── association_feedback.rs # Self-evolution feedback
│   ├── memory_worth.rs       # Dual counters
│   ├── context_engine.rs     # Main orchestrator
│   └── principles.rs         # Principle entities
└── Cargo.toml

daemons/                      # Daemon processes & workers
├── pi_daemon.py              # Multi-agent autonomous pipeline (tag-based polling)
├── audit_daemon.py           # Hourly audit + memory cleanup
├── pi_worker.ps1             # Worker launcher (Windows)
├── pi_worker.sh              # Worker launcher (Linux/macOS)
├── pi_listener.ps1           # SSE event listener
└── watchdog.ps1              # Process watchdog (auto-restart)

tests/                        # Tests
docs/                         # Design documentation
├── GOAL.md                   # Architecture overview & roadmap
├── BUILD_PLAN.md             # Build plan (historical reference)
├── architecture/             # Generated architecture documentation
└── superpowers/              # Design specs (80+ files)
scripts/                      # Helper scripts
├── start-all.bat             # One-click start (Windows)
├── start-all.sh              # One-click start (Linux/macOS)
└── eco.py                    # Carbon footprint calculator
utils/                        # Utility functions
bridge/                       # N.E.K.O bridge
.data/                        # Runtime data (SQLite + LanceDB)
experience_packs/             # Experience pack exports
```

---

## Architecture Documentation

Complete architecture documentation under `docs/architecture/`:

| Document | Description |
|----------|-------------|
| [architecture.md](docs/architecture/architecture.md) | 13-section architecture document: agents, data flow, security model, cost estimation, implementation phases |
| [diagrams/architecture.mermaid](docs/architecture/diagrams/architecture.mermaid) | Agent flow diagram: 12 MCP domains, daemon layer, storage |
| [diagrams/sequence.mermaid](docs/architecture/diagrams/sequence.mermaid) | Multi-agent workflow sequence: session init -> build -> review -> fix |
| [diagrams/components.mermaid](docs/architecture/diagrams/components.mermaid) | Component-level breakdown: memory, context, principles, defense subsystems |
| [config/mcp-config.json](docs/architecture/config/mcp-config.json) | Reference MCP configuration with tool schemas |
| [implementation-notes.md](docs/architecture/implementation-notes.md) | Setup guide, development patterns, challenges, testing strategy |

### C4 Model ASCII Diagrams

| Level | Scope | File |
|-------|-------|------|
| Level 1 — Context | Users, external systems, system boundary | [c4-level1-context.txt](docs/architecture/diagrams/c4-level1-context.txt) |
| Level 2 — Container | Services, databases, queues, subsystems | [c4-level2-container.txt](docs/architecture/diagrams/c4-level2-container.txt) |
| Level 3 — Component | Memory pipeline + ContextEngine detail | [c4-level3-component.txt](docs/architecture/diagrams/c4-level3-component.txt) |

> Mermaid diagrams can be viewed live at [mermaid.live](https://mermaid.live).

---

## Development Guide

### Setup

```bash
make dev-install          # Install all dependencies (including dev)
make pre-commit-install   # Install git hooks (ruff + mypy)
```

### Common Commands

```bash
make lint                 # Code quality check (ruff)
make format               # Auto-format (ruff)
make test                 # Run all tests
make check                # Full check chain: lint + format-check + test
```

### Running Specific Tests

```bash
pytest tests/ -k "memory"       # Memory domain tests only
pytest tests/ -k "decay"        # Decay engine tests
pytest tests/ -k "context"      # Context engine tests
pytest tests/ --cov=plastic_promise --cov-report=html  # Coverage report
```

### Conventions

This project follows the [Plastic Promise core conventions](.trae/rules). All contributors should, before committing:

1. Call `memory_recall` + `context_supply` to load relevant context
2. Ensure each substantive change has a corresponding git commit
3. Execute `step-closure` after completing work

---

## Privacy & Data Security

- **Local-first architecture**: All data is stored locally (SQLite + LanceDB). No data is sent to any external service.
- **Sensitive data isolation**: Trust scores, principle adherence records, and SCARF introspection results are stored in the local SQLite database and never leave the machine.
- **Audit trail**: Every agent action and system decision is recorded in the audit log (`audit_run`) and hunter failure log (`hunter_failure_log` table).
- **Telemetry opt-out**: Set `DO_NOT_TRACK=1` to fully disable all telemetry.
- **Rate limiting**: Built-in rate limiting on the MCP server prevents abuse in multi-agent scenarios.
- **Reporting**: Security issues can be reported via [SECURITY.md](SECURITY.md).

---

## License

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for the full text.

| Permission | Status |
|------------|--------|
| Commercial use | Allowed |
| Modification | Allowed |
| Distribution | Allowed |
| Private use | Allowed |
| Liability | Not provided |
| Warranty | Not provided |

---

<div align="center">

**Plastic Promise** — Built by [ALdaisuki](https://github.com/ALdaisuki)

Star this repo if you find it helpful.

[![GitHub Stars](https://img.shields.io/github/stars/ALdaisuki/plastic-promise?style=social)](https://github.com/ALdaisuki/plastic-promise/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/ALdaisuki/plastic-promise?style=social)](https://github.com/ALdaisuki/plastic-promise/network/members)

</div>

# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| `0.1.x` | Yes |
| Tagged releases | Yes |
| Feature branches | No |

## Reporting a Vulnerability

Plastic Promise may handle sensitive local data such as memories, audit logs, task records, and agent trust scores. If you discover a security vulnerability:

**Do not open a public issue.**

Report via:

1. **GitHub Security Advisory**: [Report a vulnerability](https://github.com/ALdaisuki/plastic-promise-release/security/advisories/new)
2. **Direct contact**: email the maintainer address listed on the GitHub profile.

Include:

- Description of the vulnerability.
- Steps to reproduce.
- Affected versions or commits.
- Potential impact.
- Suggested fixes, if available.

Response targets:

| Severity | Target |
|---|---|
| Critical | 72 hours |
| High | 7 days |
| Medium | 14 days |
| Low | Next planned release |

## Security Design

### Local-first data model

Plastic Promise stores runtime state locally by default:

- SQLite for structured memory, trust, task, and audit state.
- LanceDB for vector/search state.
- `var/log/` and `var/run/` for local runtime logs, PIDs, and heartbeat files.

Data may leave the machine only when the operator configures external agents, hosted embedding providers, hosted rerankers, hosted LLM integrations, or other network adapters. Review configuration before using external providers with sensitive data.

### Input validation

- MCP tool parameters are declared through JSON schemas.
- Database operations should use parameterized queries.
- Memory content passes through filtering and quality gates before durable use.
- Plugin metadata is validated before plugin code is imported or executed.

### Defense layers

| Layer | Purpose |
|---|---|
| L0 hard boundary | Block dangerous or forbidden operations. |
| L1 trust constraints | Adjust autonomy based on persisted trust score. |
| L2 immune patrol | Periodic scans, audit reports, and repair task generation. |

### Audit trail

- Key operations should have a tool call, git diff, audit entry, or task lifecycle trace.
- `step-closure` records substantive outcomes and lessons for future retrieval.
- Runtime audit logs are local runtime files and should not be committed unless explicitly sanitized and intended for documentation.

## Dependency Security

Recommended checks:

```bash
ruff check plastic_promise/
pytest
bandit -r plastic_promise/ -ll
cargo audit --manifest-path rust/context-engine-core/Cargo.toml
```

Review changes to dependency manifests and lockfiles carefully.

## Operational Best Practices

- Do not commit `.env` files or local secrets.
- Keep runtime directories, caches, PID files, and local database files out of release commits.
- Run `audit_pre_check` before risky write, delete, or execution actions when using the governed workflow.
- Run `audit_run(action="full")` regularly for local system health.
- Use `pack_export` for memory backups when moving knowledge between environments.
- Do not merge PRs without explicit maintainer authorization.

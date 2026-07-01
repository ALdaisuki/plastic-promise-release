# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x (latest main) | Yes |
| tagged releases | Yes |
| feature branches | No |

## Reporting a Vulnerability

Plastic Promise handles sensitive data (memories, audit logs, Agent trust scores). If you discover a security vulnerability:

**Do not open a public Issue.**

Report via:

1. **GitHub Security Advisory** (preferred): [Report a vulnerability](https://github.com/ALdaisuki/plastic-promise/security/advisories/new)
2. **Direct contact**: Email the maintainer at the address listed on the GitHub profile

### What to include

- Description of the vulnerability
- Steps to reproduce
- Affected versions / commits
- Potential impact
- Any suggested fixes (optional)

### Response timeline

- **Acknowledgment**: within 48 hours
- **Status update**: within 5 business days
- **Resolution**: depends on severity (critical: 72h target, high: 7d, medium: 14d, low: next release)

## Security Design

### Data Isolation
- Memory pool isolated by scope; no cross-contamination in multi-Agent scenarios
- Trust scores and audit logs stored in independent database tables

### Input Validation
- All MCP tool parameters validated via JSON Schema
- Memory content filtered through noise detector (`noise_filter.is_noise()`)
- SQL injection prevention: all queries use parameterized statements

### Defense Layers
- **L0 Hard Boundary**: Absolute rules, intercepted by pre_check
- **L1 Trust Constraints**: Trust-score-driven — high score relaxes, low score tightens
- **L2 Immune Patrol**: 24-hour cycle scanning with auto-repair

### Audit Trail
- Every operation generates audit trail (tool name, timestamp, parameter summary)
- Key decisions have full git trace
- Audit logs written to `step_audit_log.jsonl` (gitignored)

## Dependency Security

- Python: `bandit -r plastic_promise/ -ll` runs on every PR
- Rust: `cargo audit` runs on every PR
- Dependencies pinned with version ranges in `pyproject.toml` and `Cargo.toml`
- Review diffs to lockfiles (`Cargo.lock`) for unexpected changes

## Best Practices

- Never commit `.env` files (excluded in `.gitignore`)
- Run `audit_run(action="full")` regularly to check system health
- Trust score below 0.30 requires manual approval for Agent operations
- Use `pack_export` for periodic memory backups
- `main` branch protected; force push forbidden; all changes via reviewed PR

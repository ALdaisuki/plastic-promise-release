# Contributing to Plastic Promise

Thank you for improving Plastic Promise. This project is a local-first MCP runtime for agent memory, context supply, audit, trust, skills, and governed task dispatch.

Architecture and current status:

- [README.md](README.md)
- [docs/SYSTEM_FULL_CHAIN.md](docs/SYSTEM_FULL_CHAIN.md)
- [docs/architecture/architecture.md](docs/architecture/architecture.md)
- [docs/GOAL.md](docs/GOAL.md)

## Development Setup

```bash
git clone git@github.com:ALdaisuki/plastic-promise-release.git
cd plastic-promise-release
pip install -e ".[dev]"
```

Optional Rust engine development:

```bash
cd rust/context-engine-core
cargo build
cargo test
```

## Branch Convention

| Prefix | Usage |
|---|---|
| `feat/` | New feature |
| `fix/` | Bug fix |
| `refactor/` | Refactor with no behavior change |
| `docs/` | Documentation |
| `perf/` | Performance |
| `test/` | Tests |
| `chore/` | Build, CI, tooling |
| `worktree/<agent>/` | Agent worktree isolation |

Public release work targets `main` unless a maintainer explicitly chooses another integration branch.

Branch names should be lowercase and hyphen-separated.

## Commit Convention

Use [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<scope>): <subject>
```

Examples:

```text
feat(memory): add compaction cooldown
fix(context): label fallback retrieval explicitly
docs: refresh release README
```

Prefer small, logical commits. A PR can contain multiple commits if each commit represents a reviewable unit.

## Pull Request Flow

1. Create a topic branch.
2. Develop with small logical commits.
3. Run relevant checks.
4. Open a PR with a clear summary and verification notes.
5. Address review feedback.
6. Wait for explicit maintainer approval before merge.

Maintainer rule: do not merge any PR without explicit authorization. Creating a PR is safe; merging is a separate action that requires clear approval.

## Testing

```bash
pytest
pytest tests/ --cov=plastic_promise --cov-report=term
ruff check plastic_promise/
mypy plastic_promise/ --ignore-missing-imports
```

Make shortcuts:

```bash
make test-fast
make lint
make check
```

Tests are required for behavior changes. Documentation-only changes should still be checked for broken links, stale commands, and factual drift.

## Documentation Expectations

Update docs when public behavior changes:

- `README.md` for user-facing install, launch, architecture, or status changes.
- `docs/README.zh-CN.md` for Chinese quickstart changes.
- `docs/architecture/architecture.md` for subsystem or data-flow changes.
- `docs/TODO List/README.md` when roadmap status changes.
- `CHANGELOG.md` before releases.

Project files should use clean professional text. Avoid emoji as status markers in docs and roadmaps.

## Code Review Guidelines

Review comments should identify the kind of issue:

| Type | Meaning |
|---|---|
| `nit` | Minor style or naming issue. |
| `design` | Architecture or maintainability concern. |
| `blocking` | Correctness, security, data-loss, or release-blocking issue. |
| `praise` | Positive signal worth preserving. |

## Security

Do not open public issues for vulnerabilities. Follow [SECURITY.md](SECURITY.md).

## Questions

Open an issue or start from [docs/SYSTEM_FULL_CHAIN.md](docs/SYSTEM_FULL_CHAIN.md) for the system mental model.

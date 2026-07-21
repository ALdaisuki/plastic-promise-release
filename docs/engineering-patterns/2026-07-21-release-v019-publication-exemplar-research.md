---
title: Release v0.1.19 Publication Exemplar Research
date: 2026-07-21
status: reviewed
topic: dashboard-structured-memory-release-publication
references:
  - googleapis/release-please@5b3157824739ed7fe5d2e0fd5d1c1c8f0c4fa204
  - changesets/changesets@a736a20c230a89232a122fe12ffd612361e0eef9
  - pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247
---

# Release v0.1.19 Publication Exemplar Research

## Decision

Publish the Dashboard V2, governed synthesis, retrieval explanation, structured
chunking, and lineage work as `v0.1.19`. The public release repository already
contains immutable tag `v0.1.18`, so reusing that version would break release
identity. Version metadata, changelog text, bilingual upgrade guidance, and
rollback instructions must be reviewed in the development PR before release
synchronization. Publication then uses the existing fail-closed
`scripts/release-sync.py` dry run and its single live `--push` path.

## Reference 1: Google Release Please

Source:
[`src/manifest.ts`](https://github.com/googleapis/release-please/blob/5b3157824739ed7fe5d2e0fd5d1c1c8f0c4fa204/src/manifest.ts)
and
[`src/github.ts`](https://github.com/googleapis/release-please/blob/5b3157824739ed7fe5d2e0fd5d1c1c8f0c4fa204/src/github.ts).

### Q1: What exactly does it do?

`Manifest.buildPullRequests()` builds release candidates before
`createPullRequests()` opens or updates them (manifest lines 537-539 and
926-929). Release creation is a separate phase: `createReleases()` first groups
candidate releases by merged pull request (lines 1225-1242), while the GitHub
adapter exposes `createRelease()` independently (github lines 940-945). This
keeps reviewable version/changelog changes separate from publishing.

### Q2: How does our context differ?

Plastic Promise has one Python package and a filtered public release repository,
not a multi-component manifest. Its release script already binds source HEAD,
release-tree bytes, commit OID, annotated tag object, and remote state. Adding a
generic release framework would duplicate stronger project-specific controls.

### Q3: What should we adapt vs skip?

- **Adapt:** put version and release notes in the development PR, then publish
  only after the PR is merged.
- **Adapt:** keep PR merge and release creation as separate auditable phases.
- **Skip:** component manifests, provider abstraction, and generated release PR
  machinery.

## Reference 2: Changesets

Source:
[`packages/cli/src/commands/version/index.ts`](https://github.com/changesets/changesets/blob/a736a20c230a89232a122fe12ffd612361e0eef9/packages/cli/src/commands/version/index.ts)
and
[`packages/apply-release-plan/src/index.ts`](https://github.com/changesets/changesets/blob/a736a20c230a89232a122fe12ffd612361e0eef9/packages/apply-release-plan/src/index.ts).

### Q1: What exactly does it do?

The version command assembles a release plan and applies it before optionally
committing touched files (version command lines 119-142). Without automatic
commit it explicitly tells the operator to review and commit the files (lines
152-155). The release-plan implementation updates the package version (lines
149-156) and changelog together (lines 166-170), making release intent visible
as ordinary repository changes.

### Q2: How does our context differ?

Plastic Promise is not a package monorepo and does not need dependency graph or
peer-range rewriting. It has two synchronized version fields and a public tree
transform that substitutes the requested release version.

### Q3: What should we adapt vs skip?

- **Adapt:** update `pyproject.toml`, `plastic_promise/__init__.py`, and
  `CHANGELOG.md` in the same reviewed commit.
- **Adapt:** make the changelog state verification facts and rollback limits,
  not only feature names.
- **Skip:** changeset fragments, workspace dependency propagation, and snapshot
  release modes.

## Reference 3: PyPA GitHub Action for PyPI

Source:
[`action.yml`](https://github.com/pypa/gh-action-pypi-publish/blob/ba38be9e461d3875417946c167d0b5f3d385a247/action.yml).

### Q1: What exactly does it do?

The action defaults metadata verification to true, `skip-existing` to false,
and attestations to true for Trusted Publishing (action lines 37-50 and 83-88).
Its execution boundary receives a concrete packages directory and forwards the
verification and attestation switches (lines 171-178). Existing artifacts are
therefore not silently treated as a successful fresh publication.

### Q2: How does our context differ?

This task synchronizes and tags the public Git repository; PyPI publication is
owned by the release repository workflow. Local Codex must not upload an
unreviewed wheel or bypass the repository's provenance boundary.

### Q3: What should we adapt vs skip?

- **Adapt:** fail if `v0.1.19` already exists and preserve the default refusal
  to skip an existing distribution.
- **Adapt:** build and inspect package artifacts before publication evidence is
  claimed.
- **Skip:** direct local PyPI upload and token handling.

## Plastic Promise Release Contract

1. Use `origin/main..HEAD` only for preview; live release sync uses the exact
   merged range with source `main` clean and equal to `origin/main`.
2. Run the high-risk ten-item audit and require automated audit score at least
   `0.60` with zero blocking findings.
3. Update English and Chinese user documentation together, including defaults,
   opt-in gates, migration, and rollback.
4. Run targeted tests, whole-repository regression, Rust release tests, Ruff,
   compileall, JavaScript syntax, diff checks, live HTTP smoke, and release-sync
   dry run before merge/publication.
5. Use exactly one live release-sync invocation with `--push`; do not create a
   local release commit/tag first and do not use a manual `git push --tags`.
6. Verify remote development `main`, public release `main`, annotated
   `v0.1.19`, package version, and live runtime after publication.

## Quality Review

- All references are pinned to immutable commits and cite concrete source paths
  and line ranges inspected during this task.
- Every reference answers what it does, how Plastic Promise differs, and what
  to adapt or skip.
- The result reuses the existing release-sync authority instead of adding a
  release framework or a second publishing path.
- The version decision is grounded in the actual public release repository,
  where `v0.1.18` already exists at commit `9e86ece`.
- Publication claims remain conditional on fresh verification and audit; prior
  `0.1.18` evidence is not relabeled as `0.1.19` evidence.

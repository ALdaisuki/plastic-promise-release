---
name: superpowers:exemplar-research
description: "MUST use after brainstorming and before using-git-worktrees — search for mature implementations, analyze with three-question method, write analysis doc, quality review before storage. Ensures you learn from existing solutions before building."
---

# Exemplar Research — Learn From Mature Implementations

Search for and analyze mature engineering implementations before building. Uses the three-question method to extract patterns worth adopting or adapting.

<HARD-GATE>
Do NOT invoke any implementation skill, write any code, or take any implementation action until exemplar research is complete and the analysis doc is written and reviewed. This applies to EVERY project that has passed brainstorming.
</HARD-GATE>

## Anti-Pattern: "We Already Know The Answer"

Even when you've studied a reference project before, exemplar research forces structured comparison. Skip it and you'll miss context-specific adaptations that make the difference between copying and integrating.

## Checklist

You MUST create a task for each of these items and complete them in order:

1. **Identify 2-4 mature reference implementations** — search GitHub, docs, papers for projects that solve similar problems
2. **Three-question analysis per reference** — (a) What exactly does it do? (b) How does our context differ? (c) What to adapt vs skip?
3. **Write analysis document** — save to `docs/engineering-patterns/YYYY-MM-DD-<topic>-exemplar-research.md`
4. **Quality review** — check for concrete patterns, not vague summaries; verify claims against source code
5. **Store findings in memory** — key patterns enter the memory pool for future retrieval
6. **Transition to using-git-worktrees** — invoke the git-worktrees skill to create isolated workspace

## Three-Question Method

For each reference implementation, answer these three questions with code-level precision:

### Q1: What exactly does it do?
- Read the source code, not just the README
- Find concrete formulas, data structures, API signatures
- Report exact thresholds, defaults, fallback behaviors
- Cite specific files and line numbers

### Q2: How does our context differ?
- What governance constraints exist (trust scores, principles, audit)?
- What infrastructure is already in place (LanceDB, SQLite, daemon)?
- What are our unique advantages (entity graph, domain federation)?
- What constraints do we have (Ollama-only, no external API dependencies)?

### Q3: What should we adapt vs skip?
- **Adapt**: Patterns that fit our governance architecture with minor changes
- **Redesign**: Patterns that need significant rework for our context
- **Skip**: Patterns that conflict with core principles or duplicate existing functionality
- For each adapted pattern: specify exact integration point, expected LOC, env var gate

## Process Flow

```
Identify references → Read source code → Three-question analysis per ref
  → Write analysis doc → Quality review → Store findings → Invoke using-git-worktrees
```

## Key Principles

- **Source code over documentation** — Claims without code references are speculation
- **Adapt over copy** — Every pattern must be filtered through Plastic Promise governance
- **Concrete over vague** — Exact formulas, line numbers, thresholds
- **Gate after gate** — Exemplar research completes before any implementation begins

## Red Flags

- Quoting README without reading source code
- Recommending to "just copy" a pattern without adaptation analysis
- Skipping Q2 (context difference) — the most important question
- Proceeding to implementation without writing the analysis doc
- Storing findings without quality review

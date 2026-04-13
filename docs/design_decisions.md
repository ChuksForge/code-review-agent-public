# Design Decisions

Why this system is built the way it is — and what was deliberately left out.

---

## Why a Critic Loop Instead of a Single LLM Pass

The standard approach for LLM-based code review is one prompt: give the model the code, ask it to find issues, get output. This fails in production for two reasons.

**Hallucination.** LLMs flag correct code as buggy when they partially recognise a pattern. They're especially prone to this with security patterns (flagging safe parameterized queries as "potential injection") and performance patterns (flagging O(n) code as O(n²) based on surface appearance). Without a verification step, these hallucinations reach the developer.

**Noise flooding.** Without severity signal, every finding looks equally important. A line-length warning sits next to an SQL injection. Developers learn to ignore the output within days.

The Critic loop addresses both by making evaluation a separate, focused task. The Critic is not trying to identify issues — it's only scoring issues that were identified by someone else. This specialisation dramatically improves calibration: the model isn't anchored to its own prior output and can evaluate each finding independently.

The separation also enables category-specific threshold tuning. Security issues get a lower confidence bar than style issues. This would be impossible to express in a single-pass prompt without collapsing the distinction.

---

## Why Static Analysis Runs Before Any LLM Call

Static tools (AST, pylint, mypy, semgrep) run first and their findings are fed to the Planner as context. This is intentional.

LLMs hallucinate less when they have ground-truth signals to reason from. A bare `except:` flagged by both the AST visitor (AST001) and pylint (W0702) is far more likely to be correctly identified and correctly categorised than one the model notices by inspection alone.

Static tools also act as a precision floor: if a tool flags something and the Planner doesn't, it's a Planner miss. If the Planner flags something with no static tool corroboration, the Critic applies higher scrutiny (lower confidence score expected).

This hybrid approach — static tools for ground truth, LLMs for reasoning and explanation — is more reliable than either alone.

---

## Why the Reconciler Has No LLM Call

The Reconciler does deduplication and ranking. Both are fully deterministic operations:

- Deduplication: do two suggestions cover overlapping lines in the same file with the same category? If yes, keep the higher-severity one.
- Ranking: apply a fixed formula — `impact = (severity × category_weight) + priority_bonus`.

Using an LLM here would introduce:
- **Non-determinism**: same input, different ranking on each run
- **Latency**: an extra API call at the final stage
- **Cost**: tokens spent on something arithmetic handles better
- **Testability**: unit tests would require mocking or live API calls

Deterministic output also makes it easier to measure whether prompt changes to earlier stages actually improved results.

---

## Why Agents Have Separated Responsibilities

A common failure mode in multi-agent systems is giving one agent too much responsibility. An agent that both identifies issues *and* evaluates their severity tends to self-censor: it anchors on its own prior output and either over-commits (confident hallucinations) or under-commits (excessive hedging).

The role separation here is strict:
- **Planner**: find everything. Explicitly prohibited from filtering.
- **Critic**: evaluate everything the Planner found. Explicitly prohibited from identifying new issues.
- **Rewriter**: fix approved issues. Explicitly prohibited from refactoring beyond the specific problem.

This specialisation improves each agent's output quality and makes the system easier to debug: if recall is low, the Planner prompt needs work. If precision is low, the Critic thresholds need adjustment. You know where to look.

---

## Why Pylint is Curated to ~20 Rules

Default pylint produces 200–400 findings on a medium-sized Python file. The majority are style conventions (line length, naming, docstring format). If these reach the Planner, they dilute the signal and increase the chance that the Planner wastes its context window on noise.

The `PYLINT_SIGNAL_RULES` set in `core/config.py` keeps only rules in the E (error) and W (warning) categories that correspond to actual bugs or logic issues. All C (convention) and most R (refactor) rules are suppressed.

This is a deliberate precision/recall trade-off: we accept lower recall on style issues in exchange for dramatically higher precision on the findings that reach the LLM.

---

## Why Chunks Are Scoped to Functions and Classes

The ingestion layer splits files at the function/class boundary rather than by line count or file. The rationale:

1. **Context coherence**: a function is a natural unit of logic. The LLM can reason about a function's behaviour without needing surrounding context.
2. **Line number accuracy**: issue line numbers are relative to the chunk's `start_line`, making them accurate in the final output.
3. **Batch efficiency**: 10 small, focused chunks per Planner call produces better results than 2 large file dumps.

The 120-line cap exists as a safety limit for unusually large functions. Files without any functions or classes (scripts, config files) are returned as a single chunk.

---

## What Was Deliberately Not Built

**Multi-language support.** The ingestion layer uses Python's `ast` module, which is Python-only. Extending to TypeScript, Go, or Rust would require tree-sitter parsers for each language. The agent layer is language-agnostic — only ingestion and static analysis are Python-specific.

**IDE integration.** The primary output target is GitHub PRs and CI/CD pipelines, not LSP/editor plugins. The JSON output format is designed to be machine-readable enough that editor integration could be layered on top.

**Auto-apply fixes.** The Rewriter generates suggested fixes but never applies them automatically. Code review tools that auto-apply LLM suggestions create more problems than they solve — the developer must be in the loop on every change.

**Real-time / streaming output.** The pipeline runs to completion before producing output. Streaming partial results would require more complex state management and is reserved for the production system.

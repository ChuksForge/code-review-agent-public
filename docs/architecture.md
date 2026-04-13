# Architecture

This document explains how the Code Review Agent works, stage by stage.

---

## Pipeline Overview

```
Input
  │
  ▼
Ingestion ──────── chunks code by function/class via AST
  │
  ▼
Static Analysis ── 5 tools produce ground-truth signals
  │
  ▼
Planner ─────────── LLM identifies all potential issues (breadth-first)
  │
  ▼
Critic ──────────── LLM scores severity × confidence, filters noise   ← key stage
  │
  ▼
Rewriter ────────── LLM generates exact fixes for approved issues only
  │
  ▼
Reconciler ──────── deterministic dedup + impact ranking (no LLM)
  │
  ▼
Output ──────────── review.json + review.md
```

---

## Stage 1 — Ingestion

**File:** `tools/ingestion.py`

Raw input is converted into `CodeChunk` objects — the atomic unit passed through the entire pipeline.

Each chunk is scoped to a single top-level function or class using Python's `ast.iter_child_nodes()`. Chunks are capped at 120 lines. The rationale: smaller, focused context windows reduce irrelevant surrounding code polluting issue detection. A 500-line file with 5 functions produces 5 focused chunks, not one giant prompt.

Three input modes:

| Mode | Mechanism |
|------|-----------|
| `local` | Walks directory tree, skips `.git`/`venv`/`__pycache__`, chunks by AST |
| `github` | PyGitHub fetches changed files at PR head SHA — only reviews what changed |
| `git_diff` | `git diff --name-only HEAD~1 HEAD` — same logic, local repo |

Files that fail AST parsing (syntax errors) are returned as a single chunk so they still get reviewed.

---

## Stage 2 — Static Analysis

**File:** `tools/static_analysis.py`

Five tools run over every chunk before any LLM call is made. This is intentional: the LLM reasons *on top of* ground-truth signals rather than pure intuition. This dramatically reduces hallucination on pattern-based issues.

### AST Visitor (in-process)

A custom `ast.NodeVisitor` that catches:
- `AST001` — Bare `except:` clauses
- `AST002` — Mutable default arguments (list, dict, set)
- `AST003` — Use of `eval()` or `exec()`
- `AST004` — SQL query built with f-strings or string concatenation
- `AST005` — `assert` used for runtime validation (stripped with `-O`)
- `AST006` — Shadowing of built-in names (`list`, `id`, `type`, etc.)

Zero subprocess overhead. Runs on every chunk in milliseconds.

### pylint (subprocess, JSON output)

Configured with `--disable=all --enable=<signal_rules>` — only ~20 high-signal rule IDs are enabled. This is critical: default pylint produces hundreds of style findings that bury real issues. The curated rule list lives in `core/config.py` as `PYLINT_SIGNAL_RULES`.

### mypy (subprocess)

Runs with `--ignore-missing-imports` and `--show-column-numbers`. Catches type mismatches, missing `None` checks, and incorrect return types. Output is parsed line-by-line from stdout.

### semgrep (subprocess, JSON output)

Uses the `p/python` and `p/secrets` rulesets — curated SAST patterns covering security vulnerabilities and hardcoded credentials. More rulesets can be added in `core/config.py`.

### tree-sitter (in-process)

Used for structural pattern matching rather than semantic analysis:
- `TS001` — Nesting depth ≥ 4 (deeply nested control flow)
- `TS002` — Parameter count > 7 (consider a config object)

Falls back gracefully if `tree-sitter-python` is not installed.

All findings are deduplicated by `(tool, file_path, line, rule_id)` before entering the pipeline.

---

## Stage 3 — Planner Agent

**File:** `agents/planner_stub.py`

**Role: breadth. Find everything. Filter nothing.**

The Planner receives code chunks and static findings (batched by file, 10 chunks per API call) and produces `PlannerIssue` objects across 6 categories:

| Category | What it covers |
|----------|----------------|
| `logic_bug` | Incorrect behavior, off-by-one, wrong conditionals, missing edge cases |
| `security` | Injection, hardcoded secrets, insecure defaults, auth bypass patterns |
| `performance` | O(n²) in hot paths, N+1 queries, blocking I/O |
| `type_error` | None dereference, type mismatches, missing annotations |
| `maintainability` | Deeply nested code, god functions, dead code |
| `style` | Only when masking a logic error (e.g., ambiguous comparisons) |

The Planner system prompt explicitly instructs the model **not to filter** — that responsibility belongs to the Critic. A Planner that tries to do both tends to self-censor real bugs when it's uncertain.

---

## Stage 4 — Critic Agent

**File:** `agents/critic_stub.py`

**Role: precision. Kill the noise.**

This is the key differentiator. The Critic receives every `PlannerIssue` and scores it on two independent dimensions:

```
severity_score  ∈ [0.0, 10.0]   — how bad is this if left unfixed?
confidence      ∈ [0.0,  1.0]   — is this actually a real issue?
```

**Both** must exceed their category-specific thresholds for an issue to proceed.

### Threshold System

| Category | Severity threshold | Confidence threshold | Rationale |
|----------|--------------------|---------------------|-----------|
| `security` | 3.5 | **0.40** | Lower bar — don't miss vulnerabilities |
| `logic_bug` | 3.5 | **0.50** | Lower bar — logic bugs often have limited context |
| `type_error` | 3.5 | **0.50** | mypy corroboration helps but isn't always present |
| `performance` | 3.5 | 0.55 | Requires evidence of hot-path usage |
| `maintainability` | 3.5 | 0.55 | Default |
| `style` | **6.0** | 0.55 | Must be genuinely impactful to be worth raising |

### Verdict Schema

```
"approve"   → passes thresholds, forward to Rewriter
"reject"    → noise, false positive, or trivially unimportant
"escalate"  → approve AND flag as critical priority
```

`is_false_positive: bool` is a first-class field — the Critic explicitly marks cases where correct code was incorrectly flagged. These are discarded regardless of score.

### Why Dual Thresholds?

A single score would conflate two different failure modes:

- **High severity, low confidence** → probable hallucination. The model thinks it *might* be catastrophic but isn't sure. Reject.
- **Low severity, high confidence** → real issue, genuinely trivial. Reject.
- **High severity, high confidence** → genuine, impactful issue. Approve.

Treating them independently gives much better calibration.

### Observed Filter Rate

On typical Python codebases, the Critic filters 40–60% of Planner findings as noise or false positives. On the demo file, 17–50% are filtered depending on LLM sampling variation. This is expected and correct behaviour.

---

## Stage 5 — Rewriter Agent

**File:** `agents/rewriter_stub.py`

**Role: exact fixes, not advice.**

The Rewriter only receives Critic-approved issue IDs. For each one it generates:

| Field | Content |
|-------|---------|
| `suggested_fix` | The exact corrected code, preserving indentation and style |
| `explanation` | Why the original is wrong and what the fix achieves |
| `impact_summary` | One sentence on real-world consequence if unfixed |
| `references` | PEP numbers, CVE IDs, OWASP items where applicable |

The system prompt explicitly prohibits refactoring beyond the specific issue. No scope creep, no unrelated changes.

---

## Stage 6 — Reconciler

**File:** `agents/reconciler.py`

**Role: final output. Deterministic — no LLM call.**

Making the Reconciler LLM-free was a deliberate choice:
- **Stable**: same input always produces the same ranking
- **Testable**: unit tests don't require API calls
- **Fast**: no latency at the output stage
- **Cheap**: no tokens spent on what an algorithm handles better

### Deduplication

Overlapping suggestions (same file + overlapping line range + same category) are merged, keeping the higher-severity version. This prevents the same bug from appearing twice when AST and pylint both flag it.

### Ranking

```python
impact_score = (severity_score × category_weight) + priority_bonus
```

Category weights (from `core/config.py`):

| Category | Weight |
|----------|--------|
| security | 2.0 |
| logic_bug | 1.8 |
| type_error | 1.4 |
| performance | 1.2 |
| maintainability | 0.8 |
| style | 0.4 |

The final ranked list is serialized to `outputs/example_review.json` and rendered to `outputs/example_review.md`.

---

## Data Flow

```
CodeChunk[]
    │
    ├──► StaticFinding[]  (5 tools, deduplicated)
    │         │
    │         ▼
    └──► PlannerIssue[]  (LLM, batched)
              │
              ▼
         CriticScore[]  (LLM, dual threshold)
              │
              ├── rejected (noise / false positives) ──► discarded
              │
              └── approved_ids[]
                        │
                        ▼
                 RewriterSuggestion[]  (LLM, one per approved issue)
                        │
                        ▼
                 ReviewOutput  (deterministic dedup + rank)
```

---

## Production System Differences

The open core uses a simplified linear pipeline. The production system includes:

- **LangGraph typed StateGraph** — full state accumulation with `Annotated[list, operator.add]` reducers, conditional edges, and loop control
- **Multi-pass critic loop** — the Planner can be re-invoked with category-specific focus based on Critic feedback
- **Parallel static analysis** — tools run concurrently per chunk rather than sequentially
- **Advanced batching** — large repos (100k+ LOC) use intelligent chunking with priority ordering
- **GitHub Actions integration** — auto-posts findings as PR comments, updates on re-push

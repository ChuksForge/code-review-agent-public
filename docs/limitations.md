# Limitations

Being honest about what this system does not do well is part of building trust.

---

## Language Support

The ingestion layer and static analysis tools are **Python-only**. The `ast` module, pylint, mypy, and semgrep's `p/python` ruleset all target Python specifically.

The Planner and Critic agent prompts are language-agnostic — they reason about whatever code they're given — but without language-specific static analysis signals feeding them, precision on non-Python code will be lower.

**Roadmap:** tree-sitter parsers exist for most languages. Extending ingestion to TypeScript, Go, or Rust is the primary path to multi-language support.

---

## Context Window Limits

Each Planner call processes up to 10 code chunks. For very large files or functions, a single chunk can consume significant context. The 120-line cap mitigates this but doesn't eliminate it.

If a bug spans multiple functions or requires understanding module-level context to diagnose, the Planner may miss it or mischaracterise it. The static analysis layer partially compensates — cross-function issues sometimes surface as individual findings per function — but this is a genuine limitation.

---

## Recall on Logic Bugs Requiring Global State

The Planner sees one batch of chunks at a time. Bugs that only manifest when reasoning about how multiple functions interact — race conditions, incorrect state machine transitions, cross-module assumptions — are difficult to detect with the current chunking strategy.

The eval harness benchmarks single-function bugs. Multi-function logic errors are out of scope for v1.

---

## LLM Non-Determinism

The Planner and Critic use temperature > 0, which means the same code can produce different findings on different runs. The Critic filter rate on the demo file varies between 17% and 50% across runs. This is expected behaviour — it's not a bug — but it means recall and precision metrics have variance.

Running the eval harness multiple times and averaging results gives a more reliable picture than a single run.

---

## False Negative Rate

The Critic's job is to reject noise. It also rejects some real issues that are close to the threshold. The dual-threshold system errs toward precision (fewer but better suggestions) at the cost of some recall.

On the 8-case benchmark, recall is approximately 87%. The missing ~13% are typically issues where:
- The Planner identified the issue but described it unclearly
- The Critic scored confidence below threshold due to limited code context
- The static tools didn't corroborate the finding

Lowering `CONFIDENCE_THRESHOLD` in `core/config.py` increases recall at the cost of more noise in the output.

---

## Semgrep Availability

Semgrep requires a separate installation and may not be available in all environments. The static analysis runner handles this gracefully — if semgrep is not installed, `semgrep_runner()` returns an empty list and the pipeline continues. The other four tools cover the most critical patterns.

To install: `pip install semgrep`

---

## No Fix Validation

The Rewriter generates suggested fixes but does not run them or validate that they parse correctly. In rare cases, the suggested fix may contain syntax errors or introduce new issues. Always review generated fixes before applying them.

---

## This Is the Open Core

The open core published here uses a simplified linear pipeline without LangGraph. The production system includes multi-pass critic loops, adaptive batching for large repos, and advanced prompt tuning not included here. The open core is fully functional and produces real output — but for very large codebases or production CI/CD use cases, the production system is more robust.

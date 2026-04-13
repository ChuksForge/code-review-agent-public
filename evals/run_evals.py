"""
evals/run_evals.py
------------------
Benchmark harness for the Code Review Agent.

Runs against 8 known-buggy Python snippets with ground truth labels.
Measures recall, category accuracy, and average suggestion count.

Usage:
    python evals/run_evals.py
    python evals/run_evals.py --verbose
    python evals/run_evals.py --case bug_sql_injection  # single case

Results saved to outputs/eval_results.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.pipeline import run_pipeline


# ─────────────────────────────────────────────
# Ground truth benchmark
# ─────────────────────────────────────────────

@dataclass
class BugCase:
    id: str
    file: str                    # filename in evals/cases/
    title: str
    expected_category: str
    expected_keyword: str        # must appear in agent output to count as "found"
    severity: str


BENCHMARK: list[BugCase] = [
    BugCase("BUG001", "bug_mutable_default.py",   "Mutable default argument",               "logic_bug",     "mutable",    "high"),
    BugCase("BUG002", "bug_sql_injection.py",      "SQL injection via f-string",             "security",      "injection",  "critical"),
    BugCase("BUG003", "bug_bare_except.py",        "Bare except swallows KeyboardInterrupt", "logic_bug",     "except",     "medium"),
    BugCase("BUG004", "bug_eval_rce.py",           "eval() on user input (RCE)",             "security",      "eval",       "critical"),
    BugCase("BUG005", "bug_off_by_one.py",         "Off-by-one in list slicing",             "logic_bug",     "off-by-one", "high"),
    BugCase("BUG006", "bug_none_dereference.py",   "None dereference before .attribute",     "type_error",    "None",       "high"),
    BugCase("BUG007", "bug_on_squared.py",         "O(n²) nested loop",                      "performance",   "O(n",        "medium"),
    BugCase("BUG008", "bug_hardcoded_secret.py",   "Hardcoded API key in source",            "security",      "secret",     "critical"),
]


# ─────────────────────────────────────────────
# Eval result
# ─────────────────────────────────────────────

@dataclass
class EvalResult:
    case_id: str
    title: str
    expected_category: str
    severity: str
    found: bool = False
    correct_category: bool = False
    suggestions_count: int = 0
    agent_titles: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────

def run_case(case: BugCase, cases_dir: Path, verbose: bool = False) -> EvalResult:
    result = EvalResult(
        case_id=case.id,
        title=case.title,
        expected_category=case.expected_category,
        severity=case.severity,
    )

    case_path = cases_dir / case.file
    if not case_path.exists():
        print(f"  [WARN] Case file not found: {case_path}")
        return result

    output = run_pipeline(
        input_mode="local",
        target_path=str(case_path),
        verbose=verbose,
    )

    if not output:
        return result

    result.suggestions_count = len(output.final_suggestions)
    result.agent_titles = [s.title for s in output.final_suggestions]

    for s in output.final_suggestions:
        haystack = (s.title + " " + s.explanation + " " + s.impact_summary).lower()
        if case.expected_keyword.lower() in haystack:
            result.found = True
            result.correct_category = (s.category == case.expected_category)
            break

    return result


def run_evals(verbose: bool = False, single_case: str | None = None) -> None:
    cases_dir = Path(__file__).parent / "cases"
    outputs_dir = Path(__file__).parent.parent / "outputs"
    outputs_dir.mkdir(exist_ok=True)

    benchmark = BENCHMARK
    if single_case:
        benchmark = [c for c in BENCHMARK if c.file.replace(".py", "") == single_case]
        if not benchmark:
            print(f"Case '{single_case}' not found. Available: {[c.file for c in BENCHMARK]}")
            sys.exit(1)

    print("=" * 60)
    print("Code Review Agent — Eval Harness")
    print(f"Running {len(benchmark)} case(s)")
    print("=" * 60)

    results: list[EvalResult] = []
    for i, case in enumerate(benchmark, 1):
        print(f"\n[{i}/{len(benchmark)}] {case.id} — {case.title}")
        result = run_case(case, cases_dir, verbose=verbose)
        results.append(result)

        status = "✅ FOUND" if result.found else "❌ MISSED"
        cat = "✓ cat" if result.correct_category else "✗ cat"
        print(f"         {status} | {cat} | {result.suggestions_count} suggestion(s)")
        if result.agent_titles and verbose:
            for t in result.agent_titles:
                print(f"           → {t}")

    # Summary
    total = len(results)
    found = sum(1 for r in results if r.found)
    cat_correct = sum(1 for r in results if r.correct_category)
    total_suggestions = sum(r.suggestions_count for r in results)

    recall = found / total if total else 0
    cat_accuracy = cat_correct / max(1, found)

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Recall:              {recall:.1%} ({found}/{total} bugs found)")
    print(f"Category Accuracy:   {cat_accuracy:.1%} ({cat_correct}/{max(1,found)} correctly categorized)")
    print(f"Total suggestions:   {total_suggestions} across {total} case(s)")
    print(f"Avg per case:        {total_suggestions / max(1, total):.1f}")

    by_cat: dict[str, list[EvalResult]] = {}
    for r in results:
        by_cat.setdefault(r.expected_category, []).append(r)

    print("\nBy category:")
    for cat, cat_results in by_cat.items():
        n_found = sum(1 for r in cat_results if r.found)
        print(f"  {cat:<22} {n_found}/{len(cat_results)}")

    missed = [r for r in results if not r.found]
    if missed:
        print("\nMissed:")
        for r in missed:
            print(f"  ✗ {r.case_id}: {r.title}")
    else:
        print("\n✅ Perfect recall on all cases.")

    # Save
    metrics = {
        "recall": round(recall, 4),
        "category_accuracy": round(cat_accuracy, 4),
        "total_cases": total,
        "bugs_found": found,
        "total_suggestions": total_suggestions,
        "results": [
            {
                "id": r.case_id,
                "title": r.title,
                "found": r.found,
                "correct_category": r.correct_category,
                "suggestions": r.suggestions_count,
                "agent_titles": r.agent_titles,
            }
            for r in results
        ],
    }

    out_path = outputs_dir / "eval_results.json"
    out_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Run eval benchmark")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--case", help="Run a single case by filename (without .py)")
    args = parser.parse_args()

    run_evals(verbose=args.verbose, single_case=args.case)

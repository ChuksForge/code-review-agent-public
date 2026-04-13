"""
agents/reconciler.py
--------------------
Reconciler — full implementation (not a stub).

This is the final stage of the pipeline and it is deliberately
kept deterministic: no LLM calls, pure Python ranking logic.

Why no LLM here?
- Stable output: same input always produces same ranking
- Testable: unit tests don't require API calls or mocking
- Fast: no latency penalty at the output stage
- Cheap: no tokens spent on something an algorithm handles better

Two operations:
1. Deduplication — overlapping suggestions (same file + line range + category)
   are merged, keeping the higher-severity version
2. Ranking — composite impact score: severity × category_weight + priority_bonus
"""

from __future__ import annotations

from collections import defaultdict

from core.models import CodeChunk, ReviewOutput, RewriterSuggestion, StaticFinding
from core.config import CATEGORY_WEIGHTS, PRIORITY_BONUS


# ─────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────

def _overlaps(a: RewriterSuggestion, b: RewriterSuggestion) -> bool:
    """True if two suggestions cover overlapping lines in the same file."""
    if a.file_path != b.file_path or a.category != b.category:
        return False
    return not (a.line_end < b.line_start or b.line_end < a.line_start)


def _deduplicate(suggestions: list[RewriterSuggestion]) -> list[RewriterSuggestion]:
    """
    Remove overlapping suggestions, keeping the highest-severity version.
    Prevents the same bug from appearing twice when multiple tools flag it.
    """
    sorted_sug = sorted(suggestions, key=lambda s: s.severity_score, reverse=True)
    kept: list[RewriterSuggestion] = []
    for candidate in sorted_sug:
        if not any(_overlaps(candidate, existing) for existing in kept):
            kept.append(candidate)
    return kept


# ─────────────────────────────────────────────
# Ranking
# ─────────────────────────────────────────────

def _impact_score(s: RewriterSuggestion) -> float:
    """
    Composite impact score for final ranking.
    impact = (severity × category_weight) + priority_bonus
    """
    weight = CATEGORY_WEIGHTS.get(s.category, 1.0)
    bonus = PRIORITY_BONUS.get(s.priority, 0.0)
    return (s.severity_score * weight) + bonus


# ─────────────────────────────────────────────
# Summary generation
# ─────────────────────────────────────────────

def _build_summary(
    suggestions: list[RewriterSuggestion],
    total_found: int,
    total_approved: int,
) -> str:
    if not suggestions:
        return "No significant issues found. Code looks clean."

    cat_counts: dict[str, int] = defaultdict(int)
    pri_counts: dict[str, int] = defaultdict(int)
    for s in suggestions:
        cat_counts[s.category] += 1
        pri_counts[s.priority] += 1

    cat_summary = ", ".join(f"{n} {c}" for c, n in sorted(cat_counts.items()))
    parts = [
        f"Found {len(suggestions)} actionable issues ({cat_summary}).",
        f"Planner identified {total_found} potential issues; Critic filtered to {total_approved}.",
    ]
    if pri_counts.get("critical"):
        parts.append(f"⚠️  {pri_counts['critical']} CRITICAL issue(s) require immediate attention.")
    if pri_counts.get("high"):
        parts.append(f"🔴 {pri_counts['high']} HIGH priority issue(s).")
    return " ".join(parts)


# ─────────────────────────────────────────────
# Markdown renderer
# ─────────────────────────────────────────────

PRIORITY_EMOJI = {"critical": "🚨", "high": "🔴", "medium": "🟡", "low": "🟢"}
CATEGORY_EMOJI = {
    "logic_bug": "🐛", "security": "🔒", "performance": "⚡",
    "type_error": "🔷", "maintainability": "🔧", "style": "✨",
}


def render_markdown(output: ReviewOutput) -> str:
    """Render ReviewOutput as a GitHub-pasteable markdown report."""
    lines = [
        "# 🔍 Code Review Report",
        "",
        f"> {output.summary}",
        "",
        f"**Files reviewed**: {len(output.files_reviewed)}  ",
        f"**Tools used**: {', '.join(output.tools_used)}  ",
        f"**Pipeline**: {output.total_issues_found} identified "
        f"→ {output.issues_after_critic} validated "
        f"→ {len(output.final_suggestions)} actionable",
        "",
        "---",
        "",
    ]

    if not output.final_suggestions:
        lines.append("✅ No actionable issues found.")
        return "\n".join(lines)

    by_file: dict[str, list[RewriterSuggestion]] = defaultdict(list)
    for s in output.final_suggestions:
        by_file[s.file_path].append(s)

    for file_path, file_suggestions in by_file.items():
        lines.append(f"## 📄 `{file_path}`")
        lines.append("")
        for s in file_suggestions:
            p_emoji = PRIORITY_EMOJI.get(s.priority, "⬜")
            c_emoji = CATEGORY_EMOJI.get(s.category, "📌")
            lines += [
                f"### {p_emoji} [{s.priority.upper()}] {c_emoji} {s.title}",
                f"**Lines**: {s.line_start}–{s.line_end} | **Category**: `{s.category}` | **Severity**: {s.severity_score:.1f}/10",
                "",
                f"**Impact**: {s.impact_summary}",
                "",
                "**Original:**",
                "```python",
                s.original_code,
                "```",
                "",
                "**Fix:**",
                "```python",
                s.suggested_fix,
                "```",
                "",
                f"**Why**: {s.explanation}",
                "",
            ]
            if s.references:
                lines.append(f"**References**: {', '.join(s.references)}")
                lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def run_reconciler(
    suggestions: list[RewriterSuggestion],
    total_planner_issues: int,
    total_approved: int,
    chunks: list[CodeChunk],
    findings: list[StaticFinding],
    verbose: bool = False,
) -> ReviewOutput:
    """Deduplicate, rank, and produce the final ReviewOutput."""
    deduped = _deduplicate(suggestions)
    ranked = sorted(deduped, key=_impact_score, reverse=True)

    if verbose:
        removed = len(suggestions) - len(deduped)
        print(f"  [Reconciler] {len(suggestions)} → {len(deduped)} after dedup (removed {removed})")
        for s in ranked[:5]:
            print(f"    [{s.priority.upper()}] {s.title} ({s.file_path}:{s.line_start})")

    return ReviewOutput(
        total_issues_found=total_planner_issues,
        issues_after_critic=total_approved,
        final_suggestions=ranked,
        summary=_build_summary(ranked, total_planner_issues, total_approved),
        files_reviewed=list({c.file_path for c in chunks}),
        tools_used=list({f.tool for f in findings}) or ["ast", "pylint", "mypy"],
    )

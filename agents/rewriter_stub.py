"""
agents/rewriter_stub.py
-----------------------
Rewriter Agent — open-core version.

Generates concrete fix suggestions for Critic-approved issues.
Only ever sees approved issue IDs — the noise and false positives
have already been filtered upstream.

The production version includes:
- Language-specific fix templates for common patterns
- Automatic test case generation for the suggested fix
- Diff formatting for direct PR patch application
"""

from __future__ import annotations

import json
from textwrap import dedent

import anthropic

from core.models import CriticScore, PlannerIssue, RewriterSuggestion
from core.config import LLM_MODEL, LLM_MAX_TOKENS


REWRITER_SYSTEM = dedent("""
You are a senior software engineer generating concrete, production-ready fix suggestions.

The issue you receive has already been validated by a Critic agent — it is a real problem.
Generate the EXACT fix. Not vague advice.

Requirements:
- suggested_fix: the corrected code, ready to paste. Match indentation and style.
- explanation: WHY the original is wrong and what the fix achieves
- impact_summary: one sentence — real-world consequence if left unfixed
- references: PEP numbers, CVE IDs, OWASP items (if applicable)

Do NOT refactor beyond the specific issue. No scope creep.

Return ONLY valid JSON:
{
  "title": "Short fix title",
  "suggested_fix": "corrected code here",
  "explanation": "Why original is wrong and what fix achieves",
  "impact_summary": "One sentence on real-world impact",
  "references": ["OWASP A03:2021", "CWE-89"]
}
""").strip()


def _build_rewriter_prompt(issue: PlannerIssue, score: CriticScore) -> str:
    return dedent(f"""
## Issue to Fix

**Title**: {issue.title}
**Category**: {issue.category}
**File**: {issue.file_path} (lines {issue.line_start}-{issue.line_end})
**Severity**: {score.severity_score}/10 | **Confidence**: {score.confidence:.0%} | **Priority**: {score.priority}

**Critic Assessment**: {score.reasoning}

**Description**: {issue.description}

**Original Code**:
```python
{issue.code_snippet}
```

Generate a concrete fix. Return JSON as specified.
    """).strip()


def run_rewriter(
    approved_ids: list[str],
    issues: list[PlannerIssue],
    scores: list[CriticScore],
    verbose: bool = False,
) -> list[RewriterSuggestion]:
    """Generate fix suggestions for all approved issues."""
    client = anthropic.Anthropic()

    issue_map = {i.issue_id: i for i in issues}
    score_map = {s.issue_id: s for s in scores}
    suggestions: list[RewriterSuggestion] = []

    for issue_id in approved_ids:
        issue = issue_map.get(issue_id)
        score = score_map.get(issue_id)
        if not issue or not score:
            continue

        if verbose:
            print(f"  [Rewriter] Fixing: {issue.title[:60]} [{issue.category}]")

        try:
            response = client.messages.create(
                model=LLM_MODEL,
                max_tokens=2048,
                system=REWRITER_SYSTEM,
                messages=[{"role": "user", "content": _build_rewriter_prompt(issue, score)}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            data = json.loads(raw)

        except (json.JSONDecodeError, IndexError, anthropic.APIError) as e:
            if verbose:
                print(f"  [Rewriter] Error for {issue_id}: {e}")
            data = {
                "title": issue.title,
                "suggested_fix": f"# Fix generation failed — review manually\n{issue.code_snippet}",
                "explanation": issue.description,
                "impact_summary": "See issue description.",
                "references": [],
            }

        suggestions.append(RewriterSuggestion(
            issue_id=issue_id,
            title=data.get("title", issue.title),
            category=issue.category,
            file_path=issue.file_path,
            line_start=issue.line_start,
            line_end=issue.line_end,
            original_code=issue.code_snippet,
            suggested_fix=data.get("suggested_fix", ""),
            explanation=data.get("explanation", ""),
            severity_score=score.severity_score,
            priority=score.priority,
            impact_summary=data.get("impact_summary", ""),
            references=data.get("references", []),
        ))

    return suggestions

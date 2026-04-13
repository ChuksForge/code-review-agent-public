"""
agents/critic_stub.py
---------------------
Critic Agent — open-core version.

The Critic is the key differentiator in this architecture.
It receives every PlannerIssue and scores it before anything
reaches the Rewriter.

What's shown here:
- The dual-score schema (severity + confidence)
- The verdict system (approve / reject / escalate)
- The basic threshold gate structure
- False positive detection as a first-class concept

What's in the production system (not published):
- The full prompt with calibrated heuristics
- Category-specific threshold tuning
- Confidence adjustment logic based on static tool corroboration
- Multi-pass scoring with cross-issue consistency checks
- Ensemble approach across critic passes

The filtering quality is the moat. The structure is the signal.
"""

from __future__ import annotations

import json
from textwrap import dedent

import anthropic

from core.models import CriticScore, PlannerIssue
from core.config import LLM_MODEL, LLM_MAX_TOKENS, CRITIC_BATCH_SIZE


# Simplified system prompt — production version is significantly more detailed
CRITIC_SYSTEM = dedent("""
You are a code review Critic. You receive potential issues identified by a Planner
and must evaluate each one objectively.

For each issue, produce two scores:

severity_score (0-10): how bad is this if left unfixed?
  0-2: trivial, no real impact
  3-5: real issue, low to moderate risk
  6-8: significant bug or security risk
  9-10: critical — data loss, breach, or common-path failure

confidence (0-1): is this actually a real issue in this code?
  0.0-0.4: likely false positive or missing context
  0.5-0.7: plausible but uncertain
  0.8-1.0: high confidence, supported by evidence

verdict:
  "approve"   — forward to Rewriter
  "reject"    — noise or false positive
  "escalate"  — approve and flag as critical

Return ONLY valid JSON:
{
  "verdicts": [
    {
      "issue_id": "PLAN-XXXXXXXX",
      "verdict": "approve|reject|escalate",
      "severity_score": 7.5,
      "confidence": 0.85,
      "reasoning": "Brief explanation",
      "is_false_positive": false,
      "priority": "critical|high|medium|low"
    }
  ]
}
""").strip()


def _build_critic_prompt(issues: list[PlannerIssue]) -> str:
    parts = ["## Issues to Evaluate\n"]
    for issue in issues:
        parts.append(f"### {issue.issue_id}: {issue.title}")
        parts.append(f"- Category: {issue.category}")
        parts.append(f"- File: {issue.file_path} (lines {issue.line_start}-{issue.line_end})")
        parts.append(f"- Description: {issue.description}")
        if issue.code_snippet:
            parts.append(f"- Code:\n```python\n{issue.code_snippet}\n```")
        if issue.static_findings:
            parts.append("- Static tool signals:")
            for f in issue.static_findings[:2]:
                parts.append(f"  [{f.tool}] {f.message}")
        parts.append("")
    parts.append("Score each issue. Reject noise. Only approve where a developer should genuinely act.")
    return "\n".join(parts)


def _passes_threshold(score: CriticScore) -> bool:
    """
    Gate function — both scores must exceed minimum thresholds.
    Exact threshold values and per-category overrides are in the production system.
    """
    if score.verdict == "reject" or score.is_false_positive:
        return False
    # Simplified flat thresholds for open core
    return score.severity_score >= 4.0 and score.confidence >= 0.6


def run_critic(
    issues: list[PlannerIssue],
    verbose: bool = False,
) -> tuple[list[CriticScore], list[str]]:
    """
    Run the Critic agent over all PlannerIssues.

    Returns:
        scores       — all CriticScore objects (approved and rejected)
        approved_ids — issue IDs that passed the threshold gate
    """
    client = anthropic.Anthropic()
    all_scores: list[CriticScore] = []

    for i in range(0, len(issues), CRITIC_BATCH_SIZE):
        batch = issues[i: i + CRITIC_BATCH_SIZE]
        prompt = _build_critic_prompt(batch)

        try:
            response = client.messages.create(
                model=LLM_MODEL,
                max_tokens=LLM_MAX_TOKENS,
                system=CRITIC_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
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
                print(f"  [Critic] Error on batch {i // CRITIC_BATCH_SIZE + 1}: {e}")
            continue

        for vd in data.get("verdicts", []):
            all_scores.append(CriticScore(
                issue_id=vd.get("issue_id", ""),
                verdict=vd.get("verdict", "reject"),
                severity_score=float(vd.get("severity_score", 0)),
                confidence=float(vd.get("confidence", 0)),
                reasoning=vd.get("reasoning", ""),
                is_false_positive=bool(vd.get("is_false_positive", False)),
                priority=vd.get("priority", "medium"),
            ))

    approved_ids = [s.issue_id for s in all_scores if _passes_threshold(s)]

    return all_scores, approved_ids

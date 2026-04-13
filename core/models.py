"""
core/models.py
--------------
Public data models for the Code Review Agent.

These are the canonical schemas used throughout the pipeline.
All models use Pydantic v2 for validation — malformed LLM output
is caught at the boundary before it can corrupt downstream state.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# Input models
# ─────────────────────────────────────────────

class CodeChunk(BaseModel):
    """
    A unit of code to be reviewed.
    Scoped to a single function or class (≤120 lines) via AST chunking.
    Smaller chunks = more focused LLM analysis = fewer hallucinations.
    """
    file_path: str
    language: str = "python"
    content: str
    start_line: int = 1
    end_line: int | None = None
    context: str = ""  # e.g. "function: parse_user_input"


# ─────────────────────────────────────────────
# Static analysis models
# ─────────────────────────────────────────────

class StaticFinding(BaseModel):
    """
    A raw finding from a static analysis tool.
    These are ground-truth signals fed to the Planner agent —
    giving the LLM evidence to reason from rather than pure intuition.
    """
    tool: Literal["ast", "pylint", "mypy", "semgrep", "tree-sitter"]
    rule_id: str
    file_path: str
    line: int
    col: int = 0
    message: str
    severity: Literal["error", "warning", "info", "style"] = "warning"
    category: str = ""


# ─────────────────────────────────────────────
# Agent output models
# ─────────────────────────────────────────────

class PlannerIssue(BaseModel):
    """
    A potential issue identified by the Planner agent.
    The Planner casts wide — every possible issue is captured here.
    Filtering is the Critic's job, not the Planner's.
    """
    issue_id: str
    category: Literal[
        "logic_bug", "security", "performance",
        "type_error", "maintainability", "style"
    ]
    title: str
    description: str
    file_path: str
    line_start: int
    line_end: int
    code_snippet: str
    static_findings: list[StaticFinding] = Field(default_factory=list)
    severity_hint: str = "medium"  # planner's raw estimate before critic scoring


class CriticScore(BaseModel):
    """
    The Critic agent's evaluation of a PlannerIssue.

    Two independent scores determine whether an issue proceeds to the Rewriter:
      severity_score  — how bad is this if left unfixed? (0-10)
      confidence      — is this actually a real issue?   (0-1)

    Both must exceed category-specific thresholds. See core/config.py.

    This dual-threshold approach is the key differentiator:
    - High severity + low confidence = probable hallucination → reject
    - Low severity + high confidence = real but trivial → reject
    - High severity + high confidence = genuine issue → approve
    """
    issue_id: str
    verdict: Literal["approve", "reject", "escalate"]
    severity_score: float = Field(ge=0.0, le=10.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    is_false_positive: bool = False
    priority: Literal["critical", "high", "medium", "low"] = "medium"


class RewriterSuggestion(BaseModel):
    """
    A concrete fix suggestion from the Rewriter agent.

    The Rewriter only receives Critic-approved issues.
    Every suggestion includes:
    - The exact corrected code (not vague advice)
    - An explanation of why the original is wrong
    - The real-world impact if left unfixed
    - References (CVEs, PEPs, OWASP) where applicable
    """
    issue_id: str
    title: str
    category: str
    file_path: str
    line_start: int
    line_end: int
    original_code: str
    suggested_fix: str
    explanation: str
    severity_score: float
    priority: Literal["critical", "high", "medium", "low"]
    impact_summary: str
    references: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────
# Final output model
# ─────────────────────────────────────────────

class ReviewOutput(BaseModel):
    """
    The final output of the full pipeline.
    Produced by the Reconciler after deduplication and ranking.

    The Reconciler is fully deterministic — no LLM call.
    This keeps output stable, fast, and testable without API cost.
    """
    total_issues_found: int          # raw planner output count
    issues_after_critic: int         # after critic filtering
    final_suggestions: list[RewriterSuggestion]  # deduplicated, ranked
    summary: str
    files_reviewed: list[str]
    tools_used: list[str]

    @property
    def noise_filtered(self) -> int:
        """How many planner issues were filtered as noise or false positives."""
        return self.total_issues_found - self.issues_after_critic

    @property
    def filter_rate(self) -> float:
        """Fraction of planner issues filtered (0-1). Higher = more precise."""
        if self.total_issues_found == 0:
            return 0.0
        return self.noise_filtered / self.total_issues_found

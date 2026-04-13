"""
core/pipeline.py
----------------
Simplified linear pipeline orchestration.

This is the open-core version — it runs the full agent sequence
(ingest → static analysis → planner → critic → rewriter → reconciler)
without requiring LangGraph as a dependency.

The production system uses a typed LangGraph StateGraph with conditional
edges, multi-pass critic loops, and advanced state accumulation patterns
not included here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from core.models import (
    CodeChunk,
    CriticScore,
    PlannerIssue,
    ReviewOutput,
    RewriterSuggestion,
    StaticFinding,
)
from tools.ingestion import ingest_local, ingest_github_pr, ingest_git_diff
from tools.static_analysis import run_all_static_tools
from agents.planner_stub import run_planner
from agents.critic_stub import run_critic
from agents.rewriter_stub import run_rewriter
from agents.reconciler import run_reconciler


# ─────────────────────────────────────────────
# Pipeline state (plain dataclass, not LangGraph)
# ─────────────────────────────────────────────

@dataclass
class PipelineState:
    """Mutable state passed through each pipeline stage."""

    # Input
    input_mode: str
    target_path: str
    verbose: bool = False

    # Populated by each stage
    code_chunks: list[CodeChunk] = field(default_factory=list)
    static_findings: list[StaticFinding] = field(default_factory=list)
    planner_issues: list[PlannerIssue] = field(default_factory=list)
    critic_scores: list[CriticScore] = field(default_factory=list)
    approved_ids: list[str] = field(default_factory=list)
    suggestions: list[RewriterSuggestion] = field(default_factory=list)
    final_output: ReviewOutput | None = None
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────
# Pipeline stages
# ─────────────────────────────────────────────

def _stage_ingestion(state: PipelineState) -> None:
    if state.verbose:
        print(f"[Ingestion] mode={state.input_mode}, target={state.target_path}")

    try:
        if state.input_mode == "local":
            state.code_chunks = ingest_local(state.target_path)
        elif state.input_mode == "github":
            token = os.environ.get("GITHUB_TOKEN", "")
            repo, pr_str = state.target_path.rsplit("#", 1)
            state.code_chunks = ingest_github_pr(repo, int(pr_str), token)
        elif state.input_mode == "git_diff":
            state.code_chunks = ingest_git_diff(state.target_path)
        else:
            raise ValueError(f"Unknown input_mode: {state.input_mode}")

        if state.verbose:
            files = len({c.file_path for c in state.code_chunks})
            print(f"[Ingestion] {len(state.code_chunks)} chunks from {files} files")

    except Exception as e:
        state.errors.append(f"Ingestion error: {e}")


def _stage_static_analysis(state: PipelineState) -> None:
    if not state.code_chunks:
        return

    if state.verbose:
        print(f"[StaticAnalysis] Running on {len(state.code_chunks)} chunks...")

    state.static_findings = run_all_static_tools(state.code_chunks, verbose=state.verbose)

    if state.verbose:
        print(f"[StaticAnalysis] {len(state.static_findings)} findings")


def _stage_planner(state: PipelineState) -> None:
    if not state.code_chunks:
        return

    if state.verbose:
        print("[Planner] Identifying issues...")

    state.planner_issues = run_planner(
        state.code_chunks,
        state.static_findings,
        verbose=state.verbose,
    )

    if state.verbose:
        print(f"[Planner] {len(state.planner_issues)} issues identified")


def _stage_critic(state: PipelineState) -> None:
    if not state.planner_issues:
        return

    if state.verbose:
        print(f"[Critic] Evaluating {len(state.planner_issues)} issues...")

    state.critic_scores, state.approved_ids = run_critic(
        state.planner_issues,
        verbose=state.verbose,
    )

    if state.verbose:
        rejected = len(state.planner_issues) - len(state.approved_ids)
        rate = rejected / max(1, len(state.planner_issues))
        print(f"[Critic] {len(state.approved_ids)} approved, {rejected} rejected ({rate:.0%} noise filtered)")


def _stage_rewriter(state: PipelineState) -> None:
    if not state.approved_ids:
        if state.verbose:
            print("[Rewriter] No approved issues to fix")
        return

    if state.verbose:
        print(f"[Rewriter] Generating fixes for {len(state.approved_ids)} issues...")

    state.suggestions = run_rewriter(
        approved_ids=state.approved_ids,
        issues=state.planner_issues,
        scores=state.critic_scores,
        verbose=state.verbose,
    )

    if state.verbose:
        print(f"[Rewriter] {len(state.suggestions)} fix suggestions generated")


def _stage_reconciler(state: PipelineState) -> None:
    if state.verbose:
        print("[Reconciler] Deduplicating and ranking...")

    state.final_output = run_reconciler(
        suggestions=state.suggestions,
        total_planner_issues=len(state.planner_issues),
        total_approved=len(state.approved_ids),
        chunks=state.code_chunks,
        findings=state.static_findings,
        verbose=state.verbose,
    )


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def run_pipeline(
    input_mode: str,
    target_path: str,
    verbose: bool = False,
) -> ReviewOutput | None:
    """
    Run the full code review pipeline and return structured output.

    Args:
        input_mode: "local" | "github" | "git_diff"
        target_path: Local path, "owner/repo#PR", or repo path for diff
        verbose: Print stage-by-stage progress

    Returns:
        ReviewOutput with ranked suggestions, or None if pipeline failed
    """
    state = PipelineState(
        input_mode=input_mode,
        target_path=target_path,
        verbose=verbose,
    )

    stages = [
        ("Ingestion",        _stage_ingestion),
        ("Static Analysis",  _stage_static_analysis),
        ("Planner",          _stage_planner),
        ("Critic",           _stage_critic),
        ("Rewriter",         _stage_rewriter),
        ("Reconciler",       _stage_reconciler),
    ]

    for name, stage_fn in stages:
        try:
            stage_fn(state)
        except Exception as e:
            state.errors.append(f"{name} stage error: {e}")
            if verbose:
                print(f"[ERROR] {name}: {e}")

    if state.errors and verbose:
        print(f"\n{len(state.errors)} error(s) during run:")
        for err in state.errors:
            print(f"  {err}")

    return state.final_output

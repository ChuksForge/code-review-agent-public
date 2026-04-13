"""
agents/planner_stub.py
----------------------
Planner Agent — simplified open-core version.

The Planner's role: BREADTH. Identify every potential issue across 6 categories.
It is explicitly instructed not to filter — that is the Critic's job.
Separation of concerns between identification and evaluation is what makes
the critic loop work.

The production version includes:
- Category-specific sub-prompts with few-shot examples
- Adaptive batching based on chunk complexity
- Multi-pass planning with critic feedback integration
"""

from __future__ import annotations

import json
import uuid
from textwrap import dedent

import anthropic

from core.models import CodeChunk, PlannerIssue, StaticFinding
from core.config import LLM_MODEL, LLM_MAX_TOKENS, PLANNER_BATCH_SIZE


PLANNER_SYSTEM = dedent("""
You are a senior software engineer performing the PLANNING phase of a multi-stage code review.

Your job: identify ALL potential issues. Do NOT filter — that is handled downstream.

Categories to cover:
- logic_bug: incorrect behavior, off-by-one, wrong conditionals, missing edge cases
- security: injection, hardcoded secrets, insecure defaults, auth bypass
- performance: O(n²) in hot paths, N+1 queries, blocking I/O
- type_error: None dereference, type mismatches, missing checks
- maintainability: deeply nested code, dead code, god functions
- style: ONLY flag if it could mask a logic error

Return ONLY valid JSON:
{
  "issues": [
    {
      "category": "logic_bug|security|performance|type_error|maintainability|style",
      "title": "Short descriptive title",
      "description": "Why this is a problem and what could go wrong",
      "file_path": "path/to/file.py",
      "line_start": 10,
      "line_end": 15,
      "code_snippet": "the relevant code",
      "severity_hint": "critical|high|medium|low"
    }
  ]
}
""").strip()


def _build_prompt(chunks: list[CodeChunk], findings: list[StaticFinding]) -> str:
    findings_by_file: dict[str, list[StaticFinding]] = {}
    for f in findings:
        findings_by_file.setdefault(f.file_path, []).append(f)

    parts = ["## Code to Review\n"]
    for chunk in chunks:
        parts.append(f"### {chunk.file_path} | {chunk.context} (lines {chunk.start_line}-{chunk.end_line})")
        parts.append(f"```python\n{chunk.content}\n```\n")

    parts.append("## Static Analysis Signals\n")
    for file_path, file_findings in findings_by_file.items():
        parts.append(f"**{file_path}**")
        for f in file_findings:
            parts.append(f"  [{f.tool}:{f.rule_id}] Line {f.line}: {f.message}")
        parts.append("")

    parts.append("Identify ALL potential issues. Cast wide — do not filter.")
    return "\n".join(parts)


def run_planner(
    chunks: list[CodeChunk],
    findings: list[StaticFinding],
    verbose: bool = False,
) -> list[PlannerIssue]:
    """Run the Planner agent across all chunks and return PlannerIssues."""
    client = anthropic.Anthropic()
    all_issues: list[PlannerIssue] = []

    for i in range(0, len(chunks), PLANNER_BATCH_SIZE):
        batch_chunks = chunks[i: i + PLANNER_BATCH_SIZE]
        batch_paths = {c.file_path for c in batch_chunks}
        batch_findings = [f for f in findings if f.file_path in batch_paths]

        prompt = _build_prompt(batch_chunks, batch_findings)

        try:
            response = client.messages.create(
                model=LLM_MODEL,
                max_tokens=LLM_MAX_TOKENS,
                system=PLANNER_SYSTEM,
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
                print(f"  [Planner] Error on batch {i // PLANNER_BATCH_SIZE + 1}: {e}")
            continue

        for issue_data in data.get("issues", []):
            related = [
                f for f in batch_findings
                if f.file_path == issue_data.get("file_path", "")
                and issue_data.get("line_start", 0) <= f.line <= issue_data.get("line_end", 9999)
            ]

            # Pull code snippet from chunk if not provided by LLM
            snippet = issue_data.get("code_snippet", "")
            if not snippet:
                for chunk in batch_chunks:
                    if chunk.file_path == issue_data.get("file_path"):
                        offset = issue_data.get("line_start", chunk.start_line) - chunk.start_line
                        end_off = issue_data.get("line_end", chunk.end_line or chunk.start_line) - chunk.start_line
                        lines = chunk.content.splitlines()
                        snippet = "\n".join(lines[max(0, offset): end_off + 1])
                        break

            all_issues.append(PlannerIssue(
                issue_id=f"PLAN-{uuid.uuid4().hex[:8].upper()}",
                category=issue_data.get("category", "maintainability"),
                title=issue_data.get("title", "Unnamed issue"),
                description=issue_data.get("description", ""),
                file_path=issue_data.get("file_path", "unknown"),
                line_start=issue_data.get("line_start", 0),
                line_end=issue_data.get("line_end", 0),
                code_snippet=snippet,
                static_findings=related,
                severity_hint=issue_data.get("severity_hint", "medium"),
            ))

    return all_issues

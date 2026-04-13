"""
core/config.py
--------------
Configuration for the Code Review Agent.

Thresholds are intentionally visible — understanding why these values
exist is part of understanding how the critic loop works.
"""

from __future__ import annotations


# ─────────────────────────────────────────────
# Critic thresholds
# ─────────────────────────────────────────────

# Base thresholds — both must be exceeded for an issue to pass the Critic
SEVERITY_THRESHOLD: float = 3.5    # out of 10
CONFIDENCE_THRESHOLD: float = 0.55  # out of 1.0

# Category-specific overrides.
# Security and logic bugs get a lower confidence bar — we'd rather
# investigate a borderline security finding than miss it.
# Style issues need a much higher severity score to be worth surfacing.
CATEGORY_CONFIDENCE_OVERRIDES: dict[str, float] = {
    "security": 0.40,
    "logic_bug": 0.50,
    "type_error": 0.50,
}

CATEGORY_SEVERITY_OVERRIDES: dict[str, float] = {
    "style": 6.0,  # style issues must be genuinely impactful to pass
}


# ─────────────────────────────────────────────
# Reconciler weights
# ─────────────────────────────────────────────

# Category weights for composite impact ranking.
# impact_score = (severity × weight) + priority_bonus
# Higher weight = higher placement in final ranked output.
CATEGORY_WEIGHTS: dict[str, float] = {
    "security": 2.0,
    "logic_bug": 1.8,
    "type_error": 1.4,
    "performance": 1.2,
    "maintainability": 0.8,
    "style": 0.4,
}

PRIORITY_BONUS: dict[str, float] = {
    "critical": 3.0,
    "high": 1.5,
    "medium": 0.5,
    "low": 0.0,
}


# ─────────────────────────────────────────────
# Ingestion config
# ─────────────────────────────────────────────

SUPPORTED_EXTENSIONS: set[str] = {".py"}
MAX_CHUNK_LINES: int = 120
SKIP_DIRS: set[str] = {
    ".git", "__pycache__", "node_modules",
    ".venv", "venv", "dist", "build",
}


# ─────────────────────────────────────────────
# Pylint signal rules (curated — style noise excluded)
# ─────────────────────────────────────────────

PYLINT_SIGNAL_RULES: set[str] = {
    "E0001",  # SyntaxError
    "E0102",  # function/class redefined
    "E0401",  # import error
    "E0602",  # undefined variable
    "E0611",  # cannot import name
    "E1101",  # module has no member
    "E1120",  # no value for argument
    "W0107",  # unnecessary pass
    "W0201",  # attribute defined outside __init__
    "W0611",  # unused import
    "W0612",  # unused variable
    "W0621",  # redefine from outer scope
    "W0702",  # bare except
    "W1514",  # open without encoding
    "R0201",  # method could be a function
}


# ─────────────────────────────────────────────
# LLM config
# ─────────────────────────────────────────────

LLM_MODEL: str = "claude-sonnet-4-20250514"
LLM_MAX_TOKENS: int = 4096
PLANNER_BATCH_SIZE: int = 10   # chunks per planner call
CRITIC_BATCH_SIZE: int = 15    # issues per critic call

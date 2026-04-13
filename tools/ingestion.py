"""
tools/ingestion.py
------------------
Ingestion layer: parse a local repo/directory or GitHub PR diff into CodeChunks.
Chunks are scoped to file + top-level function/class for optimal LLM context windows.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from typing import Generator

from core.models import CodeChunk


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {".py"}   # extend to .ts, .js, .go etc. as needed
MAX_CHUNK_LINES = 120            # keep chunks LLM-friendly
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}


# ─────────────────────────────────────────────
# Local ingestion
# ─────────────────────────────────────────────

def ingest_local(path: str) -> list[CodeChunk]:
    """Walk a directory and chunk Python files by top-level functions/classes."""
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    chunks: list[CodeChunk] = []

    if root.is_file():
        chunks.extend(_chunk_file(root))
    else:
        for filepath in _walk_python_files(root):
            chunks.extend(_chunk_file(filepath))

    return chunks


def _walk_python_files(root: Path) -> Generator[Path, None, None]:
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune unwanted directories in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.suffix in SUPPORTED_EXTENSIONS:
                yield p


def _chunk_file(filepath: Path) -> list[CodeChunk]:
    """Split a Python file into per-function/class chunks via AST."""
    try:
        source = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []

    if not source.strip():
        return []

    lines = source.splitlines()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Return the whole file as one chunk if it can't be parsed
        return [CodeChunk(
            file_path=str(filepath),
            content=source,
            start_line=1,
            end_line=len(lines),
            context="(unparseable — syntax error)",
        )]

    # BUG FIX: use ast.iter_child_nodes(tree) directly — NOT ast.walk()
    # ast.walk() recurses into all descendants, so _is_top_level was broken.
    # Direct child iteration is correct and fast.
    top_level_nodes = [
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]

    if not top_level_nodes:
        # No functions/classes — return whole file as one chunk (scripts, config, etc.)
        return [CodeChunk(
            file_path=str(filepath),
            content=source,
            start_line=1,
            end_line=len(lines),
            context="module-level",
        )]

    chunks: list[CodeChunk] = []
    for node in top_level_nodes:
        start = node.lineno
        end = getattr(node, "end_lineno", start + MAX_CHUNK_LINES)
        # Clamp to MAX_CHUNK_LINES to keep LLM context tight
        actual_end = min(end, start + MAX_CHUNK_LINES)
        chunk_lines = lines[start - 1: actual_end]
        kind = "class" if isinstance(node, ast.ClassDef) else "function"

        chunks.append(CodeChunk(
            file_path=str(filepath),
            content="\n".join(chunk_lines),
            start_line=start,
            end_line=actual_end,
            context=f"{kind}: {node.name}",
        ))

    return chunks


# ─────────────────────────────────────────────
# GitHub PR ingestion
# ─────────────────────────────────────────────

def ingest_github_pr(repo_name: str, pr_number: int, github_token: str) -> list[CodeChunk]:
    """
    Fetch changed files from a GitHub PR and return CodeChunks for each.
    Only ingests files with supported extensions.

    Args:
        repo_name: "owner/repo"
        pr_number: PR number (int)
        github_token: Personal access token with repo read scope
    """
    try:
        from github import Github  # PyGitHub
    except ImportError:
        raise ImportError("PyGitHub not installed. Run: pip install PyGitHub")

    g = Github(github_token)
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    chunks: list[CodeChunk] = []

    for pr_file in pr.get_files():
        filepath = pr_file.filename
        ext = Path(filepath).suffix
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        if pr_file.status == "removed":
            continue

        # Fetch file content at PR head SHA
        try:
            content_obj = repo.get_contents(filepath, ref=pr.head.sha)
            source = content_obj.decoded_content.decode("utf-8")
        except Exception:
            continue

        # Use patch as context hint
        patch_context = pr_file.patch or ""

        # Chunk via AST
        tmp_path = Path(f"/tmp/_pr_file_{Path(filepath).name}")
        tmp_path.write_text(source)
        file_chunks = _chunk_file(tmp_path)
        tmp_path.unlink(missing_ok=True)

        for chunk in file_chunks:
            chunk.file_path = filepath  # restore real path
            chunk.context += f" | PR#{pr_number} changed file"
        chunks.extend(file_chunks)

    return chunks


# ─────────────────────────────────────────────
# Git diff ingestion (local PR-like workflow)
# ─────────────────────────────────────────────

def ingest_git_diff(repo_path: str, base_ref: str = "HEAD~1") -> list[CodeChunk]:
    """
    Ingest only the files changed in a local git diff vs base_ref.
    Useful for pre-commit hooks or CI pipelines.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", base_ref, "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    changed_files = [
        Path(repo_path) / f.strip()
        for f in result.stdout.splitlines()
        if f.strip()
    ]

    chunks: list[CodeChunk] = []
    for filepath in changed_files:
        if filepath.suffix in SUPPORTED_EXTENSIONS and filepath.exists():
            chunks.extend(_chunk_file(filepath))
    return chunks

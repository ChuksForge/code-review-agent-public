"""
tools/static_analysis.py
------------------------
Static analysis tool wrappers. Each tool returns a list of StaticFindings.
All tools are exposed as callable functions so they can be registered as
LangGraph tool nodes or called directly.

Tools:
  - ast_analyzer     : Python AST-based pattern detection (no subprocess)
  - pylint_runner    : pylint via subprocess, parses JSON output
  - mypy_runner      : mypy type checker via subprocess
  - semgrep_runner   : semgrep SAST scanner via subprocess
  - tree_sitter_analyzer: tree-sitter for cross-language analysis
"""

from __future__ import annotations

import ast
import json
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Literal

from core.models import CodeChunk, StaticFinding


# ─────────────────────────────────────────────
# AST Analyzer — zero-dependency, runs in-process
# ─────────────────────────────────────────────

class ASTAnalyzer(ast.NodeVisitor):
    """
    Custom AST visitor that catches common Python anti-patterns:
    - Bare except clauses
    - Mutable default arguments
    - Use of eval() / exec()
    - assert used for logic (stripped in -O mode)
    - Shadowing builtins
    - Broad exception re-raise without cause
    - SQL string formatting (primitive injection detection)
    """

    BUILTINS = frozenset(dir(__builtins__) if isinstance(__builtins__, dict) else dir(__builtins__))

    def __init__(self, source: str, file_path: str):
        self.source = source
        self.file_path = file_path
        self.findings: list[StaticFinding] = []
        self._lines = source.splitlines()

    def _add(self, node: ast.AST, rule: str, msg: str, sev: str = "warning") -> None:
        self.findings.append(StaticFinding(
            tool="ast",
            rule_id=rule,
            file_path=self.file_path,
            line=getattr(node, "lineno", 0),
            col=getattr(node, "col_offset", 0),
            message=msg,
            severity=sev,  # type: ignore[arg-type]
        ))

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self._add(node, "AST001", "Bare `except:` catches all exceptions including KeyboardInterrupt and SystemExit. Use `except Exception:` at minimum.", "warning")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_mutable_defaults(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _check_mutable_defaults(self, node: ast.FunctionDef) -> None:
        for default in node.args.defaults + node.args.kw_defaults:
            if default is None:
                continue
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._add(
                    node, "AST002",
                    f"Mutable default argument in `{node.name}()`. "
                    "Default is shared across all calls — use `None` and initialize inside.",
                    "error",
                )

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id in ("eval", "exec"):
                self._add(node, "AST003", f"Use of `{node.func.id}()` is a security risk — arbitrary code execution.", "error")
            elif node.func.id in self.BUILTINS and node.func.id not in ("print", "len", "range", "type", "isinstance", "issubclass", "super", "object", "list", "dict", "set", "tuple", "str", "int", "float", "bool"):
                pass  # reserved for future builtin shadow detection

        # Detect format-string SQL injection patterns
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executemany"):
            for arg in node.args:
                if isinstance(arg, (ast.JoinedStr, ast.BinOp)):
                    self._add(node, "AST004", "Possible SQL injection: query appears to use string formatting. Use parameterized queries.", "error")

        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        # Warn only if assert is used with side-effectful expressions (heuristic)
        self._add(node, "AST005",
                  "Assert statements are stripped with `-O` (optimize) flag. "
                  "Don't use `assert` for runtime validation — raise explicit exceptions.",
                  "warning")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # Detect shadowing of common builtins at assignment
        if isinstance(node.ctx, ast.Store) and node.id in ("list", "dict", "set", "id", "type", "input", "filter", "map", "zip", "open", "format"):
            self._add(node, "AST006", f"Variable name `{node.id}` shadows a built-in. Rename to avoid subtle bugs.", "warning")
        self.generic_visit(node)


def ast_analyzer(chunk: CodeChunk) -> list[StaticFinding]:
    """Run AST-based analysis on a CodeChunk."""
    try:
        tree = ast.parse(chunk.content)
    except SyntaxError as e:
        return [StaticFinding(
            tool="ast",
            rule_id="AST000",
            file_path=chunk.file_path,
            line=e.lineno or 0,
            message=f"SyntaxError: {e.msg}",
            severity="error",
        )]

    visitor = ASTAnalyzer(chunk.content, chunk.file_path)
    visitor.visit(tree)
    # Adjust line numbers by chunk offset
    for f in visitor.findings:
        f.line += chunk.start_line - 1
    return visitor.findings


# ─────────────────────────────────────────────
# Pylint Runner
# ─────────────────────────────────────────────

PYLINT_SEVERITY_MAP = {
    "E": "error",
    "F": "error",
    "W": "warning",
    "C": "style",
    "R": "info",
}

PYLINT_CATEGORY_MAP = {
    "E": "logic_bug",
    "W": "maintainability",
    "C": "style",
    "R": "maintainability",
}

# Only surface these pylint message IDs — filter the rest as noise
PYLINT_SIGNAL_RULES = {
    "E0001",  # SyntaxError
    "E0102",  # function/class redefined
    "E0401",  # import error
    "E0611",  # cannot import name
    "E1101",  # module has no member
    "W0611",  # unused import
    "W0612",  # unused variable
    "W0621",  # redefine from outer scope
    "W0702",  # bare except
    "W1514",  # open without encoding
    "E1120",  # no value for argument
    "E0602",  # undefined variable
    "W0107",  # unnecessary pass
    "C0301",  # line too long (> 120)
    "R0201",  # method could be function
    "W0201",  # attribute outside __init__
}


def pylint_runner(chunk: CodeChunk) -> list[StaticFinding]:
    """Run pylint on a code chunk and return filtered findings."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
        f.write(chunk.content)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [
                "pylint",
                tmp_path,
                "--output-format=json",
                "--disable=all",
                f"--enable={','.join(PYLINT_SIGNAL_RULES)}",
                "--max-line-length=120",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = result.stdout.strip()
        if not raw:
            return []
        messages = json.loads(raw)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    findings: list[StaticFinding] = []
    for msg in messages:
        msg_id = msg.get("message-id", "")
        sev_char = msg_id[0] if msg_id else "W"
        adjusted_line = msg.get("line", 0) + chunk.start_line - 1

        findings.append(StaticFinding(
            tool="pylint",
            rule_id=msg_id,
            file_path=chunk.file_path,
            line=adjusted_line,
            col=msg.get("column", 0),
            message=msg.get("message", ""),
            severity=PYLINT_SEVERITY_MAP.get(sev_char, "warning"),  # type: ignore[arg-type]
            category=PYLINT_CATEGORY_MAP.get(sev_char, "maintainability"),
        ))
    return findings


# ─────────────────────────────────────────────
# Mypy Runner
# ─────────────────────────────────────────────

def mypy_runner(chunk: CodeChunk) -> list[StaticFinding]:
    """Run mypy type checker on a code chunk."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
        f.write(chunk.content)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["mypy", tmp_path, "--ignore-missing-imports", "--no-error-summary", "--show-column-numbers"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        lines = result.stdout.splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    findings: list[StaticFinding] = []
    for line in lines:
        # Format: /tmp/xxx.py:LINE:COL: error: MESSAGE  [rule]
        parts = line.split(":", 4)
        if len(parts) < 5:
            continue
        try:
            raw_line = int(parts[1])
            raw_col = int(parts[2])
            rest = parts[4].strip()
            if ": " not in rest:
                continue
            sev_str, message = rest.split(": ", 1)
            sev_str = sev_str.strip()
            # Extract rule in brackets
            rule_id = "mypy-generic"
            if "[" in message and message.endswith("]"):
                rule_id = message[message.rfind("[") + 1:-1]
                message = message[:message.rfind("[")].strip()

            sev: Literal["error", "warning", "info", "style"] = (
                "error" if sev_str == "error" else
                "warning" if sev_str == "warning" else
                "info"
            )

            findings.append(StaticFinding(
                tool="mypy",
                rule_id=rule_id,
                file_path=chunk.file_path,
                line=raw_line + chunk.start_line - 1,
                col=raw_col,
                message=message,
                severity=sev,
                category="type_error",
            ))
        except (ValueError, IndexError):
            continue

    return findings


# ─────────────────────────────────────────────
# Semgrep Runner
# ─────────────────────────────────────────────

SEMGREP_RULESETS = [
    "p/python",
    "p/secrets",
    "p/security-audit",
]


def semgrep_runner(chunk: CodeChunk) -> list[StaticFinding]:
    """
    Run semgrep on a code chunk.
    Uses curated rulesets focused on security and correctness.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_file = Path(tmpdir) / "chunk.py"
        tmp_file.write_text(chunk.content, encoding="utf-8")

        try:
            result = subprocess.run(
                [
                    "semgrep",
                    "--config", "p/python",
                    "--config", "p/secrets",
                    "--json",
                    "--quiet",
                    str(tmp_file),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            raw = result.stdout.strip()
            if not raw:
                return []
            data = json.loads(raw)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            return []

    findings: list[StaticFinding] = []
    for match in data.get("results", []):
        check_id = match.get("check_id", "semgrep-unknown")
        meta = match.get("extra", {})
        start_line = match.get("start", {}).get("line", 0)
        sev = meta.get("severity", "WARNING").lower()
        sev_mapped: Literal["error", "warning", "info", "style"] = (
            "error" if sev in ("error", "high", "critical") else
            "warning" if sev in ("warning", "medium") else
            "info"
        )

        findings.append(StaticFinding(
            tool="semgrep",
            rule_id=check_id,
            file_path=chunk.file_path,
            line=start_line + chunk.start_line - 1,
            col=match.get("start", {}).get("col", 0),
            message=meta.get("message", ""),
            severity=sev_mapped,
            category=_semgrep_category(check_id),
        ))

    return findings


def _semgrep_category(check_id: str) -> str:
    cid = check_id.lower()
    if any(k in cid for k in ("sql", "inject", "xss", "csrf", "secret", "key", "token", "password", "auth")):
        return "security"
    if any(k in cid for k in ("perf", "loop", "complex")):
        return "performance"
    return "maintainability"


# ─────────────────────────────────────────────
# Tree-sitter (structural queries)
# ─────────────────────────────────────────────

def tree_sitter_analyzer(chunk: CodeChunk) -> list[StaticFinding]:
    """
    Use tree-sitter for structural pattern matching.
    Falls back gracefully if tree-sitter-python is not installed.
    Detects: deeply nested control flow, long parameter lists.
    """
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser

        PY_LANGUAGE = Language(tspython.language())
        parser = Parser(PY_LANGUAGE)
        tree = parser.parse(chunk.content.encode("utf-8"))
    except (ImportError, Exception):
        return []

    findings: list[StaticFinding] = []

    def _walk(node, depth=0):
        if node.type in ("if_statement", "for_statement", "while_statement", "try_statement"):
            if depth >= 4:
                findings.append(StaticFinding(
                    tool="tree-sitter",
                    rule_id="TS001",
                    file_path=chunk.file_path,
                    line=node.start_point[0] + chunk.start_line,
                    col=node.start_point[1],
                    message=f"Deeply nested control flow (depth={depth}). Consider extracting to functions.",
                    severity="warning",
                    category="maintainability",
                ))
        if node.type == "parameters":
            param_count = sum(1 for c in node.children if c.type not in (",", "(", ")"))
            if param_count > 7:
                findings.append(StaticFinding(
                    tool="tree-sitter",
                    rule_id="TS002",
                    file_path=chunk.file_path,
                    line=node.start_point[0] + chunk.start_line,
                    col=0,
                    message=f"Function has {param_count} parameters. Consider using a dataclass or config object.",
                    severity="info",
                    category="maintainability",
                ))
        for child in node.children:
            _walk(child, depth + 1 if node.type in ("if_statement", "for_statement", "while_statement", "try_statement") else depth)

    _walk(tree.root_node)
    return findings


# ─────────────────────────────────────────────
# Combined runner
# ─────────────────────────────────────────────

def run_all_static_tools(chunks: list[CodeChunk], verbose: bool = False) -> list[StaticFinding]:
    """Run all static analysis tools over all chunks. Returns deduplicated findings."""
    all_findings: list[StaticFinding] = []
    seen: set[tuple] = set()

    for chunk in chunks:
        for tool_fn in [ast_analyzer, pylint_runner, mypy_runner, semgrep_runner, tree_sitter_analyzer]:
            try:
                results = tool_fn(chunk)
                for f in results:
                    key = (f.tool, f.file_path, f.line, f.rule_id)
                    if key not in seen:
                        seen.add(key)
                        all_findings.append(f)
                if verbose:
                    print(f"  [{tool_fn.__name__}] {chunk.file_path}:{chunk.start_line} → {len(results)} findings")
            except Exception as e:
                if verbose:
                    print(f"  [{tool_fn.__name__}] ERROR: {e}")

    return all_findings

"""
demo/run_demo.py
----------------
One-command demo runner.

Runs the full pipeline against the sample_repo/ directory (or a path you provide)
and prints results with a Rich console UI.

Usage:
    python demo/run_demo.py
    python demo/run_demo.py --path /path/to/your/project
    python demo/run_demo.py --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.pipeline import run_pipeline
from agents.reconciler import render_markdown

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Code Review Agent — Demo Runner")
    parser.add_argument(
        "--path",
        default=str(Path(__file__).parent / "sample_repo"),
        help="Path to review (default: demo/sample_repo/)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[bold red]Error:[/] ANTHROPIC_API_KEY not set. Add it to your .env file.")
        sys.exit(1)

    console.print(Panel(
        f"[bold]Target:[/] {args.path}",
        title="[bold blue]🔍 Code Review Agent — Demo[/]",
        border_style="blue",
    ))

    output = run_pipeline(
        input_mode="local",
        target_path=args.path,
        verbose=args.verbose,
    )

    if not output:
        console.print("[red]Pipeline produced no output. Check errors above.[/]")
        sys.exit(1)

    # Summary panel
    console.print(Panel(
        f"[bold]{output.summary}[/]\n\n"
        f"Planner found: [cyan]{output.total_issues_found}[/]  →  "
        f"Critic approved: [cyan]{output.issues_after_critic}[/]  →  "
        f"Actionable: [bold green]{len(output.final_suggestions)}[/]  "
        f"([dim]noise filtered: {output.noise_filtered} | filter rate: {output.filter_rate:.0%}[/])",
        title="[bold green]Review Complete[/]",
        border_style="green",
    ))

    if not output.final_suggestions:
        console.print("[bold green]✅ No actionable issues found.[/]")
        return

    # Findings table
    table = Table(title="Ranked Findings", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Priority", width=10)
    table.add_column("Category", width=16)
    table.add_column("Title", width=44)
    table.add_column("File:Line", width=28)
    table.add_column("Score", width=8)

    priority_colors = {"critical": "red bold", "high": "red", "medium": "yellow", "low": "green"}
    category_icons = {
        "logic_bug": "🐛", "security": "🔒", "performance": "⚡",
        "type_error": "🔷", "maintainability": "🔧", "style": "✨",
    }

    for i, s in enumerate(output.final_suggestions, 1):
        color = priority_colors.get(s.priority, "white")
        icon = category_icons.get(s.category, "📌")
        table.add_row(
            str(i),
            f"[{color}]{s.priority.upper()}[/{color}]",
            f"{icon} {s.category}",
            s.title[:42] + ("…" if len(s.title) > 42 else ""),
            f"{Path(s.file_path).name}:{s.line_start}",
            f"{s.severity_score:.1f}/10",
        )

    console.print(table)

    # Save outputs
    out_dir = Path(__file__).parent.parent / "outputs"
    out_dir.mkdir(exist_ok=True)

    json_path = out_dir / "example_review.json"
    json_path.write_text(
        json.dumps(output.model_dump(), indent=2, default=str),
        encoding="utf-8",
    )

    md_path = out_dir / "example_review.md"
    md_path.write_text(render_markdown(output), encoding="utf-8")

    console.print(f"\n[dim]JSON:[/] {json_path}")
    console.print(f"[dim]Markdown:[/] {md_path}")
    console.print("\n[dim]Tip: open outputs/example_review.md to see the full formatted report.[/]")


if __name__ == "__main__":
    main()

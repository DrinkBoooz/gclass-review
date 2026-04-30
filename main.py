"""
main.py
=======
Orchestration script for the Automated Grading Pipeline.

Execution flow:
  1. Load and validate environment variables from `.env`
  2. Authenticate with Google Classroom API (OAuth 2.0)
  3. Build Google Drive API client (reuses OAuth credentials)
  4. Initialize the Gemini API client (gemini-3-flash-preview)
  5. Load the grading rubric from file
  6. Fetch all "TURNED_IN" submissions for the target assignment
  7. For each submission:
       a. Fetch the student's profile (name, email)
       b. Download and extract attachment content via Drive API
       c. Send to Gemini for structured AI grading (score + feedback)
       d. Post the draft grade back to Google Classroom
  8. Save all feedback (score + AI text) to a timestamped CSV file
  9. Print a formatted summary report

Usage:
  $ python main.py                  # Full run
  $ python main.py --dry-run        # Grade but do NOT post to Classroom
  $ python main.py --limit 3        # Process only first N submissions (testing)
  $ python main.py --delay 3.0      # Increase delay between Gemini calls
  $ python main.py --thinking high  # Use higher Gemini thinking level
"""

from __future__ import annotations  # enables list[dict] and str | None on Python 3.9

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.logging import RichHandler
from rich.panel import Panel

from classroom_handler import (
    authenticate_classroom,
    build_drive_service,
    get_turned_in_submissions,
    get_student_profile,
    collect_submission_content,
    post_draft_grade,
    save_feedback_to_csv,
)
from gemini_grader import (
    build_gemini_client,
    load_rubric,
    grade_submission,
)

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
logger = logging.getLogger("grading_pipeline")
console = Console()


# ---------------------------------------------------------------------------
# Environment Loading & Validation
# ---------------------------------------------------------------------------

def load_and_validate_env() -> dict:
    """
    Loads `.env` and validates all required variables are present.

    Returns:
        Dict of all configuration values.

    Raises:
        SystemExit: If any required variable is missing or MAX_SCORE is not
                    a valid integer.
    """
    load_dotenv()

    required_vars = [
        "GEMINI_API_KEY",
        "GOOGLE_CREDENTIALS_FILE",
        "GOOGLE_TOKEN_FILE",
        "COURSE_ID",
        "COURSEWORK_ID",
        "MAX_SCORE",
        "ASSIGNMENT_TITLE",
        "RUBRIC_FILE",
    ]

    missing = [v for v in required_vars if not os.getenv(v)]
    if missing:
        console.print(
            "[bold red]ERROR:[/bold red] Missing required environment variables:\n"
            + "\n".join(f"  - {v}" for v in missing)
            + "\n\nCopy [bold].env.example[/bold] to [bold].env[/bold] and fill in all values."
        )
        sys.exit(1)

    # Validate MAX_SCORE is a usable integer — a typo here would cause a
    # silent crash deep in the grading loop, so we catch it at startup.
    try:
        max_score = int(os.getenv("MAX_SCORE", "100"))
    except ValueError:
        console.print(
            f"[bold red]ERROR:[/bold red] MAX_SCORE='{os.getenv('MAX_SCORE')}' "
            "is not a valid integer. Check your .env file."
        )
        sys.exit(1)

    return {
        "gemini_api_key":    os.getenv("GEMINI_API_KEY"),
        "credentials_file":  os.getenv("GOOGLE_CREDENTIALS_FILE"),
        "token_file":        os.getenv("GOOGLE_TOKEN_FILE"),
        "course_id":         os.getenv("COURSE_ID"),
        "coursework_id":     os.getenv("COURSEWORK_ID"),
        "max_score":         max_score,
        "assignment_title":  os.getenv("ASSIGNMENT_TITLE"),
        "rubric_file":       os.getenv("RUBRIC_FILE", "rubric.md"),
    }


# ---------------------------------------------------------------------------
# CLI Argument Parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parses optional command-line flags."""
    parser = argparse.ArgumentParser(
        description="Automated AI Grading Pipeline — Google Classroom × Gemini 3 Flash",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Grade submissions but do NOT post results to Classroom or write CSV.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N submissions. Useful for testing.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="Seconds to wait between Gemini API calls (default: 2.0).",
    )
    parser.add_argument(
        "--thinking",
        choices=["minimal", "low", "medium", "high"],
        default="medium",
        help="Gemini 3 thinking level (default: medium). Use 'low' for speed, 'high' for complex rubrics.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Results Summary
# ---------------------------------------------------------------------------

def print_summary(
    results: list[dict],
    dry_run: bool,
    max_score: int,
    csv_path: Optional[str],
) -> None:
    """
    Prints a formatted summary table of all grading results.

    Args:
        results:    List of result dicts from the pipeline run.
        dry_run:    Whether this was a dry run.
        max_score:  Maximum possible score.
        csv_path:   Path to the exported feedback CSV, or None if dry run.
    """
    suffix = " [DRY RUN — Nothing Posted]" if dry_run else ""
    table = Table(
        title=f"Grading Summary{suffix}",
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
    )
    table.add_column("Student Name",  style="bold", min_width=25)
    table.add_column("Email",                        min_width=30)
    table.add_column("Score",         justify="right", min_width=10)
    table.add_column("Percentage",    justify="right", min_width=12)
    table.add_column("Status",                       min_width=14)

    graded = skipped = failed = 0

    for r in results:
        status = r.get("status", "unknown")
        score_str = pct_str = "—"
        score_style = "white"

        if status in ("graded", "dry_run"):
            score = r.get("score", 0)  # defensive get avoids KeyError
            pct = (score / max_score) * 100
            score_str = f"{score}/{max_score}"
            pct_str = f"{pct:.1f}%"
            score_style = "green" if pct >= 75 else ("yellow" if pct >= 50 else "red")
            label = "[blue]✓ Dry Run[/blue]" if dry_run else "[green]✓ Graded[/green]"
            graded += 1
        elif status == "skipped_no_content":
            label = "[yellow]⚠ No Content[/yellow]"
            skipped += 1
        elif status == "grading_failed":
            label = "[red]✗ AI Failed[/red]"
            failed += 1
        elif status == "posting_failed":
            score = r.get("score", "?")
            score_str = f"{score}/{max_score}"
            label = "[red]✗ Post Failed[/red]"
            failed += 1
        else:
            label = f"[grey]{status}[/grey]"

        table.add_row(
            r.get("name", "Unknown"),
            r.get("email", "Unknown"),
            f"[{score_style}]{score_str}[/{score_style}]",
            f"[{score_style}]{pct_str}[/{score_style}]",
            label,
        )

    console.print(table)
    console.print(
        f"\n[bold]Totals:[/bold] "
        f"[green]{graded} graded[/green]  |  "
        f"[yellow]{skipped} skipped[/yellow]  |  "
        f"[red]{failed} failed[/red]  |  "
        f"{len(results)} total"
    )

    if csv_path:
        console.print(
            f"\n[bold cyan]Feedback CSV:[/bold cyan] {csv_path}\n"
            "Use this file to copy-paste AI feedback into Classroom's private comment\n"
            "box per student, or to email feedback directly."
        )


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Banner ────────────────────────────────────────────────────────────────
    console.print(Panel.fit(
        "[bold blue]Automated AI Grading Pipeline[/bold blue]\n"
        "[dim]Google Classroom × Gemini 3 Flash Preview[/dim]",
        border_style="blue",
    ))
    if args.dry_run:
        console.print(
            "[bold yellow]⚠ DRY RUN MODE — "
            "No grades will be posted and no CSV will be written.[/bold yellow]\n"
        )

    # ── 1. Load config ────────────────────────────────────────────────────────
    config = load_and_validate_env()

    # ── 2. Authenticate with Classroom ────────────────────────────────────────
    console.print("[bold]Step 1:[/bold] Authenticating with Google Classroom API...")
    try:
        classroom_service, creds = authenticate_classroom(
            credentials_file=config["credentials_file"],
            token_file=config["token_file"],
        )
        drive_service = build_drive_service(creds)
        console.print("  [green]✓[/green] Classroom & Drive authenticated.\n")
    except FileNotFoundError as e:
        console.print(f"[bold red]Authentication Error:[/bold red] {e}")
        sys.exit(1)

    # ── 3. Initialize Gemini ──────────────────────────────────────────────────
    console.print("[bold]Step 2:[/bold] Initializing Gemini API client...")
    gemini_client = build_gemini_client(api_key=config["gemini_api_key"])
    console.print(f"  [green]✓[/green] Gemini client ready (thinking={args.thinking}).\n")

    # ── 4. Load rubric ────────────────────────────────────────────────────────
    console.print("[bold]Step 3:[/bold] Loading grading rubric...")
    rubric = load_rubric(config["rubric_file"])
    console.print(f"  [green]✓[/green] Rubric loaded ({len(rubric)} chars).\n")

    # ── 5. Fetch submissions ──────────────────────────────────────────────────
    console.print("[bold]Step 4:[/bold] Fetching TURNED_IN submissions from Classroom...")
    try:
        submissions = get_turned_in_submissions(
            service=classroom_service,
            course_id=config["course_id"],
            coursework_id=config["coursework_id"],
        )
    except Exception as e:
        console.print(f"[bold red]Failed to fetch submissions:[/bold red] {e}")
        sys.exit(1)

    if not submissions:
        console.print("[yellow]No TURNED_IN submissions found. Nothing to grade.[/yellow]")
        sys.exit(0)

    if args.limit:
        submissions = submissions[: args.limit]
        console.print(
            f"  [dim](Limiting to first {args.limit} submission(s) per --limit flag)[/dim]"
        )

    console.print(
        f"  [green]✓[/green] Found [bold]{len(submissions)}[/bold] submission(s) to grade.\n"
    )

    # ── 6. Grade each submission ──────────────────────────────────────────────
    console.print(f"[bold]Step 5:[/bold] Grading {len(submissions)} submission(s)...\n")

    results: list[dict] = []
    start_time = datetime.now()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Grading submissions...", total=len(submissions))

        for i, submission in enumerate(submissions, start=1):
            submission_id = submission.get("id")
            user_id = submission.get("userId")

            # ── a. Student profile ────────────────────────────────────────
            progress.update(task, description=f"[{i}/{len(submissions)}] Fetching profile...")
            profile = get_student_profile(classroom_service, config["course_id"], user_id)

            result_entry: dict = {
                "submission_id": submission_id,
                "user_id":       user_id,
                "name":          profile["name"],
                "email":         profile["email"],
                "max_score":     config["max_score"],
            }

            logger.info(
                "\n─── Submission %d/%d: %s ───",
                i, len(submissions), profile["name"]
            )

            # ── b. Extract attachment content ─────────────────────────────
            progress.update(
                task, description=f"[{i}/{len(submissions)}] Extracting: {profile['name']}..."
            )
            student_content = collect_submission_content(submission, drive_service)

            if student_content.startswith("[NO ATTACHMENTS") or \
               student_content.startswith("[COULD NOT EXTRACT"):
                logger.warning("  Skipping %s — no usable content.", profile["name"])
                result_entry["status"] = "skipped_no_content"
                results.append(result_entry)
                progress.advance(task)
                continue

            # ── c. AI grading ─────────────────────────────────────────────
            progress.update(
                task, description=f"[{i}/{len(submissions)}] Grading: {profile['name']}..."
            )
            grading_result = grade_submission(
                client=gemini_client,
                assignment_title=config["assignment_title"],
                rubric=rubric,
                student_name=profile["name"],
                student_content=student_content,
                max_score=config["max_score"],
                thinking_level=args.thinking,
            )

            if grading_result is None:
                logger.error("  Grading failed for %s.", profile["name"])
                result_entry["status"] = "grading_failed"
                results.append(result_entry)
                progress.advance(task)
                continue

            result_entry["score"]    = grading_result["score"]
            result_entry["feedback"] = grading_result["feedback"]

            # ── d. Post draft grade to Classroom ──────────────────────────
            if not args.dry_run:
                progress.update(
                    task, description=f"[{i}/{len(submissions)}] Posting: {profile['name']}..."
                )
                posted = post_draft_grade(
                    service=classroom_service,
                    course_id=config["course_id"],
                    coursework_id=config["coursework_id"],
                    submission_id=submission_id,
                    score=grading_result["score"],
                    max_score=config["max_score"],
                )
                result_entry["status"] = "graded" if posted else "posting_failed"
            else:
                result_entry["status"] = "dry_run"
                logger.info(
                    "  [DRY RUN] score=%d, feedback preview: %s...",
                    grading_result["score"],
                    grading_result["feedback"][:100],
                )

            results.append(result_entry)
            progress.advance(task)

            # Pace Gemini calls to stay within rate limits
            if i < len(submissions):
                time.sleep(args.delay)

    # ── 7. Save feedback CSV ──────────────────────────────────────────────────
    csv_path: Optional[str] = None
    if not args.dry_run and any(r.get("feedback") for r in results):
        console.print("\n[bold]Step 6:[/bold] Saving feedback to CSV...")
        try:
            csv_path = save_feedback_to_csv(results, output_dir=".")
            console.print(f"  [green]✓[/green] Saved to: [bold]{csv_path}[/bold]\n")
        except RuntimeError as e:
            console.print(f"  [bold red]✗ CSV save failed:[/bold red] {e}")

    # ── 8. Summary ────────────────────────────────────────────────────────────
    elapsed = (datetime.now() - start_time).total_seconds()
    console.print(f"[dim]Completed in {elapsed:.1f}s[/dim]\n")
    print_summary(results, dry_run=args.dry_run, max_score=config["max_score"], csv_path=csv_path)

    if not args.dry_run:
        console.print(
            "\n[bold green]✓ Draft grades posted.[/bold green] "
            "Open Google Classroom, review each grade, then click "
            "[bold]Return[/bold] to release to students."
        )
    else:
        console.print(
            "\n[bold blue]Dry run complete.[/bold blue] "
            "Remove [bold]--dry-run[/bold] to post grades to Classroom."
        )


if __name__ == "__main__":
    main()

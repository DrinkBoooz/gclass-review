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
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import asyncio
from local_pipeline import run_local_queue
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
    load_system_prompt,
    build_cached_prefix,
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
    load_dotenv(override=True)

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
        "system_prompt_file": os.getenv("SYSTEM_PROMPT_FILE", "activity-checker-v4-fixed.md"),
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
        "--menu",
        action="store_true",
        help="Open interactive CLI menu to choose grading options.",
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
        default=12.0,
        metavar="SECONDS",
        help="Seconds to wait between Gemini API calls (default: 12.0).",
    )
    parser.add_argument(
        "--thinking",
        choices=["minimal", "low", "medium", "high"],
        default="medium",
        help="Gemini 3 thinking level (default: medium). Use 'low' for speed, 'high' for complex rubrics.",
    )
    parser.add_argument(
        "--rubric",
        type=str,
        default=None,
        metavar="PATH",
        help="Override the rubric file path (PDF/DOCX/MD/TXT).",
    )
    parser.add_argument(
        "--csv-only",
        action="store_true",
        help="Skip posting grades and write the CSV only.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser.parse_args()


def _prompt_input(prompt: str, default: Optional[str] = None) -> str:
    if default is not None and default != "":
        value = input(f"{prompt} [{default}]: ").strip()
        return value if value else str(default)
    return input(f"{prompt}: ").strip()


def _prompt_bool(prompt: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{prompt} ({suffix}): ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes", "1", "true")


def _prompt_int(prompt: str, default: Optional[int]) -> Optional[int]:
    default_text = "none" if default is None else str(default)
    value = _prompt_input(prompt, default_text).strip().lower()
    if value in ("", "none", "null", "0"):
        return None
    try:
        return int(value)
    except ValueError:
        return default


def _prompt_float(prompt: str, default: float) -> float:
    value = _prompt_input(prompt, f"{default:.1f}").strip()
    try:
        return float(value)
    except ValueError:
        return default


def _prompt_choice(prompt: str, choices: list[str], default: str) -> str:
    value = _prompt_input(prompt, default).strip().lower()
    if not value:
        return default
    if value in choices:
        return value
    return default


def _select_rubric_file(current_path: str) -> str:
    rubric_dir = Path("rubric")
    candidates: list[Path] = []
    if rubric_dir.exists():
        for path in sorted(rubric_dir.iterdir()):
            if path.suffix.lower() in (".pdf", ".docx", ".md", ".txt"):
                candidates.append(path)

    console.print("\n[bold]Rubric selection[/bold]")
    if candidates:
        for idx, path in enumerate(candidates, start=1):
            console.print(f"  {idx}) {path}")
        console.print("  M) Enter path manually")
        console.print(f"  Enter to keep current: {current_path}")
        choice = input("Choose rubric: ").strip().lower()
        if not choice:
            return current_path
        if choice == "m":
            return _prompt_input("Rubric path", current_path).strip('"')
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(candidates):
                return str(candidates[idx - 1])
    else:
        console.print("No files found in ./rubric. Enter a path manually.")

    return _prompt_input("Rubric path", current_path).strip('"')


def _select_course_id(service, current_id: str) -> str:
    resp = service.courses().list(pageSize=200).execute()
    courses = resp.get("courses", [])
    if not courses:
        console.print("No courses found for the authorized account.")
        return current_id

    console.print("\n[bold]Course selection[/bold]")
    for idx, course in enumerate(courses, start=1):
        name = course.get("name", "(no name)")
        section = course.get("section") or ""
        course_id = course.get("id", "")
        suffix = f" — {section}" if section else ""
        console.print(f"  {idx}) {name}{suffix} [{course_id}]")

    choice = input("Choose course (Enter to keep current): ").strip()
    if not choice:
        return current_id
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(courses):
            return courses[idx - 1].get("id", current_id)
    return current_id


def _select_coursework_id(service, course_id: str, current_id: str) -> tuple[str, str]:
    if not course_id:
        return current_id, ""

    results = service.courses().courseWork().list(courseId=course_id).execute()
    assignments = results.get("courseWork", [])
    if not assignments:
        console.print(f"No assignments found for course ID {course_id}.")
        return current_id, ""

    console.print("\n[bold]Coursework selection[/bold]")
    for idx, assignment in enumerate(assignments, start=1):
        title = assignment.get("title", "(no title)")
        work_id = assignment.get("id", "")
        console.print(f"  {idx}) {title} [{work_id}]")

    choice = input("Choose coursework (Enter to keep current): ").strip()
    if not choice:
        return current_id, ""
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(assignments):
            selected = assignments[idx - 1]
            return selected.get("id", current_id), selected.get("title", "")
    return current_id, ""


def run_cli_menu(args: argparse.Namespace, config: dict) -> None:
    console.print("\n[bold]CLI grading menu[/bold]")
    if _prompt_bool("Fetch courses from Google Classroom", False):
        try:
            service, _ = authenticate_classroom(
                credentials_file=config["credentials_file"],
                token_file=config["token_file"],
            )
            config["course_id"] = _select_course_id(service, config.get("course_id", ""))
            coursework_id, coursework_title = _select_coursework_id(
                service, config.get("course_id", ""), config.get("coursework_id", "")
            )
            config["coursework_id"] = coursework_id
            config["coursework_title"] = coursework_title
        except Exception as exc:
            console.print(f"[bold red]Failed to fetch courses:[/bold red] {exc}")
            config["course_id"] = _prompt_input("Course ID", config.get("course_id", ""))
            config["coursework_id"] = _prompt_input("Coursework ID", config.get("coursework_id", ""))
            config["coursework_title"] = config.get("coursework_title", "")
    else:
        config["course_id"] = _prompt_input("Course ID", config.get("course_id", ""))
        config["coursework_id"] = _prompt_input("Coursework ID", config.get("coursework_id", ""))
        config["coursework_title"] = config.get("coursework_title", "")
    args.csv_only = _prompt_bool("CSV-only mode (no posting, write CSV)", args.csv_only)
    args.limit = _prompt_int("Limit submissions", args.limit)
    args.delay = _prompt_float("Delay between Gemini calls (seconds)", args.delay)
    args.thinking = _prompt_choice("Thinking level (minimal/low/medium/high)", ["minimal", "low", "medium", "high"], args.thinking)

    config["rubric_file"] = _select_rubric_file(config.get("rubric_file", "rubric.md"))
    config["system_prompt_file"] = "activity-checker-v4-fixed.md"


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

    if not args.dry_run and not args.csv_only:
        args.csv_only = True

    # ── Banner ────────────────────────────────────────────────────────────────
    console.print(Panel.fit(
        "[bold blue]Automated AI Grading Pipeline[/bold blue]\n"
        "[dim]Google Classroom × Gemini 3 Flash Preview[/dim]",
        border_style="blue",
    ))
    if args.csv_only:
        console.print(
            "[bold yellow]⚠ CSV-ONLY MODE — "
            "No grades will be posted; CSV will be written.[/bold yellow]\n"
        )
    elif args.dry_run:
        console.print(
            "[bold yellow]⚠ DRY RUN MODE — "
            "No grades will be posted and no CSV will be written.[/bold yellow]\n"
        )

    # ── 1. Load config ────────────────────────────────────────────────────────
    config = load_and_validate_env()
    config["csv_only"] = args.csv_only

    if args.menu:
        run_cli_menu(args, config)

    if args.rubric:
        config["rubric_file"] = args.rubric

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
    config["rubric_text"] = rubric
    console.print(f"  [green]✓[/green] Rubric loaded ({len(rubric)} chars).\n")

    console.print("[bold]Step 3b:[/bold] Loading Gemini system prompt...")
    system_prompt = load_system_prompt(config.get("system_prompt_file", ""))
    config["system_prompt"] = system_prompt
    console.print(f"  [green]✓[/green] System prompt loaded ({len(system_prompt)} chars).\n")

    console.print("[bold]Step 3c:[/bold] Building cached prompt prefix...")
    rubric_label = Path(config.get("rubric_file", "")).stem
    cached_name = build_cached_prefix(gemini_client, rubric, system_prompt, rubric_label)
    config["cached_content_name"] = cached_name
    if cached_name:
        console.print("  [green]✓[/green] Cached prefix ready.\n")
    else:
        console.print("  [yellow]![/yellow] Cached prefix not available; using full prompts.\n")

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

    if not config.get("coursework_title"):
        try:
            coursework = (
                classroom_service.courses()
                .courseWork()
                .get(courseId=config["course_id"], id=config["coursework_id"])
                .execute()
            )
            config["coursework_title"] = coursework.get("title", "")
        except Exception as e:
            logger.warning("Could not fetch coursework title: %s", e)

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

    # ── 6. Grade each submission (Async Local Queue) ──────────────────────
    console.print(f"[bold]Step 5:[/bold] Grading {len(submissions)} submission(s)...\n")
    start_time = datetime.now()

    # Propagate CLI overrides into the config for the local pipeline
    config["thinking_level"] = args.thinking

    # Force dry_run to True to skip Classroom 403 errors and reduce overhead
    # We will rely on the CSV file for the final grades.
    is_local = "localhost" in os.getenv("AI_ENDPOINT", "") or os.getenv("GEMINI_API_KEY", "") == "gemini-3.5-flash"
    base_delay = 1.0 if is_local else args.delay

    run_dry = args.dry_run or args.csv_only
    results, cost_report = asyncio.run(
        run_local_queue(
            submissions=submissions,
            config=config,
            classroom_service=classroom_service,
            drive_service=drive_service,
            gemini_client=gemini_client,
            dry_run=run_dry,
            max_concurrency=1,    # Keep at 1 so the GPU focuses on one student
            base_delay=base_delay,
            output_dir=".",
        )
    )

    console.print(f"[dim]⏱ Pipeline completed in {(datetime.now() - start_time).total_seconds():.1f}s[/dim]\n")

    # ── 7. Save feedback CSV ──────────────────────────────────────────────────
    csv_path: Optional[str] = None
    if (args.csv_only or not args.dry_run) and any(r.get("feedback") for r in results):
        console.print("\n[bold]Step 6:[/bold] Saving feedback to CSV...")
        try:
            csv_path = save_feedback_to_csv(
                results,
                output_dir=".",
                coursework_title=config.get("coursework_title", ""),
            )
            console.print(f"  [green]✓[/green] Saved to: [bold]{csv_path}[/bold]\n")
        except RuntimeError as e:
            console.print(f"  [bold red]✗ CSV save failed:[/bold red] {e}")

    # ── 8. Summary ────────────────────────────────────────────────────────────
    elapsed = (datetime.now() - start_time).total_seconds()
    console.print(f"[dim]Completed in {elapsed:.1f}s[/dim]\n")
    print_summary(results, dry_run=args.dry_run, max_score=config["max_score"], csv_path=csv_path)
    console.print(f"\n[bold cyan]{cost_report}[/bold cyan]\n")

    if not args.dry_run and not args.csv_only:
        console.print(
            "\n[bold green]✓ Draft grades posted.[/bold green] "
            "Open Google Classroom, review each grade, then click "
            "[bold]Return[/bold] to release to students."
        )
    elif args.csv_only:
        console.print(
            "\n[bold blue]CSV-only run complete.[/bold blue] "
            "Remove [bold]--csv-only[/bold] to enable posting."
        )
    else:
        console.print(
            "\n[bold blue]Dry run complete.[/bold blue] "
            "Remove [bold]--dry-run[/bold] to post grades to Classroom."
        )


if __name__ == "__main__":
    main()

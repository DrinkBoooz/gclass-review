"""
local_pipeline.py
Local async grading pipeline implementing Priority C:
- Async job queue with configurable concurrency
- Adaptive rate-limiting (auto-adjusts on 429s)
- Dead-letter queue for permanently failed submissions
- Real-time cost & token tracking

Key design:
  Content pre-screening happens ONCE in run_local_queue before any batching.
  Only students with usable content are batched and sent to Gemini.
  Students with no content are resolved immediately as "No attachments submitted."
  and never consume a Gemini API slot. This guarantees every batch sent to
  Gemini is packed to batch_size with real submissions.
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from classroom_handler import (
    get_student_profile, collect_submission_content, post_draft_grade,
)
from roster import (
    resolve_group_from_content,
    build_group_header_note,
    detect_duplicate_submissions,
)
from gemini_grader import grade_submission_batch

logger = logging.getLogger("local_pipeline")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class AdaptiveRateLimiter:
    def __init__(self, base_delay: float = 12.0, max_concurrency: int = 3):
        self.base_delay = base_delay
        self.max_concurrency = max_concurrency
        self.current_delay = base_delay
        self.concurrency = max_concurrency
        self._429_count = 0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            await asyncio.sleep(self.current_delay)

    def on_success(self):
        if self._429_count > 0:
            self._429_count = max(0, self._429_count - 1)
            self.current_delay = max(self.base_delay, self.current_delay * 0.8)
            self.concurrency = min(self.max_concurrency, self.concurrency + 1)

    def on_rate_limit(self):
        self._429_count += 1
        self.current_delay = min(60.0, self.base_delay * (2 ** self._429_count))
        self.concurrency = max(1, self.concurrency - 1)


# ---------------------------------------------------------------------------
# Cost tracker
# ---------------------------------------------------------------------------

class CostTracker:
    def __init__(self):
        self.total_prompt = 0
        self.total_completion = 0
        self.total_cached = 0
        self.graded_count = 0

    def record(self, result: dict):
        prompt_tokens     = result.get("prompt_tokens", 0) or 0
        completion_tokens = result.get("completion_tokens", 0) or 0
        cached_tokens     = result.get("cached_tokens", 0) or 0
        total_tokens      = result.get("total_tokens", 0) or 0

        if not prompt_tokens and not completion_tokens and total_tokens:
            prompt_tokens = total_tokens

        self.total_prompt     += prompt_tokens
        self.total_completion += completion_tokens
        self.total_cached     += cached_tokens
        self.graded_count     += 1

    @property
    def estimated_cost_usd(self):
        # gemini-1.5-flash pricing (approx): $0.0375 / 1M input, $0.15 / 1M output
        return (self.total_prompt * 0.0375 + self.total_completion * 0.15) / 1_000_000

    def report(self) -> str:
        return (
            f"📊 Tokens: {self.total_prompt:,} in / {self.total_completion:,} out / "
            f"{self.total_cached:,} cached | "
            f"💰 Est. Cost: ${self.estimated_cost_usd:.4f} | "
            f"✅ Graded: {self.graded_count}"
        )


# ---------------------------------------------------------------------------
# Dead-letter queue
# ---------------------------------------------------------------------------

class DeadLetterQueue:
    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.items: list[dict] = []

    def add(self, submission_id: str, error: str, retries: int):
        self.items.append({
            "timestamp": datetime.utcnow().isoformat(),
            "submission_id": submission_id,
            "error": str(error),
            "retries": retries,
        })

    def flush(self):
        if not self.items:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dl_path = self.filepath / f"dead_letter_queue_{ts}.json"
        with open(dl_path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, indent=2)
        logger.info("💀 Dead-letter queue flushed to %s", dl_path)


# ---------------------------------------------------------------------------
# Content pre-screening
# ---------------------------------------------------------------------------

_EMPTY_CONTENT_PREFIXES = (
    "[NO ATTACHMENTS",
    "[COULD NOT EXTRACT",
    "[Binary",
    "[PDF contains no extractable",
)


def _is_empty_content(text: str) -> bool:
    """Returns True if the extracted text has no gradeable content."""
    if not text or not text.strip():
        return True
    return text.startswith(_EMPTY_CONTENT_PREFIXES)


def _prescreeen_submissions(
    submissions: list[dict],
    config: dict,
    classroom_service,
    drive_service,
    roster: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Fetches profiles and content for every submission, resolves group membership
    from document content using fuzzy roster matching, detects duplicate group
    submissions, and splits into ready/empty lists.
    """
    ready: list[dict] = []
    empty: list[dict] = []

    total = len(submissions)
    for idx, submission in enumerate(submissions, start=1):
        submission_id = submission.get("id")
        user_id       = submission.get("userId")

        profile = get_student_profile(classroom_service, config["course_id"], user_id)
        name    = profile["name"]

        logger.info("  [%d/%d] Fetching content for %s...", idx, total, name)
        student_content = collect_submission_content(submission, drive_service)
        text = student_content.get("text", "")

        if _is_empty_content(text):
            logger.warning("  No usable content for %s -- marked as no-submission.", name)
            empty.append({
                "status":       "No attachments submitted.",
                "name":         name,
                "email":        profile["email"],
                "max_score":    config["max_score"],
                "score":        "-",
                "rubric_score": "-",
                "feedback":     "",
            })
            continue

        # Group / individual resolution from document content
        group_info = resolve_group_from_content(
            text=text,
            roster=roster,
            submission_owner_name=name,
        )
        logger.info(
            "  %s -> %s (%s)",
            name,
            "GROUP" if group_info["is_group"] else "INDIVIDUAL",
            group_info["detection"],
        )

        ready.append({
            "submission_id": submission_id,
            "profile":       profile,
            "content":       text,
            "late":          submission.get("late", False),
            "group_info":    group_info,
            "dup_penalty":   False,
        })

    # Duplicate submission detection (needs all ready students at once)
    dup_penalty_map = detect_duplicate_submissions(ready)

    for student in ready:
        sid        = student["submission_id"]
        group_info = student["group_info"]
        note       = build_group_header_note(group_info, student["profile"]["name"])
        if note:
            student["content"] = note + "\n" + student["content"]
        if dup_penalty_map.get(sid):
            student["dup_penalty"] = True
            logger.warning(
                "  %s: duplicate group submission -- 5-point deduction will apply.",
                student["profile"]["name"],
            )

    logger.info(
        "Pre-screen complete: %d ready for grading, %d with no content.",
        len(ready), len(empty),
    )
    return ready, empty


async def process_batch(
    batch_idx: int,
    batch: list[dict],          # every entry is guaranteed to have content
    config: dict,
    classroom_service,
    drive_service,
    gemini_client,
    rate_limiter: AdaptiveRateLimiter,
    cost_tracker: CostTracker,
    dead_letter: DeadLetterQueue,
    dry_run: bool,
    max_retries: int = 3,
    cached_content_name: str = "",
):
    """
    Sends a pre-screened batch (all students have content) to Gemini,
    parses results, applies late penalties, and optionally posts grades.
    """
    logger.info(
        "🚀 Batch %d: sending %d student(s) to Gemini...", batch_idx, len(batch)
    )

    # ── 1. Call Gemini ────────────────────────────────────────────────────────
    batch_results = []
    for attempt in range(max_retries):
        await rate_limiter.acquire()
        try:
            batch_results = await asyncio.to_thread(
                grade_submission_batch,
                client=gemini_client,
                assignment_title=config["assignment_title"],
                rubric=config.get("rubric_text", ""),
                system_prompt=config.get("system_prompt", ""),
                students_data=batch,
                max_score=config["max_score"],
                thinking_level=config.get("thinking_level", "medium"),
                cached_content_name=cached_content_name,
            )

            if batch_results:
                usage = batch_results[0].pop("usage_metrics", None)
                if usage:
                    cost_tracker.record(usage)
                rate_limiter.on_success()
                break

        except Exception as e:
            logger.warning(
                "🔄 Retry %d/%d for batch %d: %s", attempt + 1, max_retries, batch_idx, e
            )
            rate_limiter.on_rate_limit()
            await asyncio.sleep(2 ** attempt)

    # ── 2. Handle total failure ───────────────────────────────────────────────
    final_results: list[dict] = []

    if not batch_results:
        for student in batch:
            dead_letter.add(student["submission_id"], "Exhausted retries for batch", max_retries)
            final_results.append({
                "status":     "grading_failed",
                "name":       student["profile"]["name"],
                "email":      student["profile"]["email"],
                "max_score":  config["max_score"],
                "score":      "",
                "rubric_score": "",
                "feedback":   "",
            })
        return final_results

    # ── 3. Map results back to students ──────────────────────────────────────
    returned_ids = {r.get("submission_id") for r in batch_results}

    for student in batch:
        profile       = student["profile"]
        submission_id = student["submission_id"]

        stu_result = next(
            (r for r in batch_results if r.get("submission_id") == submission_id), None
        )

        if not stu_result:
            logger.warning(
                "  ⚠ No grading block found for %s (id=%s) — model skipped this student.",
                profile["name"], submission_id,
            )
            final_results.append({
                "status":       "grading_failed",
                "name":         profile["name"],
                "email":        profile["email"],
                "max_score":    config["max_score"],
                "score":        "",
                "rubric_score": "",
                "feedback":     "",
            })
            continue

        # Late penalty
        if student["late"]:
            original_score = stu_result.get("score", 0) or 0
            stu_result["score"] = max(0, original_score - 10)
            stu_result["feedback"] = (
                (stu_result.get("feedback") or "") +
                "\n\n[System Note: A 10-point penalty was applied for a late submission.]"
            )

        # Duplicate group submission penalty (-5 pts, applied after late deduction)
        if student.get("dup_penalty"):
            original_score = stu_result.get("score", 0) or 0
            stu_result["score"] = max(0, original_score - 5)
            stu_result["feedback"] = (
                (stu_result.get("feedback") or "") +
                "\n\n[System Note: Multiple Members Submitted, -5 Points]"
            )

        # Post grade
        if not dry_run:
            posted = post_draft_grade(
                service=classroom_service,
                course_id=config["course_id"],
                coursework_id=config["coursework_id"],
                submission_id=submission_id,
                score=stu_result["score"],
                max_score=config["max_score"],
            )
            stu_result["status"] = "graded" if posted else "graded_locally_only"
        else:
            stu_result["status"] = "success" if config.get("csv_only") else "dry_run"

        stu_result["name"]      = profile["name"]
        stu_result["email"]     = profile["email"]
        stu_result["max_score"] = config["max_score"]
        final_results.append(stu_result)

    return final_results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_local_queue(
    submissions: list[dict],
    config: dict,
    classroom_service,
    drive_service,
    gemini_client,
    dry_run: bool = False,
    max_concurrency: int = 3,
    base_delay: float = 12.0,
    output_dir: str = ".",
    batch_size: int = 3,
    cached_content_name: str = "",
    roster=None,
):
    """
    Orchestrates the full grading pipeline:

    1. Pre-screen all submissions synchronously — fetch profiles + content,
       split into `ready` (has content) and `empty` (no submission).
    2. `empty` students are resolved immediately with status
       "No attachments submitted." and never touch Gemini.
    3. `ready` students are packed into dense batches of `batch_size`
       (no empty slots) and processed concurrently up to `max_concurrency`.
    4. Results from both groups are merged and returned in original order.
    """
    rate_limiter = AdaptiveRateLimiter(base_delay=base_delay, max_concurrency=max_concurrency)
    cost_tracker = CostTracker()
    dead_letter  = DeadLetterQueue(Path(output_dir))

    # ── Step 1: Pre-screen ────────────────────────────────────────────────────
    logger.info("📋 Pre-screening %d submission(s) for usable content...", len(submissions))
    ready, empty_results = _prescreeen_submissions(
        submissions, config, classroom_service, drive_service,
        roster=roster or [],
    )

    # ── Step 2: Batch only the ready students ─────────────────────────────────
    # Slicing ready (not raw submissions) guarantees every batch is dense.
    batches = [ready[i:i + batch_size] for i in range(0, len(ready), batch_size)]
    logger.info(
        "📦 %d student(s) ready → %d batch(es) of up to %d | "
        "%d student(s) skipped (no content).",
        len(ready), len(batches), batch_size, len(empty_results),
    )

    # ── Step 3: Process batches concurrently ─────────────────────────────────
    semaphore = asyncio.Semaphore(max_concurrency)

    async def worker(idx: int, batch: list[dict]):
        async with semaphore:
            return await process_batch(
                idx, batch, config, classroom_service, drive_service,
                gemini_client, rate_limiter, cost_tracker, dead_letter,
                dry_run, max_retries=3, cached_content_name=cached_content_name,
            )

    tasks = [asyncio.create_task(worker(i, batch)) for i, batch in enumerate(batches)]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    # ── Step 4: Merge results ─────────────────────────────────────────────────
    graded_results: list[dict] = []
    for r_list in results_lists:
        if isinstance(r_list, Exception):
            logger.error("💥 Worker crashed: %s", r_list)
        else:
            graded_results.extend(r_list)

    # Combine: empty results first preserves the original roster order
    # (skipped students appear at the top of the CSV matching Classroom order)
    final_results = empty_results + graded_results

    dead_letter.flush()
    return final_results, cost_tracker.report()
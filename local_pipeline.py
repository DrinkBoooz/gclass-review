"""
local_pipeline.py
Local async grading pipeline implementing Priority C:
- Async job queue with configurable concurrency
- Adaptive rate-limiting (auto-adjusts on 429s)
- Dead-letter queue for permanently failed submissions
- Real-time cost & token tracking
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from classroom_handler import get_student_profile, collect_submission_content, post_draft_grade
from gemini_grader import grade_submission

logger = logging.getLogger("local_pipeline")

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

class CostTracker:
    def __init__(self):
        self.total_prompt = 0
        self.total_completion = 0
        self.total_cached = 0
        self.graded_count = 0

    def record(self, result: dict):
        prompt_tokens = result.get("prompt_tokens", 0) or 0
        completion_tokens = result.get("completion_tokens", 0) or 0
        cached_tokens = result.get("cached_tokens", 0) or 0
        total_tokens = result.get("total_tokens", 0) or 0

        if not prompt_tokens and not completion_tokens and total_tokens:
            prompt_tokens = total_tokens

        self.total_prompt += prompt_tokens
        self.total_completion += completion_tokens
        self.total_cached += cached_tokens
        self.graded_count += 1

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
        # Join the filename to the directory path instead of using with_stem
        dl_path = self.filepath / f"dead_letter_queue_{ts}.json"
        with open(dl_path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, indent=2)
        logger.info("💀 Dead-letter queue flushed to %s", dl_path)


async def process_submission(
    idx: int,
    submission: dict,
    config: dict,
    classroom_service,
    drive_service,
    gemini_client,
    rate_limiter: AdaptiveRateLimiter,
    cost_tracker: CostTracker,
    dead_letter: DeadLetterQueue,
    dry_run: bool,
    max_retries: int = 3,
):
    submission_id = submission.get("id")
    user_id = submission.get("userId")
    profile = get_student_profile(classroom_service, config["course_id"], user_id)
    student_content = collect_submission_content(submission, drive_service)
    text = student_content.get("text", "")

    if not text or text.startswith(("[NO ATTACHMENTS", "[COULD NOT EXTRACT", "[Binary", "[PDF contains no extractable")):
        logger.warning("⏭ Skipping %s — no usable content.", profile["name"])
        return {
            "status": "skipped_no_content",
            "name": profile["name"],
            "email": profile["email"],
            "max_score": config["max_score"],
        }

    result = None
    for attempt in range(max_retries):
        await rate_limiter.acquire()
        try:
            result = grade_submission(
                client=gemini_client,
                assignment_title=config["assignment_title"],
                rubric=config.get("rubric_text", ""),
                system_prompt=config.get("system_prompt", ""),
                student_name=profile["name"],
                student_content=student_content,
                max_score=config["max_score"],
                thinking_level=config.get("thinking_level", "medium"),
                cached_content_name=config.get("cached_content_name", ""),
            )
            if result:
                cost_tracker.record(result)
                rate_limiter.on_success()
                break
        except Exception as e:
            logger.warning("🔄 Retry %d/%d for %s: %s", attempt+1, max_retries, profile["name"], e)
            rate_limiter.on_rate_limit()
            await asyncio.sleep(2 ** attempt)

    if not result:
        dead_letter.add(submission_id, "Exhausted retries", max_retries)
        return {
            "status": "failed_after_retries",
            "name": profile["name"],
            "email": profile["email"],
            "max_score": config["max_score"],
        }

    if not dry_run:
        posted = post_draft_grade(
            service=classroom_service,
            course_id=config["course_id"],
            coursework_id=config["coursework_id"],
            submission_id=submission_id,
            score=result["score"],
            max_score=config["max_score"],
        )
        # Even if posting to Classroom failed (403/permission), keep the AI result
        # so it can be written to the CSV and reviewed manually.
        result["status"] = "graded" if posted else "graded_locally_only"
    else:
        result["status"] = "success" if config.get("csv_only") else "dry_run"

    result["name"] = profile["name"]
    result["email"] = profile["email"]
    result["max_score"] = config["max_score"]
    return result


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
):
    rate_limiter = AdaptiveRateLimiter(base_delay=base_delay, max_concurrency=max_concurrency)
    cost_tracker = CostTracker()
    dead_letter = DeadLetterQueue(Path(output_dir))
    results = []
    semaphore = asyncio.Semaphore(max_concurrency)

    async def worker(idx, sub):
        async with semaphore:
            return await process_submission(
                idx, sub, config, classroom_service, drive_service,
                gemini_client, rate_limiter, cost_tracker, dead_letter, dry_run
            )

    tasks = [asyncio.create_task(worker(i, sub)) for i, sub in enumerate(submissions)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter exceptions
    final_results = []
    for r in results:
        if isinstance(r, Exception):
            logger.error("💥 Worker crashed: %s", r)
            final_results.append({"status": "worker_crash", "name": "Unknown", "email": "Unknown"})
        else:
            final_results.append(r)

    dead_letter.flush()
    return final_results, cost_tracker.report()

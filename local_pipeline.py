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
from datetime import datetime
from pathlib import Path

from classroom_handler import get_student_profile, collect_submission_content, post_draft_grade
from gemini_grader import grade_submission_batch

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


async def process_batch(
    batch_idx: int,
    batch: list[dict],
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
    students_data = []
    skipped_results = []

    # 1. Gather all contents for the batch
    for submission in batch:
        submission_id = submission.get("id")
        user_id = submission.get("userId")
        profile = get_student_profile(classroom_service, config["course_id"], user_id)
        student_content = collect_submission_content(submission, drive_service)
        text = student_content.get("text", "")

        if not text or text.startswith(("[NO ATTACHMENTS", "[COULD NOT EXTRACT", "[Binary", "[PDF contains no extractable")):
            logger.warning("⏭ Skipping %s — no usable content.", profile["name"])
            skipped_results.append({
                "status": "skipped_no_content",
                "name": profile["name"],
                "email": profile["email"],
                "max_score": config["max_score"],
                "score": "-",
                "rubric_score": "-",
                "feedback": "No attachments submitted."
            })
            continue

        students_data.append({
            "submission_id": submission_id,
            "profile": profile,
            "content": text,
            "late": submission.get("late", False),
        })

    if not students_data:
        return skipped_results

    # 2. Process via Gemini
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
                students_data=students_data,
                max_score=config["max_score"],
                thinking_level=config.get("thinking_level", "medium"),
            )

            if batch_results:
                # Extract usage from the first item if present
                usage = batch_results[0].pop("usage_metrics", None)
                if usage:
                    cost_tracker.record(usage)
                rate_limiter.on_success()
                break

        except Exception as e:
            logger.warning("🔄 Retry %d/%d for batch %d: %s", attempt + 1, max_retries, batch_idx, e)
            rate_limiter.on_rate_limit()
            await asyncio.sleep(2 ** attempt)

    # 3. Handle failures & map results
    final_results = []
    final_results.extend(skipped_results)

    if not batch_results:
        for student in students_data:
            dead_letter.add(student["submission_id"], "Exhausted retries for batch", max_retries)
            final_results.append({
                "status": "failed_after_retries",
                "name": student["profile"]["name"],
                "email": student["profile"]["email"],
                "max_score": config["max_score"],
            })
        return final_results

    # Link results back to student IDs and post grades
    for student in students_data:
        profile = student["profile"]
        submission_id = student["submission_id"]

        # Find this student's result from the parsed batch array
        stu_result = next((r for r in batch_results if r.get("name") == profile["name"]), None)

        if not stu_result:
            final_results.append({
                "status": "failed_parsing",
                "name": profile["name"],
                "email": profile["email"],
                "max_score": config["max_score"],
            })
            continue

        if student["late"]:
            original_score = stu_result["score"]
            stu_result["score"] = max(0, original_score - 10)
            stu_result["feedback"] += "\n\n[System Note: A 10-point penalty was applied for a late submission.]"

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

        stu_result["email"] = profile["email"]
        stu_result["max_score"] = config["max_score"]
        final_results.append(stu_result)

    return final_results


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
    batch_size: int = 5,
):
    rate_limiter = AdaptiveRateLimiter(base_delay=base_delay, max_concurrency=max_concurrency)
    cost_tracker = CostTracker()
    dead_letter = DeadLetterQueue(Path(output_dir))

    # Chunk the submissions list into groups of batch_size
    batches = [submissions[i:i + batch_size] for i in range(0, len(submissions), batch_size)]
    semaphore = asyncio.Semaphore(max_concurrency)

    async def worker(idx, batch):
        async with semaphore:
            return await process_batch(
                idx, batch, config, classroom_service, drive_service,
                gemini_client, rate_limiter, cost_tracker, dead_letter, dry_run
            )

    tasks = [asyncio.create_task(worker(i, batch)) for i, batch in enumerate(batches)]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter exceptions
    final_results = []
    for r_list in results_lists:
        if isinstance(r_list, Exception):
            logger.error("💥 Worker crashed: %s", r_list)
        else:
            final_results.extend(r_list)

    dead_letter.flush()
    return final_results, cost_tracker.report()

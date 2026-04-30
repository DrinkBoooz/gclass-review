"""
gemini_grader.py
================
Handles all interactions with the Google AI Studio (Gemini) API:
  - Constructing the grading prompt from rubric + student submission
  - Enforcing a strict JSON output schema via `response_schema`
  - Calling `gemini-3-flash-preview` with retry logic for rate limits
  - Wiring Gemini 3 ThinkingConfig for controllable reasoning depth
  - Guarding against safety-blocked responses before accessing response.text
  - Parsing and validating the structured response

The structured output forces Gemini to return ONLY a JSON object with:
  {
    "score":    <integer between 0 and MAX_SCORE>,
    "feedback": "<detailed grading feedback string>"
  }
"""

from __future__ import annotations  # enables dict[str, ...] syntax on Python 3.8/3.9

import json
import logging
from pathlib import Path
from typing import Optional

import google.genai as genai
from google.genai import types
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model Configuration
# ---------------------------------------------------------------------------

# Target model for the automated grading pipeline.
# This is separate from the AI Studio system-prompt model — both use
# gemini-3-flash-preview, but the pipeline controls its own instance here.
GEMINI_MODEL = "gemini-3-flash-preview"

# Maximum characters of student content sent per request.
# gemini-3-flash-preview supports a 1M token context window, but we cap
# input to manage cost and latency. Adjust upward for very large notebooks.
MAX_CONTENT_CHARS = 80_000

# Valid thinking levels for gemini-3-flash-preview.
# Maps the CLI --thinking flag value to the SDK ThinkingLevel enum.
THINKING_LEVEL_MAP: dict[str, types.ThinkingLevel] = {
    "minimal": types.ThinkingLevel.MINIMAL,
    "low":     types.ThinkingLevel.LOW,
    "medium":  types.ThinkingLevel.MEDIUM,
    "high":    types.ThinkingLevel.HIGH,
}

# Finish reasons that indicate the response was blocked or incomplete.
# These are not retryable — they require a different prompt or rubric adjustment.
_BLOCKED_FINISH_REASONS = {
    types.FinishReason.SAFETY,
    types.FinishReason.RECITATION,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.SPII,
}


# ---------------------------------------------------------------------------
# Gemini Client Initialization
# ---------------------------------------------------------------------------

def build_gemini_client(api_key: str) -> genai.Client:
    """
    Initializes and returns the Google GenAI client.

    Args:
        api_key: Your Google AI Studio API key (from GEMINI_API_KEY in .env).

    Returns:
        An initialized `google.genai.Client` instance.
    """
    client = genai.Client(api_key=api_key)
    logger.info("Google GenAI client initialized (model: %s).", GEMINI_MODEL)
    return client


# ---------------------------------------------------------------------------
# Rubric Loading
# ---------------------------------------------------------------------------

def load_rubric(rubric_file: str) -> str:
    """
    Reads the grading rubric from a plain-text or Markdown file.

    Args:
        rubric_file: Path to the rubric file (set in .env as RUBRIC_FILE).

    Returns:
        The rubric content as a string, or a default generic rubric if
        the file is not found.
    """
    path = Path(rubric_file)
    if not path.exists():
        logger.warning(
            "Rubric file '%s' not found. Using a generic rubric. "
            "Please create this file for accurate grading.",
            rubric_file,
        )
        return (
            "Evaluate the submission holistically on:\n"
            "  1. Correctness and accuracy of the solution (40%)\n"
            "  2. Code quality, readability, and best practices (30%)\n"
            "  3. Logical approach and algorithmic efficiency (20%)\n"
            "  4. Documentation and comments (10%)\n"
        )

    rubric_text = path.read_text(encoding="utf-8").strip()
    logger.info("Loaded rubric from '%s' (%d chars).", rubric_file, len(rubric_text))
    return rubric_text


# ---------------------------------------------------------------------------
# Prompt Construction
# ---------------------------------------------------------------------------

def build_grading_prompt(
    assignment_title: str,
    rubric: str,
    student_name: str,
    student_content: str,
    max_score: int,
) -> str:
    """
    Constructs the full grading prompt to send to Gemini.

    The prompt injects the rubric, the student's work, and explicit
    output instructions into a structured format that maximises
    consistent, evidence-based scoring.

    Args:
        assignment_title:  Human-readable assignment name (from .env).
        rubric:            The grading rubric text loaded from RUBRIC_FILE.
        student_name:      The student's full name (for context only).
        student_content:   The extracted text content of the student's work.
        max_score:         The maximum possible score (from MAX_SCORE in .env).

    Returns:
        A fully formatted prompt string.
    """
    # Truncate very long submissions to stay within cost/latency targets
    if len(student_content) > MAX_CONTENT_CHARS:
        logger.warning(
            "Student content for '%s' exceeds %d chars. Truncating.",
            student_name, MAX_CONTENT_CHARS,
        )
        student_content = (
            student_content[:MAX_CONTENT_CHARS]
            + f"\n\n[... CONTENT TRUNCATED AT {MAX_CONTENT_CHARS} CHARACTERS ...]"
        )

    return f"""You are an expert academic grader for the College of Engineering and Information Technology.
You are grading submissions for the following assignment: **{assignment_title}**

Your task is to evaluate the student's submitted work against the provided rubric and assign a fair, consistent score.

---
## GRADING RUBRIC
{rubric}
---

## STUDENT INFORMATION
- Student Name: {student_name}

## STUDENT'S SUBMITTED WORK
{student_content}

---
## YOUR GRADING TASK

Evaluate the above submission strictly according to the rubric.

Requirements:
- Assign an integer score between 0 and {max_score}.
- Write detailed, constructive feedback explaining:
    1. What the student did well.
    2. Specific areas where points were deducted and why.
    3. Concrete suggestions for improvement.
- Your feedback must be professional, specific to the actual submitted content, and helpful for learning.
- Do NOT give a perfect score unless the work is truly exceptional and meets every rubric criterion.
- If the submission appears empty, plagiarised, or completely off-topic, assign a score of 0 and explain why.

Respond ONLY with the JSON object as specified. Do not include any explanation outside the JSON.
"""


# ---------------------------------------------------------------------------
# Structured Output Schema
# ---------------------------------------------------------------------------

def get_grading_schema() -> types.Schema:
    """
    Defines the strict JSON response schema for Gemini structured output.

    Using `response_schema` forces Gemini to return EXACTLY:
      {
        "score":    <integer>,
        "feedback": "<string>"
      }

    No extra keys, no markdown fences, no prose outside the JSON object.
    This is critical for reliable automated parsing.

    Returns:
        A `google.genai.types.Schema` object.
    """
    return types.Schema(
        type=types.Type.OBJECT,
        description="Grading result containing a numeric score and detailed feedback.",
        properties={
            "score": types.Schema(
                type=types.Type.INTEGER,
                description="The integer score awarded to the student (0 to MAX_SCORE).",
            ),
            "feedback": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Detailed, constructive grading feedback explaining the score, "
                    "referencing specific parts of the student's submission."
                ),
            ),
        },
        required=["score", "feedback"],
    )


# ---------------------------------------------------------------------------
# Response Safety Guard
# ---------------------------------------------------------------------------

def _check_response_safety(response) -> None:
    """
    Inspects a GenerateContentResponse for safety blocks or incomplete outputs
    BEFORE accessing response.text, which raises if candidates are absent.

    Args:
        response: The GenerateContentResponse from client.models.generate_content.

    Raises:
        ValueError: If the response was blocked, truncated by MAX_TOKENS,
                    or has no usable candidates.
    """
    if not response.candidates:
        # No candidates at all — typically a prompt-level safety block
        raise ValueError(
            "Gemini returned no candidates. The prompt may have triggered a "
            "content safety filter. Check the student submission content."
        )

    candidate = response.candidates[0]
    finish_reason = candidate.finish_reason

    if finish_reason in _BLOCKED_FINISH_REASONS:
        raise ValueError(
            f"Gemini response blocked by safety filter "
            f"(finish_reason={finish_reason.name}). "
            "The student submission may contain content that triggered content policy. "
            "Review submission manually."
        )

    if finish_reason == types.FinishReason.MAX_TOKENS:
        logger.warning(
            "Gemini response truncated by MAX_TOKENS limit. "
            "Feedback may be incomplete. Consider increasing max_output_tokens."
        )
        # Allow processing to continue — partial feedback is better than none.

    if not candidate.content or not candidate.content.parts:
        raise ValueError(
            f"Gemini candidate has no content parts "
            f"(finish_reason={finish_reason}). Cannot extract text."
        )


# ---------------------------------------------------------------------------
# Gemini API Call with Retry Logic
# ---------------------------------------------------------------------------

class GeminiRateLimitError(Exception):
    """Raised when the Gemini API returns a rate limit (429) or quota error."""
    pass


@retry(
    retry=retry_if_exception_type(GeminiRateLimitError),
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=5, max=60),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _call_gemini_with_retry(
    client: genai.Client,
    prompt: str,
    schema: types.Schema,
    thinking_level: str = "medium",
) -> dict:
    """
    Makes the actual API call to Gemini with exponential backoff retry logic.

    Separated from `grade_submission` so tenacity can cleanly manage retries
    without rebuilding the prompt on each attempt.

    Args:
        client:         The initialized GenAI client.
        prompt:         The fully constructed grading prompt.
        schema:         The response schema enforcing structured output.
        thinking_level: One of 'minimal', 'low', 'medium', 'high'.
                        Controls how much internal reasoning gemini-3-flash-preview
                        performs. Higher = better accuracy, higher latency/cost.

    Returns:
        A dict with 'score' (int) and 'feedback' (str).

    Raises:
        GeminiRateLimitError: On 429 / quota exceeded — triggers tenacity retry.
        ValueError:           On blocked responses, empty content, or parse failure.
    """
    level_enum = THINKING_LEVEL_MAP.get(thinking_level, types.ThinkingLevel.MEDIUM)

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.2,          # Low temperature for consistent grading
                max_output_tokens=2048,   # Sufficient for detailed feedback
                thinking_config=types.ThinkingConfig(
                    thinking_level=level_enum,
                ),
            ),
        )
    except Exception as e:
        error_str = str(e).lower()
        if "429" in error_str or "quota" in error_str or "rate" in error_str:
            logger.warning("Gemini rate limit hit. Will retry after backoff: %s", e)
            raise GeminiRateLimitError(str(e)) from e
        raise  # Re-raise all non-rate-limit errors immediately

    # Guard: inspect candidates and finish_reason before touching response.text
    _check_response_safety(response)

    raw_text = response.text

    if not raw_text or not raw_text.strip():
        raise ValueError("Gemini returned an empty text response after safety check.")

    # Defensive strip of accidental markdown fences (schema should prevent this)
    clean_text = raw_text.strip().removeprefix("```json").removesuffix("```").strip()

    try:
        result = json.loads(clean_text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Could not parse Gemini response as JSON: {e}\nRaw response: {raw_text}"
        ) from e

    if "score" not in result or "feedback" not in result:
        raise ValueError(
            f"Gemini response missing required keys. "
            f"Got: {list(result.keys())}\nRaw: {raw_text}"
        )

    # Coerce score to int as a safety measure
    result["score"] = int(result["score"])
    return result


# ---------------------------------------------------------------------------
# Public Grading Interface
# ---------------------------------------------------------------------------

def grade_submission(
    client: genai.Client,
    assignment_title: str,
    rubric: str,
    student_name: str,
    student_content: str,
    max_score: int,
    thinking_level: str = "medium",
) -> Optional[dict]:
    """
    Orchestrates the AI grading of a single student's submission.

    Builds the prompt, enforces structured output via response_schema,
    applies ThinkingConfig, guards against safety blocks, and returns
    the validated grading result.

    Args:
        client:            The initialized GenAI client (from `build_gemini_client`).
        assignment_title:  Human-readable assignment name.
        rubric:            The grading rubric text.
        student_name:      The student's full name.
        student_content:   The extracted text content of the student's work.
        max_score:         The maximum achievable score.
        thinking_level:    Gemini 3 reasoning depth: 'minimal'|'low'|'medium'|'high'.
                           Defaults to 'medium'. Use 'high' for complex rubrics.

    Returns:
        A dict with:
          - 'score'    (int):  The awarded score, clamped to [0, max_score].
          - 'feedback' (str):  Detailed grading feedback.
        Returns None if the API call fails after all retries.
    """
    logger.info(
        "  Sending to Gemini (thinking=%s): %s", thinking_level, student_name
    )

    prompt = build_grading_prompt(
        assignment_title=assignment_title,
        rubric=rubric,
        student_name=student_name,
        student_content=student_content,
        max_score=max_score,
    )
    schema = get_grading_schema()

    try:
        result = _call_gemini_with_retry(client, prompt, schema, thinking_level)

        # Clamp score to valid range in case model drifts outside bounds
        original_score = result["score"]
        result["score"] = max(0, min(result["score"], max_score))
        if result["score"] != original_score:
            logger.warning(
                "  Gemini returned out-of-range score (%d). Clamped to %d.",
                original_score, result["score"],
            )

        logger.info(
            "  ✓ Grading complete — %s: %d/%d",
            student_name, result["score"], max_score,
        )
        return result

    except GeminiRateLimitError:
        logger.error(
            "  ✗ Gemini rate limit exhausted for '%s'. Skipping.", student_name
        )
        return None
    except ValueError as e:
        logger.error("  ✗ Response error for '%s': %s", student_name, e)
        return None
    except Exception as e:
        logger.error(
            "  ✗ Unexpected error grading '%s': %s", student_name, e, exc_info=True
        )
        return None

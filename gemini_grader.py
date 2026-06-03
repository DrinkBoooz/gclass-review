import logging
import os
import re
from pathlib import Path
from typing import Optional

import pypdf
from google import genai
from google.genai import types
from google.genai.types import GenerateContentConfig, ThinkingConfig
try:
    import docx
except Exception:  # pragma: no cover - optional dependency
    docx = None

logger = logging.getLogger(__name__)

# Resolve model once at import time so build_cached_prefix and grade_submission
# always reference the same model string — a mismatch causes a 400 from the API.
MODEL = os.getenv("GEMINI_MODEL") or os.getenv("AI_MODEL") or "gemini-3-flash-preview"


def _is_gemini_model(model: str) -> bool:
    """
    Returns True only for models served by Google's Gemini API.
    Local Ollama models (e.g. llama3.1:8b) must return False so caching and
    search grounding are skipped — those features only exist on Google infra.
    """
    name = model.lower().strip()
    if ":" in name:   # Ollama-style names always contain a colon (model:tag)
        return False
    return name.startswith("gemini")


DEFAULT_SYSTEM_PROMPT = "You are a rigorous academic grader. Follow the rubric exactly."


# ---------------------------------------------------------------------------
# File reading helpers
# ---------------------------------------------------------------------------

def _read_pdf_text(path: Path) -> str:
    reader = pypdf.PdfReader(str(path))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        text = text.strip()
        if text:
            pages.append(text)
    return "\n\n".join(pages)


def _read_docx_text(path: Path) -> str:
    if docx is None:
        return ""
    document = docx.Document(str(path))
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    table_lines: list[str] = []
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            cells = [c for c in cells if c]
            if cells:
                table_lines.append(" | ".join(cells))

    if table_lines:
        return "\n".join(paragraphs + [""] + table_lines)
    return "\n".join(paragraphs)


def load_prompt_file(file_path: str, default_text: str) -> str:
    if not file_path:
        return default_text
    path = Path(file_path)
    if not path.exists():
        return default_text
    if path.suffix.lower() == ".pdf":
        text = _read_pdf_text(path)
        return text if text else default_text
    if path.suffix.lower() == ".docx":
        text = _read_docx_text(path)
        return text if text else default_text
    text = path.read_text(encoding="utf-8").strip()
    return text if text else default_text


def build_gemini_client(api_key: str) -> genai.Client:
    logger.info("Gemini client initialized (model: %s).", MODEL)
    return genai.Client(api_key=api_key)


def load_system_prompt(system_prompt_file: str) -> str:
    return load_prompt_file(system_prompt_file, DEFAULT_SYSTEM_PROMPT)


def load_rubric(rubric_file: str) -> str:
    return load_prompt_file(rubric_file, "Grade holistically.")


# ---------------------------------------------------------------------------
# Explicit cache builder
# ---------------------------------------------------------------------------

def build_cached_prefix(
    client: genai.Client,
    rubric: str,
    system_prompt: str,
    rubric_label: str = "",
) -> str:
    """
    Creates an explicit CachedContent object containing the rubric and system
    prompt. Per-student calls that receive the returned name skip re-sending
    those tokens, cutting prompt cost for large batches.

    Returns "" when:
      - No rubric is provided
      - MODEL is a local/non-Gemini model (Ollama etc.) — caching API is
        Google-only.
    Falls back gracefully on any API error.
    """
    if not rubric:
        return ""

    if not _is_gemini_model(MODEL):
        logger.info(
            "Skipping explicit cache — model '%s' is not a Gemini API model.", MODEL
        )
        return ""

    display_name = f"grading-rubric-{rubric_label}" if rubric_label else "grading-rubric"

    try:
        cached = client.caches.create(
            model=MODEL,  # pinned — must match every generate_content call
            config=types.CreateCachedContentConfig(
                display_name=display_name,
                contents=[rubric],
                system_instruction=system_prompt,
                ttl="3600s",
            ),
        )
        logger.info("Cached prefix created: %s", cached.name)
        return cached.name or ""
    except Exception as e:
        logger.error(
            "Cached prefix not created — falling back to full prompts "
            "(all students pay full rubric token cost): %s", e
        )
        return ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_search_tool() -> types.Tool:
    """Google Search grounding tool — model decides when to invoke it."""
    return types.Tool(google_search=types.GoogleSearch())


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

def _count_cdrp_flags(raw: str) -> int:
    """
    Count CONFIRMED CDRP flags — not raw Pass 1 candidates.

    The gradewithai prompt runs a two-pass process:
      Pass 1 — extracts candidates into <AI_DETECTION_EVIDENCE_LOG>
      Pass 2 — runs CDRP Steps 1-3 on each candidate; only phrases that
               survive all three steps appear as FLAG FORMAT entries in the
               AI Usage Notice inside ===STUDENT FEEDBACK===

    A raw candidate that fails any CDRP step is dropped and must NOT be
    counted. Counting evidence log entries inflates the flag count because
    most candidates are legitimately rejected after adversarial challenge.

    Counting strategy (in priority order):
      1. Count (Category X —) FLAG FORMAT entries in the AI Usage Notice.
         These are the only phrases that passed all three CDRP steps.
      2. If the AI Usage Notice template is present but has zero Category
         entries (malformed — model wrote the notice but omitted flag blocks),
         treat as 1 confirmed flag so the warning tier still fires.
      3. If no AI Usage Notice at all → 0 confirmed flags.
    """
    # Extract student feedback section (everything after ===STUDENT FEEDBACK===)
    feedback_split = re.split(r"={3,}STUDENT FEEDBACK={3,}", raw, maxsplit=1)
    student_section = feedback_split[1] if len(feedback_split) == 2 else raw

    # Check if AI Usage Notice is present (notice template anchor phrase)
    notice_present = bool(re.search(
        r"contextually detached rhetorical polish", student_section, re.IGNORECASE
    ))

    if not notice_present:
        return 0

    # Count FLAG FORMAT entries: "(Category A —" / "(Category B –" etc.
    confirmed = re.findall(
        r"\(Category\s+[A-E]\s*[—–\-]", student_section, re.IGNORECASE
    )
    if confirmed:
        return len(confirmed)

    # Notice present but flag entries missing (malformed output) — treat as 1
    return 1


def _apply_penalty_tiers(rubric_score: float, cdrp_flags: int,
                          tr_status: str, max_score: int) -> float:
    """
    Apply the CEIT Faculty Operational Protocol penalty tiers.
    This overrides whatever penalty (or lack thereof) the model applied
    based on its own AI Score percentage.

    Tiers (as specified):
      Any flags + valid TR                → full rubric score, no penalty
      0 flags, no TR                      → full rubric score, no penalty
      1–2 flags, no TR                    → full rubric score, warning only
      3–4 flags, no TR                    → score capped at 60, oral defense notice
      5+ flags,  no TR                    → score set to 0, oral defense notice
    """
    tr_valid = tr_status.strip().lower() == "present and reflective"

    if tr_valid or cdrp_flags <= 2:
        return rubric_score                              # no penalty
    if cdrp_flags <= 4:
        return min(rubric_score, 60.0)                  # cap at 60
    return 0.0                                          # zero


def _parse_model_output(raw: str, max_score: int) -> dict:
    """
    Parse the model's free-form structured text output into a result dict.

    Uses the gradewithai prompt output format:
        <AI_DETECTION_EVIDENCE_LOG> ... </AI_DETECTION_EVIDENCE_LOG>
        ===GRADE BREAKDOWN===
        - Total Score: X / Y            (FORMAT A — no model penalty)
        - Rubric Score (before ...): X  (FORMAT B/C — model applied penalty)
        - Reflected Score: X / Y        (FORMAT B/C)
        - AI Score: N%
        - Transparency Report: ...
        CRITERIA: ...
        ===STUDENT FEEDBACK===

    Key design decisions:
      - CDRP flag count is counted from the evidence log, NOT from a field
        the gradewithai prompt never writes.
      - Rubric score is always extracted as the pre-penalty score so our
        own tier logic can recompute the final score independently.
      - The model's own penalty decision (FORMAT B/C based on AI Score %)
        is IGNORED — our tier logic overwrites it.
      - feedback = full raw output (gradewithai behaviour — instructor sees all).
    """
    result: dict = {
        "score": 0.0,
        "rubric_score": 0.0,
        "cdrp_flag_count": 0,
        "ai_score_pct": "",
        "transparency_report": "Not present",
        "feedback": raw.strip(),
        "raw_output": raw.strip(),
        "penalty_tier": "none",   # for logging: none | warning | cap60 | zero
    }

    # ── Step 1: Count confirmed CDRP flags from AI Usage Notice ─────────────
    # Uses confirmed Category A–E entries in the notice, NOT raw evidence log
    # candidates — candidates that fail Steps 1–3 must not be counted.
    result["cdrp_flag_count"] = _count_cdrp_flags(raw)

    # ── Step 2: Extract Transparency Report status ───────────────────────────
    tr_match = re.search(
        r"-\s*Transparency Report\s*:\s*(.+?)(?:\n|$)", raw, re.IGNORECASE
    )
    if tr_match:
        result["transparency_report"] = tr_match.group(1).strip()

    # ── Step 3: Extract the rubric (pre-penalty) score ───────────────────────
    # Always use Rubric Score line if present — it's the pre-penalty score
    # regardless of whether the model applied FORMAT B or C.
    # Fall back to Total Score (FORMAT A — no penalty was applied).
    rubric_match = re.search(
        r"-\s*Rubric Score[^:\n]*:\s*(\d+(?:\.\d+)?)\s*/\s*\d+", raw, re.IGNORECASE
    )
    total_match = re.search(
        r"-\s*Total Score\s*:\s*(\d+(?:\.\d+)?)\s*/\s*\d+", raw, re.IGNORECASE
    )

    if rubric_match:
        rubric_score = float(rubric_match.group(1))
    elif total_match:
        rubric_score = float(total_match.group(1))
    else:
        # Last resort: try Reflected Score (shouldn't happen but be safe)
        reflected_match = re.search(
            r"-\s*Reflected Score\s*:\s*(\d+(?:\.\d+)?)\s*/\s*\d+", raw, re.IGNORECASE
        )
        rubric_score = float(reflected_match.group(1)) if reflected_match else 0.0

    rubric_score = max(0.0, min(rubric_score, float(max_score)))
    result["rubric_score"] = rubric_score

    # ── Step 4: Apply our penalty tiers ─────────────────────────────────────
    # This overwrites the model's own FORMAT B/C decision entirely.
    final_score = _apply_penalty_tiers(
        rubric_score=rubric_score,
        cdrp_flags=result["cdrp_flag_count"],
        tr_status=result["transparency_report"],
        max_score=max_score,
    )
    result["score"] = final_score

    # Record which tier fired for logging
    flags = result["cdrp_flag_count"]
    tr_valid = result["transparency_report"].strip().lower() == "present and reflective"
    if tr_valid or flags <= 2:
        result["penalty_tier"] = "none" if flags == 0 else ("none_tr" if tr_valid else "warning")
    elif flags <= 4:
        result["penalty_tier"] = "cap60"
    else:
        result["penalty_tier"] = "zero"

    # ── Step 5: Extract AI Score percentage (reference only) ────────────────
    ai_match = re.search(r"-\s*AI Score\s*:\s*(\d+(?:\.\d+)?%?)", raw, re.IGNORECASE)
    if ai_match:
        val = ai_match.group(1).strip()
        result["ai_score_pct"] = val if "%" in val else f"{val}%"

    return result


# ---------------------------------------------------------------------------
# Two-pass grading when Google Search is active (non-cached path)
# ---------------------------------------------------------------------------

def _grade_with_search(
    client: genai.Client,
    system_prompt: str,
    rubric: str,
    student_name: str,
    text: str,
    max_score: int,
    thinking_level: str,
) -> tuple[Optional[dict], Optional[object]]:
    """
    Free-form grading call with Google Search grounding enabled.

    No response_schema — the model writes the structured text output defined
    in activity-checker-v4-fixed.md. _parse_model_output() extracts the
    fields afterwards. This prevents schema-forced hallucination where the
    model invents evidence to fill required JSON fields.
    """
    grading_prompt = (
        f"Grade the following student submission strictly against the rubric.\n"
        f"You may use Google Search to verify factual claims or look up domain "
        f"knowledge that helps you apply the rubric more accurately.\n\n"
        f"STUDENT NAME: {student_name}\n"
        f"SUBMISSION:\n{text}"
    )

    contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=grading_prompt)],
        )
    ]

    try:
        config = GenerateContentConfig(
            system_instruction=f"{system_prompt}\n\nRUBRIC:\n{rubric}",
            tools=[_build_search_tool()],
            temperature=0.6,
            thinking_config=ThinkingConfig(thinking_level=thinking_level),
            # No response_mime_type / response_schema — free-form text output
        )
        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=config,
        )

        raw = response.text or ""

        # Log search queries used — useful for rubric auditing
        candidate = response.candidates[0] if response.candidates else None
        if candidate and getattr(candidate, "grounding_metadata", None):
            queries = getattr(candidate.grounding_metadata, "web_search_queries", [])
            if queries:
                logger.info("  🔍 Search queries used: %s", queries)

        return raw, response

    except Exception as e:
        logger.error("  ✗ Search grading error: %s", e)
        return None, None


# ---------------------------------------------------------------------------
# Common post-processing — token usage attachment
# ---------------------------------------------------------------------------

def _attach_usage(result: dict, response: object) -> dict:
    usage = getattr(response, "usage_metadata", None)
    if usage:
        result["prompt_tokens"]     = getattr(usage, "prompt_token_count", 0)
        result["completion_tokens"] = getattr(usage, "response_token_count", 0)
        result["cached_tokens"]     = getattr(usage, "cached_content_token_count", 0)
        result["total_tokens"]      = getattr(usage, "total_token_count", 0)
    return result


# ---------------------------------------------------------------------------
# Main public entry point
# ---------------------------------------------------------------------------

def grade_submission_batch(
    client: genai.Client,
    assignment_title: str,
    rubric: str,
    system_prompt: str,
    students_data: list[dict],
    max_score: int,
    thinking_level: str = "minimal",
    cached_content_name: str = "",
) -> list[dict]:
    """
    Batches multiple student submissions into a single Gemini prompt using text delimiters,
    preserving the complex textual output schema defined in the system prompt.
    """
    # 1. Construct the combined prompt with strict delimiter instructions
    batch_prompt = (
        "You are receiving a batch of student submissions. You must grade EACH student "
        "individually according to the system prompt rules.\n\n"
        "CRITICAL OUTPUT INSTRUCTION:\n"
        "For EACH student, output your complete, fully-formatted grading block. "
        "The grading block starts with <AI_DETECTION_EVIDENCE_LOG> and ends after "
        "the ===STUDENT FEEDBACK=== prose paragraphs (the feedback prose is the last "
        "content — ===STUDENT FEEDBACK=== is a section header, not the final line).\n\n"
        "DELIMITER RULE: You MUST begin each student's grading block with this exact "
        "line on its own line, using the student's actual submission ID:\n"
        "  @@@STUDENT:<submission_id>@@@\n\n"
        "Example (structure only — use the real IDs below):\n"
        "  @@@STUDENT:abc123xyz@@@\n"
        "  <AI_DETECTION_EVIDENCE_LOG>\n"
        "  ...\n"
        "  </AI_DETECTION_EVIDENCE_LOG>\n"
        "  ===GRADE BREAKDOWN===\n"
        "  ...\n"
        "  ===STUDENT FEEDBACK===\n"
        "  [feedback prose here]\n\n"
        "Do not output anything before the first @@@STUDENT:...@@@ delimiter.\n"
        "Do not output anything between grading blocks except the next delimiter.\n\n"
        "SUBMISSIONS TO GRADE:\n\n"
    )

    for student in students_data:
        batch_prompt += f"@@@STUDENT:{student['submission_id']}@@@\n"
        batch_prompt += f"SUBMISSION:\n{student['content']}\n\n"

    contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=batch_prompt)],
        )
    ]

    try:
        if cached_content_name:
            config = GenerateContentConfig(
                temperature=0.6,
                thinking_config=ThinkingConfig(thinking_level=thinking_level),
                cached_content=cached_content_name,
            )
        else:
            config = GenerateContentConfig(
                system_instruction=f"{system_prompt}\n\nRUBRIC:\n{rubric}",
                temperature=0.6,
                thinking_config=ThinkingConfig(thinking_level=thinking_level),
                # No response_mime_type / response_schema — free-form text output
            )

        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=config,
        )

        raw = response.text or ""

        # 2. Parse the combined text response
        results = []
        # Split by the delimiter prefix
        parts = raw.split("@@@STUDENT:")

        for part in parts:
            if not part.strip():
                continue

            # Find the end of the submission id delimiter
            name_end = part.find("@@@")
            if name_end == -1:
                continue

            extracted_id = part[:name_end].strip()
            student_output = part[name_end + 3:].strip()

            # 3. Pass the extracted block into the existing parsing logic
            parsed_result = _parse_model_output(student_output, max_score)
            parsed_result["submission_id"] = extracted_id
            results.append(parsed_result)

        # Warn for any student whose block was not found in the model output
        returned_ids = {r["submission_id"] for r in results}
        for student in students_data:
            if student["submission_id"] not in returned_ids:
                logger.warning(
                    "  ⚠ Batch parse: no grading block found for submission_id=%s (%s). "
                    "This student will be marked failed_parsing and should be retried.",
                    student["submission_id"],
                    student.get("profile", {}).get("name", "?"),
                )

        # 4. Handle usage tracking (applied to the first result as a proxy for the batch)
        if results:
            usage = getattr(response, "usage_metadata", None)
            if usage:
                results[0]["usage_metrics"] = {
                    "prompt_tokens": getattr(usage, "prompt_token_count", 0),
                    "completion_tokens": getattr(usage, "response_token_count", 0),
                    "cached_tokens": getattr(usage, "cached_content_token_count", 0),
                    "total_tokens": getattr(usage, "total_token_count", 0),
                }

        return results

    except Exception as e:
        logger.error("  ✗ Batch grading error: %s", e)
        return []

def grade_submission(
    client: genai.Client,
    assignment_title: str,
    rubric: str,
    system_prompt: str,
    student_name: str,
    student_content: dict | str,
    max_score: int,
    thinking_level: str = "minimal",
    cached_content_name: str = "",
) -> Optional[dict]:
    logger.info("  [Gemini] Processing: %s", student_name)
    text = (
        student_content.get("text", "")
        if isinstance(student_content, dict)
        else str(student_content)
    )

    try:
        if cached_content_name:
            # ── Explicit-cache path ──────────────────────────────────────────
            # Rubric + system prompt already cached; only send per-student work.
            # No search tool, no response_schema — free-form text output.
            contents = [
                types.Content(
                    role="user",
                    parts=[types.Part(text=(
                        f"Grade this student submission.\n\n"
                        f"STUDENT NAME: {student_name}\n"
                        f"SUBMISSION:\n{text}"
                    ))],
                )
            ]
            config = GenerateContentConfig(
                temperature=0.6,
                thinking_config=ThinkingConfig(thinking_level=thinking_level),
                cached_content=cached_content_name,
                # No response_mime_type / response_schema — free-form text
            )
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=config,
            )
            raw = response.text or ""

        elif _is_gemini_model(MODEL):
            # ── Full-prompt path with Google Search grounding (Gemini only) ──
            # system_instruction carries rubric + system prompt identically
            # across all students — implicit caching fires automatically from
            # the second call onward.
            raw, response = _grade_with_search(
                client=client,
                system_prompt=system_prompt,
                rubric=rubric,
                student_name=student_name,
                text=text,
                max_score=max_score,
                thinking_level=thinking_level,
            )
            if raw is None:
                return None

        else:
            # ── Local model path (Ollama / custom endpoint) ──────────────────
            # No caching, no Google Search — plain generate_content call.
            # No response_schema — free-form text output.
            contents = [
                types.Content(
                    role="user",
                    parts=[types.Part(text=(
                        f"Grade this student submission.\n\n"
                        f"STUDENT NAME: {student_name}\n"
                        f"SUBMISSION:\n{text}"
                    ))],
                )
            ]
            config = GenerateContentConfig(
                system_instruction=f"{system_prompt}\n\nRUBRIC:\n{rubric}",
                temperature=0.6,
            )
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=config,
            )
            raw = response.text or ""

        # ── Parse free-form text output into structured result dict ──────────
        result = _parse_model_output(raw, max_score)
        result = _attach_usage(result, response)

        logger.info(
            "  ✓ Finished — %s: %d/%d (rubric: %d, cdrp_flags: %d)",
            student_name,
            result["score"],
            max_score,
            result["rubric_score"],
            result["cdrp_flag_count"],
        )
        return result

    except Exception as e:
        logger.error("  ✗ Gemini Error for %s: %s", student_name, e)
        return None
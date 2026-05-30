import json
import logging
import os
from pathlib import Path
from typing import List, Optional

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
MODEL = os.getenv("GEMINI_MODEL") or os.getenv("AI_MODEL") or "gemini-3.5-flash"


def _is_gemini_model(model: str) -> bool:
    """
    Returns True only for models served by Google's Gemini API.
    Local Ollama models (e.g. llama3.1:8b), custom endpoints, and any
    non-gemini string must return False so caching and search grounding
    are skipped — those features only exist on Google's infrastructure.
    """
    name = model.lower().strip()
    if ":" in name:          # Ollama-style names always contain a colon (model:tag)
        return False
    return name.startswith("gemini")


DEFAULT_SYSTEM_PROMPT = "You are a rigorous academic grader. Follow the rubric exactly."

# ---------------------------------------------------------------------------
# Output schema for the structured grading result.
# Passed as response_schema so the model is constrained to this shape.
#
# CriterionScore captures the per-criterion breakdown the rubric defines.
# GradeResult wraps the full grading output including that breakdown.
# ---------------------------------------------------------------------------
from pydantic import BaseModel


class CriterionScore(BaseModel):
    name: str         # Criterion name exactly as it appears in the rubric
    score: int        # Points awarded for this criterion
    max_score: int    # Maximum points for this criterion
    level: str        # Exact level label from the rubric (e.g. "Excellent", "Good")
    evidence: str     # Specific evidence: file + field + value found in submission


class GradeResult(BaseModel):
    rubric_score: int          # Sum of all criterion scores before any AI penalty
    reflected_score: int       # Score after AI penalty applied (same as rubric_score if no penalty)
    feedback: str              # Full student-facing feedback (markdown)
    ai_likelihood: str         # "Low" | "Med" | "High"
    cdrp_flag_count: int       # Number of phrases that passed all 3 CDRP steps (0–N)
    transparency_report: str   # "Present and reflective" | "Present but not reflective" | "Not present"
    criteria: List[CriterionScore]   # One entry per rubric criterion
    math_verified: str         # e.g. "25+20+18+25 = 88" — confirms criterion scores sum correctly


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
    prompt. All per-student calls that receive the returned name skip re-sending
    those tokens, cutting prompt cost for large batches.

    Returns "" (and skips the API call entirely) when:
      - No rubric is provided
      - MODEL is a local/non-Gemini model (e.g. llama3.1:8b via Ollama)
        because Google's caching API only supports Gemini models.
    Falls back gracefully on any other API error so callers use full prompts.
    """
    if not rubric:
        return ""

    if not _is_gemini_model(MODEL):
        logger.info(
            "Skipping explicit cache — model '%s' is not a Gemini API model "
            "(local/Ollama models don't support createCachedContent).", MODEL
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
        # Surface as error so operators know caching is off for this run.
        logger.error(
            "Cached prefix not created — falling back to full prompts "
            "(all students in this run pay full rubric token cost): %s", e
        )
        return ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_search_tool() -> types.Tool:
    """Google Search grounding tool — model decides when to invoke it."""
    return types.Tool(google_search=types.GoogleSearch())


def _extract_json(raw: str) -> dict:
    """
    Pull the first complete JSON object from raw text.
    Gemini with google_search active returns prose around the JSON;
    this handles that gracefully.
    """
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON object found in response: {raw[:200]!r}")
    return json.loads(raw[start:end])


def _fields_description(max_score: int) -> str:
    """
    Shared JSON field description injected into every grading prompt so the
    model always knows the exact shape required by GradeResult.
    """
    return (
        f"Return ONLY a JSON object with these exact fields:\n"
        f"  rubric_score (int, 0–{max_score}) — sum of all criterion scores before AI penalty\n"
        f"  reflected_score (int, 0–{max_score}) — after AI penalty; same as rubric_score if no penalty\n"
        f"  feedback (string, markdown) — full student-facing feedback\n"
        f"  ai_likelihood (string, one of: Low / Med / High)\n"
        f"  cdrp_flag_count (int) — number of phrases that passed all 3 CDRP steps\n"
        f"  transparency_report (string, one of: "
        f"'Present and reflective' | 'Present but not reflective' | 'Not present')\n"
        f"  criteria (array) — one object per rubric criterion, each with:\n"
        f"    name (string) — criterion name exactly as in the rubric\n"
        f"    score (int) — points awarded\n"
        f"    max_score (int) — maximum points for this criterion\n"
        f"    level (string) — exact level label from the rubric\n"
        f"    evidence (string) — specific evidence: file + field + value found\n"
        f"  math_verified (string) — e.g. '25+20+18+25 = 88'"
    )


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
    Grounded grading call with Google Search enabled.

    Tries response_schema=GradeResult + search first (compatible on gemini-3.5-flash
    per the Gemini 3 structured-output update). If the API rejects the combination
    (older model behaviour), retries without response_schema and parses prose JSON.
    """
    grading_prompt = (
        f"Grade the following student submission strictly against the rubric. "
        f"You may use Google Search to verify factual claims or look up domain knowledge "
        f"that helps you apply the rubric more accurately.\n\n"
        f"STUDENT NAME: {student_name}\n"
        f"SUBMISSION:\n{text}\n\n"
        + _fields_description(max_score)
    )

    contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=grading_prompt)],
        )
    ]

    for use_schema in (True, False):
        try:
            config_kwargs = dict(
                system_instruction=f"{system_prompt}\n\nRUBRIC:\n{rubric}",
                tools=[_build_search_tool()],
                temperature=0.6,
                thinking_config=ThinkingConfig(thinking_level=thinking_level),
            )
            if use_schema:
                config_kwargs["response_mime_type"] = "application/json"
                config_kwargs["response_schema"] = GradeResult

            config = GenerateContentConfig(**config_kwargs)
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=config,
            )

            raw = response.text or ""
            result = json.loads(raw) if use_schema else _extract_json(raw)

            # Log search queries used — useful for rubric auditing
            candidate = response.candidates[0] if response.candidates else None
            if candidate and getattr(candidate, "grounding_metadata", None):
                queries = getattr(candidate.grounding_metadata, "web_search_queries", [])
                if queries:
                    logger.info("  🔍 Search queries used: %s", queries)

            return result, response

        except Exception as e:
            if use_schema:
                logger.warning(
                    "  Search + JSON schema rejected by API (%s) — retrying without schema.", e
                )
                continue
            logger.error("  ✗ Search grading error: %s", e)
            return None, None

    return None, None


# ---------------------------------------------------------------------------
# Common post-processing
# ---------------------------------------------------------------------------

def _postprocess(result: dict, response: object, max_score: int) -> dict:
    """
    Clamps scores, back-fills legacy 'score' key, attaches token usage.
    Called by all three code paths after a successful generate_content call.
    """
    # Clamp rubric_score and reflected_score
    rubric_score = max(0, min(int(result.get("rubric_score", result.get("score", 0))), max_score))
    reflected_score = max(0, min(int(result.get("reflected_score", rubric_score)), max_score))
    result["rubric_score"] = rubric_score
    result["reflected_score"] = reflected_score
    # Keep legacy 'score' key pointing at reflected_score so downstream
    # pipeline code (CostTracker, CSV writer, summary table) still works
    # without changes.
    result["score"] = reflected_score

    # Clamp each criterion score against its own max_score
    for c in result.get("criteria", []):
        c_max = int(c.get("max_score", max_score))
        c["score"] = max(0, min(int(c.get("score", 0)), c_max))

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
            # No search tool — response_mime_type=application/json is safe here.
            contents = [
                types.Content(
                    role="user",
                    parts=[types.Part(text=(
                        f"Grade this student submission.\n\n"
                        f"STUDENT NAME: {student_name}\n"
                        f"SUBMISSION:\n{text}\n\n"
                        + _fields_description(max_score)
                    ))],
                )
            ]
            config = GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GradeResult,
                temperature=0.6,
                thinking_config=ThinkingConfig(thinking_level=thinking_level),
                cached_content=cached_content_name,
            )
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=config,
            )
            result = json.loads(response.text or "{}")

        elif _is_gemini_model(MODEL):
            # ── Full-prompt path with Google Search grounding (Gemini only) ──
            # system_instruction carries rubric + system prompt so the stable
            # prefix is identical across all students — implicit caching fires
            # automatically from the second call onward.
            result, response = _grade_with_search(
                client=client,
                system_prompt=system_prompt,
                rubric=rubric,
                student_name=student_name,
                text=text,
                max_score=max_score,
                thinking_level=thinking_level,
            )
            if result is None:
                return None

        else:
            # ── Local model path (Ollama / custom endpoint) ──────────────────
            # No caching, no Google Search — plain generate_content call.
            contents = [
                types.Content(
                    role="user",
                    parts=[types.Part(text=(
                        f"Grade this student submission.\n\n"
                        f"STUDENT NAME: {student_name}\n"
                        f"SUBMISSION:\n{text}\n\n"
                        + _fields_description(max_score)
                    ))],
                )
            ]
            config = GenerateContentConfig(
                system_instruction=f"{system_prompt}\n\nRUBRIC:\n{rubric}",
                response_mime_type="application/json",
                temperature=0.6,
            )
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=config,
            )
            result = _extract_json(response.text or "{}")

        # ── Common post-processing ───────────────────────────────────────────
        result = _postprocess(result, response, max_score)
        logger.info(
            "  ✓ Finished — %s: %d/%d (rubric: %d, reflected: %d)",
            student_name,
            result["score"],
            max_score,
            result["rubric_score"],
            result["reflected_score"],
        )
        return result

    except Exception as e:
        logger.error("  ✗ Gemini Error for %s: %s", student_name, e)
        return None
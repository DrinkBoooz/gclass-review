"""
classroom_handler.py
====================
Handles all interactions with the Google Classroom API:
  - OAuth 2.0 authentication (with token caching)
  - Fetching "turned in" student submissions for a given course/assignment
  - Downloading attachment content via Google Drive
  - Patching submissions with draft grades
  - Saving AI feedback to a local CSV (see note below)

─── IMPORTANT: Private Comments Limitation ───────────────────────────────────
The Google Classroom REST API v1 does NOT expose a private comment endpoint
for programmatic posting. The only fields patchable on a studentSubmission are
`draft_grade` and `assigned_grade`. There is no `privateComment` resource
accessible via the REST API outside of the Classroom add-on (IFrame) context.

This pipeline therefore:
  1. Posts the draft grade via the API (works correctly).
  2. Saves all AI feedback to a timestamped CSV file locally (see
     `save_feedback_to_csv`). The instructor can copy-paste feedback from
     the CSV into Classroom's private comment box, or use it as a mail-merge
     source to email students directly.

OAuth Setup:
  1. Enable the Google Classroom API and Google Drive API in your Google Cloud
     project at https://console.cloud.google.com/
  2. Create an OAuth 2.0 "Desktop app" credential → download as credentials.json
  3. Set GOOGLE_CREDENTIALS_FILE and GOOGLE_TOKEN_FILE in your .env file
  4. On first run, a browser tab will open for authorization
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations  # enables list[dict] syntax on Python 3.8/3.9

import csv
import logging
import os
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import pypdf
try:
    import pytesseract
    from pdf2image import convert_from_bytes
    from PIL import Image
except Exception:  # pragma: no cover - optional OCR deps
    pytesseract = None
    convert_from_bytes = None
    Image = None
try:
    import docx
except Exception:  # pragma: no cover - optional dependency
    docx = None
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OAuth Scopes
# ---------------------------------------------------------------------------
# Minimum required:
#   classroom.coursework.students  — list & patch all student submissions
#   classroom.rosters.readonly     — read student profiles (name, email)
#   drive.readonly                 — download submission attachments
SCOPES = [
    "https://www.googleapis.com/auth/classroom.coursework.students",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/classroom.courses.readonly",
]


def _is_rate_limit(exc: Exception) -> bool:
    """
    Tenacity retry predicate — returns True only for HTTP 429 (rate limit).

    Using this instead of retry_if_exception_type(HttpError) prevents
    unnecessary retries on permanent errors such as 403 Forbidden,
    404 Not Found, or 500 Internal Server Error.
    """
    return isinstance(exc, HttpError) and exc.resp.status == 429


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate_classroom(credentials_file: str, token_file: str):
    """
    Authenticates with the Google Classroom API using OAuth 2.0.

    On first run, opens a browser tab for the instructor to authorize the app.
    Subsequent runs reuse the cached token stored at `token_file`.

    Args:
        credentials_file: Path to the OAuth client secrets JSON downloaded
                          from Google Cloud Console.
        token_file:        Path where the cached user token will be stored.

    Returns:
        Tuple of (classroom_service, credentials) — the credentials are
        returned so they can be reused to build the Drive service.

    Raises:
        FileNotFoundError: If credentials_file does not exist.
    """
    if not Path(credentials_file).exists():
        raise FileNotFoundError(
            f"OAuth credentials file not found: '{credentials_file}'\n"
            "Download your OAuth 2.0 Desktop App client secrets from "
            "Google Cloud Console > APIs & Services > Credentials, "
            "save them as the file specified in GOOGLE_CREDENTIALS_FILE."
        )

    creds: Optional[Credentials] = None

    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        logger.info("Loaded cached OAuth token from '%s'.", token_file)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired OAuth token...")
            creds.refresh(Request())
        else:
            logger.info("No valid token found — opening browser for authorization...")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)

            # Support headless / non-interactive environments by honoring
            # the NO_BROWSER environment variable. When set (1/true/yes),
            # use the console OAuth flow which prints a URL and accepts the
            # authorization code on stdin instead of spinning up a local web
            # server.
            no_browser = os.getenv("NO_BROWSER", "").lower() in ("1", "true", "yes")
            if no_browser:
                logger.info("NO_BROWSER set — using console OAuth flow (run_console()).")
                creds = flow.run_console()
            else:
                try:
                    creds = flow.run_local_server(port=0)
                except Exception as e:
                    logger.warning(
                        "Local server OAuth failed (%s); falling back to console flow.", e
                    )
                    creds = flow.run_console()

        with open(token_file, "w") as f:
            f.write(creds.to_json())
        logger.info("OAuth token saved to '%s'.", token_file)

    service = build("classroom", "v1", credentials=creds)
    logger.info("Google Classroom API client initialized.")
    return service, creds


# ---------------------------------------------------------------------------
# Fetching Submissions
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception(_is_rate_limit),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def get_turned_in_submissions(service, course_id: str, coursework_id: str) -> list[dict]:
    """
    Fetches all student submissions with state "TURNED_IN" for a given assignment.

    Handles pagination automatically. Retries ONLY on HTTP 429 (rate limit)
    with exponential backoff — permanent errors (403, 404, 500) are raised
    immediately without burning retry attempts.

    Args:
        service:        Authenticated Google Classroom API service.
        course_id:      The Classroom course ID.
        coursework_id:  The specific assignment (coursework) ID.

    Returns:
        List of submission resource dicts, each containing keys such as
        'id', 'userId', 'assignmentSubmission' (with 'attachments'), etc.

    Raises:
        HttpError: On any API error (429 is retried; all others raised immediately).
    """
    logger.info(
        "Fetching TURNED_IN submissions — course=%s, coursework=%s",
        course_id, coursework_id,
    )

    submissions = []
    page_token = None

    while True:
        response = (
            service.courses()
            .courseWork()
            .studentSubmissions()
            .list(
                courseId=course_id,
                courseWorkId=coursework_id,
                states=["TURNED_IN"],
                pageToken=page_token,
            )
            .execute()
        )
        # HttpError propagates directly — 429 triggers the retry decorator,
        # all other status codes surface immediately to the caller.

        page_submissions = response.get("studentSubmissions", [])
        submissions.extend(page_submissions)
        logger.info("  Fetched %d submission(s) so far...", len(submissions))

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logger.info("Total TURNED_IN submissions retrieved: %d", len(submissions))
    return submissions


def get_student_profile(service, course_id: str, user_id: str) -> dict:
    """
    Retrieves a student's display name and email from the course roster.

    Args:
        service:    Authenticated Classroom API service.
        course_id:  The Classroom course ID.
        user_id:    The student's Google user ID (from the submission resource).

    Returns:
        Dict with 'name' (str) and 'email' (str).
        Falls back to safe placeholder values on any error.
    """
    try:
        student = (
            service.courses()
            .students()
            .get(courseId=course_id, userId=user_id)
            .execute()
        )
        profile = student.get("profile", {})
        name = profile.get("name", {}).get("fullName", f"User_{user_id}")
        email = profile.get("emailAddress", "unknown@email.com")
        return {"name": name, "email": email}
    except HttpError as e:
        logger.warning("Could not fetch profile for userId=%s: %s", user_id, e)
        return {"name": f"User_{user_id}", "email": "unknown@email.com"}


# ---------------------------------------------------------------------------
# Attachment Content Extraction
# ---------------------------------------------------------------------------

def build_drive_service(creds):
    """Builds a Google Drive API service using the same OAuth credentials."""
    return build("drive", "v3", credentials=creds)


def extract_attachment_content(attachment: dict, drive_service) -> tuple[Optional[str], str]:
    """
    Extracts readable text content from a single submission attachment.

    Supports:
      - Google Drive files: exported as plain text (Docs, Sheets, Colab, .py, .ipynb)
      - Link attachments: returns URL string for context
      - YouTube attachments: returns URL string for context

    Args:
        attachment:    A single attachment dict from the submission resource.
        drive_service: Authenticated Google Drive API service.

    Returns:
        String containing the attachment content, or a descriptive placeholder.
        Returns None if the attachment type is unrecognized.
    """
    # --- Google Drive file ---
    drive_file = attachment.get("driveFile")
    if drive_file:
        file_id = drive_file.get("id")
        file_title = drive_file.get("title", "Untitled")
        logger.info("  Downloading Drive file: '%s' (id=%s)", file_title, file_id)

        try:
            file_meta = drive_service.files().get(
                fileId=file_id, fields="mimeType,name"
            ).execute()
            mime_type = file_meta.get("mimeType", "")

            if "google-apps" in mime_type:
                export_map = {
                    "application/vnd.google-apps.document": "text/plain",
                    "application/vnd.google-apps.spreadsheet": "text/csv",
                    "application/vnd.google-apps.presentation": "text/plain",
                    "application/vnd.google.colaboratory": "text/plain",
                }
                content_bytes = drive_service.files().export(
                    fileId=file_id, mimeType=export_map.get(mime_type, "text/plain")
                ).execute()
                return content_bytes.decode("utf-8", errors="replace"), "text/plain"
            else:
                content_bytes = drive_service.files().get_media(fileId=file_id).execute()
                if mime_type == "application/pdf":
                    try:
                        reader = pypdf.PdfReader(BytesIO(content_bytes))
                        pages = [page.extract_text() or "" for page in reader.pages]
                        text = "\n\n".join(pages).strip()
                        if not text:
                            ocr_text = _ocr_pdf_bytes(content_bytes)
                            if ocr_text:
                                return ocr_text, "application/pdf"
                            return "[PDF contains no extractable text (scanned/image-based)]", "application/pdf"
                        return text, "application/pdf"
                    except Exception as e:
                        return f"[PDF read error: {e}]", "application/pdf"
                # Handle .docx files (local uploads or mislabelled types)
                elif (
                    mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    or (isinstance(file_title, str) and file_title.lower().endswith(".docx"))
                ):
                    if docx is None:
                        return (
                            f"[Binary .docx attachment (python-docx not installed): {file_title}]",
                            mime_type,
                        )
                    try:
                        document = docx.Document(BytesIO(content_bytes))
                        full_text = [para.text for para in document.paragraphs]
                        text = "\n".join(full_text).strip()
                        if not text:
                            return (f"[DOCX contains no extractable text: {file_title}]", mime_type)
                        return text, mime_type
                    except Exception as e:
                        return (f"[DOCX read error: {e}]", mime_type)

                elif _is_text_mime(mime_type):
                    return content_bytes.decode("utf-8", errors="replace"), mime_type
                else:
                    return f"[Binary/non-text attachment: {file_title}]", mime_type

        except HttpError as e:
            return f"[Could not retrieve attachment: {file_title} — {e}]", "text/plain"
        except UnicodeDecodeError:
            return f"[Binary/non-text attachment: {file_title}]", "text/plain"

    # --- Link attachment ---
    link = attachment.get("link")
    if link:
        url = link.get("url", "")
        title = link.get("title", url)
        logger.info("  Link attachment: '%s' → %s", title, url)
        return f"[Link Submission: {title} | URL: {url}]", "text/plain"

    # --- YouTube attachment ---
    youtube = attachment.get("youTubeVideo")
    if youtube:
        url = f"https://www.youtube.com/watch?v={youtube.get('id', '')}"
        title = youtube.get("title", url)
        logger.info("  YouTube attachment: '%s'", title)
        return f"[YouTube Submission: {title} | URL: {url}]", "text/plain"

    logger.warning("  Unrecognized attachment type: %s", list(attachment.keys()))
    return f"[Unrecognized attachment type]", "text/plain"


def collect_submission_content(submission: dict, drive_service) -> str:
    """
    Aggregates all attachment content from a student submission into a single
    string suitable for passing to the Gemini grader.

    Args:
        submission:    Full submission resource dict from the Classroom API.
        drive_service: Authenticated Google Drive API service.

    Returns:
        Combined string of all attachment contents, or a clear placeholder
        if no attachments are found or no content could be extracted.
    """
    assignment_submission = submission.get("assignmentSubmission", {})
    attachments = assignment_submission.get("attachments", [])

    if not attachments:
        logger.warning(
            "  Submission ID=%s has no attachments.", submission.get("id")
        )
        return {"text": "", "binary_parts": []}

    text_parts: list[str] = []
    binary_parts: list[tuple[bytes, str]] = []

    for i, attachment in enumerate(attachments, start=1):
        logger.info("  Processing attachment %d/%d...", i, len(attachments))
        content, mime = extract_attachment_content(attachment, drive_service)
        if content is None:
            continue

        if isinstance(content, bytes):
            binary_parts.append((content, mime or "application/octet-stream"))
        else:
            # content is text
            text_parts.append(str(content))

    if not text_parts and not binary_parts:
        return {"text": "", "binary_parts": []}

    return {"text": "\n\n".join(text_parts), "binary_parts": binary_parts}


def _is_text_mime(mime: str) -> bool:
    """Rudimentary check whether a MIME type represents text."""
    if not mime:
        return False
    return mime.startswith("text/") or mime in (
        "application/json",
        "application/javascript",
        "application/xml",
        "application/x-python-code",
    )


def _ocr_pdf_bytes(content_bytes: bytes) -> str:
    if pytesseract is None or convert_from_bytes is None or Image is None:
        logger.warning("OCR skipped: pytesseract/pdf2image/Pillow not installed.")
        return ""

    poppler_path = os.getenv("POPPLER_PATH", "").strip() or None
    tesseract_path = os.getenv("TESSERACT_PATH", "").strip()
    if tesseract_path:
        tesseract_exe = tesseract_path
        if os.path.isdir(tesseract_exe):
            tesseract_exe = os.path.join(tesseract_exe, "tesseract.exe")
        if os.path.exists(tesseract_exe):
            pytesseract.pytesseract.tesseract_cmd = tesseract_exe
        else:
            logger.warning("TESSERACT_PATH set but tesseract.exe not found: %s", tesseract_exe)
    if poppler_path:
        pdftoppm_path = os.path.join(poppler_path, "pdftoppm.exe")
        if not os.path.exists(pdftoppm_path):
            logger.warning("POPPLER_PATH set but pdftoppm.exe not found: %s", pdftoppm_path)
    else:
        logger.warning("POPPLER_PATH is not set; OCR may fail for scanned PDFs.")

    try:
        images = convert_from_bytes(content_bytes, poppler_path=poppler_path)
    except Exception as e:
        logger.warning("OCR skipped: PDF to image conversion failed (%s).", e)
        return ""

    ocr_pages: list[str] = []
    for img in images:
        try:
            ocr_pages.append(pytesseract.image_to_string(img))
        except Exception as e:
            logger.warning("OCR page failed: %s", e)

    return "\n\n".join([t.strip() for t in ocr_pages if t.strip()])


# ---------------------------------------------------------------------------
# Posting Draft Grade
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception(_is_rate_limit),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def post_draft_grade(
    service,
    course_id: str,
    coursework_id: str,
    submission_id: str,
    score: int,
    max_score: int,
) -> bool:
    """
    Posts a draft grade to a student's submission via the Classroom API.

    The grade is posted as a DRAFT — it is visible only to the instructor
    until they click "Return" in the Classroom UI to release it to the student.

    Retries ONLY on HTTP 429 (rate limit). Permanent errors (403, 500, etc.)
    are surfaced immediately without consuming retry attempts.

    NOTE: The Classroom REST API v1 does not support posting private comments
    programmatically. Feedback is saved to a local CSV by `save_feedback_to_csv`.
    See module docstring for full explanation.

    Args:
        service:        Authenticated Classroom API service.
        course_id:      The Classroom course ID.
        coursework_id:  The assignment ID.
        submission_id:  The specific submission ID to update.
        score:          The integer score from the AI grader.
        max_score:      Maximum possible score — used to clamp the value.

    Returns:
        True on success, False on unrecoverable failure.
    """
    clamped_score = max(0, min(score, max_score))

    try:
        service.courses().courseWork().studentSubmissions().patch(
            courseId=course_id,
            courseWorkId=coursework_id,
            id=submission_id,
            updateMask="draftGrade",
            body={"draftGrade": clamped_score},
        ).execute()

        logger.info("  ✓ Draft grade posted: %d/%d", clamped_score, max_score)
        return True

    except HttpError as e:
        logger.error(
            "  ✗ Failed to post draft grade for submission %s: %s",
            submission_id, e
        )
        return False


# ---------------------------------------------------------------------------
# Feedback CSV Export
# ---------------------------------------------------------------------------

def _safe_filename(label: str) -> str:
    # Strip only Windows-invalid filename characters; keep title punctuation like # and en dash.
    cleaned = re.sub(r"[<>:\"/|?*]", "", label)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned



def save_feedback_to_csv(
    results: list[dict],
    output_dir: str = ".",
    coursework_title: str = "",
) -> str:
    """
    Saves all grading results and AI feedback to a timestamped CSV file.

    This is the primary mechanism for delivering per-student feedback, since
    the Classroom REST API does not support posting private comments.

    The instructor can use this CSV to:
      - Copy-paste feedback into Classroom's private comment box per student
      - Mail-merge feedback directly to students via email
      - Keep a local grading record for auditing

    CSV columns (core):
        Student Name, Score, Rubric Score, Max Score, Percentage, Result,
        AI Detection, CDRP Flags, Transparency Report, Feedback, Status

    Per-criterion columns are appended dynamically based on the criteria list
    returned by the grader — one column per criterion in the format
    "[Criterion Name] Score", "[Criterion Name] Level", "[Criterion Name] Evidence".
    This means the column count adapts automatically to any rubric.

    Args:
        results:    List of result dicts accumulated during the pipeline run.
        output_dir: Directory where the CSV will be written. Defaults to CWD.
        coursework_title: Used for the output filename when provided.

    Returns:
        The full path to the written CSV file.

    Raises:
        RuntimeError: If the CSV cannot be written (disk full, bad path, etc.).
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if coursework_title:
        slug = _safe_filename(coursework_title)
        filename = f"{slug}-results.csv" if slug else f"grading_results_{timestamp}.csv"
    else:
        filename = f"grading_results_{timestamp}.csv"
    filepath = os.path.join(output_dir, filename)

    # ── AI label derived from CDRP flags + Transparency Report ───────────────
    # Based on CEIT Faculty Operational Protocol (Dan's AI Detection Guide).
    # Numeric percentages are rejected by the guide (Section 3) as unreliable.
    def _ai_label(flag_count: Optional[int], tr_status: str) -> str:
        """
        Rules (matching the guide's Section 5 Human-in-the-Loop flow):
          - Valid Transparency Report always clears to Human Written regardless
            of flag count — student disclosed and explained their use.
          - 0 flags → Human Written  (no structural AI markers found)
          - 1 flag  → Mostly Human   (one weak signal; borderline)
          - 2–3 flags → Mostly AI    (pattern is consistent across submission)
          - 4+  flags → AI Generated (pervasive CDRP throughout)
        """
        if tr_status == "Present and reflective":
            return "Human Written"
        if flag_count is None:
            return ""
        if flag_count == 0:
            return "Human Written"
        if flag_count == 1:
            return "Mostly Human"
        if flag_count <= 3:
            return "Mostly AI"
        return "AI Generated"

    # ── Discover all criterion names across all results ───────────────────────
    # Criteria names are collected in insertion order so the column layout is
    # consistent even if different submissions have slightly different ordering.
    seen_criteria: list[str] = []
    for r in results:
        for c in r.get("criteria", []):
            name = c.get("name", "")
            if name and name not in seen_criteria:
                seen_criteria.append(name)

    # ── Build fieldnames ──────────────────────────────────────────────────────
    core_fields = [
        "Student Name",
        "Score",           # reflected_score (after any AI penalty)
        "Rubric Score",    # rubric_score (before penalty) — for instructor review
        "Max Score",
        "Percentage",
        "Result",          # PASS / FAIL based on 75% threshold
        "AI Detection",
        "CDRP Flags",
        "Transparency Report",
        "Math Verified",
        "Feedback",
        "Status",
    ]
    # Append one triplet of columns per criterion in rubric order
    criterion_fields = []
    for name in seen_criteria:
        criterion_fields += [
            f"{name} — Score",
            f"{name} — Level",
            f"{name} — Evidence",
        ]
    fieldnames = core_fields + criterion_fields

    try:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for r in results:
                score        = r.get("score", "")          # reflected_score
                rubric_score = r.get("rubric_score", score) # before penalty
                max_s        = r.get("max_score", "")

                flag_count = r.get("cdrp_flag_count")
                flag_count_int = None
                if isinstance(flag_count, (int, float)):
                    flag_count_int = int(flag_count)
                elif isinstance(flag_count, str) and flag_count.strip().isdigit():
                    flag_count_int = int(flag_count.strip())

                tr_status = r.get("transparency_report", "")

                pct = (
                    f"{(score / max_s * 100):.1f}%"
                    if isinstance(score, int) and max_s
                    else ""
                )

                # PASS/FAIL at 75% threshold (Philippine HE standard)
                if isinstance(score, int) and max_s:
                    result_label = "PASS" if (score / max_s) >= 0.75 else "FAIL"
                else:
                    result_label = ""

                row = {
                    "Student Name":      r.get("name", ""),
                    "Score":             score,
                    "Rubric Score":      rubric_score,
                    "Max Score":         max_s,
                    "Percentage":        pct,
                    "Result":            result_label,
                    "AI Detection":      _ai_label(flag_count_int, tr_status),
                    "CDRP Flags":        flag_count_int if flag_count_int is not None else "",
                    "Transparency Report": tr_status,
                    "Math Verified":     r.get("math_verified", ""),
                    "Feedback":          r.get("feedback", ""),
                    "Status":            r.get("status", ""),
                }

                # Index this submission's criteria by name for fast lookup
                criteria_by_name = {
                    c.get("name", ""): c
                    for c in r.get("criteria", [])
                }
                for name in seen_criteria:
                    c = criteria_by_name.get(name, {})
                    row[f"{name} — Score"]    = c.get("score", "") if c else ""
                    row[f"{name} — Level"]    = c.get("level", "") if c else ""
                    row[f"{name} — Evidence"] = c.get("evidence", "") if c else ""

                writer.writerow(row)

    except OSError as e:
        raise RuntimeError(
            f"Failed to write feedback CSV to '{filepath}': {e}\n"
            "Check that the output directory exists and has write permissions."
        ) from e

    logger.info("Feedback CSV saved to: %s", filepath)
    return filepath
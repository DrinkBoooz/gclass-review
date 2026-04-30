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
from datetime import datetime
from pathlib import Path
from typing import Optional

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
            creds = flow.run_local_server(port=0)

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


def extract_attachment_content(attachment: dict, drive_service) -> Optional[str]:
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
                # Map Google Workspace MIME types to plain-text export formats
                export_map = {
                    "application/vnd.google-apps.document":     "text/plain",
                    "application/vnd.google-apps.spreadsheet":  "text/csv",
                    "application/vnd.google-apps.presentation": "text/plain",
                    "application/vnd.google.colaboratory":      "text/plain",
                }
                export_mime = export_map.get(mime_type, "text/plain")
                content_bytes = drive_service.files().export(
                    fileId=file_id, mimeType=export_mime
                ).execute()
                content_str = content_bytes.decode("utf-8", errors="replace")
                logger.info("  Exported as plain text (%d chars).", len(content_str))
                return f"=== Attachment: {file_title} ===\n{content_str}"

            else:
                # Binary or plain-text file uploaded directly (.py, .ipynb, etc.)
                content_bytes = drive_service.files().get_media(fileId=file_id).execute()
                content_str = content_bytes.decode("utf-8", errors="replace")
                logger.info("  Downloaded binary/text file (%d chars).", len(content_str))
                return f"=== Attachment: {file_title} ===\n{content_str}"

        except HttpError as e:
            if e.resp.status == 403:
                logger.warning(
                    "  Permission denied for '%s'. Check that the OAuth account "
                    "has Viewer access to student Drive files.", file_title
                )
            else:
                logger.warning("  Failed to download '%s': %s", file_title, e)
            return f"[Could not retrieve attachment: {file_title} — {e}]"

        except UnicodeDecodeError:
            logger.warning(
                "  '%s' appears to be a non-text binary (PDF, image). "
                "Manual review required.", file_title
            )
            return f"[Binary/non-text attachment: {file_title}. Manual review required.]"

    # --- Link attachment ---
    link = attachment.get("link")
    if link:
        url = link.get("url", "")
        title = link.get("title", url)
        logger.info("  Link attachment: '%s' → %s", title, url)
        return f"[Link Submission: {title} | URL: {url}]"

    # --- YouTube attachment ---
    youtube = attachment.get("youTubeVideo")
    if youtube:
        url = f"https://www.youtube.com/watch?v={youtube.get('id', '')}"
        title = youtube.get("title", url)
        logger.info("  YouTube attachment: '%s'", title)
        return f"[YouTube Submission: {title} | URL: {url}]"

    logger.warning("  Unrecognized attachment type: %s", list(attachment.keys()))
    return None


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
        return "[NO ATTACHMENTS FOUND — student may have submitted without uploading work.]"

    content_parts = []
    for i, attachment in enumerate(attachments, start=1):
        logger.info("  Processing attachment %d/%d...", i, len(attachments))
        content = extract_attachment_content(attachment, drive_service)
        if content:
            content_parts.append(content)

    if not content_parts:
        return "[COULD NOT EXTRACT ANY ATTACHMENT CONTENT — all attachments failed to download.]"

    return "\n\n".join(content_parts)


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

def save_feedback_to_csv(results: list[dict], output_dir: str = ".") -> str:
    """
    Saves all grading results and AI feedback to a timestamped CSV file.

    This is the primary mechanism for delivering per-student feedback, since
    the Classroom REST API does not support posting private comments.

    The instructor can use this CSV to:
      - Copy-paste feedback into Classroom's private comment box per student
      - Mail-merge feedback directly to students via email
      - Keep a local grading record for auditing

    CSV columns:
        name, email, submission_id, score, max_score, percentage, status, feedback

    Args:
        results:    List of result dicts accumulated during the pipeline run.
                    Each dict should have: name, email, submission_id, score,
                    feedback, status keys.
        output_dir: Directory where the CSV will be written. Defaults to CWD.

    Returns:
        The full path to the written CSV file.

    Raises:
        RuntimeError: If the CSV cannot be written (disk full, bad path, etc.).
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"grading_results_{timestamp}.csv"
    filepath = os.path.join(output_dir, filename)

    fieldnames = [
        "name", "email", "submission_id",
        "score", "max_score", "percentage",
        "status", "feedback"
    ]

    try:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for r in results:
                score = r.get("score", "")
                max_s = r.get("max_score", "")
                pct = (
                    f"{(score / max_s * 100):.1f}%"
                    if isinstance(score, int) and max_s
                    else ""
                )
                writer.writerow({
                    "name":          r.get("name", ""),
                    "email":         r.get("email", ""),
                    "submission_id": r.get("submission_id", ""),
                    "score":         score,
                    "max_score":     max_s,
                    "percentage":    pct,
                    "status":        r.get("status", ""),
                    "feedback":      r.get("feedback", ""),
                })
    except OSError as e:
        raise RuntimeError(
            f"Failed to write feedback CSV to '{filepath}': {e}\n"
            "Check that the output directory exists and has write permissions."
        ) from e

    logger.info("Feedback CSV saved to: %s", filepath)
    return filepath

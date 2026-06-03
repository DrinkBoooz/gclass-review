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
import json
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

    def _needs_full_submission(submission: dict) -> bool:
        assignment_submission = submission.get("assignmentSubmission")
        if assignment_submission is None or assignment_submission == {}:
            return True
        if "attachments" not in assignment_submission:
            return True
        return False

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
                fields="*"
            )
            .execute()
        )
        # HttpError propagates directly — 429 triggers the retry decorator,
        # all other status codes surface immediately to the caller.

        page_submissions = response.get("studentSubmissions", [])

        # Some list responses omit assignmentSubmission data even when requested.
        # If that happens, fetch the full submission via a get() call.
        for index, submission in enumerate(page_submissions):
            if not _needs_full_submission(submission):
                continue
            submission_id = submission.get("id")
            if not submission_id:
                continue
            try:
                full_submission = (
                    service.courses()
                    .courseWork()
                    .studentSubmissions()
                    .get(
                        courseId=course_id,
                        courseWorkId=coursework_id,
                        id=submission_id,
                        fields="*"
                    )
                    .execute()
                )
                page_submissions[index] = full_submission
            except HttpError as e:
                logger.warning(
                    "  Failed to refetch submission %s for full data: %s",
                    submission_id,
                    e,
                )

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
                # Docs are exported as HTML so that embedded hyperlinks
                # ("click here" style anchors) are preserved and converted to
                # inline [URL] markers. All other Google types stay as text/plain.
                if mime_type == "application/vnd.google-apps.document":
                    html_bytes = drive_service.files().export(
                        fileId=file_id, mimeType="text/html"
                    ).execute()
                    html_str = html_bytes.decode("utf-8", errors="replace")
                    return _html_to_text_with_links(html_str), "text/plain"
                export_map = {
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
                        image_count = _count_pdf_images(content_bytes)
                        if not text:
                            ocr_text = _ocr_pdf_bytes(content_bytes)
                            if ocr_text:
                                if image_count:
                                    ocr_text = f"{ocr_text}\n\n[Embedded images detected: {image_count}]"
                                return ocr_text, "application/pdf"
                            return "[PDF contains no extractable text (scanned/image-based)]", "application/pdf"
                        if image_count:
                            text = f"{text}\n\n[Embedded images detected: {image_count}]"
                        # Extract clickable link annotations (e.g. "click here" hyperlinks)
                        pdf_links = _extract_pdf_hyperlinks(content_bytes)
                        if pdf_links:
                            link_lines = "\n".join(
                                f"[Hyperlink: {disp!r} → {url}]"
                                for disp, url in pdf_links
                            )
                            text = text + "\n\n" + link_lines
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
                        image_count = 0
                        try:
                            image_count = len(document.inline_shapes)
                        except Exception:
                            image_count = 0
                        full_text = [para.text for para in document.paragraphs]
                        text = "\n".join(full_text).strip()
                        # Extract hyperlinks embedded in paragraphs via XML walk.
                        # para.text silently drops <w:hyperlink> runs — we recover them.
                        hyperlinks = _extract_docx_hyperlinks(document)
                        if hyperlinks:
                            link_lines = "\n".join(
                                f"[Hyperlink: {disp!r} → {url}]"
                                for disp, url in hyperlinks
                            )
                            text = text + "\n\n" + link_lines
                        if not text.strip():
                            return (f"[DOCX contains no extractable text: {file_title}]", mime_type)
                        if image_count:
                            text = f"{text}\n\n[Embedded images detected: {image_count}]"
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
    # A "link" attachment may be a plain URL, a Google Drive link, or a YouTube
    # link submitted via the "Add link" button rather than "Add Drive file".
    # We detect each case and handle it appropriately.
    link = attachment.get("link")
    if link:
        url   = (link.get("url") or "").strip()
        title = (link.get("title") or "").strip() or url or "(untitled link)"

        if not url:
            logger.warning("  Link attachment has empty URL (title=%r) — skipping.", title)
            return f"[Link Submission: {title} | URL: (none)]", "text/plain"

        logger.info("  Link attachment: %r → %s", title, url)

        # ── YouTube URL submitted as a plain link ────────────────────────────
        yt_id = _extract_youtube_id(url)
        if yt_id:
            yt_url = f"https://www.youtube.com/watch?v={yt_id}"
            logger.info("  Resolved link as YouTube video: %s", yt_url)
            return f"[YouTube Submission: {title} | URL: {yt_url}]", "text/plain"

        # ── Google Forms URL — content not fetchable via API ─────────────────
        if _GFORMS_RE.search(url):
            logger.info("  Link is a Google Form — responses not accessible via API.")
            return f"[Google Form Submission: {title} | URL: {url}]", "text/plain"

        # ── Google Drive / Docs / Sheets / Slides URL ────────────────────────
        # When a student uses "Add link" to share a Google Doc (instead of
        # "Add Drive file"), the attachment is a link, not a driveFile.
        # We detect the file ID and fetch the content exactly as we would for
        # a driveFile attachment.
        gdrive_id = _extract_gdrive_id(url)
        if gdrive_id and drive_service is not None:
            logger.info(
                "  Link URL contains Drive file ID %s — fetching content.", gdrive_id
            )
            try:
                file_meta = drive_service.files().get(
                    fileId=gdrive_id, fields="mimeType,name"
                ).execute()
                mime_type  = file_meta.get("mimeType", "")
                file_title = file_meta.get("name", title)

                if "google-apps" in mime_type:
                    export_map = {
                        "application/vnd.google-apps.document":     "text/plain",
                        "application/vnd.google-apps.spreadsheet":  "text/csv",
                        "application/vnd.google-apps.presentation": "text/plain",
                        "application/vnd.google.colaboratory":      "text/plain",
                    }
                    content_bytes = drive_service.files().export(
                        fileId=gdrive_id,
                        mimeType=export_map.get(mime_type, "text/plain"),
                    ).execute()
                    text = content_bytes.decode("utf-8", errors="replace")
                    logger.info(
                        "  Fetched Drive file via link: %r (%s, %d chars)",
                        file_title, mime_type, len(text),
                    )
                    return text, "text/plain"

                # Binary Drive file (PDF, docx uploaded to Drive, etc.)
                # Re-use the same driveFile branch by constructing a synthetic
                # driveFile attachment dict and recursing once.
                synthetic = {"driveFile": {"id": gdrive_id, "title": file_title}}
                return extract_attachment_content(synthetic, drive_service)

            except HttpError as e:
                logger.warning(
                    "  Could not fetch Drive file %s from link URL (%s): %s",
                    gdrive_id, url, e,
                )
                # Fall through to plain-URL fallback below

        # ── Plain external URL ───────────────────────────────────────────────
        return f"[Link Submission: {title} | URL: {url}]", "text/plain"

    # --- YouTube attachment (submitted via the dedicated YouTube picker) -----
    youtube = attachment.get("youTubeVideo")
    if youtube:
        yt_id  = youtube.get("id", "")
        url    = f"https://www.youtube.com/watch?v={yt_id}" if yt_id else ""
        title  = (youtube.get("title") or "").strip() or url or "(untitled video)"
        logger.info("  YouTube attachment: %r", title)
        return f"[YouTube Submission: {title} | URL: {url}]", "text/plain"

    logger.warning("  Unrecognized attachment type: %s", list(attachment.keys()))
    return "[Unrecognized attachment type]", "text/plain"


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
    short_answer = submission.get("shortAnswerSubmission", {})
    multiple_choice = submission.get("multipleChoiceSubmission", {})

    text_parts: list[str] = []
    binary_parts: list[tuple[bytes, str]] = []
    image_attachments: list[str] = []

    short_answer_text = (short_answer or {}).get("answer")
    if isinstance(short_answer_text, str) and short_answer_text.strip():
        text_parts.append(short_answer_text.strip())

    multiple_choice_answer = (multiple_choice or {}).get("answer")
    if isinstance(multiple_choice_answer, str) and multiple_choice_answer.strip():
        text_parts.append(multiple_choice_answer.strip())

    if not attachments and not text_parts:
        logger.warning(
            "  Submission ID=%s has no attachments. Raw assignmentSubmission keys: %s",
            submission.get("id"),
            list(assignment_submission.keys()) or "(empty — API returning partial response)",
        )
        snapshot = json.dumps(submission, default=str, ensure_ascii=True)
        if len(snapshot) > 4000:
            snapshot = f"{snapshot[:4000]}... [truncated]"
        logger.warning("  Submission snapshot: %s", snapshot)
        return {"text": "", "binary_parts": []}

    for i, attachment in enumerate(attachments, start=1):
        logger.info("  Processing attachment %d/%d...", i, len(attachments))
        content, mime = extract_attachment_content(attachment, drive_service)
        if content is None:
            continue

        if isinstance(content, bytes):
            resolved_mime = mime or "application/octet-stream"
            if resolved_mime.startswith("image/"):
                image_attachments.append(f"attachment_{i}{Path(resolved_mime).suffix}")
            binary_parts.append((content, resolved_mime))
        else:
            # content is text
            text_parts.append(str(content))
            if (mime or "").startswith("image/"):
                image_attachments.append(f"attachment_{i}{Path(mime).suffix}")

    if not text_parts and not binary_parts:
        return {"text": "", "binary_parts": []}

    if image_attachments:
        image_list = ", ".join(image_attachments)
        text_parts.insert(0, f"[Attachments: {len(image_attachments)} image(s) — {image_list}]")

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


# ---------------------------------------------------------------------------
# URL helpers — extract Drive file IDs and YouTube video IDs from raw URLs
# ---------------------------------------------------------------------------

# Matches Google Drive / Docs / Sheets / Slides / Colab file IDs inside URLs.
# Groups: (docs_id, drive_path_id, drive_query_id, colab_id)
_GDRIVE_ID_RE = re.compile(
    r'(?:'
    r'docs\.google\.com/(?:document|spreadsheets|presentation|forms)/d/([a-zA-Z0-9_-]{20,})'
    r'|drive\.google\.com/file/d/([a-zA-Z0-9_-]{20,})'
    r'|drive\.google\.com/(?:open|uc)\?(?:[^&\s]*&)*id=([a-zA-Z0-9_-]{20,})'
    r'|colab\.research\.google\.com/drive/([a-zA-Z0-9_-]{20,})'
    r')',
)

# Matches YouTube video IDs in watch?v=, youtu.be/, and embed/ URLs.
_YOUTUBE_ID_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:[^&\s]*&)*v=|embed/)|youtu\.be/)([a-zA-Z0-9_-]{11})'
)

# Google Forms URLs — we recognise them but cannot fetch their responses.
_GFORMS_RE = re.compile(r'docs\.google\.com/forms/d/')


def _extract_gdrive_id(url: str) -> Optional[str]:
    """Return the Drive file ID embedded in *url*, or None if not a Drive URL."""
    m = _GDRIVE_ID_RE.search(url)
    if not m:
        return None
    return next((g for g in m.groups() if g), None)


def _extract_youtube_id(url: str) -> Optional[str]:
    """Return the YouTube video ID embedded in *url*, or None."""
    m = _YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None



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


def _count_pdf_images(content_bytes: bytes) -> int:
    try:
        reader = pypdf.PdfReader(BytesIO(content_bytes))
    except Exception:
        return 0

    count = 0
    for page in reader.pages:
        try:
            resources = page.get("/Resources") or {}
            xobject = resources.get("/XObject")
            if not xobject:
                continue
            xobject = xobject.get_object()
            for obj in xobject.values():
                try:
                    obj = obj.get_object()
                    if obj.get("/Subtype") == "/Image":
                        count += 1
                except Exception:
                    continue
        except Exception:
            continue
    return count


# ---------------------------------------------------------------------------
# Hyperlink extraction helpers
# ---------------------------------------------------------------------------

def _extract_docx_hyperlinks(document) -> list[tuple[str, str]]:
    """
    Walks all paragraph XML trees to find <w:hyperlink> elements with external
    relationship IDs. Returns a list of (display_text, url) tuples.
    Plain text export (para.text) silently drops these — this recovers them.
    """
    if docx is None:
        return []
    try:
        from docx.oxml.ns import qn as _qn
    except Exception:
        return []

    results: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    for para in document.paragraphs:
        for hl in para._element.findall(".//" + _qn("w:hyperlink")):
            r_id = hl.get(_qn("r:id"))
            if not r_id:
                continue
            try:
                url = document.part.target_ref(r_id)
            except Exception:
                continue
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            texts = [t.text for t in hl.findall(".//" + _qn("w:t")) if t.text]
            display = "".join(texts).strip() or url
            results.append((display, url))

    return results


def _extract_pdf_hyperlinks(content_bytes: bytes) -> list[tuple[str, str]]:
    """
    Reads all /Link annotations from every page of a PDF and returns a list of
    (display_text, url) tuples. The display text is the annotation rectangle's
    approximate text region (best-effort); falls back to the URL itself.
    """
    results: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    try:
        reader = pypdf.PdfReader(BytesIO(content_bytes))
        for page in reader.pages:
            if "/Annots" not in page:
                continue
            for annot_ref in page["/Annots"]:
                try:
                    annot = annot_ref.get_object()
                except Exception:
                    continue
                if annot.get("/Subtype") != "/Link":
                    continue
                action = annot.get("/A", {})
                url = ""
                try:
                    url = str(action.get("/URI", "") or "").strip()
                except Exception:
                    pass
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                # Use the URL itself as display text (PDF doesn't reliably expose it)
                results.append((url, url))
    except Exception as e:
        logger.debug("PDF hyperlink extraction error: %s", e)
    return results


def _html_to_text_with_links(html: str) -> str:
    """
    Converts HTML (from Google Docs export) to plain text, replacing
    <a href="URL">TEXT</a> with "TEXT [URL]" so hyperlinks are preserved.
    """
    # Replace each anchor tag with display + URL
    def _sub(m: re.Match) -> str:
        url     = m.group(1).strip()
        display = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not display or display.lower() in ("click here", "here", "link", "this", "view"):
            return f"{display} [{url}]".strip()
        return f"{display} [{url}]"

    _HREF_RE = re.compile(
        r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    text = _HREF_RE.sub(_sub, html)
    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse whitespace runs
    text = re.sub(r"\n\s*\n", "\n\n", text).strip()
    return text


# ---------------------------------------------------------------------------
# Group Work Detection
# ---------------------------------------------------------------------------

#: Keywords (case-insensitive) in the coursework title or description that
#: strongly suggest the assignment is a group/team activity.
_GROUP_KEYWORDS_RE = re.compile(
    r'\b(group|team|pair|collab|pangkat|grupo|partnership|tandem)\b',
    re.IGNORECASE,
)


def detect_group_submissions(
    submissions: list[dict],
    coursework_title: str = "",
    coursework_description: str = "",
) -> dict[str, dict]:
    """
    Identifies group-work submissions by detecting shared Drive file IDs across
    multiple students, then enriches each submission with group metadata.

    Detection uses two signals (both zero extra API calls):

    PRIMARY — Shared Drive file:
        If two or more submissions reference the exact same driveFile ID, those
        students submitted the same document and are in a group.

    SECONDARY — Coursework title / description keyword:
        If the assignment title or description contains group-work keywords
        ("group", "team", "pair", "pangkat", "grupo", etc.), every submission is
        flagged as group work even when Drive IDs cannot confirm it.

    Args:
        submissions:             Full list of studentSubmission dicts from the API.
        coursework_title:        The assignment title string (optional).
        coursework_description:  The assignment description string (optional).

    Returns:
        A dict keyed by submission ID, each value being a metadata dict:
            {
                "is_group_work": bool,
                "group_members": list[str],   # submission IDs of all members
                "shared_file_id": str | None, # Drive file ID if detected
                "detection_method": str,      # "shared_file" | "keyword" | "none"
            }
        Submissions with no group signal get is_group_work=False.
    """
    # ── Signal 1: keyword in coursework title / description ──────────────────
    keyword_signal = bool(
        _GROUP_KEYWORDS_RE.search(coursework_title or "")
        or _GROUP_KEYWORDS_RE.search(coursework_description or "")
    )
    if keyword_signal:
        logger.info(
            "Group-work keyword detected in coursework title/description: %r",
            coursework_title,
        )

    # ── Signal 2: shared driveFile IDs ───────────────────────────────────────
    # Build a map: file_id → [submission_id, ...]
    file_id_to_submissions: dict[str, list[str]] = {}

    for sub in submissions:
        sub_id      = sub.get("id", "")
        attachments = (sub.get("assignmentSubmission") or {}).get("attachments", [])
        for att in attachments:
            drive_file = att.get("driveFile")
            if drive_file:
                fid = drive_file.get("id")
                if fid:
                    file_id_to_submissions.setdefault(fid, []).append(sub_id)
                continue
            # Also detect Drive IDs embedded in link URLs
            link_url = (att.get("link") or {}).get("url", "")
            if link_url:
                fid = _extract_gdrive_id(link_url)
                if fid:
                    file_id_to_submissions.setdefault(fid, []).append(sub_id)

    # Shared files: any file_id referenced by more than one submission
    shared: dict[str, list[str]] = {
        fid: sids
        for fid, sids in file_id_to_submissions.items()
        if len(sids) > 1
    }

    if shared:
        logger.info(
            "Shared Drive files detected across submissions: %s",
            {fid: sids for fid, sids in shared.items()},
        )

    # ── Build result metadata per submission ──────────────────────────────────
    result: dict[str, dict] = {}

    for sub in submissions:
        sub_id = sub.get("id", "")

        # Find if this submission is part of any shared-file group
        matched_file_id: Optional[str] = None
        group_members:   list[str]     = []

        for fid, sids in shared.items():
            if sub_id in sids:
                matched_file_id = fid
                group_members   = sids
                break

        if matched_file_id:
            result[sub_id] = {
                "is_group_work":    True,
                "group_members":    group_members,
                "shared_file_id":   matched_file_id,
                "detection_method": "shared_file",
            }
        elif keyword_signal:
            # Keyword signal only — we know it's group work but can't name members
            result[sub_id] = {
                "is_group_work":    True,
                "group_members":    [],
                "shared_file_id":   None,
                "detection_method": "keyword",
            }
        else:
            result[sub_id] = {
                "is_group_work":    False,
                "group_members":    [],
                "shared_file_id":   None,
                "detection_method": "none",
            }

    return result


def build_group_context_note(
    submission_id: str,
    group_map: dict[str, dict],
    profile_map: dict[str, dict],
) -> str:
    """
    Returns a plain-text note to prepend to a submission's content so the
    grader knows it is group work and who the members are.

    Args:
        submission_id: The current student's submission ID.
        group_map:     Output of detect_group_submissions().
        profile_map:   Dict mapping submission_id → {"name": str, "email": str}.

    Returns:
        A non-empty string if this is group work, else "".
    """
    meta = group_map.get(submission_id, {})
    if not meta.get("is_group_work"):
        return ""

    method  = meta.get("detection_method", "none")
    members = meta.get("group_members", [])

    if method == "shared_file" and members:
        names = []
        for sid in members:
            p = profile_map.get(sid)
            names.append(p["name"] if p else sid)
        members_str = ", ".join(names)
        return (
            f"[GROUP WORK \u2014 Shared submission]\n"
            f"This document was submitted by multiple students: {members_str}.\n"
            f"Grade this submission as a GROUP output. All listed members receive "
            f"the same grade unless the rubric specifies otherwise.\n"
        )

    if method == "keyword":
        return (
            "[GROUP WORK \u2014 Assignment title indicates a group/team activity]\n"
            "Grade this submission as a GROUP output. All members of this group "
            "receive the same grade unless the rubric specifies otherwise.\n"
        )

    return ""


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
    Saves all grading results to a timestamped CSV file.

    Schema matches gradeflowai.com output format with one addition:
    CDRP Flags replaces the numeric AI Score percentage — derived from the
    CEIT Faculty Operational Protocol (Dan's AI Detection Guide) which
    explicitly rejects numeric percentages as statistically unreliable.

    Columns:
        Student Name, Score, Max Score, Percentage,
        Feedback, Status, AI Detection, CDRP Flags

    Feedback column contains ONLY the ===STUDENT FEEDBACK=== prose extracted
    from the model output — not the full grade breakdown. The full model
    output is preserved in result["raw_output"] for audit purposes.

    Args:
        results:          List of result dicts from the pipeline run.
        output_dir:       Directory where the CSV will be written.
        coursework_title: Used for the output filename when provided.

    Returns:
        Full path to the written CSV file.

    Raises:
        RuntimeError: If the CSV cannot be written.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if coursework_title:
        slug = _safe_filename(coursework_title)
        filename = f"{slug}-results.csv" if slug else f"grading_results_{timestamp}.csv"
    else:
        filename = f"grading_results_{timestamp}.csv"
    filepath = os.path.join(output_dir, filename)

    # ── AI label derived from CDRP flag count + Transparency Report ──────────
    # Mirrors _apply_penalty_tiers in gemini_grader.py exactly.
    # Tiers:
    #   Any flags + valid TR  → Human Written (disclosed; full score)
    #   0 flags               → Human Written
    #   1–2 flags, no TR      → Mostly Human  (warning only, no score change)
    #   3–4 flags, no TR      → Mostly AI     (score capped at 60, oral defense)
    #   5+ flags,  no TR      → AI Generated  (score set to 0, oral defense)
    def _ai_label(flag_count: Optional[int], tr_status: str) -> str:
        tr_valid = (tr_status or "").strip().lower() == "present and reflective"
        if tr_valid:
            return "Human Written"
        if flag_count is None:
            return ""
        if flag_count == 0:
            return "Human Written"
        if flag_count <= 2:
            return "Mostly Human"
        if flag_count <= 4:
            return "Mostly AI"
        return "AI Generated"

    fieldnames = [
        "Student Name",
        "Score",
        "Max Score",
        "Percentage",
        "Feedback",
        "Status",
        "AI Detection",
        "CDRP Flags",   # Our differentiator — integer count, not a percentage
    ]

    try:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for r in results:
                score = r.get("score", "")
                max_s = r.get("max_score", "")

                flag_count = r.get("cdrp_flag_count")
                flag_count_int = None
                if isinstance(flag_count, (int, float)):
                    flag_count_int = int(flag_count)
                elif isinstance(flag_count, str) and flag_count.strip().isdigit():
                    flag_count_int = int(flag_count.strip())

                tr_status = r.get("transparency_report", "")

                # Support both int and float scores (e.g. 93.3 / 100)
                try:
                    pct = (
                        f"{(float(score) / float(max_s) * 100):.2f}%"
                        if score != "" and max_s
                        else ""
                    )
                except (TypeError, ZeroDivisionError):
                    pct = ""

                writer.writerow({
                    "Student Name": r.get("name", ""),
                    "Score":        score,
                    "Max Score":    max_s,
                    "Percentage":   pct,
                    "Feedback":     r.get("feedback", ""),
                    "Status":       r.get("status", ""),
                    "AI Detection": _ai_label(flag_count_int, tr_status),
                    "CDRP Flags":   flag_count_int if flag_count_int is not None else "",
                })

    except OSError as e:
        raise RuntimeError(
            f"Failed to write feedback CSV to '{filepath}': {e}\n"
            "Check that the output directory exists and has write permissions."
        ) from e

    logger.info("Feedback CSV saved to: %s", filepath)
    return filepath
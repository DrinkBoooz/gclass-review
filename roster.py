"""
roster.py
=========
Roster loading, name extraction from submission text, and fuzzy student matching.

Supports:
  - Loading a class roster from one or more CSV/Excel files (LASTNAME, FIRSTNAME MI. format)
  - Extracting student names from document headers (group labels + individual labels)
  - Fuzzy-matching extracted names against the roster using rapidfuzz WRatio
  - Detecting group vs individual submissions and resolving member identities

Group detection logic (in order of priority):
  1. Document contains a group-work label AND 2+ names matched → group
  2. Document contains an individual label AND exactly 1 name matched → individual
  3. Document contains 2+ matched names with no label → group (names-only header)
  4. Document contains exactly 1 matched name → individual
  5. No names found → unresolved (fall back to submission owner)

Duplicate submission penalty:
  If multiple students in the same group each submit their own copy of a file
  (i.e. the same group_key appears more than once in TURNED_IN submissions),
  a 5-point deduction is applied to all members and a note is appended.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

try:
    from rapidfuzz import fuzz, process as fuzz_process  # type: ignore[import-untyped]
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover
    fuzz = None          # type: ignore[assignment]
    fuzz_process = None  # type: ignore[assignment]
    _RAPIDFUZZ_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "rapidfuzz not installed — fuzzy name matching disabled. "
        "Run: pip install rapidfuzz"
    )

try:
    import openpyxl  # type: ignore[import-untyped]
    _OPENPYXL_AVAILABLE = True
except ImportError:
    openpyxl = None  # type: ignore[assignment]
    _OPENPYXL_AVAILABLE = False

try:
    import pandas as pd  # type: ignore[import-untyped]
    _PANDAS_AVAILABLE = True
except ImportError:
    pd = None  # type: ignore[assignment]
    _PANDAS_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum WRatio score to accept a name match (0–100).
FUZZY_THRESHOLD = 72

#: How many characters from the top of a document to scan for name headers.
HEADER_SCAN_CHARS = 1200

#: Labels that indicate a GROUP submission.
_GROUP_LABEL_RE = re.compile(
    r'\b(group\s*members?|members?|submitted\s+by|prepared\s+by|'
    r'pangkat|grupo|group\s*name|team\s*members?|proponents?)\b',
    re.IGNORECASE,
)

#: Labels that indicate an INDIVIDUAL submission.
_INDIVIDUAL_LABEL_RE = re.compile(
    r'\b(student\s*name|name|your\s+name|individual|pangalan|'
    r'prepared\s+by|submitted\s+by)\b',
    re.IGNORECASE,
)

#: Splits a raw header value into candidate name tokens.
_HEADER_VALUE_RE = re.compile(
    r'(?:group\s*members?|members?|submitted\s+by|prepared\s+by|'
    r'student\s*names?|names?|pangkat|grupo|pangalan|team\s*members?|proponents?)'
    r'\s*[:=\-]?\s*(.+?)(?:\n\n|\Z)',
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Roster loading
# ---------------------------------------------------------------------------

def load_roster(paths: list[str]) -> list[dict]:
    """
    Loads one or more CSV or Excel roster files and returns a unified list of
    student records.

    Each record is a dict:
        {
            "roster_name":  str,   # original entry e.g. "SANTOS, ALICE M."
            "normalized":   str,   # lowercase, no punctuation e.g. "alice santos m"
            "last":         str,   # "SANTOS"
            "first":        str,   # "ALICE"
            "middle":       str,   # "M." (may be empty)
            "section":      str,   # e.g. "BSIT-3A" (may be empty)
            "email":        str,   # may be empty
        }

    The roster file must have at least one column whose header contains "name"
    (case-insensitive). Optional columns: "section", "email".

    Args:
        paths: List of file paths to CSV or Excel roster files.

    Returns:
        Deduplicated list of student records sorted by last name.

    Raises:
        ValueError: If a file cannot be parsed or has no name column.
    """
    all_records: list[dict] = []
    seen_normalized: set[str] = set()

    for path_str in paths:
        path = Path(path_str)
        if not path.exists():
            logger.warning("Roster file not found: %s", path)
            continue

        suffix = path.suffix.lower()
        try:
            if suffix in (".xlsx", ".xls"):
                records = _load_excel_roster(path)
            elif suffix == ".csv":
                records = _load_csv_roster(path)
            else:
                logger.warning("Unsupported roster format: %s", suffix)
                continue
        except Exception as e:
            raise ValueError(f"Failed to load roster file '{path}': {e}") from e

        added = 0
        for rec in records:
            norm = rec["normalized"]
            if norm and norm not in seen_normalized:
                seen_normalized.add(norm)
                all_records.append(rec)
                added += 1

        logger.info("Loaded %d student(s) from %s", added, path.name)

    all_records.sort(key=lambda r: r["last"])
    logger.info("Roster total: %d unique student(s).", len(all_records))
    return all_records


def _parse_name_entry(raw: str) -> dict:
    """
    Parses a single roster name entry in "LASTNAME, FIRSTNAME [MI.]" format.
    Also handles "FIRSTNAME LASTNAME" (no comma) as a fallback.
    """
    raw = raw.strip()
    parts = [p.strip() for p in raw.split(",", 1)]

    if len(parts) == 2:
        last = parts[0].strip()
        first_tokens = parts[1].strip().split()
        first  = first_tokens[0] if first_tokens else ""
        middle = first_tokens[1] if len(first_tokens) > 1 else ""
    else:
        # No comma — treat last word as last name
        tokens = raw.split()
        last   = tokens[-1] if tokens else raw
        first  = tokens[0]  if len(tokens) > 1 else ""
        middle = tokens[1]  if len(tokens) > 2 else ""

    normalized = _normalize_name(f"{first} {last} {middle}")
    return {
        "roster_name": raw,
        "normalized":  normalized,
        "last":        last.upper(),
        "first":       first.upper(),
        "middle":      middle.upper(),
        "section":     "",
        "email":       "",
    }


def _normalize_name(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = re.sub(r"[.,\'\"]", "", s).lower().strip()
    return re.sub(r"\s+", " ", s)


def _find_name_column(headers: list[str]) -> Optional[int]:
    """Return index of the first column whose header contains 'name'."""
    for i, h in enumerate(headers):
        if "name" in h.lower():
            return i
    return None


def _find_column(headers: list[str], keyword: str) -> Optional[int]:
    for i, h in enumerate(headers):
        if keyword.lower() in h.lower():
            return i
    return None


def _looks_like_bare_last(s: str) -> bool:
    """All-caps, 1–3 words, no periods — looks like a LASTNAME(-only) cell."""
    words = s.strip().split()
    return (1 <= len(words) <= 3 and
            all(w.isupper() and "." not in w for w in words))


def _looks_like_first_cluster(s: str) -> bool:
    """1–3 words starting with an uppercase letter, no digits or hyphens."""
    words = s.strip().split()
    if not words or len(words) > 3 or not words[0][0].isupper():
        return False
    return not any(re.search(r"[\d\-]", w) for w in words)


def _merge_split_name(row: list[str], name_col: int) -> str:
    """
    Handles unquoted CSVs where "HO, KEVIN LESTER" was split by the comma
    into row[name_col]="HO" and row[name_col+1]=" KEVIN LESTER".
    If the adjacent cell looks like a first-name cluster, merges them back.
    """
    raw = row[name_col].strip()
    next_col = name_col + 1
    if next_col < len(row):
        nxt = row[next_col].strip()
        if _looks_like_bare_last(raw) and _looks_like_first_cluster(nxt):
            return f"{raw}, {nxt}"
    return raw


def _load_csv_roster(path: Path) -> list[dict]:
    import csv
    records = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        return []

    headers = rows[0]
    name_col    = _find_name_column(headers)
    section_col = _find_column(headers, "section")
    email_col   = _find_column(headers, "email")

    if name_col is None:
        raise ValueError(f"No 'name' column found in {path.name}. Headers: {headers}")

    for row in rows[1:]:
        if name_col >= len(row):
            continue
        # Merge split LAST / FIRST columns caused by unquoted CSV commas
        raw_name = _merge_split_name(row, name_col)
        if not raw_name:
            continue
        rec = _parse_name_entry(raw_name)
        # section_col may have shifted if we merged two name columns;
        # use the column after the potential merged pair as the safe offset.
        effective_section_col = section_col
        effective_email_col   = email_col
        rec["section"] = (row[effective_section_col].strip()
                          if effective_section_col and effective_section_col < len(row) else "")
        rec["email"]   = (row[effective_email_col].strip()
                          if effective_email_col   and effective_email_col   < len(row) else "")
        records.append(rec)

    return records


def _load_excel_roster(path: Path) -> list[dict]:
    if _PANDAS_AVAILABLE:
        import pandas as pd  # type: ignore[import-untyped]
        df = pd.read_excel(path, dtype=str).fillna("")
        headers = list(df.columns)
    elif _OPENPYXL_AVAILABLE:
        import openpyxl  # type: ignore[import-untyped]
        wb  = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws  = wb.active
        rows = [[str(c.value or "").strip() for c in row] for row in ws.iter_rows()]
        wb.close()
        if not rows:
            return []
        headers = rows[0]
        # Convert to pseudo-dataframe list of dicts
        df_rows = [dict(zip(headers, row)) for row in rows[1:]]
        df = df_rows  # handled below
    else:
        raise ImportError("Install pandas or openpyxl to read Excel files.")

    name_col    = _find_name_column(headers)
    section_col = _find_column(headers, "section")
    email_col   = _find_column(headers, "email")

    if name_col is None:
        raise ValueError(f"No 'name' column found in {path.name}. Headers: {headers}")

    name_key    = headers[name_col]
    section_key = headers[section_col] if section_col is not None else None
    email_key   = headers[email_col]   if email_col   is not None else None

    records = []
    rows_iter = df.itertuples(index=False) if _PANDAS_AVAILABLE else df
    for row in rows_iter:
        if _PANDAS_AVAILABLE:
            raw_name = str(getattr(row, name_key.replace(" ", "_"), "") or "").strip()
        else:
            raw_name = row.get(name_key, "").strip()
        if not raw_name:
            continue
        rec = _parse_name_entry(raw_name)
        if section_key:
            rec["section"] = (str(getattr(row, section_key.replace(" ","_"), "") or "").strip()
                               if _PANDAS_AVAILABLE else row.get(section_key, "").strip())
        if email_key:
            rec["email"] = (str(getattr(row, email_key.replace(" ","_"), "") or "").strip()
                             if _PANDAS_AVAILABLE else row.get(email_key, "").strip())
        records.append(rec)

    return records


# ---------------------------------------------------------------------------
# Name extraction from document text
# ---------------------------------------------------------------------------

def _looks_like_last_name(s: str) -> bool:
    words = s.strip().split()
    if not words or len(words) > 3:
        return False
    return all(w[0].isupper() and "." not in w for w in words if w)


def _looks_like_first_mi(s: str) -> bool:
    words = s.strip().split()
    if not words or len(words) > 2:
        return False
    if len(words) == 2:
        return len(words[-1].rstrip(".")) == 1
    return True


def _try_pair_last_first(parts: list[str]) -> list[str]:
    """
    Given comma-split tokens, try to pair them as LAST, FIRST [MI.] sequences.
    Returns a list of reconstructed "FIRST LAST [MI.]" strings, or None if
    the tokens don't fit the pattern.
    """
    result = []
    i = 0
    while i < len(parts):
        part = parts[i].strip()
        if not part:
            i += 1
            continue
        if i + 1 < len(parts):
            nxt = parts[i + 1].strip()
            if _looks_like_last_name(part) and _looks_like_first_mi(nxt):
                first_tokens = nxt.split()
                fname = first_tokens[0]
                mi    = (" " + first_tokens[1]) if len(first_tokens) > 1 else ""
                result.append((fname + " " + part + mi).strip())
                i += 2
                continue
        result.append(part)
        i += 1
    return result if all(len(r) > 2 for r in result) else None


def extract_names_from_text(text: str) -> tuple[list[str], str]:
    """
    Scans the top of a document for a name/member header and extracts candidate
    name strings from it.

    Args:
        text: Full extracted submission text.

    Returns:
        (candidate_names, label_type) where label_type is one of:
            "group"      — a group-work label was found
            "individual" — an individual label was found
            "none"       — no label found (names may still be at top)
    """
    header = text[:HEADER_SCAN_CHARS]

    # Find the first matching header line
    match = _HEADER_VALUE_RE.search(header)
    if not match:
        return [], "none"

    raw_value = match.group(1).strip()

    # Determine label type from the matched label keyword
    label_text = header[match.start(): match.start() + 40]
    if _GROUP_LABEL_RE.search(label_text):
        label_type = "group"
    elif _INDIVIDUAL_LABEL_RE.search(label_text):
        label_type = "individual"
    else:
        label_type = "none"

    # Parse the raw value into name candidates
    candidates = _parse_raw_names(raw_value)
    return candidates, label_type


def _parse_raw_names(raw: str) -> list[str]:
    """Split a raw header value into individual name strings."""
    raw = raw.replace("\r", "\n").strip()
    names = []
    for line in raw.split("\n"):
        line = line.strip().rstrip(",;")
        if not line:
            continue
        chunks = re.split(r"\s+and\s+|;\s*", line, flags=re.IGNORECASE)
        for chunk in chunks:
            chunk = chunk.strip().rstrip(",;")
            if not chunk:
                continue
            comma_parts = [p.strip() for p in chunk.split(",") if p.strip()]
            paired = _try_pair_last_first(comma_parts)
            if paired is not None:
                names.extend(paired)
            else:
                for cp in comma_parts:
                    cp = cp.strip()
                    if len(cp) > 2:
                        names.append(cp)
    return [n for n in names if len(n) > 2]


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def _match_names_exact(
    candidate_names: list[str],
    roster: list[dict],
) -> list[dict]:
    """Exact match on normalized names (no fuzzy scoring)."""
    roster_map = {r.get("normalized", ""): r for r in roster if r.get("normalized")}
    matched: list[dict] = []
    seen_norms: set[str] = set()

    for name in candidate_names:
        norm = _normalize_name(name)
        if not norm:
            continue
        rec = roster_map.get(norm)
        if rec and rec["normalized"] not in seen_norms:
            seen_norms.add(rec["normalized"])
            matched.append(rec)
            logger.debug("  Name exact match: %r → %r", name, rec["roster_name"])

    return matched


def match_names_to_roster(
    candidate_names: list[str],
    roster: list[dict],
    threshold: int = FUZZY_THRESHOLD,
    fuzzy_enabled: bool = True,
) -> list[dict]:
    """
    Matches extracted name strings against the loaded roster.

    Args:
        candidate_names: Name strings extracted from the document.
        roster:          Loaded roster records from load_roster().
        threshold:       Minimum WRatio score to accept a match (0–100).
        fuzzy_enabled:   Whether to use fuzzy matching; if False, exact match.

    Returns:
        List of matched roster records (deduplicated). Unmatched names are
        logged as warnings but do not raise exceptions.
    """
    if not fuzzy_enabled:
        return _match_names_exact(candidate_names, roster)

    if not _RAPIDFUZZ_AVAILABLE:
        logger.warning("rapidfuzz unavailable — returning empty match list.")
        return []

    roster_norms = [r["normalized"] for r in roster]
    matched: list[dict] = []
    seen_norms: set[str] = set()

    for name in candidate_names:
        norm = _normalize_name(name)
        if not norm:
            continue
        result = fuzz_process.extractOne(
            norm, roster_norms, scorer=fuzz.WRatio
        )
        if result is None:
            continue
        best_match, score, idx = result
        if score >= threshold:
            rec = roster[idx]
            if rec["normalized"] not in seen_norms:
                seen_norms.add(rec["normalized"])
                matched.append(rec)
                logger.debug(
                    "  Name match: %r → %r (score=%d)",
                    name, rec["roster_name"], score,
                )
        else:
            logger.warning(
                "  Name %r could not be matched to any roster entry "
                "(best=%r, score=%d < threshold=%d).",
                name, best_match, score, threshold,
            )

    return matched


# ---------------------------------------------------------------------------
# Group resolution
# ---------------------------------------------------------------------------

def resolve_group_from_content(
    text: str,
    roster: list[dict],
    submission_owner_name: str = "",
    threshold: int = FUZZY_THRESHOLD,
    fuzzy_enabled: bool = True,
) -> dict:
    """
    Full pipeline: extract names from document, match to roster, determine
    whether the submission is group or individual work.

    Args:
        text:                  Extracted submission text.
        roster:                Loaded roster records.
        submission_owner_name: The name of the student who submitted (from Classroom API),
                               used as fallback if no names are found in the document.
        threshold:             Fuzzy matching threshold.

    Returns:
        {
            "is_group":      bool,
            "label_type":    str,          # "group" | "individual" | "none"
            "members":       list[dict],   # matched roster records
            "unresolved":    list[str],    # candidate names that couldn't be matched
            "detection":     str,          # human-readable summary
        }
    """
    candidates, label_type = extract_names_from_text(text)
    matched = match_names_to_roster(candidates, roster, threshold, fuzzy_enabled)

    # Unresolved = candidates that produced no match above the threshold.
    # Guard against rapidfuzz being unavailable: fuzz_process is None in that
    # case, and calling None.extractOne() raises AttributeError, crashing the
    # entire pre-screening phase for all students.
    matched_norms = {r["normalized"] for r in matched}
    if not fuzzy_enabled or not _RAPIDFUZZ_AVAILABLE or not roster:
        # Without fuzzy matching we cannot verify scores — treat every
        # unmatched candidate as unresolved.
        unresolved = [c for c in candidates if _normalize_name(c) not in matched_norms]
    else:
        roster_norms = [r["normalized"] for r in roster]
        unresolved = []
        for c in candidates:
            norm = _normalize_name(c)
            if norm in matched_norms:
                continue
            result = fuzz_process.extractOne(norm, roster_norms, scorer=fuzz.WRatio)
            if result is None or result[1] < threshold:
                unresolved.append(c)

    # Determine group vs individual
    if label_type == "group" and len(matched) >= 2:
        is_group   = True
        detection  = f"group label + {len(matched)} matched members"
    elif label_type == "group" and len(matched) == 1:
        is_group   = True
        detection  = "group label (only 1 member matched — others may be unresolved)"
    elif label_type == "individual" and len(matched) <= 1:
        is_group   = False
        detection  = "individual label"
    elif label_type == "none" and len(matched) >= 2:
        is_group   = True
        detection  = f"no label but {len(matched)} names matched at top of document"
    elif len(matched) == 1:
        is_group   = False
        detection  = "single name matched"
    else:
        is_group   = False
        detection  = "no names found — treated as individual (submission owner)"

    return {
        "is_group":   is_group,
        "label_type": label_type,
        "members":    matched,
        "unresolved": unresolved,
        "detection":  detection,
    }


def build_group_header_note(group_info: dict, submitter_name: str = "") -> str:
    """
    Builds the context note prepended to submission content before grading.
    Tells the grader this is group work, who the members are, and to apply
    the same grade to all.
    """
    if not group_info["is_group"]:
        return ""

    members = group_info["members"]
    if members:
        names_str = ", ".join(m["roster_name"] for m in members)
    else:
        names_str = "(members not resolved from roster)"

    note = (
        "[GROUP WORK DETECTED]\n"
        f"Members identified in submission: {names_str}\n"
        "Grade this as a GROUP output. Apply the same score to all listed members "
        "unless the rubric explicitly requires individual scoring.\n"
    )
    if group_info["unresolved"]:
        note += (
            f"Warning — the following names in the document could not be matched "
            f"to the roster: {', '.join(group_info['unresolved'])}. "
            "Instructor should verify manually.\n"
        )
    return note


# ---------------------------------------------------------------------------
# Duplicate submission detection
# ---------------------------------------------------------------------------

def detect_duplicate_submissions(
    ready_students: list[dict],
) -> dict[str, bool]:
    """
    Detects when multiple students in the same group each submitted their own
    copy of the submission (multiple TURNED_IN entries for the same group).

    Uses the set of matched roster member normalized names as the group key.
    If two or more students in ready_students share the same group_key, all are
    flagged for a 5-point deduction.

    Args:
        ready_students: The ready list produced by _prescreeen_submissions,
                        each entry having a "group_info" key.

    Returns:
        Dict mapping submission_id → True if a duplicate-submission penalty applies.
    """
    # Build group_key → [submission_id, ...]
    group_key_to_subs: dict[str, list[str]] = {}

    for student in ready_students:
        group_info = student.get("group_info", {})
        if not group_info.get("is_group"):
            continue
        members = group_info.get("members", [])
        if not members:
            continue
        # Sort member normalized names to make the key order-independent
        key = "|".join(sorted(m["normalized"] for m in members))
        group_key_to_subs.setdefault(key, []).append(student["submission_id"])

    penalty_map: dict[str, bool] = {}
    for key, sub_ids in group_key_to_subs.items():
        if len(sub_ids) > 1:
            logger.warning(
                "Duplicate group submission detected for group key %r: "
                "submission IDs %s — applying 5-point deduction to all.",
                key, sub_ids,
            )
            for sid in sub_ids:
                penalty_map[sid] = True

    return penalty_map
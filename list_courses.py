"""
scripts/list_courses.py
------------------------
Simple helper to list Google Classroom courses reachable with the
current OAuth token. Use this to confirm the `COURSE_ID` value in
your `.env` or environment.

Usage:
  python scripts/list_courses.py

"""
from dotenv import load_dotenv
import os
import sys

from classroom_handler import authenticate_classroom


def main() -> None:
    load_dotenv()

    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE")
    token_file = os.getenv("GOOGLE_TOKEN_FILE")

    if not credentials_file or not token_file:
        print("Set GOOGLE_CREDENTIALS_FILE and GOOGLE_TOKEN_FILE in your .env or environment.")
        sys.exit(1)

    service, _ = authenticate_classroom(credentials_file=credentials_file, token_file=token_file)

    resp = service.courses().list(pageSize=200).execute()
    courses = resp.get("courses", [])

    if not courses:
        print("No courses found for the authorized account.")
        return

    print("Found courses (id\tname\tsection\townerId):")
    for c in courses:
        print(f"{c.get('id')}\t{c.get('name')}\t{c.get('section')}\t{c.get('ownerId')}")


if __name__ == "__main__":
    main()

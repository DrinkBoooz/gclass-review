from dotenv import load_dotenv
import os
import sys
from classroom_handler import authenticate_classroom

def main():
    load_dotenv(override=True)
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE")
    token_file = os.getenv("GOOGLE_TOKEN_FILE")
    course_id = os.getenv("COURSE_ID")

    if not course_id:
        print("Please set COURSE_ID in your .env first!")
        return

    service, _ = authenticate_classroom(credentials_file, token_file)

    # Fetch assignments for the specific course
    results = service.courses().courseWork().list(courseId=course_id).execute()
    assignments = results.get("courseWork", [])

    if not assignments:
        print(f"No assignments found for course ID {course_id}")
        return

    print(f"\nAssignments for Course: {course_id}")
    print("ID\t\t\tTitle")
    print("-" * 50)
    for a in assignments:
        print(f"{a.get('id')}\t{a.get('title')}")

if __name__ == "__main__":
    main()
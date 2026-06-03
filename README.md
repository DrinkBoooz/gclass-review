# Google Classroom Utilities

A small set of scripts to interact with Google Classroom data in this workspace.

## Requirements

- Python 3.8+ (tested on Windows)
- A virtual environment is recommended
- Install dependencies:

```bash
python -m venv .venv
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# then
pip install -r requirements.txt
```

## Credentials

This project uses OAuth credentials stored in `credentials.json` and an OAuth token `token.json`.
Place your Google API `credentials.json` in the repository root before first run. The app will create/update `token.json` after authorizing.

## Run (Menu)

To launch the interactive menu:

```bash
python main.py --menu
```

Other useful commands / scripts:

- `list_courses.py` — list courses accessible by the credentials
- `list_coursework.py` — list coursework for a course
- `roster.py` — generate or inspect student rosters
- `local_pipeline.py` — local processing pipeline (see file docstring)
- `gemini_grader.py` — grading helper (requires configured APIs)
- `activity-checker-v4-fixed.md` — prompt/manual backup in `backupprompt/`

## Data / Results

CSV result files are included for sample outputs (e.g., `L9P9 Cryptography Case Study-results.csv`). These are example exports from past runs.

## Development notes

- Keep `credentials.json` private — do not commit it to public repos.
- If you run into permission/authorization errors, remove `token.json` and re-run the script to re-authorize.

## Troubleshooting

- If packages fail to install, ensure your virtual environment is active and `pip` is up to date:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

- For detailed usage of specific scripts, open the top of the script file where inline usage/help is provided.

## License & Contact

This repository does not include a license file. For questions, inspect the top-level `how-to-run.md` or contact the project owner.

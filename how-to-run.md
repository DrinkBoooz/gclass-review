# How to run

Quick instructions to set up the environment and run the interactive menu.

## 1) Create and activate a virtual environment

```bash
python -m venv .venv
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
```

## 2) Install dependencies

```bash
pip install -r requirements.txt
```

## 3) Prepare credentials

Place your Google API `credentials.json` in the repository root. The first run will create or update `token.json` after you authorize the app.

## 4) Run the interactive menu

```bash
python main.py --menu
```

## Notes & troubleshooting

- If you see authorization errors, remove `token.json` and re-run to re-authorize.
- Ensure the virtual environment is active before installing packages or running the script.
- If `pip` installation fails, upgrade pip first:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

For more details, see the project `README.md` in the repository root.

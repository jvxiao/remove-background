python -m venv .venv

./.venv/Scripts/Activate.ps1

pip install -r requirements.txt

uvicorn main:app --host --reload --host 0.0.0.0 --port 8000 
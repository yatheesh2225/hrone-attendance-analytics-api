# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Run locally

Requirements: Python 3.11+ and MongoDB 6.0+ (the grader uses MongoDB 7.0).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Set MONGO_URI and MONGO_DB in your shell, or use a local .env file.
uvicorn app.main:app --port 8000
```

The app reads `MONGO_URI` and `MONGO_DB` from environment variables (environment variables take priority over `.env`) and creates indexes idempotently at startup. `GET /health` checks MongoDB readiness.

## Implementation

- Application code is in `app/main.py`, as required.
- API instants are epoch milliseconds; MongoDB stores BSON UTC datetimes.
- List endpoints paginate in MongoDB; analytics use aggregation pipelines.
- `REVIEW.md` records starter-code defects and `DECISIONS.md` records the design choices.

## Verification status

Python syntax compilation and static comparison of routes against `openapi.yaml` pass. Runtime tests against MongoDB, concurrent-request tests, and explain-plan checks have not been run in this environment. Verify these before treating the assignment as grader-tested. Do not commit `.env`, credentials, virtual environments, data dumps, or a Dockerfile.

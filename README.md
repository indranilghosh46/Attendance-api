# Attendance-api
# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in .env
uvicorn app.main:app --port 8000
```
Needs MongoDB 6.0+ (uses `$setWindowFields`, `$lookup` with `localField` + `pipeline`). Indexes are created at startup.

## Notes
* Analytics are single aggregation pipelines; derived values are read from the stored fields, never recomputed.
  Half-up rounding is done with integer arithmetic (inside the pipeline for the trend, in Python for scalar results).
* Every list/analytics query is pinned to its index with `hint`, and `/admin/explain/{endpoint}` builds its query with the same functions as the endpoint.
* Choices where the contract is silent: trend range limit is 92 rows inclusive (`from`..`to`); an explicit `null` in a PATCH body is a 422;
  "request changes nothing" means status, `punch_in` and `punch_out` all equal the stored values; concurrent PATCH losers get 409.
* `REVIEW.md` and `DECISIONS.md` are the Part A and Part C write-ups.

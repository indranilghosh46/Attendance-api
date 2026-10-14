# REVIEW.md

Defects found in the starter `app/main.py`. "R" numbers refer to the business rules in `openapi.yaml`.
All are fixed in the submitted file. The starter helpers were **not** reusable as they were; I rewrote them.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds the shift start with `punch_in.replace(hour=h, minute=m)`, i.e. in the punch-in's own time zone (naive server time), not IST. | Punch in at 10:05 IST (04:35Z) for a 09:30 shift: the "start" becomes 09:30 *UTC* (15:00 IST), the result is negative, so `late_minutes` is 0 instead of 35. | Shift start is built as an IST datetime on the record's attendance date (`shift_bounds`). |
| 2 | `compute_late_minutes` | Grace test is on already-floored minutes (`minutes > 10`), but R2 is "strictly more than 10:00". | 09:40:01 on a 09:30 shift must give 10, the starter gives 0. | Compare whole seconds (`> 600`), then floor to minutes. |
| 3 | `compute_late_minutes`, `compute_overtime` | No overnight handling (R1): the shift always starts and ends on the calendar day of the punch. Seconds are never truncated. | Shift 22:00-06:00, punch-out 06:40 next day: overtime is computed against 06:00 of the *start* day, giving a huge number. | `shift_bounds` puts the end on the next day when `shift_end <= shift_start`; `attendance_date` applies the R1 rule; instants are truncated to seconds on input. |
| 4 | `compute_overtime` | `punch_out` read from PyMongo is naive, `end` is IST-aware: subtracting them raises `TypeError`. | Any punch-out (once written) returns 500. | The client is created with `tz_aware=True, tzinfo=UTC`, so every datetime is aware; all arithmetic is aware-minus-aware. |
| 5 | `compute_overtime` | No 30-minute minimum (R3). | Punch-out 18:35 on a 18:30 shift returns 5, must be 0. | Returns minutes only when `>= 30`. |
| 6 | `compute_work_hours` | `round(x, 2)` is banker's rounding on a binary float, not half-up (R4); no second truncation. | 54 s is 0.015 h: starter gives 0.01, R4 says 0.02. | Integer arithmetic (`round_half_up`): `floor((2*n*100 + d) / 2d)`. |
| 7 | `EmployeeIn` | No validation: any `emp_code` format, e-mail, shift string, date; `shift_start == shift_end` allowed. | POST `emp_code="x"`, `shift_start="25:99"`, or equal shifts returns 201 (must be 422). | Pattern, length and date checks and a model validator; digits written `[0-9]` so Unicode digits are rejected. |
| 8 | `create_employee` | Check-then-insert race and no unique index: parallel creates can both pass `find_one`. | 20 parallel POSTs with the same code produce duplicates. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 9 | `create_employee` | `created_at = datetime.now()` is naive local time, and it is returned as an ISO string. | Response `created_at` is a string; the stored value is off by the server's UTC offset. | UTC server clock, returned as epoch milliseconds. |
| 10 | `list_employees` | `skip = page * page_size` (page 1 skips the first page). | Page 1 never shows the first employees. | `(page - 1) * page_size`. |
| 11 | `list_employees` | `total` is `count_documents({})`, ignoring the department filter; no sort, so paging is unstable. | Filter by a department: `total` is the whole collection. | `count_documents(q)`; sorted by `emp_code`. |
| 12 | `list_employees`, `list_attendance` | `page` / `page_size` unchecked (R10): `page=0`, negative or huge values. | `GET /employees?page=-1` raises (negative skip) and returns 500; `/attendance?page=0` slices with a negative start and returns wrong rows. | `page >= 1`, `1 <= page_size <= 100`, enforced by FastAPI (422). |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises `TypeError`. | POST an unknown `emp_code`: 500 (must be 404). | 404 before any work. |
| 14 | `punch_in` | Date from `ts.date()` of a naive server-local datetime, not the IST date (R1); `if body.punched_at` treats `0` as "missing". | Punch at 19:00Z (00:30 IST next day) is filed under the wrong date. | `attendance_date()` converts to IST and applies the overnight rule; `is not None` checks. |
| 15 | `punch_in` | Check-then-insert race, no unique index. | Parallel identical punch-ins give several 201s and duplicate documents. | Unique `(emp_code, date)` index; `DuplicateKeyError` becomes 409. |
| 16 | `PunchInIn` / `punch_in` | `status` is a free string (ABSENT, "banana" accepted); `punched_at` accepts seconds, floats, negatives, strings; no truncation to whole seconds. | `status="LEAVE"`, `punched_at=1783312500` (seconds) or `"abc"`. | Enum of the three presence statuses; strict integer in `[1e11, 4102444800000]`; ms truncated to seconds. |
| 17 | `punch_in`, `list_attendance` | Response leaks `id` (the ObjectId); instants returned as datetimes, not epoch ms; legacy records without `half_day` / `history` are returned as-is. | Response has `id` and ISO strings; legacy documents lack fields. | `record_out()` shapes every record: no `_id`, epoch ms, defaults for missing fields. |
| 18 | `list_attendance` | Loads every matching document into memory and sorts/pages in Python; sorts by `date` only. | 100k records: slow and memory-hungry; ties in `date` come out in arbitrary order. | Server-side sort `(date desc, emp_code asc)`, `skip`/`limit`, count via `count_documents`, supporting indexes and `hint`. |
| 19 | `list_attendance` | `date_from`/`date_to`/`status` unvalidated; `date_from > date_to` not rejected. | `date_from=2026-08-01&date_to=2026-07-01` returns 200 with nothing. | Date format and ordering checked (422); `status` is an enum. |
| 20 | `/health` | Always returns ok, never pings MongoDB. | Stop MongoDB: still 200 (contract says 503). | `ping`; 503 on failure. |
| 21 | module setup | `MongoClient` returns naive datetimes; no indexes are created; 30 s default server-selection timeout can blow the 20 s readiness budget. | See #4, #8, #15, #18. | `tz_aware`, indexes created idempotently at startup (retried from `/health`), 5 s selection timeout. |

## Looked at and decided it is not a defect

* `load_dotenv()` is called with the default `override=False`, so real environment variables already win over `.env`.
* `IST = timezone(timedelta(hours=5, minutes=30))` is correct (no DST in IST).
* 409 for an already-existing record, and `status_code=201` on the two create endpoints, match the contract.
* `{"_id": 0}` projection in `list_employees` correctly keeps `_id` out of the response.
* Plain `def` endpoints with the synchronous PyMongo client are fine: FastAPI runs them in a thread pool, and `MongoClient` is thread-safe.
* The department filter being an exact, case-sensitive match is what the contract asks for.

"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env works too, real env vars win)

Rough order of the file: setup, small helper functions for the rules,
request models, indexes, the aggregation pipelines, then the endpoints.
"""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional, Tuple

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# ---- setup ----
load_dotenv()  # doesn't override real env vars, which is what we want

log = logging.getLogger("attendance")

# tz_aware so pymongo gives us UTC-aware datetimes. The starter got naive ones back and
# then crashed when it subtracted them from aware IST times.
client = MongoClient(
    os.getenv("MONGO_URI") or "mongodb://localhost:27017",
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB") or "attendance_db"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ABSENCE_STATUSES = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = 4.5
MAX_SHIFT_SECONDS = 24 * 3600
MAX_TREND_DAYS = 92

# fixed index names so I can hint() them later (see the indexes section)
IX_EMP_CODE = "emp_code_uq"
IX_EMP_DEPT_CODE = "department_emp_code"
IX_EMP_DEPT_JOINED = "department_joined_on"
IX_EMP_JOINED = "joined_on"
IX_LOG_EMP_DATE = "emp_code_date_uq"
IX_LOG_DATE_EMP = "date_desc_emp_code"
IX_LOG_STATUS = "status_date_desc_emp_code"
IX_LOG_EMP_PUNCH = "emp_code_punch_in_desc"


# ---- helpers for the business rules (no db calls in here) ----
def ms_to_dt(ms: int) -> datetime:
    # ms -> UTC datetime, dropping the sub-second part (R1 says truncate)
    ms -= ms % 1000
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    # datetime -> epoch ms. Using timedelta division so no float rounding sneaks in
    if dt.tzinfo is None:  # shouldn't happen, but treat naive as UTC just in case
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    # current time, whole seconds
    return datetime.now(UTC).replace(microsecond=0)


def now_utc_ms() -> datetime:
    # current time, keeping milliseconds (mongo dates only hold ms anyway)
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond - n.microsecond % 1000)


def api_value(v: Any) -> Any:
    # datetimes go out as epoch ms, anything else is returned as is
    return dt_to_ms(v) if isinstance(v, datetime) else v


def round_half_up(numer: int, denom: int, places: int) -> float:
    # numer/denom rounded half-up. Python's round() does banker's rounding on floats
    # (0.125 -> 0.12, 2.675 -> 2.67) which is not what R4/R8 want, so do it with ints.
    # Only works for numer >= 0 and denom > 0, which is all we ever pass in.
    scale = 10 ** places
    scaled = (2 * numer * scale + denom) // (2 * denom)
    return scaled / scale


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return to_minutes(shift_end) <= to_minutes(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    # R1: date is the IST date of the punch-in. For overnight shifts a punch-in before
    # shift_end (like 00:10 on a 22:00-06:00 shift) belongs to the previous day's shift.
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        seconds_into_day = ist.hour * 3600 + ist.minute * 60 + ist.second
        if seconds_into_day < to_minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> Tuple[datetime, datetime]:
    # start/end of the shift for a given attendance date. Overnight shifts end the next day.
    d = date.fromisoformat(date_str)
    sh, sm = divmod(to_minutes(shift_start), 60)
    eh, em = divmod(to_minutes(shift_end), 60)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    # R2: late only if MORE than 10 min after shift start, then count from shift start (not from the grace end)
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    seconds = int((punch_in.replace(microsecond=0) - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    # R3: minutes after shift end, but only if it's at least 30
    _, end = shift_bounds(date_str, shift_start, shift_end)
    seconds = int((punch_out.replace(microsecond=0) - end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    # R4: hours with 2 decimals, half-up
    seconds = int((punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds())
    return round_half_up(seconds, 3600, 2)


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  date_str: str, shift_start: str, shift_end: str) -> Dict[str, Any]:
    # work_hours, late, overtime and half_day in one go (same table as DATA_MODEL.md)
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, shift_start, shift_end)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, date_str, shift_start, shift_end),
        "half_day": hours < HALF_DAY_HOURS,
    }


def count_weekdays(start: date, end: date) -> int:
    # Mon-Fri days between start and end (R7). Max ~31 loops, it's just a calendar count,
    # not looping over documents.
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# ---- models and validation ----
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

# strict int so floats/strings/bools get a 422, and the range catches values that look like seconds
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
MonthParam = Annotated[str, Query(pattern=MONTH_PATTERN)]


def unprocessable(loc: Tuple[str, ...], msg: str) -> None:
    # raises a 422 in the same shape FastAPI uses for its own validation errors
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def parse_date(value: str) -> Optional[date]:
    if not DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:  # things like 2026-02-30 or year 0000
        return None


def require_date(value: str, loc: Tuple[str, ...]) -> date:
    d = parse_date(value)
    if d is None:
        unprocessable(loc, "must be a valid calendar date, YYYY-MM-DD")
    return d


def month_bounds(month: str) -> Tuple[str, str]:
    # '2026-07' -> ('2026-07-01', '2026-07-31'). FastAPI already checked the pattern, only year 0000 slips through
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        unprocessable(("query", "month"), "year out of range")
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, v: str) -> str:
        if parse_date(v) is None:
            raise ValueError("must be a valid calendar date, YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=100)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=100)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self) -> "RegularizeIn":
        # omitted means "leave it alone", but an explicit null is not allowed
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


# ---- turning db documents into API responses ----
def employee_out(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": api_value(doc["created_at"]) if doc.get("created_at") is not None else None,
    }


def history_out(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "at": api_value(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": api_value(ch.get("from")), "to": api_value(ch.get("to"))}
            for field, ch in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: Dict[str, Any]) -> Dict[str, Any]:
    # no _id in the output, and old records may be missing a few fields so default them
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": api_value(doc.get("punch_in")),
        "punch_out": api_value(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# ---- indexes ----
_index_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> None:
    # Safe to call many times (create_index does nothing if the index is already there).
    # If mongo is down at startup we just log it and /health tries again later.
    global _indexes_ready
    if _indexes_ready:
        return
    with _index_lock:
        if _indexes_ready:
            return
        try:
            db.employees.create_index([("emp_code", 1)], name=IX_EMP_CODE, unique=True)
            db.employees.create_index([("department", 1), ("emp_code", 1)], name=IX_EMP_DEPT_CODE)
            db.employees.create_index([("department", 1), ("joined_on", 1)], name=IX_EMP_DEPT_JOINED)
            db.employees.create_index([("joined_on", 1)], name=IX_EMP_JOINED)
            db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], name=IX_LOG_EMP_DATE, unique=True)
            db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name=IX_LOG_DATE_EMP)
            db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name=IX_LOG_STATUS)
            db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name=IX_LOG_EMP_PUNCH)
            _indexes_ready = True
        except PyMongoError as exc:
            log.warning("index creation failed (will retry): %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError) -> JSONResponse:
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# ---- aggregation pipelines (the endpoints and /admin/explain both use these) ----
# Small reusable pieces. They read the stored work_hours / late_minutes / half_day
# and never recompute anything from the punch times.
_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
_LATE = {"$ifNull": ["$late_minutes", 0]}
_IS_LATE = {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}
# count present days in halves (full day = 2, half day = 1) so the sums stay whole numbers
_PRESENT_HALVES = {"$cond": [_PRESENT, {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}, 0]}
# same but Mon-Fri only (R7), weekend records still count for late/overtime just not present_days
_PRESENT_HALVES_WEEKDAY = {"$cond": [{"$and": [_PRESENT, _WEEKDAY]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}, 0]}
_HAS_HOURS = {"$and": [_PRESENT, {"$isNumber": "$work_hours"}]}


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> List[Dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_halves": {"$sum": _PRESENT_HALVES_WEEKDAY},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _IS_LATE},
            "total_late_minutes": {"$sum": _LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> Tuple[List[Dict[str, Any]], str]:
    # Start from employees (not logs) so people with zero logs still get counted,
    # pull in each person's logs for the month, then group by department.
    # Returns the pipeline and the index to hint.
    match: Dict[str, Any] = {"joined_on": {"$lte": end}}  # R9
    if department is not None:
        match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {
                    "_id": 0,
                    "halves": _PRESENT_HALVES_WEEKDAY,
                    "late": _LATE,
                    "is_late": _IS_LATE,
                    "leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                    "on_duty": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
                    # work_hours as whole hundredths so the sum is exact, rounding happens once in Python
                    "hours_cents": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, None]},
                    "hours_n": {"$cond": [_HAS_HOURS, 1, 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_halves": {"$sum": {"$sum": "$logs.halves"}},
            "late_count": {"$sum": {"$sum": "$logs.is_late"}},
            "total_late_minutes": {"$sum": {"$sum": "$logs.late"}},
            "leave_count": {"$sum": {"$sum": "$logs.leave"}},
            "on_duty_count": {"$sum": {"$sum": "$logs.on_duty"}},
            "hours_cents": {"$sum": {"$sum": "$logs.hours_cents"}},
            "hours_n": {"$sum": {"$sum": "$logs.hours_n"}},
        }},
        {"$sort": {"_id": 1}},
    ]
    return pipeline, (IX_EMP_DEPT_JOINED if department is not None else IX_EMP_JOINED)


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs for employees that don't exist drop out here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # so ranking is inside that department
    pipeline += [
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        # $rank = 1,2,2,4 style ranking, which is what the spec asks for
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit goes after ranking, so ties can go past it
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, start: date, end: date) -> List[Dict[str, Any]]:
    # How this works: take the department's employees and squash them into one document
    # (codes + join dates), look up the per-day log totals, then use $range + $unwind to
    # get one doc per calendar day (that is what fills the gaps), then a moving average.
    # Rates are kept as integers in 1/10000ths so the half-up rounding is exact.
    n_days = (end - start).days + 1
    start_dt = datetime(start.year, start.month, start.day)  # midnight UTC, only used as a calendar date
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"codes": "$codes"},
            "pipeline": [
                {"$match": {
                    "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                    "$expr": {"$in": ["$emp_code", "$$codes"]},
                }},
                {"$group": {"_id": "$date", "halves": {"$sum": _PRESENT_HALVES}, "late": {"$sum": _IS_LATE}}},
            ],
            "as": "daily",
        }},
        {"$project": {"_id": 0, "joined": 1, "daily": 1, "offset": {"$range": [0, n_days]}}},
        {"$unwind": "$offset"},
        {"$addFields": {"day": {"$add": [start_dt, {"$multiply": ["$offset", 86400000]}]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "dow": {"$isoDayOfWeek": "$day"},
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {"input": "$joined", "cond": {"$lte": ["$$this", "$date"]}}}},  # R9
            "stats": {"$arrayElemAt": [{"$filter": {"input": "$daily", "cond": {"$eq": ["$$this._id", "$date"]}}}, 0]},
        }},
        {"$addFields": {
            "is_working_day": {"$lte": ["$dow", 5]},
            "halves": {"$ifNull": ["$stats.halves", 0]},
            "late_count": {"$ifNull": ["$stats.late", 0]},
        }},
        # rate in 1/10000ths, rounded half-up:
        #   floor(halves/2/headcount*10000 + 0.5) = floor((halves*10000 + headcount) / (2*headcount))
        {"$addFields": {"rate_i": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$halves", 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]},
            ]}},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"offset": 1},
            "output": {
                "w_sum": {"$sum": "$rate_i", "window": window},  # nulls are skipped by $sum
                "w_n": {"$sum": {"$cond": [{"$ne": ["$rate_i", None]}, 1, 0]}, "window": window},
            },
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$divide": ["$halves", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_i", None]}, None, {"$divide": ["$rate_i", 10000]}]},
            # average of the rounded rates in the window, half-up to 4 decimals
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$w_n", 0]},
                None,
                {"$divide": [
                    {"$floor": {"$divide": [
                        {"$add": [{"$multiply": ["$w_sum", 2]}, "$w_n"]},
                        {"$multiply": ["$w_n", 2]},
                    ]}},
                    10000,
                ]},
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


# ---- attendance list helpers (shared with /admin/explain) ----
ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


def validate_attendance_filters(date_from: Optional[str], date_to: Optional[str]) -> None:
    d_from = require_date(date_from, ("query", "date_from")) if date_from is not None else None
    d_to = require_date(date_to, ("query", "date_to")) if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        unprocessable(("query", "date_from"), "date_from must not be after date_to")


def build_attendance_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                           status: Optional[Status]) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        rng: Dict[str, str] = {}
        if date_from is not None:
            rng["$gte"] = date_from
        if date_to is not None:
            rng["$lte"] = date_to
        q["date"] = rng
    if status is not None:
        q["status"] = status.value
    return q


def attendance_hint(q: Dict[str, Any]) -> str:
    # pick the index that fits the filter, which also stops mongo from doing a collection scan
    if "emp_code" in q:
        return IX_LOG_EMP_DATE
    if "status" in q:
        return IX_LOG_STATUS
    return IX_LOG_DATE_EMP


# ---- endpoints ----
def get_employee_or_404(emp_code: str) -> Dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    ensure_indexes()  # does nothing once done, retries if mongo was down at startup
    return {"status": "ok"}


# -- employees --
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_ms()
    try:
        db.employees.insert_one(doc)  # the unique index does the duplicate check, no find_one first
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)  # uses the filter, not the whole collection
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# -- attendance --
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "history": [],
        **derive_fields(body.status.value, ts, None, day, emp["shift_start"], emp["shift_end"]),
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index, so only one parallel request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # latest record with punch_in <= punched_at. Can't look it up by today's date because
    # for an overnight shift the record's date is yesterday.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin:
        unprocessable(("body", "punched_at"), "punched_at must be after the punch-in")
    if (ts - pin).total_seconds() > MAX_SHIFT_SECONDS:
        unprocessable(("body", "punched_at"), "punched_at is more than 24 hours after the punch-in")
    derived = derive_fields(rec["status"], pin, ts, rec["date"], emp["shift_start"], emp["shift_end"])
    # only update if punch_out is still empty. If two punch-outs come in together one of
    # them matches and the other gets None back, which becomes the 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_attendance_filters(date_from, date_to)
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(list(ATTENDANCE_SORT.items()))  # sorting and paging happen in mongo, not in python
        .hint(attendance_hint(q))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(body: RegularizeIn, emp_code: str, date: str):
    # (the `date` param shadows the date class in here, but I don't need the class in this function)
    att_date = date
    require_date(att_date, ("path", "date"))
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
    shift_start, shift_end = emp["shift_start"], emp["shift_end"]

    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status.value if body.status is not None else old_status
    sup_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    sup_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    if new_status in ABSENCE_STATUSES:
        if sup_in is not None or sup_out is not None:
            unprocessable(("body", "status"), f"{new_status} clears the punch times; do not supply punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = sup_in if sup_in is not None else old_in
        if new_in is None:
            unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
        new_out = sup_out if sup_out is not None else old_out
        if attendance_date(new_in, shift_start, shift_end) != rec["date"]:
            unprocessable(("body", "punch_in"), "punch_in must fall on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                unprocessable(("body", "punch_out"), "punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > MAX_SHIFT_SECONDS:
                unprocessable(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")

    if new_status == old_status and new_in == old_in and new_out == old_out:
        unprocessable(("body",), "the request changes nothing")

    old_vals = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    new_vals = {
        "status": new_status, "punch_in": new_in, "punch_out": new_out,
        **derive_fields(new_status, new_in, new_out, rec["date"], shift_start, shift_end),
    }
    changes = {f: {"from": old_vals[f], "to": new_vals[f]} for f in old_vals if old_vals[f] != new_vals[f]}
    entry = {"at": now_utc_ms(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    old_history = rec.get("history")
    update: Dict[str, Any] = {"$set": dict(new_vals)}
    if old_history is None:  # old records may not have history at all, so start it
        update["$set"]["history"] = [entry]
    else:
        update["$push"] = {"history": entry}

    # Only write if the record is still how we read it (same status, same punches, same
    # history length). If someone else changed it in between nothing matches and we
    # return 409 instead of overwriting their edit.
    guard = {
        "_id": rec["_id"],
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(old_history or [])]},
    }
    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; reload it and retry")
    return record_out(updated)


# -- analytics --
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    start, end = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    row = next(iter(db.attendance_logs.aggregate(
        employee_monthly_pipeline(emp_code, start, end), hint=IX_LOG_EMP_DATE)), None) or {}

    # R7: count weekdays from the 1st, or from joined_on if they joined mid-month
    first = max(date.fromisoformat(start), date.fromisoformat(emp["joined_on"]))
    working_days = count_weekdays(first, date.fromisoformat(end))
    halves = int(row.get("present_halves", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        # present/working*100 is the same as halves*50/working_days, rounded half-up
        "attendance_pct": round_half_up(halves * 50, working_days, 2) if working_days > 0 else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = Query(None)):
    start, end = month_bounds(month)
    pipeline, hint = department_summary_pipeline(start, end, department)
    items = []
    for row in db.employees.aggregate(pipeline, hint=hint):
        n = int(row["hours_n"])
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": int(row["present_halves"]) / 2,
            # average over records (not per employee): total hundredths / record count
            "avg_work_hours": round_half_up(int(row["hours_cents"]), 100 * n, 2) if n > 0 else None,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthParam,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    start, end = month_bounds(month)
    pipeline = late_leaderboard_pipeline(start, end, limit, department)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in db.attendance_logs.aggregate(pipeline, hint=IX_LOG_DATE_EMP)
    ]
    return {"month": month, "items": items}


def validate_trend_range(from_: str, to: str) -> Tuple[date, date]:
    d_from = require_date(from_, ("query", "from"))
    d_to = require_date(to, ("query", "to"))
    if d_to < d_from:
        unprocessable(("query", "to"), "to must not be before from")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        unprocessable(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    d_from, d_to = validate_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = list(db.employees.aggregate(department_trend_pipeline(department, d_from, d_to), hint=IX_EMP_DEPT_CODE))
    return {"department": department, "items": items}


# -- admin --
def _explain_command(inner: Dict[str, Any]) -> Dict[str, Any]:
    result = db.command({"explain": inner, "verbosity": "executionStats"})
    # explain output has bson types (Timestamp, dates...), convert to plain json. NaN/inf become strings so FastAPI can encode it
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS),
                      parse_constant=lambda c: c)


def _missing(name: str) -> None:
    unprocessable(("query", name), "field required")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = Query(None),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # runs explain on the same query/pipeline the real endpoint builds
    if endpoint is ExplainTarget.attendance_list:
        validate_attendance_filters(date_from, date_to)
        q = build_attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        inner = {
            "find": collection, "filter": q, "projection": {"_id": 0}, "sort": ATTENDANCE_SORT,
            "hint": attendance_hint(q), "skip": (page - 1) * page_size, "limit": page_size,
        }
    elif endpoint is ExplainTarget.employee_monthly:
        if emp_code is None:
            _missing("emp_code")
        if month is None:
            _missing("month")
        start, end = month_bounds(month)
        collection = "attendance_logs"
        inner = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, start, end),
                 "cursor": {}, "hint": IX_LOG_EMP_DATE}
    elif endpoint is ExplainTarget.department_summary:
        if month is None:
            _missing("month")
        start, end = month_bounds(month)
        pipeline, hint = department_summary_pipeline(start, end, department)
        collection = "employees"
        inner = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}
    elif endpoint is ExplainTarget.late_leaderboard:
        if month is None:
            _missing("month")
        start, end = month_bounds(month)
        collection = "attendance_logs"
        inner = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(start, end, limit, department),
                 "cursor": {}, "hint": IX_LOG_DATE_EMP}
    else:  # department_trend
        if department is None:
            _missing("department")
        if from_ is None:
            _missing("from")
        if to is None:
            _missing("to")
        d_from, d_to = validate_trend_range(from_, to)
        collection = "employees"
        inner = {"aggregate": collection, "pipeline": department_trend_pipeline(department, d_from, d_to),
                 "cursor": {}, "hint": IX_EMP_DEPT_CODE}
    return {"endpoint": endpoint.value, "collection": collection, "explain": _explain_command(inner)}

"""Employee Attendance & Analytics API for the HROne engineering assignment.

Run from the repository root with: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables (optional local .env).
"""
from __future__ import annotations

import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# override=False is the default: real environment variables always take priority.
load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500, tz_aware=False)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    """Create required indexes idempotently before the application serves work."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_late_month")


# ---------------------------------------------------------------------------
# Validation and conversion helpers
# ---------------------------------------------------------------------------
def _valid_date(value: str, field_name: str = "date") -> date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{field_name} must be a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    return parsed


def _month_bounds(month: str) -> tuple[str, str, date, date]:
    if not MONTH_RE.fullmatch(month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    start = date(year, mon, 1)
    end = date(year, mon, monthrange(year, mon)[1])
    return start.isoformat(), end.isoformat(), start, end


def _working_days(start: date, end: date) -> int:
    """Count Monday-Friday inclusively, without walking every MongoDB document."""
    if end < start:
        return 0
    full_weeks, rem = divmod((end - start).days + 1, 7)
    count = full_weeks * 5
    for offset in range(rem):
        if (start + timedelta(days=full_weeks * 7 + offset)).weekday() < 5:
            count += 1
    return count


def _epoch_to_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in the supported range")
    # Truncate to whole seconds before calculations and persistence (R1).
    return datetime.fromtimestamp(value // 1000, tz=UTC).replace(tzinfo=None)


def _now_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def _to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    # Mongo stores naive datetimes as UTC; timestamps from the contract are UTC instants.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def _clean_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert BSON datetime fields to contract epoch milliseconds; never return _id."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["punch_in"] = _to_epoch_ms(out.get("punch_in"))
    out["punch_out"] = _to_epoch_ms(out.get("punch_out"))
    out["history"] = out.get("history") or []
    for entry in out["history"]:
        entry["at"] = _to_epoch_ms(entry.get("at"))
        for change in (entry.get("changes") or {}).values():
            for endpoint in ("from", "to"):
                if isinstance(change.get(endpoint), datetime) or change.get(endpoint) is None:
                    # Only punch timestamps are dates in the audit changes; non-timestamp nulls remain null.
                    pass
        for key, change in (entry.get("changes") or {}).items():
            if key in {"punch_in", "punch_out"}:
                change["from"] = _to_epoch_ms(change.get("from"))
                change["to"] = _to_epoch_ms(change.get("to"))
    return out


def _clean_employee(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = _to_epoch_ms(out.get("created_at"))
    return out


def _round_half_up(value: float | int | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _shift_start_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> tuple[datetime, date]:
    local = punch_in.replace(tzinfo=UTC).astimezone(IST)
    day = local.date()
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    overnight = (end_h, end_m) <= (start_h, start_m)
    if overnight and (local.hour, local.minute) < (end_h, end_m):
        day -= timedelta(days=1)
    start_local = datetime.combine(day, time(start_h, start_m), tzinfo=IST)
    return start_local.astimezone(UTC).replace(tzinfo=None), day


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str = "18:30") -> int:
    start, _ = _shift_start_for_punch(punch_in, shift_start, shift_end)
    elapsed = int((punch_in - start).total_seconds() // 60)
    seconds_late = (punch_in - start).total_seconds()
    return elapsed if seconds_late > 600 else 0


def _shift_end_for_record(record_date: str, shift_start: str, shift_end: str) -> datetime:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    day = _valid_date(record_date)
    end_day = day + timedelta(days=1) if (end_h, end_m) <= (start_h, start_m) else day
    return datetime.combine(end_day, time(end_h, end_m), tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return _round_half_up(Decimal(str(seconds)) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, record_date: str) -> int:
    end = _shift_end_for_record(record_date, shift_start, shift_end)
    seconds = (punch_out - end).total_seconds()
    minutes = int(seconds // 60)
    return minutes if minutes >= 30 else 0


def _presence_fraction(doc: dict[str, Any]) -> float:
    if doc.get("status") not in PRESENCE_STATUSES:
        return 0.0
    if _valid_date(doc["date"]).weekday() >= 5:
        return 0.0
    return 0.5 if doc.get("half_day", False) else 1.0


def _jsonify_explain(value: Any) -> Any:
    """Return raw explain output in JSON-compatible form, including ObjectIds if any."""
    if isinstance(value, datetime):
        return _to_epoch_ms(value)
    if isinstance(value, dict):
        return {str(k): _jsonify_explain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonify_explain(v) for v in value]
    if hasattr(value, "__str__") and value.__class__.__name__ == "ObjectId":
        return str(value)
    return value


def _aggregate_explain(collection_name: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    result = db.command({
        "explain": {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
        "verbosity": "executionStats",
    })
    return _jsonify_explain(result)


# ---------------------------------------------------------------------------
# Request models. Extra fields are ignored per the OpenAPI contract.
# ---------------------------------------------------------------------------
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        _valid_date(value, "joined_on")
        return value

    @field_validator("name", "department")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_timestamp(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not EPOCH_MIN <= value <= EPOCH_MAX:
            raise ValueError("punched_at must be an integer epoch-millisecond timestamp")
        return value

    @field_validator("status")
    @classmethod
    def valid_presence_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_timestamp(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not EPOCH_MIN <= value <= EPOCH_MAX:
            raise ValueError("punched_at must be an integer epoch-millisecond timestamp")
        return value


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def valid_timestamp(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not EPOCH_MIN <= value <= EPOCH_MAX:
            raise ValueError("timestamp must be an integer epoch-millisecond value")
        return value

    @field_validator("regularized_by")
    @classmethod
    def nonblank_by(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("regularized_by must not be blank")
        return value

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


# ---------------------------------------------------------------------------
# Shared MongoDB query / aggregation builders
# ---------------------------------------------------------------------------
def _attendance_match(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        bounds: dict[str, str] = {}
        if date_from:
            bounds["$gte"] = date_from
        if date_to:
            bounds["$lte"] = date_to
        query["date"] = bounds
    if status:
        query["status"] = status
    return query


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, end, _, _ = _month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$set": {"_parsed_date": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$lte": [{"$dayOfWeek": "$_parsed_date"}, 6]}, {"$gte": [{"$dayOfWeek": "$_parsed_date"}, 2]}]},
                {"$cond": ["$half_day", 0.5, 1]}, 0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
        {"$project": {"_id": 0, "present_days": 1, "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1}},
    ]


def _department_summary_pipeline(month: str, department: Optional[str]) -> list[dict[str, Any]]:
    start, end, _, _ = _month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]},
                ]}}},
                {"$set": {"_parsed_date": {"$dateFromString": {"dateString": "$date"}}}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$gte": [{"$dayOfWeek": "$_parsed_date"}, 2]}, {"$lte": [{"$dayOfWeek": "$_parsed_date"}, 6]}]},
                        {"$cond": ["$half_day", 0.5, 1]}, 0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    "hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                    "hours_n": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                }},
            ], "as": "_metrics",
        }},
        {"$set": {"_metrics": {"$ifNull": [{"$first": "$_metrics"}, {
            "present_days": 0, "late_count": 0, "total_late_minutes": 0, "leave_count": 0,
            "on_duty_count": 0, "hours_sum": 0, "hours_n": 0,
        }]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$_metrics.present_days"},
            "late_count": {"$sum": "$_metrics.late_count"},
            "total_late_minutes": {"$sum": "$_metrics.total_late_minutes"},
            "leave_count": {"$sum": "$_metrics.leave_count"},
            "on_duty_count": {"$sum": "$_metrics.on_duty_count"},
            "hours_sum": {"$sum": "$_metrics.hours_sum"}, "hours_n": {"$sum": "$_metrics.hours_n"},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_n", 0]}, {"$divide": ["$hours_sum", "$hours_n"]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def _leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict[str, Any]]:
    start, end, _, _ = _month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "_employee"}},
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend([
        {"$set": {"emp_code": "$_id", "name": "$_employee.name", "department": "$_employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
    ])
    return pipeline


def _trend_pipeline(department: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
    start_s, end_s = start_date.isoformat(), end_date.isoformat()
    return [
        # Keep the department group even when all employees join after the range;
        # headcount then remains zero for each requested day (R9).
        {"$match": {"department": department}},
        {"$group": {"_id": "$department", "_employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {"_days": {"$map": {"input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": {"$dateFromString": {"dateString": start_s}}, "endDate": {"$dateFromString": {"dateString": end_s}}, "unit": "day"}}, 1]}]}, "as": "offset", "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start_s}}, "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$_days"},
        {"$set": {"date": {"$dateToString": {"date": "$_days", "format": "%Y-%m-%d"}},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_days"}, 2]}, {"$lte": [{"$dayOfWeek": "$_days"}, 6]}]},
            "headcount": {"$size": {"$filter": {"input": "$_employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", {"$dateToString": {"date": "$_days", "format": "%Y-%m-%d"}}]}}}},
            "_emp_codes": {"$map": {"input": "$_employees", "as": "e", "in": "$$e.emp_code"}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date", "codes": "$_emp_codes"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }},
        ], "as": "_daily"}},
        {"$set": {"_daily": {"$ifNull": [{"$first": "$_daily"}, {"present_count": 0, "late_count": 0}]}}},
        {"$set": {"present_count": "$_daily.present_count", "late_count": "$_daily.late_count",
            "attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$_daily.present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB unavailable") from exc


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = _now_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return _clean_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = [_clean_employee(d) for d in db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched_at = _epoch_to_datetime(body.punched_at) if body.punched_at is not None else _now_truncated()
    shift_start, attendance_day = _shift_start_for_punch(punched_at, emp["shift_start"], emp["shift_end"])
    # Calculate late based on the actual shift that starts on the attendance date.
    shift_start_utc = datetime.combine(attendance_day, time(*map(int, emp["shift_start"].split(":"))), tzinfo=IST).astimezone(UTC).replace(tzinfo=None)
    elapsed_seconds = (punched_at - shift_start_utc).total_seconds()
    late_minutes = int(elapsed_seconds // 60) if elapsed_seconds > 600 else 0
    record = {
        "emp_code": body.emp_code, "date": attendance_day.isoformat(), "status": body.status,
        "punch_in": punched_at, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes, "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return _clean_attendance(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched_out = _epoch_to_datetime(body.punched_at) if body.punched_at is not None else _now_truncated()
    candidates = list(db.attendance_logs.find({"emp_code": body.emp_code, "punch_in": {"$lte": punched_out}}, {"_id": 1, "date": 1, "punch_in": 1, "punch_out": 1}).sort([("punch_in", DESCENDING), ("date", DESCENDING)]).limit(1))
    if not candidates:
        raise HTTPException(404, "no punch-in found")
    rec = candidates[0]
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    seconds = (punched_out - rec["punch_in"]).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch-in and within 24 hours")
    work_hours = compute_work_hours(rec["punch_in"], punched_out)
    overtime = compute_overtime(punched_out, emp["shift_start"], emp["shift_end"], rec["date"])
    half_day = work_hours < 4.50
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_out, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return _clean_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if date_from is not None:
        _valid_date(date_from, "date_from")
    if date_to is not None:
        _valid_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if status is not None and status not in STATUSES:
        raise HTTPException(422, "invalid status")
    query = _attendance_match(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    items = [_clean_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest) -> dict[str, Any]:
    target_date = _valid_date(date)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": target_date.isoformat()})
    if existing is None:
        raise HTTPException(404, "attendance record not found")

    desired = {k: existing.get(k) for k in ("status", "punch_in", "punch_out")}
    requested = body.model_dump(exclude={"reason", "regularized_by"}, exclude_unset=True)
    for field, value in requested.items():
        if field in {"punch_in", "punch_out"}:
            desired[field] = _epoch_to_datetime(value) if value is not None else None
        elif value is not None:
            desired[field] = value
    status_value = desired.get("status")
    if status_value in {"ABSENT", "LEAVE"}:
        if "punch_in" in requested and requested.get("punch_in") is not None or "punch_out" in requested and requested.get("punch_out") is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        desired["punch_in"] = None
        desired["punch_out"] = None
    else:
        if desired.get("punch_in") is None:
            raise HTTPException(422, "presence statuses require punch_in")
        local_punch = desired["punch_in"].replace(tzinfo=UTC).astimezone(IST)
        if local_punch.date().isoformat() != target_date.isoformat():
            # An overnight shift's attendance date is the shift start date, so the date still must align.
            if not ((emp["shift_end"] <= emp["shift_start"]) and local_punch.date() == target_date + timedelta(days=1) and local_punch.time().replace(tzinfo=None) < time(*map(int, emp["shift_end"].split(":")))):
                raise HTTPException(422, "punch_in must fall on the record attendance date")
        if desired.get("punch_out") is not None:
            seconds = (desired["punch_out"] - desired["punch_in"]).total_seconds()
            if seconds <= 0 or seconds > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    changed_fields = [k for k in ("status", "punch_in", "punch_out") if desired.get(k) != existing.get(k)]
    if not changed_fields:
        raise HTTPException(422, "request does not change the record")

    if desired.get("punch_in") is not None:
        punch_in_local = desired["punch_in"].replace(tzinfo=UTC).astimezone(IST)
        start_local = datetime.combine(target_date, time(*map(int, emp["shift_start"].split(":"))), tzinfo=IST)
        if emp["shift_end"] <= emp["shift_start"] and punch_in_local.date() == target_date + timedelta(days=1):
            start_local = datetime.combine(target_date, time(*map(int, emp["shift_start"].split(":"))), tzinfo=IST)
        elapsed = (desired["punch_in"] - start_local.astimezone(UTC).replace(tzinfo=None)).total_seconds()
        late = int(elapsed // 60) if elapsed > 600 else 0
    else:
        late = 0
    if desired.get("punch_in") is not None and desired.get("punch_out") is not None:
        hours = compute_work_hours(desired["punch_in"], desired["punch_out"])
        overtime = compute_overtime(desired["punch_out"], emp["shift_start"], emp["shift_end"], target_date.isoformat())
        half = hours < 4.50
    else:
        hours, overtime, half = None, 0, False

    final_values = {"status": desired.get("status", existing.get("status")), "punch_in": desired.get("punch_in"), "punch_out": desired.get("punch_out"), "late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": half}
    changes: dict[str, dict[str, Any]] = {}
    for field, after in final_values.items():
        before = existing.get(field, False if field == "half_day" else 0 if field in {"late_minutes", "overtime_minutes"} else None)
        if before != after:
            changes[field] = {"from": before, "to": after}
    history_entry = {"at": _now_truncated(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic version check ensures simultaneous corrections cannot overwrite each other's history.
    update_filter: dict[str, Any] = {"emp_code": emp_code, "date": target_date.isoformat()}
    # Missing history is equivalent to an empty history in the data model, but MongoDB
    # does not match a missing field when queried with an empty array. Preserve the
    # exact stored state so the optimistic concurrency check works for both shapes.
    update_filter["history"] = existing["history"] if "history" in existing else {"$exists": False}
    for key in ("status", "punch_in", "punch_out"):
        if key in existing:
            update_filter[key] = existing[key]
        else:
            update_filter[key] = {"$exists": False}
    update = {"$set": final_values, "$push": {"history": history_entry}}
    updated = db.attendance_logs.find_one_and_update(update_filter, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry regularization")
    return _clean_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    start, end, first, last = _month_bounds(month)
    working_start = max(first, _valid_date(emp["joined_on"], "joined_on"))
    working_days = _working_days(working_start, last)
    agg = list(db.attendance_logs.aggregate(_employee_monthly_pipeline(emp_code, month)))
    metrics = agg[0] if agg else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    present = _round_half_up(metrics.get("present_days", 0), 2)
    pct = None if working_days == 0 else _round_half_up(Decimal(str(present)) * Decimal(100) / Decimal(working_days), 2)
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": present,
            "leave_days": int(metrics.get("leave_days", 0)), "late_count": int(metrics.get("late_count", 0)),
            "total_late_minutes": int(metrics.get("total_late_minutes", 0)), "total_overtime_minutes": int(metrics.get("total_overtime_minutes", 0)), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict[str, Any]:
    _, end, _, _ = _month_bounds(month)
    items = list(db.employees.aggregate(_department_summary_pipeline(month, department)))
    items = [item for item in items if item["headcount"] > 0 and item["department"] is not None]
    for item in items:
        item["present_days"] = _round_half_up(item["present_days"], 2)
        item["avg_work_hours"] = None if item.get("avg_work_hours") is None else _round_half_up(item["avg_work_hours"], 2)
        for field in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            item[field] = int(item.get(field, 0))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    items = list(db.attendance_logs.aggregate(_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
) -> dict[str, Any]:
    if from_date is None or to_date is None:
        raise HTTPException(422, "from and to are required")
    start = _valid_date(from_date, "from")
    end = _valid_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(_trend_pipeline(department, start, end)))
    for item in items:
        for key in ("attendance_rate", "moving_avg_7d"):
            item[key] = None if item.get(key) is None else _round_half_up(item[key], 4)
        item["present_count"] = _round_half_up(item.get("present_count", 0), 2)
        item["headcount"] = int(item.get("headcount", 0))
        item["late_count"] = int(item.get("late_count", 0))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(422, "unknown explain endpoint")
    if endpoint == "attendance_list":
        if date_from:
            _valid_date(date_from, "date_from")
        if date_to:
            _valid_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        if status and status not in STATUSES:
            raise HTTPException(422, "invalid status")
        query = _attendance_match(emp_code, date_from, date_to, status)
        explain = db.command({"explain": {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _jsonify_explain(explain)}
    if endpoint != "department_trend":
        if month is None:
            raise HTTPException(422, "month is required")
        _month_bounds(month)
    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required")
        pipeline = _employee_monthly_pipeline(emp_code, month)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _aggregate_explain("attendance_logs", pipeline)}
    if endpoint == "department_summary":
        pipeline = _department_summary_pipeline(month, department)
        return {"endpoint": endpoint, "collection": "employees", "explain": _aggregate_explain("employees", pipeline)}
    if endpoint == "late_leaderboard":
        pipeline = _leaderboard_pipeline(month, limit, department)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _aggregate_explain("attendance_logs", pipeline)}
    if from_date is None or to_date is None or department is None:
        raise HTTPException(422, "department, from and to are required for department_trend")
    start, end = _valid_date(from_date, "from"), _valid_date(to_date, "to")
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid trend date range")
    pipeline = _trend_pipeline(department, start, end)
    return {"endpoint": endpoint, "collection": "employees", "explain": _aggregate_explain("employees", pipeline)}


"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (reads from environment variables, .env fallback)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from bson import SON, json_util
import json
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Ensure environment variables win over .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required database indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def dt_to_millis(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def millis_to_utc_dt(ms: int) -> datetime:
    # Truncate to whole seconds (R1)
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def now_utc_truncated() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def round_half_up(val: float, decimals: int = 2) -> float:
    d = Decimal(str(val))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def parse_time(t_str: str) -> time:
    h, m = map(int, t_str.split(":"))
    return time(hour=h, minute=m)


def determine_attendance_date(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """R1: Attendance date in IST. Overnight shifts punch-in before shift_end belongs to previous day."""
    dt = punch_in_utc.replace(microsecond=0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_ist = dt.astimezone(IST)

    start_t = parse_time(shift_start_str)
    end_t = parse_time(shift_end_str)

    if end_t <= start_t:
        # Overnight shift
        if dt_ist.time() < end_t:
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str, date_str: str) -> int:
    """R2: Grace period 10 minutes (600s). Late minutes = elapsed whole minutes since shift start."""
    dt = punch_in_utc.replace(microsecond=0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_ist = dt.astimezone(IST)

    d = date.fromisoformat(date_str)
    start_t = parse_time(shift_start_str)
    shift_start_dt = datetime.combine(d, start_t, tzinfo=IST)

    elapsed = int((dt_ist - shift_start_dt).total_seconds())
    if elapsed > 600:
        return elapsed // 60
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start_str: str, shift_end_str: str, date_str: str) -> int:
    """R3: Overtime in whole minutes from shift_end to punch_out, only if >= 30 min."""
    dt = punch_out_utc.replace(microsecond=0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_ist = dt.astimezone(IST)

    d = date.fromisoformat(date_str)
    start_t = parse_time(shift_start_str)
    end_t = parse_time(shift_end_str)

    if end_t <= start_t:
        # Overnight shift: ends on next calendar day
        shift_end_dt = datetime.combine(d + timedelta(days=1), end_t, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(d, end_t, tzinfo=IST)

    diff = int((dt_ist - shift_end_dt).total_seconds())
    if diff >= 1800:
        return diff // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    t_in = punch_in_utc.replace(microsecond=0)
    t_out = punch_out_utc.replace(microsecond=0)
    sec = int((t_out - t_in).total_seconds())
    hours = Decimal(sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes_api = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            from_val = dt_to_millis(from_val) if isinstance(from_val, datetime) else from_val
            to_val = dt_to_millis(to_val) if isinstance(to_val, datetime) else to_val
        changes_api[k] = {"from": from_val, "to": to_val}

    at_val = entry.get("at")
    at_ms = dt_to_millis(at_val) if isinstance(at_val, datetime) else at_val
    return {
        "at": at_ms,
        "by": entry.get("by", ""),
        "reason": entry.get("reason", ""),
        "changes": changes_api,
    }


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    p_in = doc.get("punch_in")
    p_out = doc.get("punch_out")

    p_in_ms = dt_to_millis(p_in) if isinstance(p_in, datetime) else p_in
    p_out_ms = dt_to_millis(p_out) if isinstance(p_out, datetime) else p_out

    history_api = [serialize_history_entry(e) for e in doc.get("history", [])]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": p_in_ms,
        "punch_out": p_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_api,
    }


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    created_ms = dt_to_millis(created_at) if isinstance(created_at, datetime) else created_at
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": created_ms,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
TIME_REGEX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    department: str = Field(..., min_length=1)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatusType = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_int(cls, v):
        if v is not None and not isinstance(v, int):
            raise ValueError("punched_at must be an integer epoch milliseconds")
        return v


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_int(cls, v):
        if v is not None and not isinstance(v, int):
            raise ValueError("punched_at must be an integer epoch milliseconds")
        return v


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[StatusType] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_int(cls, v):
        if v is not None and not isinstance(v, int):
            raise ValueError("Timestamp must be an integer epoch milliseconds")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created_at = now_utc_truncated()
    doc = body.model_dump()
    doc["created_at"] = created_at
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_in_utc = millis_to_utc_dt(body.punched_at)
    else:
        punch_in_utc = now_utc_truncated()

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    attendance_date = determine_attendance_date(punch_in_utc, shift_start, shift_end)
    late_min = compute_late_minutes(punch_in_utc, shift_start, shift_end, attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_out_utc = millis_to_utc_dt(body.punched_at)
    else:
        punch_out_utc = now_utc_truncated()

    # Find the most recent record whose punch_in is <= punch_out_utc
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        # Check if employee has any record with punch_in > punch_out_utc
        has_future = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": punch_out_utc}}
        )
        if has_future:
            raise HTTPException(422, "punched_at must be strictly after punch_in")
        raise HTTPException(404, "No punch-in found")

    punch_in_utc = rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_seconds <= 0:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if diff_seconds > 24 * 3600:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    attendance_date = rec["date"]

    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_min = compute_overtime(punch_out_utc, shift_start, shift_end, attendance_date)
    is_half_day = work_hrs < 4.50

    # Atomic update ensuring exactly one concurrent punch-out succeeds
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Record already punched out")

    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and not DATE_REGEX.match(date_from):
        raise HTTPException(422, "Invalid date_from format")
    if date_to and not DATE_REGEX.match(date_to):
        raise HTTPException(422, "Invalid date_to format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be greater than date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "Attendance record not found")

    # Status check
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")

    target_status = body.status if body.status is not None else rec.get("status")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = millis_to_utc_dt(body.punch_in)
        else:
            new_punch_in = rec.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(422, "Presence status requires punch_in")

        # Must stay on the record's attendance date (R1)
        calc_date = determine_attendance_date(new_punch_in, shift_start, shift_end)
        if calc_date != date:
            raise HTTPException(422, "punch_in attendance date does not match record date")

        if body.punch_out is not None:
            new_punch_out = millis_to_utc_dt(body.punch_out)
        else:
            new_punch_out = rec.get("punch_out")

        if new_punch_out is not None:
            if new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if diff_sec > 24 * 3600:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, shift_start, shift_end, date)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, shift_start, shift_end, date)

    # Normalize old record values for comparison
    old_status = rec.get("status")
    old_punch_in = rec.get("punch_in")
    old_punch_out = rec.get("punch_out")
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    changes_mongo = {}
    if target_status != old_status:
        changes_mongo["status"] = {"from": old_status, "to": target_status}
    if new_punch_in != old_punch_in:
        changes_mongo["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes_mongo["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes_mongo["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes_mongo["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes_mongo["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes_mongo["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_mongo:
        raise HTTPException(422, "No fields changed")

    now_utc = now_utc_truncated()
    new_entry_mongo = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    old_history = rec.get("history") or []

    # Optimistic concurrency check: match current history length / identity
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "history": old_history,
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": new_entry_mongo,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(409, "Concurrent modification conflict")

    return serialize_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    last_day_str = f"{month}-{num_days:02d}"

    # Calculate working days (Mon-Fri, from joined_on if mid-month)
    joined_on_str = emp["joined_on"]
    working_days = 0
    cur_date = date(year, m, 1)
    end_date = date(year, m, num_days)
    joined_date = date.fromisoformat(joined_on_str)

    while cur_date <= end_date:
        if cur_date >= joined_date and cur_date.weekday() < 5:
            working_days += 1
        cur_date += timedelta(days=1)

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": f"{month}-01", "$lte": last_day_str}
        }},
        {"$group": {
            "_id": None,
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "present_days": {"$sum": {
                "$cond": [
                    {
                        "$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                        ]
                    },
                    {"$cond": ["$half_day", 0.5, 1.0]},
                    0.0
                ]
            }}
        }}
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def get_departments_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    last_day_str = f"{month}-{num_days:02d}"

    match_stage = {"joined_on": {"$lte": last_day_str}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ecode": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$ecode"]},
                            {"$gte": ["$date", f"{month}-01"]},
                            {"$lte": ["$date", last_day_str]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True
        }},
        {"$group": {
            "_id": {
                "department": "$department",
                "emp_code": "$emp_code"
            },
            "emp_present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
                            ]
                        },
                        {"$cond": ["$logs.half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "emp_work_hours_list": {
                "$push": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$logs.work_hours", None]}
                            ]
                        },
                        "$logs.work_hours",
                        "$$REMOVE"
                    ]
                }
            },
            "emp_late_count": {
                "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
            },
            "emp_total_late_minutes": {
                "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
            },
            "emp_leave_count": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
            },
            "emp_on_duty_count": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
            }
        }},
        {"$group": {
            "_id": "$_id.department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$emp_present_days"},
            "all_work_hours": {"$push": "$emp_work_hours_list"},
            "late_count": {"$sum": "$emp_late_count"},
            "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
            "leave_count": {"$sum": "$emp_leave_count"},
            "on_duty_count": {"$sum": "$emp_on_duty_count"}
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "flat_work_hours": {
                "$reduce": {
                    "input": "$all_work_hours",
                    "initialValue": [],
                    "in": {"$concatArrays": ["$$value", "$$this"]}
                }
            }
        }},
        {"$project": {
            "department": 1,
            "headcount": 1,
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": [{"$size": "$flat_work_hours"}, 0]},
                    {"$round": [{"$avg": "$flat_work_hours"}, 2]},
                    None
                ]
            }
        }},
        {"$sort": {"department": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    last_day_str = f"{month}-{num_days:02d}"

    pipeline = [
        {"$match": {
            "date": {"$gte": f"{month}-01", "$lte": last_day_str},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"}
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(422, "'to' date must be on or after 'from' date")
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    # Verify department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Unknown department")

    from_dt = datetime.combine(d_from, time(0, 0), tzinfo=timezone.utc)
    to_dt_bounds = datetime.combine(d_to + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)

    pipeline = [
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            }
        }},
        {"$project": {
            "as_date": {"$dateFromString": {"dateString": "$_id"}},
            "present_count": 1,
            "late_count": 1
        }},
        {"$densify": {
            "field": "as_date",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt_bounds]
            }
        }},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$as_date"}},
            "as_date": 1,
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]}
        }},
        {"$sort": {"date": 1}},
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$d"]}
                        ]
                    }
                }},
                {"$count": "count"}
            ],
            "as": "hc"
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$as_date"}, [2, 3, 4, 5, 6]]}
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {
                        "$and": [
                            {"$eq": ["$is_working_day", True]},
                            {"$gt": ["$headcount", 0]}
                        ]
                    },
                    {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "raw_ma": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, "current"]
                    }
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$round": ["$raw_ma", 4]}
        }}
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be greater than date_to")
        q = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        sort_son = SON([("date", -1), ("emp_code", 1)])
        skip = (page - 1) * page_size
        find_cmd = SON([
            ("find", "attendance_logs"),
            ("filter", q),
            ("sort", sort_son),
            ("skip", skip),
            ("limit", page_size)
        ])
        res = db.command(SON([("explain", find_cmd), ("verbosity", "executionStats")]))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        year, m = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, m)
        last_day_str = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": last_day_str}
            }},
            {"$group": {
                "_id": None,
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "present_days": {"$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                }}
            }}
        ]
        agg_cmd = SON([
            ("aggregate", "attendance_logs"),
            ("pipeline", pipeline),
            ("cursor", {})
        ])
        res = db.command(SON([("explain", agg_cmd), ("verbosity", "executionStats")]))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res)),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        year, m = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, m)
        last_day_str = f"{month}-{num_days:02d}"

        match_stage = {"joined_on": {"$lte": last_day_str}}
        if department is not None:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$ecode"]},
                                {"$gte": ["$date", f"{month}-01"]},
                                {"$lte": ["$date", last_day_str]}
                            ]
                        }
                    }}
                ],
                "as": "logs"
            }},
            {"$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }},
            {"$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code"
                },
                "emp_present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "emp_work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "emp_late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "emp_total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "emp_leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "emp_on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                }
            }},
            {"$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "all_work_hours": {"$push": "$emp_work_hours_list"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"}
            }},
            {"$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "flat_work_hours": {
                    "$reduce": {
                        "input": "$all_work_hours",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }},
            {"$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$flat_work_hours"}, 0]},
                        {"$round": [{"$avg": "$flat_work_hours"}, 2]},
                        None
                    ]
                }
            }},
            {"$sort": {"department": 1}}
        ]
        agg_cmd = SON([
            ("aggregate", "employees"),
            ("pipeline", pipeline),
            ("cursor", {})
        ])
        res = db.command(SON([("explain", agg_cmd), ("verbosity", "executionStats")]))
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(res)),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        year, m = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, m)
        last_day_str = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {
                "date": {"$gte": f"{month}-01", "$lte": last_day_str},
                "late_minutes": {"$gt": 0}
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"}
        ]
        if department is not None:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }}
        ])
        agg_cmd = SON([
            ("aggregate", "attendance_logs"),
            ("pipeline", pipeline),
            ("cursor", {})
        ])
        res = db.command(SON([("explain", agg_cmd), ("verbosity", "executionStats")]))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res)),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(422, "'to' date must be on or after 'from' date")
        if (d_to - d_from).days > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        from_dt = datetime.combine(d_from, time(0, 0), tzinfo=timezone.utc)
        to_dt_bounds = datetime.combine(d_to + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)

        pipeline = [
            {"$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }},
            {"$project": {
                "as_date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }},
            {"$densify": {
                "field": "as_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_bounds]
                }
            }},
            {"$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$as_date"}},
                "as_date": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }},
            {"$sort": {"date": 1}},
            {"$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$department", department]},
                                {"$lte": ["$joined_on", "$$d"]}
                            ]
                        }
                    }},
                    {"$count": "count"}
                ],
                "as": "hc"
            }},
            {"$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "is_working_day": {"$in": [{"$dayOfWeek": "$as_date"}, [2, 3, 4, 5, 6]]}
            }},
            {"$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None
                    ]
                }
            }},
            {"$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "raw_ma": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }},
            {"$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$raw_ma", 4]}
            }}
        ]
        agg_cmd = SON([
            ("aggregate", "attendance_logs"),
            ("pipeline", pipeline),
            ("cursor", {})
        ])
        res = db.command(SON([("explain", agg_cmd), ("verbosity", "executionStats")]))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res)),
        }

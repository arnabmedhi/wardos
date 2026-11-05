"""The record's words: fact types, statuses and the small helpers that build documents."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import uuid

PATIENTS = "patients"
ADMISSIONS = "admissions"
SOURCES = "sources"
PAGES = "pages"
FACTS = "facts"
QUESTIONS = "questions"
EVENTS = "events"
REVIEWS = "reviews"
DOCUMENTS = "documents"
FINALS = "finals"
HELD = "held_facts"
ALL_COLLECTIONS = (PATIENTS, ADMISSIONS, SOURCES, PAGES, FACTS, QUESTIONS, EVENTS, REVIEWS, DOCUMENTS, FINALS, HELD)

FACT_TYPES = (
    "identity", "admin", "complaint", "history", "observation", "vital", "lab_result", "investigation_request",
    "investigation_result", "med_event", "procedure", "diagnosis", "plan", "decision", "allergy",
)
SINGLE_VALUED = {"vital", "lab_result", "identity", "admin"}

ASSERTIONS = ("present", "absent", "uncertain", "planned", "history", "family")
PRECISIONS = ("exact", "day", "unknown")

ACTIVE = "active"
SUPERSEDED = "superseded"
DISPUTED = "disputed"
DOCTOR_LOCKED = "doctor_locked"
RETRACTED = "retracted"
LIVE_STATUSES = {ACTIVE, DISPUTED, DOCTOR_LOCKED}

MED_EVENTS = ("started", "continued", "given", "dose_changed", "held", "restarted", "stopped", "discharge_med",
              "advised")
MED_ON = {"started", "continued", "given", "dose_changed", "restarted"}
MED_OFF = {"held", "stopped"}

SOURCE_KINDS = ("photo", "pdf", "voice", "text", "portal_report", "doctor_edit")
SRC_RECEIVED, SRC_READ, SRC_MERGED, SRC_DUPLICATE, SRC_HELD, SRC_FAILED = (
    "received", "read", "merged", "duplicate", "held", "failed")

Q_CONFLICT = "conflict"
Q_DOCTOR_CONFLICT = "conflict_with_doctor"
Q_UNCLEAR_DATE = "unclear_date"
Q_WRONG_PATIENT = "wrong_patient"
HOLD_IT, THIS_PATIENT = "Hold it", "It is this patient"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def stable_id(prefix: str, *parts) -> str:
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def fact_doc(*, admission_id: str, source_id: str | None, page_id: str | None, index: int, type: str, key: str,
             label: str, value: dict, clinical_time: str | None, time_precision: str, assertion: str, quote: str,
             quote_found: bool, created_by: str = "agent", status: str = ACTIVE) -> dict:
    return {
        "id": stable_id("fact", admission_id, source_id, page_id, index, type, key, value.get("text")),
        "admission_id": admission_id,
        "type": type, "key": key, "label": label, "value": value,
        "clinical_time": clinical_time, "time_precision": time_precision, "assertion": assertion,
        "status": status, "source_id": source_id, "page_id": page_id,
        "quote": quote, "quote_found": quote_found,
        "support": [source_id] if source_id else [],
        "supersedes": None, "superseded_by": None, "dispute_group": None,
        "created_by": created_by, "created_at": now_iso(),
        "added_in": None, "changed_in": None,
    }

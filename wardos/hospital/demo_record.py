"""Guest-only record adapter for the demo robot, using the ordinary record write path."""
from __future__ import annotations

import json

from .demo_tasks import checked_id
from ..chain import model as m
from ..chain.normalize import fact_key
from ..chain.record import Record
from ..chain.today import build_today


class GuestStore:
    def __init__(self, inner, guest):
        self.inner, self.guest = inner, checked_id(guest)
        if hasattr(inner, "prefix") and inner.prefix != "wardos_demo_":
            raise ValueError("A demo store is required")

    def get(self, collection, doc_id):
        row = self.inner.get(collection, doc_id)
        return row if row and row.get("guest") == self.guest else None

    def query(self, collection, **equals):
        return self.inner.query(collection, **{**equals, "guest": self.guest})

    def put(self, collection, doc):
        existing = self.inner.get(collection, doc["id"])
        if existing and existing.get("guest") != self.guest:
            raise ValueError("Demo document belongs to another guest")
        self.inner.put(collection, {**doc, "guest": self.guest})

    def transact(self, fn):
        return self.inner.transact(lambda tx: fn(GuestStore(tx, self.guest)))


class DemoTextFiles:
    def __init__(self, store):
        self.store = store

    def save(self, parts, name, data):
        ident = m.stable_id("file", *parts, name)
        self.store.put("report_files", {"id": ident, "text": data.decode("utf-8")})
        return "demo-text:" + ident

    def read(self, uri):
        if not uri.startswith("demo-text:"):
            raise ValueError("Only demo text files can be opened here")
        row = self.store.get("report_files", uri[10:])
        if not row:
            raise ValueError("Demo source not found")
        return row["text"].encode("utf-8")


def guest_record(store, guest):
    scoped = GuestStore(store, guest)
    return Record(scoped, DemoTextFiles(scoped))


def snapshot(record, admission_id):
    admission = record.admission(admission_id)
    return build_today(admission, record.patient_of(admission_id), record.facts(admission_id),
                       record.questions(admission_id), sources=record.store.query(m.SOURCES, admission_id=admission_id))


def value_key(key, when, number, text):
    return fact_key("lab_result", key), (when or "")[:16], number if number is not None else text


def ingest(record, task, values):
    from ..chain.alerts import new_notifications
    admission = record.admission(task["admission_id"])
    if (record.store.guest != task["guest"] or str(admission.get("demo_bed")) != str(task["bed"])
            or admission.get("status") != "active"):
        raise ValueError("Demo task does not match this guest admission")
    before = snapshot(record, admission["id"])
    asked = str(task.get("test") or "").strip().lower() if task.get("kind") == "get_reports" else ""
    if asked:                                           # "fetch potassium": only that test is read into the record
        values = [v for v in values if asked.split()[0] in str(v.analyte).lower()]
    known = {value_key(f["key"], f.get("clinical_time"), f["value"].get("number"), f["value"].get("text"))
             for f in record.facts(admission["id"], live_only=False) if f["type"] == "lab_result"}
    fresh = []
    for value in values:
        key = value_key(value.analyte, value.collected_at, value.number, value.value)
        if key not in known:
            fresh.append(value)
            known.add(key)
    report_ids = []
    for value in fresh:
        payload = json.dumps({"test": value.analyte, "value": value.value, "unit": value.unit,
                              "time": value.collected_at, "reference": value.reference}, sort_keys=True)
        source, _ = record.add_source(admission["id"], "portal_report", text=payload,
                                     mime="text/plain", filename="lab-report.txt", author="Lab report")
        record.update_source(source["id"], label="Lab report", portal="openmrs")
        page = record.save_pages(source["id"], [{"text": payload, "page_type": "lab_report"}])[0]
        fact = m.fact_doc(admission_id=admission["id"], source_id=source["id"], page_id=page["id"], index=0,
                          type="lab_result", key=fact_key("lab_result", value.analyte), label=value.analyte,
                          value={"text": value.value, "number": value.number, "unit": value.unit,
                                 "reference": value.reference, "lab": "OpenMRS", "sample_no": value.sample_no},
                          clinical_time=value.collected_at, time_precision="exact", assertion="present",
                          quote=value.value, quote_found=True)
        record.apply(source["id"], [fact])
        report_ids.append(source["id"])
    after = snapshot(record, admission["id"])
    for event in new_notifications(before, after, report_ids=report_ids):
        ident = m.stable_id("notification", admission["id"], event["key"])
        def enqueue(tx):
            if not tx.get("notification_outbox", ident):
                tx.put("notification_outbox", {"id": ident, "admission_id": admission["id"],
                                               "event": event, "status": "waiting", "created_at": m.now_iso()})
        record.store.transact(enqueue)
    return fresh

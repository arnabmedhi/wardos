"""Read-only index of the summary service's past patients; import saved readings only when a doctor asks."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
import re
import threading
import time
import uuid
import weakref

from . import model as m
from .normalize import group_names, name_words, parse_clinical_time, words_match
from .record import Record, RecordError
from .settings import load_chain_settings, open_record
from .testsets import PAGE_NAME, _download, _runs, _services, _walk, image_blocks

COLLECTION = "register"
_sync_lock = threading.Lock()
_local = threading.local()
_snapshots = weakref.WeakKeyDictionary()
_snapshot_lock = threading.RLock()
SNAPSHOT_SECONDS = 300
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20
OLD_WORDS = re.compile(r"\b(?:old|past|previous|purana|purani|pichla|pichli|discharged)\b", re.I)
_FILLER = set("old past previous purana purani pichla pichli discharged patient case show find open bring in me the a an of for please uhid name unit department dept from register".split())


def _clients(token_path):
    key = (str(token_path), _services)
    if getattr(_local, "token", None) != key:
        _local.sheets, _local.drive = _services(Path(token_path))
        _local.token = key
    return _local.sheets, _local.drive


def _drive(token_path):
    return _clients(token_path)[1]


def _snapshot(record):
    with _snapshot_lock:
        hit = _snapshots.get(record)
        if hit is None or time.monotonic() - hit[0] > SNAPSHOT_SECONDS:
            hit = (time.monotonic(), {d["run_id"]: d for d in record.store.query(COLLECTION)})
            _snapshots[record] = hit
        return dict(hit[1])


def _remember(record, doc):
    with _snapshot_lock:
        hit = _snapshots.get(record)
        if hit:
            hit[1][doc["run_id"]] = dict(doc)


def identity(reading: str, run_date: str) -> dict:
    votes = {k: Counter() for k in ("name", "uhid", "age", "sex", "admitted_at", "discharged_at")}
    around = date.fromisoformat(run_date[:10])
    for text in (image_blocks(reading) or {1: reading}).values():
        seen = {k: set() for k in votes}
        plain = re.sub(r"[*_`]+", "", text).replace("|", " : ")
        for line in plain.splitlines():
            for match in re.finditer(r"\b(?:patient(?:'s)?\s+name|name\s+of\s+patient|name)\s*[:\-]+\s*(.+)", line, re.I):
                value = re.split(r"\s{2,}|\s*:\s*|\b(?:age|sex|gender|uhid|s/[od]|w/o)\b", match[1], flags=re.I)[0].strip(" .,-")
                if name_words(value):
                    seen["name"].add(value)
            for match in re.finditer(r"\b\d{11}\b", line):
                if re.search(r"\b(?:uhid|unique\s*(?:health|hospital)|registration|cr\s*(?:no|number))\b", line, re.I):
                    seen["uhid"].add(match[0])
            age = re.search(r"\bage\s*[:\-]+\s*(\d{1,3})(?:\s*(?:years?|yrs?|y))?\b", line, re.I)
            pair = re.search(r"\b(\d{1,3})\s*(?:years?|yrs?|y)?\s*/\s*(male|female|m|f)\b", line, re.I)
            sex = re.search(r"\b(?:sex|gender)\s*[:\-]+\s*(male|female|m|f)\b", line, re.I)
            if age or pair:
                value = (age or pair)[1]
                if 0 < int(value) < 120:
                    seen["age"].add(value)
            if sex or pair:
                seen["sex"].add((sex[1] if sex else pair[2])[0].upper())
            for key, label in (("admitted_at", r"date\s+of\s+admission|admission\s+date|doa|admitted(?:\s+on)?"),
                               ("discharged_at", r"date\s+of\s+discharge|discharge\s+date|dod|discharged(?:\s+on)?")):
                hit = re.search(rf"\b(?:{label})\s*[:\-]+\s*(.+)", line, re.I)
                if hit:
                    when, _ = parse_clinical_time(hit[1], around=around)
                    if when:
                        seen[key].add(when[:10])
        for key, values in seen.items():
            votes[key].update(values)
    groups = group_names(dict(votes["name"]))
    return {"name": groups[0]["canonical"] if groups else None,
            **{k: v.most_common(1)[0][0] if v else None for k, v in votes.items() if k != "name"}}


def _index(row: dict, token_path) -> tuple[dict | None, str | None]:
    folder = re.search(r"folders/([A-Za-z0-9_-]+)", row.get("Case folder", ""))
    if not folder:
        return None, "bad_folder"
    try:
        drive = _drive(token_path)
        files = _walk(drive, folder[1])
        readings = sorted((f for f in files if "stage1_ocr" in f["name"].lower()), key=lambda f: f["name"], reverse=True)
        photos = sorted((dict(id=f["id"], number=int(PAGE_NAME.match(f["name"])[1]),
                              mime=f.get("mimeType") or "image/jpeg") for f in files
                         if PAGE_NAME.match(f["name"]) and f.get("mimeType", "").startswith("image/")),
                        key=lambda f: f["number"])
        if not readings or not photos:
            return None, "no_reading_or_photos"
        reading = _download(drive, readings[0]["id"]).decode("utf-8", "replace")
        blocks = image_blocks(reading)
        if len({p["number"] for p in photos}) != len(photos) or set(blocks) != {p["number"] for p in photos}:
            return None, "page_mismatch"
        run_date = row.get("Run date", "")[:10]
        doc = {"id": row["Run ID"], "run_id": row["Run ID"], **identity(reading, run_date),
               "department": row.get("Department"), "unit": row.get("Unit"), "run_date": run_date,
               "doctor_name": row.get("Doctor name"), "folder_id": folder[1], "reading_file_id": readings[0]["id"],
               "photos": photos, "frozen_link": row.get("AI draft (frozen) link"),
               "document_link": row.get("Doctor's document link"), "indexed_at": m.now_iso()}
        return doc, None
    except Exception:
        return None, "read_failed"


def sync(record, token_path=None, *, limit: int | None = None) -> dict:
    started = time.monotonic()
    token_path = token_path or load_chain_settings().drive_token
    with _sync_lock:
        sheets, _ = _clients(token_path)
        rows = _runs(sheets)
        known = set(_snapshot(record))
        counts = Counter(rows=len(rows), indexed=0, with_name=0, with_uhid=0)
        pending, seen = [], set(known)
        for row in sorted(rows, key=lambda r: (r.get("Run date", ""), r.get("Run time", "")), reverse=True):
            rid = row.get("Run ID")
            if row.get("Status") != "Success" or not row.get("Case folder") or not rid:
                counts["ineligible"] += 1
            elif rid in seen:
                counts["known"] += 1
            elif limit is not None and len(pending) >= limit:
                counts["limited"] += 1
            else:
                seen.add(rid)
                existing = record.store.get(COLLECTION, rid)
                if existing:
                    _remember(record, existing)
                    counts["known"] += 1
                else:
                    pending.append(row)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for row, (doc, reason) in zip(pending, pool.map(lambda row: _index(row, token_path), pending)):
                if reason:
                    counts[reason] += 1
                    if reason in ("bad_folder", "no_reading_or_photos", "page_mismatch"):
                        unavailable = {"id": row["Run ID"], "run_id": row["Run ID"], "unavailable": reason,
                                       "run_date": row.get("Run date", "")[:10], "indexed_at": m.now_iso()}
                        def keep_unavailable(tx):
                            existing = tx.get(COLLECTION, unavailable["id"])
                            if existing:
                                return existing
                            tx.put(COLLECTION, unavailable)
                            return unavailable
                        _remember(record, record.store.transact(keep_unavailable))
                    continue
                def write(tx):
                    if tx.get(COLLECTION, doc["id"]):
                        return False
                    tx.put(COLLECTION, doc)
                    return True
                if record.store.transact(write):
                    _remember(record, doc)
                    counts["indexed"] += 1
                    counts["with_name"] += bool(doc["name"])
                    counts["with_uhid"] += bool(doc["uhid"])
                else:
                    counts["known"] += 1
                    _remember(record, record.store.get(COLLECTION, doc["id"]))
        return dict(counts, seconds=round(time.monotonic() - started, 2))


def query_parts(text):
    words = re.findall(r"[a-z]+|\d+", text.lower())
    words = [{"one": "1", "two": "2", "three": "3"}.get(w, w) for w in words]
    units = {w for w in words if w in {"surgery", "medicine", "ent", "pediatrics", "obgyn"} or w.isdigit()}
    return [w for w in words if w not in _FILLER and w not in units], units


def lookup_requested(text):
    if re.search(r"\b\d{11}\b", text):
        return True
    if not OLD_WORDS.search(text) or re.search(r"\bhow\s+old\b|\byears?\s+old\b", text, re.I):
        return False
    if re.search(r"\b(?:patient|case|stay)\b", text, re.I):
        return True
    wanted, _ = query_parts(text)
    return bool(set(wanted) - {"reports", "report", "tests", "test", "labs", "vitals", "medicines", "photo", "photos", "plan"})


def lookup_intent(record, text):
    if not lookup_requested(text):
        return None
    from .ask import interpret, ward_entry
    patients = {p["id"]: p for p in record.store.query(m.PATIENTS)}
    ward = []
    for admission in record.store.query(m.ADMISSIONS, status="active"):
        if not admission.get("archived"):
            patient = patients.get(admission["patient_id"], {})
            ward.append(ward_entry(admission, patient, {"patient": patient, "admission": admission}))
    intent = interpret(text, ward)
    return intent if intent["action"] == "old_patient" else None


_ASK_WORDS = set("what whats is are was the sodium tests test reports report labs lab vitals medicines medicine photo photos "
                 "plan brief summary about tell give show how why when who his her their".split())


def _brought_in_names(record) -> dict[str, list[str]]:
    stays = record.store.query(m.ADMISSIONS, old_case=True)
    if not stays:
        return {}
    patients = {p["id"]: p for p in record.store.query(m.PATIENTS)}
    return {a["old_run_id"]: name_words((patients.get(a["patient_id"]) or {}).get("name")) for a in stays if a.get("old_run_id")}


def name_only_lookup(record, text: str) -> list[dict]:
    wanted, _ = query_parts(text)
    wanted = [w for w in wanted if w not in _ASK_WORDS and len(w) > 2 and not w.isdigit()]
    if not wanted or len(wanted) > 3:
        return []
    return find(record, " ".join(wanted))


def find(record, text: str) -> list[dict]:
    docs = [d for d in _snapshot(record).values() if not d.get("unavailable")]
    uhid = re.search(r"\b\d{11}\b", text)
    if uhid:
        matches = [d for d in docs if d.get("uhid") == uhid[0]]
    else:
        wanted, units = query_parts(text)
        later = _brought_in_names(record) if wanted else {}
        matches = [d for d in docs if wanted
                   and all(any(words_match(w, n) for n in name_words(d.get("name")) + later.get(d["run_id"], [])) for w in wanted)
                   and units <= set(re.findall(r"[a-z]+|\d+", unit_name(d).lower()))]
    matches = sorted(matches, key=lambda d: (d.get("run_date") or "", d["run_id"]), reverse=True)[:5]
    fresh = record.run_parallel(lambda d: record.store.get(COLLECTION, d["run_id"]), matches)
    return [d for d in fresh if d and not d.get("unavailable")]


def unit_name(doc: dict) -> str:
    department, unit = doc.get("department") or "", doc.get("unit") or ""
    return unit if unit.lower().startswith(department.lower()) else f"{department} {unit}".strip()


def card(matches: list[dict], not_on_ward: bool = False) -> dict:
    return {"kind": "old_case", "title": "Old stays", "patient": None,
            "text": ("Not on your ward. Found in old patients:" if not_on_ward else "Choose an old stay.") if matches
                    else "No old case matches that name or UHID.",
            "matches": [{k: d.get(k) for k in ("run_id", "name", "age", "sex", "run_date", "admission_id")}
                        | {"unit": unit_name(d), "ready": d.get("import_status") == "ready"} for d in matches]}


def _lease_until(doc):
    if doc.get("import_lease_until") is not None:
        return float(doc["import_lease_until"])
    try:
        return datetime.fromisoformat(doc["import_started_at"]).timestamp() + LEASE_SECONDS
    except (KeyError, TypeError, ValueError):
        return float("inf")


def _save_import(record, run_id, claim_id, **fields):
    def write(tx):
        doc = tx.get(COLLECTION, run_id)
        if not doc or doc.get("import_claim") != claim_id:
            raise RecordError("this import has been resumed elsewhere")
        doc.update(fields)
        tx.put(COLLECTION, doc)
        return doc
    doc = record.store.transact(write)
    _remember(record, doc)
    return doc


def _renew_import(record, run_id, claim_id):
    def write(tx):
        doc = tx.get(COLLECTION, run_id)
        if not doc or doc.get("import_claim") != claim_id or doc.get("import_status") != "processing":
            return False
        tx.put(COLLECTION, dict(doc, import_lease_until=time.time() + LEASE_SECONDS))
        return True
    return record.store.transact(write)


class _ImportModel:
    def __init__(self, model, record, run_id, claim_id):
        self.model, self.record, self.run_id, self.claim_id = model, record, run_id, claim_id

    async def generate(self, *args, **kwargs):
        async def check():
            if not await asyncio.to_thread(_renew_import, self.record, self.run_id, self.claim_id):
                raise RecordError("this import has been resumed elsewhere")
        await check()
        result = await self.model.generate(*args, **kwargs)
        await check()
        return result


async def pull_in(record, run_id: str, doctor: str, *, model=None, settings=None) -> dict:
    settings = settings or load_chain_settings()
    started = time.monotonic()
    claim_id = uuid.uuid4().hex
    def claim(tx):
        doc = tx.get(COLLECTION, run_id)
        if not doc:
            raise RecordError("unknown register case")
        if doc.get("unavailable"):
            raise RecordError("this register case has no matching saved reading and photos")
        if doc.get("import_status") == "ready":
            return doc, False
        if doc.get("import_status") == "processing" and _lease_until(doc) > time.time():
            raise RecordError("this old stay is being brought in; try again shortly")
        doc.update(import_status="processing", import_started_at=m.now_iso(), import_claim=claim_id,
                   import_lease_until=time.time() + LEASE_SECONDS)
        tx.put(COLLECTION, doc)
        return doc, True
    doc, work = await asyncio.to_thread(record.store.transact, claim)
    if not work:
        return record.admission(doc["admission_id"])
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                if not _renew_import(record, run_id, claim_id):
                    return
            except Exception:
                return
    pulse = threading.Thread(target=heartbeat, daemon=True, name="old-stay-lease")
    pulse.start()
    try:
        if not doc.get("admission_id"):
            def allocate(tx):
                current = tx.get(COLLECTION, run_id)
                if not current or current.get("import_claim") != claim_id:
                    raise RecordError("this import has been resumed elsewhere")
                if current.get("admission_id"):
                    return tx.get(m.ADMISSIONS, current["admission_id"])
                local = Record(tx, record.files)
                patient = local.create_patient(name=doc.get("name") or "Name not read yet", uhid=doc.get("uhid"),
                                               age=doc.get("age"), sex=doc.get("sex"))
                admission = local.create_admission(patient["id"], unit=unit_name(doc),
                                                   admitted_at=doc.get("admitted_at"), reference_date=doc["run_date"])
                admission.update(old_case=True, old_run_id=run_id, old_run_date=doc["run_date"], archived=True,
                                 discharged_at=doc.get("discharged_at"))
                tx.put(m.ADMISSIONS, admission)
                tx.put(COLLECTION, dict(current, admission_id=admission["id"]))
                return admission
            admission = await asyncio.to_thread(record.store.transact, allocate)
            doc["admission_id"] = admission["id"]
            await asyncio.to_thread(record.archive_admission, admission["id"], doctor)
            doc = await asyncio.to_thread(_save_import, record, run_id, claim_id, admission_id=admission["id"])
        aid = doc["admission_id"]
        reading = await asyncio.to_thread(lambda: _download(_drive(settings.drive_token), doc["reading_file_id"]).decode("utf-8", "replace"))
        blocks = image_blocks(reading)
        if set(blocks) != {p["number"] for p in doc["photos"]}:
            raise RecordError("saved reading and photos no longer line up")
        source_ids = []
        for photo in doc["photos"]:
            source, _ = await asyncio.to_thread(record.add_reference_source, aid, "drive:" + photo["id"],
                                                mime=photo["mime"], author=doctor,
                                                received_at=doc["run_date"] + "T23:59:59+05:30")
            if source["status"] == m.SRC_RECEIVED:
                await asyncio.to_thread(record.save_pages, source["id"], [{"number": 1, "text": blocks[photo["number"]]}])
            source_ids.append(source["id"])
        if model is None:
            from ..models import get_model_client
            from ..config import load_settings
            model = get_model_client(load_settings())
        from .worker import process_batch
        report = await process_batch(record, _ImportModel(model, record, run_id, claim_id), settings, aid, source_ids,
                                     saved_text={sid: blocks[p["number"]] for sid, p in zip(source_ids, doc["photos"])})
        doc = await asyncio.to_thread(_save_import, record, run_id, claim_id,
                   import_status="ready" if all(record.source(s)["status"] == m.SRC_MERGED for s in source_ids) else "incomplete",
                   cost_inr=round(float(doc.get("cost_inr") or 0) + report.read_cost_inr + report.extract_cost_inr, 4),
                   seconds=round(float(doc.get("seconds") or 0) + time.monotonic() - started, 2),
                   last_report={k: v for k, v in asdict(report).items() if k != "errors"}, errors=len(report.errors))
        if doc["import_status"] != "ready":
            raise RecordError("some saved pages could not be extracted; Bring in again to retry")
        return record.admission(aid)
    except Exception:
        try:
            if doc.get("import_status") == "processing":
                await asyncio.to_thread(_save_import, record, run_id, claim_id, import_status="incomplete")
        except RecordError:
            pass
        raise RecordError("the old stay could not be brought in; retry or tell the owner") from None
    finally:
        stop.set()
        pulse.join(timeout=1)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m wardos.chain.register")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("sync")
    cmd.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        result = sync(open_record(), limit=args.limit)
    except Exception as error:
        print(f"sync failed ({type(error).__name__}); no case details printed")
        return 1
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

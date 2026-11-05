"""The patient record: patients, admissions, sources, pages, facts, questions, events, versions."""
from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePath

from . import model as m
from .docstore import DocStore
from .files import FileStore
from .merge import MergeResult, medicine_states, merge
from .normalize import (NAME_KEYS, OTHER_NUMBER_KEY, OUTSIDE_PAGE_TYPES, UHID_KEYS, classify_uhids, fingerprint_distance,
                        group_names, is_future_time, norm_uhid, not_a_medicine, photo_fingerprint)

SEVERITIES = ("stable", "watch", "unstable")
AUTO = "WardOS"
NEAR_DUPLICATE_BITS = 6
UPLOAD_WORKERS = 4
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf",
              "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/wav": ".wav", "audio/ogg": ".ogg",
              "audio/webm": ".webm", "text/plain": ".txt"}


class RecordError(Exception):
    pass


def _sort_key(fact: dict) -> tuple:
    t = fact.get("clinical_time")
    if not t:
        return (1, "", fact.get("created_at") or "")
    if len(t) == 10:
        t = t + "T00:00"
    return (0, t, fact.get("created_at") or "")


class Record:
    def __init__(self, store: DocStore, files: FileStore):
        self.store = store
        self.files = files
        self._pool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()
        self._save_locks = [threading.Lock() for _ in range(64)]


    def create_patient(self, *, name: str, uhid: str | None = None, sex: str | None = None,
                       age: str | None = None) -> dict:
        doc = {"id": m.new_id("pat"), "name": name, "uhid": uhid, "sex": sex, "age": age, "created_at": m.now_iso()}
        self.store.put(m.PATIENTS, doc)
        return doc

    def find_patients(self, text: str) -> list[dict]:
        q = (text or "").strip().lower()
        return [p for p in self.store.query(m.PATIENTS)
                if q and (q in (p.get("name") or "").lower() or q == (p.get("uhid") or ""))]

    def create_admission(self, patient_id: str, *, unit: str, admitted_at: str | None = None,
                         ward: str | None = None, bed: str | None = None, reference_date: str | None = None) -> dict:
        if not self.store.get(m.PATIENTS, patient_id):
            raise RecordError("unknown patient")
        active = [a for a in self.store.query(m.ADMISSIONS, patient_id=patient_id) if a.get("status") == "active"]
        doc = {"id": m.new_id("adm"), "patient_id": patient_id, "unit": unit, "admitted_at": admitted_at,
               "reference_date": reference_date or admitted_at,
               "ward": ward, "bed": bed, "status": "active", "discharged_at": None, "version": 0,
               "created_at": m.now_iso(), "updated_at": m.now_iso(),
               "warning": "another admission of this patient is still open" if active else None}
        self.store.put(m.ADMISSIONS, doc)
        return doc

    def admission(self, admission_id: str) -> dict:
        doc = self.store.get(m.ADMISSIONS, admission_id)
        if not doc:
            raise RecordError("unknown admission")
        return doc

    def create_stay(self, *, name=None, uhid=None, sex=None, age=None, unit, admitted_at=None, bed=None, ward=None):
        from .beds import clean_bed
        at = m.now_iso()
        patient = dict(id=m.new_id("pat"), name=name, uhid=uhid, sex=sex, age=age, created_at=at)
        admission = dict(id=m.new_id("adm"), patient_id=patient["id"], unit=unit, admitted_at=admitted_at,
                         reference_date=admitted_at, bed=clean_bed(bed), ward=ward, status="active",
                         discharged_at=None, version=0, created_at=at, updated_at=at, warning=None)
        def write(tx):
            tx.put(m.PATIENTS, patient)
            tx.put(m.ADMISSIONS, admission)
        self.store.transact(write)
        return patient, admission

    def patient_of(self, admission_id: str) -> dict:
        return self.store.get(m.PATIENTS, self.admission(admission_id)["patient_id"]) or {}


    def add_source(self, admission_id: str, kind: str, *, data: bytes | None = None, text: str | None = None,
                   mime: str | None = None, filename: str | None = None, author: str | None = None,
                   received_at: str | None = None) -> tuple[dict, bool]:
        return self.add_sources(admission_id, [dict(kind=kind, data=data, text=text, mime=mime, filename=filename,
                                                    author=author, received_at=received_at)])[0]

    def add_sources(self, admission_id: str, items: list[dict]) -> list[tuple[dict, bool]]:
        admission = self.admission(admission_id)
        specs = []
        for item in items:
            if item["kind"] not in m.SOURCE_KINDS:
                raise RecordError(f"unknown source kind {item['kind']}")
            data, text = item.get("data"), item.get("text")
            payload = data if data is not None else (text or "").encode("utf-8")
            if not payload:
                raise RecordError("empty source")
            sha = hashlib.sha256(payload).hexdigest()
            specs.append(dict(item, payload=payload, sha=sha, source_id=m.stable_id("src", admission_id, sha)))

        unique = list(dict.fromkeys(s["source_id"] for s in specs))
        known = dict(zip(unique, self.run_parallel(lambda sid: self.store.get(m.SOURCES, sid), unique)))

        photos, first_of, tasks = None, {}, []
        for i, spec in enumerate(specs):
            sid = spec["source_id"]
            if sid in first_of or known[sid]:
                tasks.append(None if sid in first_of else (lambda s=sid: self._duplicate(admission_id, known[s])))
                continue
            first_of[sid] = i
            fingerprint = photo_fingerprint(spec["payload"]) if spec["kind"] == "photo" else None
            near = None
            if fingerprint:
                if photos is None:
                    photos = self.store.query(m.SOURCES, admission_id=admission_id, kind="photo")
                near = next((o["id"] for o in photos
                             if fingerprint_distance(fingerprint, o.get("fingerprint")) <= NEAR_DUPLICATE_BITS), None)
                photos.append({"id": sid, "fingerprint": fingerprint})
            tasks.append(lambda sp=spec, fp=fingerprint, nr=near: self._store_new(admission, sp, fp, nr))

        done = self.run_parallel(lambda task: task() if task else None, tasks)
        out = []
        for spec, result in zip(specs, done):
            if result is None:
                source = next(r[0] for s, r in zip(specs, done) if r and s["source_id"] == spec["source_id"])
                self._event(admission_id, "source.duplicate", source_id=source["id"])
                result = (source, True)
            out.append(result)
        return out

    def run_parallel(self, fn, args: list) -> list:
        if len(args) <= 1:
            return [fn(a) for a in args]
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="record")
        futures = [self._pool.submit(fn, a) for a in args]
        results, error = [], None
        for future in futures:
            try:
                results.append(future.result())
            except BaseException as e:
                results.append(None)
                error = error or e
        if error:
            raise error
        return results

    def _duplicate(self, admission_id: str, existing: dict) -> tuple[dict, bool]:
        self._event(admission_id, "source.duplicate", source_id=existing["id"])
        return existing, True

    def _store_new(self, admission: dict, spec: dict, fingerprint, near) -> tuple[dict, bool]:
        admission_id, source_id, kind = admission["id"], spec["source_id"], spec["kind"]
        ext = EXTENSIONS.get(spec.get("mime") or "", PurePath(spec.get("filename") or "").suffix.lower() or ".bin")
        lock = self._save_locks[hash(source_id) % len(self._save_locks)]
        contended = not lock.acquire(blocking=False)
        if contended:
            lock.acquire()
        try:
            if contended and self.store.get(m.SOURCES, source_id):
                return self._duplicate(admission_id, self.store.get(m.SOURCES, source_id))
            uri = self.files.save(["Patients", admission["patient_id"], admission_id, "sources"],
                                  f"{source_id}{ext}", spec["payload"])
            source = {"id": source_id, "admission_id": admission_id, "kind": kind, "mime": spec.get("mime"),
                      "sha256": spec["sha"], "fingerprint": fingerprint, "near_duplicate_of": near, "file_uri": uri,
                      "received_at": spec.get("received_at") or m.now_iso(), "author": spec.get("author"),
                      "status": m.SRC_RECEIVED, "held_reason": None, "merged_in": None, "model": None,
                      "cost_inr": 0.0, "seconds": 0.0, "error": None}

            def write(tx):
                if tx.get(m.SOURCES, source_id):
                    return tx.get(m.SOURCES, source_id), True
                tx.put(m.SOURCES, source)
                return source, False
            source, duplicate = self.store.transact(write)
        finally:
            lock.release()
        self._event(admission_id, "source.duplicate" if duplicate else "source.added", source_id=source_id,
                    kind=kind, near_duplicate_of=near)
        return source, duplicate

    def source(self, source_id: str) -> dict:
        doc = self.store.get(m.SOURCES, source_id)
        if not doc:
            raise RecordError("unknown source")
        return doc

    def add_reference_source(self, admission_id: str, file_uri: str, *, mime: str = "image/jpeg",
                             author: str | None = None, received_at: str | None = None) -> tuple[dict, bool]:
        self.admission(admission_id)
        if not file_uri.startswith("drive:") or not file_uri[6:] or "/" in file_uri[6:]:
            raise RecordError("not an original Drive file uri")
        sid = m.stable_id("src", admission_id, file_uri)
        def write(tx):
            existing = tx.get(m.SOURCES, sid)
            if existing:
                return existing, True
            source = {"id": sid, "admission_id": admission_id, "kind": "photo", "mime": mime,
                      "file_uri": file_uri, "sha256": None, "fingerprint": None, "near_duplicate_of": None,
                      "received_at": received_at or m.now_iso(), "author": author, "status": m.SRC_RECEIVED,
                      "held_reason": None, "merged_in": None, "model": None, "cost_inr": 0.0,
                      "seconds": 0.0, "error": None, "reference": True}
            tx.put(m.SOURCES, source)
            return source, False
        return self.store.transact(write)

    def read_file(self, source: dict) -> bytes:
        return self.files.read(source["file_uri"])

    def update_source(self, source_id: str, **fields) -> dict:
        def write(tx):
            doc = tx.get(m.SOURCES, source_id)
            if not doc:
                raise RecordError("unknown source")
            doc.update(fields)
            tx.put(m.SOURCES, doc)
            return doc
        return self.store.transact(write)

    def save_pages(self, source_id: str, pages: list[dict]) -> list[dict]:
        source = self.source(source_id)
        docs = []
        for page in pages:
            number = int(page.get("number") or len(docs) + 1)
            docs.append({"id": m.stable_id("page", source_id, number), "admission_id": source["admission_id"],
                         "source_id": source_id, "number": number, "text": page.get("text") or "",
                         "page_type": page.get("page_type"), "dates_seen": page.get("dates_seen") or [],
                         "patient_seen": page.get("patient_seen") or {}, "quality": page.get("quality"),
                         "held": False})

        def write(tx):
            for doc in docs:
                tx.put(m.PAGES, doc)
            src = tx.get(m.SOURCES, source_id)
            if src["status"] == m.SRC_RECEIVED:
                src["status"] = m.SRC_READ
                tx.put(m.SOURCES, src)
        self.store.transact(write)
        self.store.put(m.EVENTS, {"id": m.stable_id("evt", source_id, "read"), "admission_id": source["admission_id"],
                                  "version": None, "type": "source.read", "created_at": m.now_iso(),
                                  "detail": {"source_id": source_id, "pages": len(docs)}})
        return docs

    def update_pages(self, updates: dict[str, dict]) -> None:
        def write(tx):
            docs = {pid: tx.get(m.PAGES, pid) for pid in updates}
            for pid, fields in updates.items():
                if docs[pid]:
                    tx.put(m.PAGES, dict(docs[pid], **fields))
        self.store.transact(write)

    def update_patient(self, patient_id: str, **fields) -> dict:
        def write(tx):
            doc = tx.get(m.PATIENTS, patient_id)
            if not doc:
                raise RecordError("unknown patient")
            doc.update(fields)
            tx.put(m.PATIENTS, doc)
            return doc
        return self.store.transact(write)

    def pages(self, admission_id: str, source_id: str | None = None) -> list[dict]:
        filters = {"admission_id": admission_id}
        if source_id:
            filters["source_id"] = source_id
        return sorted(self.store.query(m.PAGES, **filters), key=lambda p: (p["source_id"], p["number"]))


    def apply(self, source_id: str, candidates: list[dict], *, held_pages: list[dict] | None = None,
              set_aside: dict[str, list[dict]] | None = None) -> dict:
        held_pages = held_pages or []
        set_aside = set_aside or {}

        def work(tx):
            source = tx.get(m.SOURCES, source_id)
            if not source:
                raise RecordError("unknown source")
            if source["status"] == m.SRC_MERGED:
                return {"version": source["merged_in"], "already_merged": True}
            admission = tx.get(m.ADMISSIONS, source["admission_id"])
            existing = tx.query(m.FACTS, admission_id=admission["id"])
            open_questions = tx.query(m.QUESTIONS, admission_id=admission["id"], status="open")
            page_docs = {p["id"]: p for p in tx.query(m.PAGES, source_id=source_id)}
            candidates_ = [self._screen(c, source) for c in candidates]
            result: MergeResult = merge(existing, candidates_, admission_id=admission["id"],
                                        open_questions=open_questions)
            version = int(admission.get("version") or 0) + 1
            for fact in result.add:
                tx.put(m.FACTS, dict(fact, added_in=version))
            for fact in result.update.values():
                tx.put(m.FACTS, dict(fact, changed_in=version))
            for q in result.questions:
                tx.put(m.QUESTIONS, dict(q, version=version))
            for held in held_pages:
                page = page_docs.get(held["page_id"])
                if page:
                    tx.put(m.PAGES, dict(page, held=True))
                q = {"id": m.stable_id("q", admission["id"], m.Q_WRONG_PATIENT, held["page_id"]),
                     "admission_id": admission["id"], "kind": m.Q_WRONG_PATIENT, "group": held["page_id"],
                     "fact_ids": [], "page_id": held["page_id"], "source_id": source_id, "status": "open", "answer": None,
                     "values_kept": True,
                     "answered_by": None, "created_at": m.now_iso(), "version": version,
                     "text": f"Page {held.get('number', '?')} of this upload may belong to another patient "
                             f"({held.get('why', 'name/UHID differ')}). Keep it out of this record?"}
                tx.put(m.QUESTIONS, q)
                tx.put(m.HELD, {"id": m.stable_id("held", held["page_id"]), "admission_id": admission["id"],
                                "source_id": source_id, "page_id": held["page_id"], "status": "set_aside",
                                "candidates": set_aside.get(held["page_id"]) or [], "created_at": m.now_iso()})
            source.update(status=m.SRC_MERGED, merged_in=version,
                          held_reason="some pages held: other patient" if held_pages else None)
            tx.put(m.SOURCES, source)
            admission.update(version=version, updated_at=m.now_iso())
            tx.put(m.ADMISSIONS, admission)
            summary = {"version": version, "added": len(result.add), "changed": len(result.update),
                       "corroborated": result.corroborated, "superseded": result.superseded,
                       "disputed": result.disputed, "questions": len(result.questions), "held_pages": len(held_pages)}
            tx.put(m.EVENTS, {"id": m.stable_id("evt", admission["id"], version, "merge"),
                              "admission_id": admission["id"], "version": version, "type": "source.merged",
                              "source_id": source_id, "detail": summary, "created_at": m.now_iso()})
            return summary
        summary = self.store.transact(work)
        if not summary.get("already_merged"):
            try:
                self.resolve_identity(self.source(source_id)["admission_id"])
            except Exception as error:
                summary["identity_error"] = f"{type(error).__name__}: {str(error)[:120]}"
        return summary

    @staticmethod
    def _screen(cand: dict, source: dict) -> dict:
        cand = dict(cand)
        if cand.get("assertion") != "planned" and cand.get("clinical_time") \
                and is_future_time(cand["clinical_time"], source.get("received_at")):
            cand["time_doubtful"] = True
        if cand["type"] == "med_event" and not_a_medicine(cand.get("label"), (cand.get("value") or {}).get("text")):
            cand.update(status=m.RETRACTED, retracted_by=AUTO, retract_reason="not a medicine", auto_identity=True)
        return cand


    def resolve_identity(self, admission_id: str) -> dict:
        writes, summary = self._identity_plan(self.store, admission_id)
        if not writes:
            return summary

        def fn(tx):
            return {"_writes": self._identity_plan(tx, admission_id)[0]}
        self._doctor_change(admission_id, fn, "identity.resolved", doctor=AUTO, **summary)
        return summary

    def _identity_plan(self, reader, admission_id: str) -> tuple[list[tuple[str, dict]], dict]:
        admission = reader.get(m.ADMISSIONS, admission_id)
        patient = reader.get(m.PATIENTS, admission["patient_id"]) or {}
        pages = [p for p in reader.query(m.PAGES, admission_id=admission_id) if not p.get("held")]
        facts = [f for f in reader.query(m.FACTS, admission_id=admission_id, type="identity")
                 if f.get("key") in UHID_KEYS | NAME_KEYS | {OTHER_NUMBER_KEY}
                 and (f["status"] in m.LIVE_STATUSES or f.get("auto_identity"))]
        open_q = reader.query(m.QUESTIONS, admission_id=admission_id, status="open")
        page_type = {p["id"]: p.get("page_type") for p in pages}
        writes: dict[str, tuple[str, dict]] = {}
        summary = {"uhid": None, "name": None, "set_aside": 0, "other_numbers": 0, "asked": 0}

        def put(collection, doc, **change):
            current = writes.get(doc["id"], (collection, doc))[1]
            if any(current.get(k) != v for k, v in change.items()):
                writes[doc["id"]] = (collection, dict(current, **change))

        asks: list[dict] = []
        uhid_facts = [f for f in facts if f["key"] in UHID_KEYS | {OTHER_NUMBER_KEY}]
        own: dict[str, set] = {}
        outside_seen: set[str] = set()
        for p in pages:
            value = norm_uhid((p.get("patient_seen") or {}).get("uhid"))
            if value:
                (outside_seen.add(value) if p.get("page_type") in OUTSIDE_PAGE_TYPES
                 else own.setdefault(p["id"], set()).add(value))
        for f in uhid_facts:
            value = norm_uhid((f.get("value") or {}).get("text"))
            if not value:
                continue
            if page_type.get(f.get("page_id")) in OUTSIDE_PAGE_TYPES:
                outside_seen.add(value)
            else:
                own.setdefault(f.get("page_id") or f["id"], set()).add(value)
        votes: dict[str, int] = {}
        for values in own.values():
            for value in values:
                votes[value] = votes.get(value, 0) + 1
        outside_only = {v for v in outside_seen if v not in votes}
        locked = next((norm_uhid(f["value"].get("text")) for f in uhid_facts if f["status"] == m.DOCTOR_LOCKED), None)
        verdict = classify_uhids(votes, outside_only, locked)
        winner = verdict["winner"]
        group = m.stable_id("dg", admission_id, "identity", "uhid")
        by_value: dict[str, list[dict]] = {}
        for f in uhid_facts:
            by_value.setdefault(norm_uhid(f["value"].get("text")), []).append(f)
        for value, same in by_value.items():
            same = sorted(same, key=lambda f: (f["status"] != m.DOCTOR_LOCKED, -len(f.get("support") or []), f["id"]))
            lead, extra = same[0], same[1:]
            if lead["status"] == m.DOCTOR_LOCKED:
                continue
            if value in verdict["ask"]:
                put(m.FACTS, lead, status=m.DISPUTED, dispute_group=group, key="uhid")
            elif value == winner:
                support = sorted({s for f in same for s in f.get("support") or []})
                written = lead["value"].get("text") if lead["value"].get("text") != value else lead["value"].get("written")
                clean_value = dict(lead["value"], text=value, **({"written": written} if written else {}))
                put(m.FACTS, lead, status=m.ACTIVE, dispute_group=None, key="uhid", support=support, value=clean_value)
                for f in extra:
                    put(m.FACTS, f, status=m.SUPERSEDED, superseded_by=lead["id"], dispute_group=None, auto_identity=True)
            elif value in verdict["misread"]:
                for f in same:
                    put(m.FACTS, f, status=m.RETRACTED, retracted_by=AUTO, retract_reason=verdict["misread"][value],
                        dispute_group=None, auto_identity=True)
                summary["set_aside"] += len(same)
            elif value in verdict["other"]:
                for f in same:
                    put(m.FACTS, f, key=OTHER_NUMBER_KEY, label="Other hospital number", status=m.ACTIVE,
                        dispute_group=None, auto_identity=True)
                summary["other_numbers"] += 1
        if verdict["ask"]:
            contenders = [by_value[v][0] for v in verdict["ask"] if v in by_value]
            if len(contenders) > 1:
                shown = " and ".join(f"{v} ({votes.get(v, 0)} page{'s' if votes.get(v, 0) != 1 else ''})"
                                     for v in verdict["ask"])
                asks.append({"group": group, "facts": contenders,
                             "text": f"Two different UHIDs are written on this patient's pages: {shown}. Which one is right?"})
        summary["uhid"] = winner
        name_facts = [f for f in facts if f["key"] in NAME_KEYS]
        name_votes: dict[str, int] = {}
        seen_by_page: dict[str, set] = {}
        for p in pages:
            written = ((p.get("patient_seen") or {}).get("name") or "").strip()
            if written:
                seen_by_page.setdefault(p["id"], set()).add(written)
        for f in name_facts:
            written = str((f.get("value") or {}).get("text") or "").strip()
            if written:
                seen_by_page.setdefault(f.get("page_id") or f["id"], set()).add(written)
        for values in seen_by_page.values():
            for written in values:
                name_votes[written] = name_votes.get(written, 0) + 1
        groups = group_names(name_votes)
        name_group = m.stable_id("dg", admission_id, "identity", "name")
        locked_name = next((f for f in name_facts if f["status"] == m.DOCTOR_LOCKED), None)
        canonical = None
        if locked_name:
            kept = group_names({str(locked_name["value"].get("text") or ""): 1})
            canonical = kept[0]["canonical"] if kept else None
        elif groups:
            canonical = groups[0]["canonical"]
            lead_of: list[dict] = []
            for g in groups:
                mine = [f for f in name_facts if str(f["value"].get("text") or "").strip() in g["names"]]
                if not mine:
                    continue
                mine.sort(key=lambda f: (f["value"].get("text") != g["best"], -len(f.get("support") or []), f["id"]))
                lead_of.append(mine[0])
                if len(groups) == 1 or g is groups[0]:
                    support = sorted({s for f in mine for s in f.get("support") or []})
                    put(m.FACTS, mine[0], status=m.ACTIVE, dispute_group=None, support=support)
                    for f in mine[1:]:
                        put(m.FACTS, f, status=m.SUPERSEDED, superseded_by=mine[0]["id"], dispute_group=None,
                            auto_identity=True)
            if len(lead_of) > 1:
                for f in lead_of:
                    put(m.FACTS, f, status=m.DISPUTED, dispute_group=name_group)
                shown = " and ".join(f"'{g['canonical']}'" for g in groups if any(
                    str(f['value'].get('text') or '').strip() in g['names'] for f in name_facts))
                asks.append({"group": name_group, "facts": lead_of,
                             "text": f"Different names are written on this patient's pages: {shown}. Which one is right?"})
                canonical = canonical if patient.get("name") is None else None
        summary["name"] = canonical
        identity_ids = {f["id"] for f in facts}
        keep = {a["group"] for a in asks}
        for q in open_q:
            if q.get("kind") in (m.Q_CONFLICT, m.Q_DOCTOR_CONFLICT) and q.get("group") not in keep \
                    and set(q.get("fact_ids") or []) & identity_ids:
                put(m.QUESTIONS, q, status="answered", answer="picked by WardOS", answered_by=AUTO)
        for a in asks:
            qid = m.stable_id("q", admission_id, m.Q_CONFLICT, a["group"])
            existing = reader.get(m.QUESTIONS, qid)
            if existing and existing.get("status") != "open":
                continue
            first = next((f for f in a["facts"] if f.get("page_id")), a["facts"][0])
            doc = existing or {"id": qid, "admission_id": admission_id, "kind": m.Q_CONFLICT, "group": a["group"],
                               "status": "open", "answer": None, "answered_by": None, "created_at": m.now_iso()}
            put(m.QUESTIONS, doc, fact_ids=[f["id"] for f in a["facts"]], text=a["text"], page_id=first.get("page_id"),
                source_id=first.get("source_id"))
            if qid not in writes:
                writes[qid] = (m.QUESTIONS, doc)
            summary["asked"] += 1
        fields = {}
        if winner and patient.get("uhid") != winner:
            fields["uhid"] = winner
        if canonical and patient.get("name") != canonical:
            fields["name"] = canonical
        others = verdict["other"]
        if (patient.get("other_hospital_numbers") or []) != others and (others or patient.get("other_hospital_numbers")):
            fields["other_hospital_numbers"] = others
        if fields and patient.get("id"):
            put(m.PATIENTS, patient, **fields)
        return list(writes.values()), summary


    def tidy_admission(self, admission_id: str) -> dict:
        sources = {s["id"]: s for s in self.store.query(m.SOURCES, admission_id=admission_id)}
        changes, doubtful, food = [], 0, 0
        for f in self.store.query(m.FACTS, admission_id=admission_id):
            source = sources.get(f.get("source_id")) or {}
            if f.get("clinical_time") and f.get("assertion") != "planned" and not f.get("time_doubtful") \
                    and is_future_time(f["clinical_time"], source.get("received_at")):
                changes.append((m.FACTS, dict(f, time_doubtful=True)))
                doubtful += 1
            elif f["type"] == "med_event" and f["status"] in (m.ACTIVE, m.DISPUTED) \
                    and not_a_medicine(f.get("label"), (f.get("value") or {}).get("text")):
                changes.append((m.FACTS, dict(f, status=m.RETRACTED, retracted_by=AUTO, retract_reason="not a medicine",
                                              auto_identity=True)))
                food += 1
        if changes:
            self._doctor_change(admission_id, lambda tx: {"_writes": changes}, "record.tidied", doctor=AUTO,
                                doubtful_dates=doubtful, not_medicines=food)
        return {"doubtful_dates": doubtful, "not_medicines": food, "identity": self.resolve_identity(admission_id)}

    def archive_admission(self, admission_id: str, doctor: str) -> dict:
        def work(tx):
            admission = tx.get(m.ADMISSIONS, admission_id)
            if not admission:
                raise RecordError("unknown admission")
            version = int(admission.get("version") or 0) + 1
            admission.update(archived=True, archived_by=doctor, archived_at=m.now_iso(), version=version,
                             updated_at=m.now_iso())
            tx.put(m.ADMISSIONS, admission)
            tx.put(m.EVENTS, {"id": m.stable_id("evt", admission_id, version, "admission.archived"),
                              "admission_id": admission_id, "version": version, "type": "admission.archived",
                              "detail": {"doctor": doctor}, "created_at": m.now_iso()})
            return admission
        return self.store.transact(work)


    def facts(self, admission_id: str, *, live_only: bool = True) -> list[dict]:
        facts = self.store.query(m.FACTS, admission_id=admission_id)
        if live_only:
            facts = [f for f in facts if f["status"] in m.LIVE_STATUSES]
        return sorted(facts, key=_sort_key)

    def timeline(self, admission_id: str) -> list[dict]:
        return self.facts(admission_id)

    def changes_since(self, admission_id: str, version: int) -> list[dict]:
        return [f for f in self.store.query(m.FACTS, admission_id=admission_id)
                if (f.get("added_in") or 0) > version or (f.get("changed_in") or 0) > version]

    def questions(self, admission_id: str, status: str | None = "open") -> list[dict]:
        filters = {"admission_id": admission_id}
        if status:
            filters["status"] = status
        return self.store.query(m.QUESTIONS, **filters)

    def medicines(self, admission_id: str) -> dict[str, dict]:
        return medicine_states(self.facts(admission_id))

    def events(self, admission_id: str, since: str | None = None) -> list[dict]:
        events = [e for e in self.store.query(m.EVENTS, admission_id=admission_id)
                  if not since or (e.get("created_at") or "") > since]
        return sorted(events, key=lambda e: e.get("created_at") or "")


    def _doctor_change(self, admission_id: str, fn, event_type: str, **detail) -> dict:
        def work(tx):
            admission = tx.get(m.ADMISSIONS, admission_id)
            out = fn(tx)
            version = int(admission.get("version") or 0) + 1
            for collection, doc in out.pop("_writes"):
                if collection == m.FACTS:
                    doc = dict(doc, changed_in=version) if doc.get("added_in") else dict(doc, added_in=version)
                tx.put(collection, doc)
            admission.update(version=version, updated_at=m.now_iso())
            tx.put(m.ADMISSIONS, admission)
            tx.put(m.EVENTS, {"id": m.stable_id("evt", admission_id, version, event_type),
                              "admission_id": admission_id, "version": version, "type": event_type,
                              "detail": detail, "created_at": m.now_iso()})
            return dict(out, version=version)
        return self.store.transact(work)

    def correct_fact(self, fact_id: str, *, doctor: str, text: str, number: float | None = None,
                     unit: str | None = None) -> dict:
        old = self.store.get(m.FACTS, fact_id)
        if not old:
            raise RecordError("unknown fact")

        def fn(tx):
            current = tx.get(m.FACTS, fact_id)
            group = current.get("dispute_group")
            others = [f for f in tx.query(m.FACTS, dispute_group=group) if f["id"] != fact_id] if group else []
            new = m.fact_doc(admission_id=current["admission_id"], source_id=None, page_id=current.get("page_id"),
                             index=0, type=current["type"], key=current["key"], label=current.get("label") or "",
                             value={"text": text, "number": number, "unit": unit},
                             clinical_time=current.get("clinical_time"), time_precision=current.get("time_precision"),
                             assertion=current.get("assertion") or "present", quote=current.get("quote") or "",
                             quote_found=bool(current.get("quote_found")), created_by="doctor",
                             status=m.DOCTOR_LOCKED)
            new["id"] = m.stable_id("fact", fact_id, "doctor", doctor, text)
            new.update(supersedes=fact_id, doctor=doctor, support=current.get("support") or [], dispute_group=None)
            writes = [(m.FACTS, dict(current, status=m.SUPERSEDED, superseded_by=new["id"])), (m.FACTS, new)]
            for other in others:
                if other["status"] == m.DISPUTED:
                    writes.append((m.FACTS, dict(other, status=m.SUPERSEDED, superseded_by=new["id"])))
            if group:
                for q in tx.query(m.QUESTIONS, group=group, status="open"):
                    writes.append((m.QUESTIONS, dict(q, status="answered", answer=new["id"], answered_by=doctor)))
            return {"fact": new, "_writes": writes}
        return self._doctor_change(old["admission_id"], fn, "fact.corrected", fact_id=fact_id, doctor=doctor)

    def confirm_fact(self, fact_id: str, *, doctor: str) -> dict:
        fact = self.store.get(m.FACTS, fact_id)
        if not fact:
            raise RecordError("unknown fact")
        return self.correct_fact(fact_id, doctor=doctor, text=(fact.get("value") or {}).get("text") or "",
                                 number=(fact.get("value") or {}).get("number"), unit=(fact.get("value") or {}).get("unit"))

    def dismiss_fact(self, fact_id: str, *, doctor: str, reason: str) -> dict:
        fact = self.store.get(m.FACTS, fact_id)
        if not fact:
            raise RecordError("unknown fact")

        def fn(tx):
            current = tx.get(m.FACTS, fact_id)
            return {"fact": current, "_writes": [(m.FACTS, dict(current, status=m.RETRACTED, retracted_by=doctor,
                                                                retract_reason=reason))]}
        return self._doctor_change(fact["admission_id"], fn, "fact.dismissed", fact_id=fact_id, doctor=doctor)

    def answer_question(self, question_id: str, *, doctor: str, answer: str, fact_id: str | None = None) -> dict:
        q = self.store.get(m.QUESTIONS, question_id)
        if not q:
            raise RecordError("unknown question")
        if q.get("kind") == m.Q_WRONG_PATIENT:
            return self._answer_held_page(q, doctor=doctor, answer=answer)
        if fact_id:
            if fact_id not in (q.get("fact_ids") or []):
                raise RecordError("that value is not part of this question")
            chosen = self.confirm_fact(fact_id, doctor=doctor)["fact"]
            for rival in q.get("fact_ids") or []:
                current = self.store.get(m.FACTS, rival)
                if rival != fact_id and current and current["status"] in m.LIVE_STATUSES \
                        and current.get("superseded_by") != chosen["id"]:
                    self.dismiss_fact(rival, doctor=doctor, reason="doctor chose another value")
            q = self.store.get(m.QUESTIONS, question_id)
            if q.get("status") != "open":
                return {"question": q, "fact": chosen, "version": self.admission(q["admission_id"])["version"]}

        def fn(tx):
            current = tx.get(m.QUESTIONS, question_id)
            return {"question": current, "_writes": [(m.QUESTIONS, dict(current, status="answered", answer=answer,
                                                                        answered_by=doctor))]}
        return self._doctor_change(q["admission_id"], fn, "question.answered", question_id=question_id, doctor=doctor)

    def _answer_held_page(self, q: dict, *, doctor: str, answer: str) -> dict:
        this_patient = str(answer or "").strip().lower() == m.THIS_PATIENT.lower()
        held_id = m.stable_id("held", q["page_id"]) if q.get("page_id") else None

        def work(tx):
            current = tx.get(m.QUESTIONS, q["id"])
            admission = tx.get(m.ADMISSIONS, q["admission_id"])
            if current["status"] != "open":
                return {"question": current, "added": 0, "already_answered": True,
                        "version": int(admission.get("version") or 0)}
            page = tx.get(m.PAGES, q["page_id"]) if q.get("page_id") else None
            held = tx.get(m.HELD, held_id) if held_id else None
            waiting = bool(held and held.get("status") == "set_aside")
            kept = (held.get("candidates") or []) if (this_patient and waiting) else []
            result = None
            if kept:
                source = tx.get(m.SOURCES, held["source_id"])
                existing = tx.query(m.FACTS, admission_id=admission["id"])
                open_questions = tx.query(m.QUESTIONS, admission_id=admission["id"], status="open")
                result = merge(existing, [self._screen(c, source) for c in kept], admission_id=admission["id"],
                               open_questions=open_questions)
            version = int(admission.get("version") or 0) + 1
            added = 0
            if result:
                for fact in result.add:
                    tx.put(m.FACTS, dict(fact, added_in=version))
                for fact in result.update.values():
                    tx.put(m.FACTS, dict(fact, changed_in=version))
                for question in result.questions:
                    tx.put(m.QUESTIONS, dict(question, version=version))
                added = len(result.add)
            if this_patient and page:
                tx.put(m.PAGES, dict(page, held=False))
            if held:
                tx.put(m.HELD, dict(held, candidates=[], status="merged" if this_patient else "kept_out",
                                    answered_by=doctor, answered_at=m.now_iso()))
            tx.put(m.QUESTIONS, dict(current, status="answered", answer=answer, answered_by=doctor))
            admission.update(version=version, updated_at=m.now_iso())
            tx.put(m.ADMISSIONS, admission)
            detail = {"question_id": q["id"], "doctor": doctor, "answer": answer, "added": added,
                      "corroborated": result.corroborated if result else 0}
            tx.put(m.EVENTS, {"id": m.stable_id("evt", admission["id"], version, "question.answered"),
                              "admission_id": admission["id"], "version": version, "type": "question.answered",
                              "detail": detail, "created_at": m.now_iso()})
            return {"question": dict(current, status="answered", answer=answer, answered_by=doctor),
                    "added": added, "version": version,
                    "values_not_added": bool(this_patient and held is None)}
        out = self.store.transact(work)
        if this_patient and not out.get("already_answered"):
            try:
                self.resolve_identity(q["admission_id"])
            except Exception as error:
                out["identity_error"] = f"{type(error).__name__}: {str(error)[:120]}"
        return out

    def set_bed(self, admission_id: str, *, doctor: str, bed: str | None,
                confirm_occupied: bool = False) -> dict:
        from .beds import BedTaken, clean_bed, occupants
        bed = clean_bed(bed)

        def work(tx):
            admission = tx.get(m.ADMISSIONS, admission_id)
            if not admission:
                raise RecordError("unknown admission")
            if admission.get("archived") or admission.get("status") != "active":
                raise RecordError("Only an active admission can change beds")
            taken = occupants(admission, bed, tx.query(m.ADMISSIONS, status="active"))
            if taken and not confirm_occupied:
                raise BedTaken(taken)
            from .today import effective_bed
            old = effective_bed(admission, tx.query(m.FACTS, admission_id=admission_id)
                                if not admission.get("bed") and not admission.get("bed_set_at") else [])
            if old == bed and admission.get("bed_set_at"):
                return admission
            at = m.now_iso()
            version = int(admission.get("version") or 0) + 1
            admission.update(bed=bed, bed_set_by=doctor, bed_set_at=at, version=version, updated_at=at)
            tx.put(m.ADMISSIONS, admission)
            tx.put(m.EVENTS, {"id": m.stable_id("evt", admission_id, version, "bed.set"),
                              "admission_id": admission_id, "version": version, "type": "bed.set",
                              "detail": {"from": old, "bed": bed, "doctor": doctor,
                                         "occupied_confirmed": [a["id"] for a in taken]}, "created_at": at})
            return admission
        return self.store.transact(work)

    def set_severity(self, admission_id: str, *, doctor: str, severity: str | None) -> dict:
        if severity not in (None, *SEVERITIES):
            raise RecordError("severity must be stable, watch or unstable")

        def work(tx):
            admission = tx.get(m.ADMISSIONS, admission_id)
            if not admission:
                raise RecordError("unknown admission")
            if admission.get("archived") and admission.get("old_case"):
                raise RecordError("Old stays are view-and-ask only")
            version = int(admission.get("version") or 0) + 1
            admission.update(severity=severity, severity_by=doctor, severity_at=m.now_iso(), version=version,
                             updated_at=m.now_iso())
            tx.put(m.ADMISSIONS, admission)
            tx.put(m.EVENTS, {"id": m.stable_id("evt", admission_id, version, "severity.set"),
                              "admission_id": admission_id, "version": version, "type": "severity.set",
                              "detail": {"severity": severity, "doctor": doctor}, "created_at": m.now_iso()})
            return admission
        return self.store.transact(work)

    def review_admission(self, admission_id: str, today=None) -> int:
        from .review import review
        admission = self.admission(admission_id)
        found = review(self.facts(admission_id), admission=admission, today=today)

        def write(tx):
            new = [q for q in found if tx.get(m.QUESTIONS, q["id"]) is None]
            for q in new:
                tx.put(m.QUESTIONS, dict(q, version=admission.get("version")))
            return len(new)
        return self.store.transact(write)

    def mark_reviewed(self, doctor: str, admission_id: str) -> dict:
        doc = {"id": m.stable_id("rev", doctor, admission_id), "doctor": doctor, "admission_id": admission_id,
               "version": int(self.admission(admission_id).get("version") or 0), "reviewed_at": m.now_iso()}
        self.store.put(m.REVIEWS, doc)
        return doc

    def since_last_review(self, doctor: str, admission_id: str) -> list[dict]:
        review = self.store.get(m.REVIEWS, m.stable_id("rev", doctor, admission_id))
        return self.changes_since(admission_id, int(review["version"]) if review else 0)


    def _event(self, admission_id: str, type_: str, **detail):
        self.store.put(m.EVENTS, {"id": m.new_id("evt"), "admission_id": admission_id, "version": None,
                                  "type": type_, "detail": detail, "created_at": m.now_iso()})

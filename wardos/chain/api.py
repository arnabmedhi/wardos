"""The ward record HTTP API: the doors used by the screens, by fetched hospital reports and by the clinical helper."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any, Protocol

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..config import load_settings
from ..models import get_model_client
from . import finals
from .alerts import Alerts, ICONS, SubscribeIn, UnsubscribeIn
from .auth import Auth, Doctor, any_caller, install as install_auth, owner_only, signed_in
from . import model as m
from .record import Record, RecordError
from .render import summary_text, update_summary
from .settings import AuthSettings, ChainSettings, load_auth_settings, load_chain_settings, open_record
from .today import _sex, build_today, reports_in_order
from .worker import process_batch

MIME_KIND = {"image/jpeg": "photo", "image/png": "photo", "image/webp": "photo", "application/pdf": "pdf"}
VOICE_MIMES = {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav", "audio/wave"}
VOICE_MAX_BYTES = 10 * 1024 * 1024
DEMO_FINALS_PER_COPY = 3

def lab_result_page(data: bytes) -> str | None:
    """A demo hospital report is stored as one result in JSON: show it as a readable page. Anything else (a real PDF) is left alone."""
    import html, json
    if not data.lstrip().startswith(b"{"):
        return None
    try:
        row = json.loads(data)
    except ValueError:
        return None
    if not isinstance(row, dict) or not row.get("test"):
        return None
    esc = lambda key: html.escape(str(row.get(key) or ""))
    when = esc("time").replace("T", " ")[:16]
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Lab report</title>"
            "<body style='margin:0;min-height:100vh;display:grid;place-items:center;background:#EDF6F2;color:#0F1A2E;font:16px/1.5 system-ui,sans-serif'>"
            "<main style='width:min(420px,90vw);background:#fff;border-radius:18px;padding:26px;box-shadow:0 10px 28px -18px rgba(20,70,60,.5)'>"
            f"<div style='color:#48546A;font-size:14px'>Lab report &middot; demo hospital</div><h1 style='margin:6px 0 14px;font-size:24px'>{esc('test')}</h1>"
            f"<div style='font-size:36px;font-weight:800'>{esc('value')} <small style='font-size:17px;font-weight:500;color:#48546A'>{esc('unit')}</small></div>"
            f"<p style='margin:14px 0 0;color:#48546A'>Normal range: {esc('reference') or 'not given'}<br>Collected: {when}</p></main>")


class Queue(Protocol):
    async def enqueue(self, admission_id: str, source_ids: list[str], *, attempt: str = "") -> None: ...


class LocalQueue:

    def __init__(self, run):
        self._run, self._locks, self.tasks = run, {}, set()

    async def enqueue(self, admission_id, source_ids, *, attempt=""):
        lock = self._locks.setdefault(admission_id, asyncio.Lock())

        async def job():
            async with lock:
                await self._run(admission_id, source_ids)
        task = asyncio.create_task(job())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


class CloudTasksQueue:

    def __init__(self, *, project: str, location: str, queue: str, service_url: str, service_account: str):
        self.parent = f"projects/{project}/locations/{location}/queues/{queue}"
        self.url, self.account = service_url.rstrip("/"), service_account
        self._session = None

    def _post(self, body: dict) -> int:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        if self._session is None:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(credentials)
        response = self._session.post(f"https://cloudtasks.googleapis.com/v2/{self.parent}/tasks", json=body,
                                      timeout=30)
        if response.status_code not in (200, 409):
            raise RuntimeError(f"Cloud Tasks refused the task: {response.status_code} {response.text[:200]}")
        return response.status_code

    async def enqueue(self, admission_id, source_ids, *, attempt="", path="/internal/process", extra=None):
        import base64
        import hashlib
        import json
        name = hashlib.sha256("|".join([admission_id, *sorted(source_ids), attempt]).encode()).hexdigest()[:32]
        payload = json.dumps({"admission_id": admission_id, "source_ids": source_ids, **(extra or {})}).encode()
        body = {"task": {"name": f"{self.parent}/tasks/b{name}",
                         "dispatchDeadline": "1800s",
                         "httpRequest": {"httpMethod": "POST", "url": f"{self.url}{path}",
                                         "headers": {"Content-Type": "application/json"},
                                         "body": base64.b64encode(payload).decode(),
                                         "oidcToken": {"serviceAccountEmail": self.account, "audience": self.url}}}}
        await asyncio.to_thread(self._post, body)


def queue_from_env() -> Queue | None:
    import os
    if (os.environ.get("WARDOS_CHAIN_QUEUE") or "local").lower() != "cloudtasks":
        return None
    env = os.environ
    return CloudTasksQueue(project=env.get("WARDOS_CHAIN_PROJECT", "your-gcp-project"),
                           location=env.get("WARDOS_CHAIN_TASKS_LOCATION", "asia-south1"),
                           queue=env.get("WARDOS_CHAIN_TASKS_QUEUE", "wardos-chain-sources"),
                           service_url=env["WARDOS_CHAIN_SERVICE_URL"], service_account=env["WARDOS_CHAIN_RUNNER_SA"])


class PatientIn(BaseModel):
    name: str | None = None
    uhid: str | None = None
    sex: str | None = None
    age: str | None = None


class AdmissionIn(BaseModel):
    patient_id: str
    unit: str
    admitted_at: str | None = None
    ward: str | None = None
    bed: str | None = None


class NoteIn(BaseModel):
    text: str


class NewStayIn(PatientIn):
    unit: str
    admitted_at: str | None = None
    bed: str | None = None
    ward: str | None = None


class FactChange(BaseModel):
    action: str
    text: str | None = None
    number: float | None = None
    unit: str | None = None
    reason: str | None = None


class AnswerIn(BaseModel):
    answer: str
    fact_id: str | None = None


class SeverityIn(BaseModel):
    severity: str | None


class BedIn(BaseModel):
    bed: str | None
    confirm_occupied: bool = False


class AskIn(BaseModel):
    text: str
    admission_id: str | None = None
    reply_to: str | None = None
    direct: bool = False          # the page's own lookups (the lab table): straight to the record, no AI


class ProcessIn(BaseModel):
    admission_id: str
    source_ids: list[str]


def create_app(record: Record | None = None, model=None, settings: ChainSettings | None = None,
               queue: Queue | None = None, job_runner=None, drive_creds=None,
               auth_settings: AuthSettings | None = None, verify_token=None, exchange_code=None,
               clock=None, auth: Auth | None = None, guest: bool = False, ask_model=None, demo=None,
               final_folders: tuple[str, str] | None = None) -> FastAPI:
    import os
    import time
    settings = settings or load_chain_settings()
    record = record or open_record(settings)
    auth = auth or Auth(auth_settings or load_auth_settings(), record.store, verify_token=verify_token,
                        exchange_code=exchange_code, clock=clock or time.time)
    state: dict[str, Any] = {"model": model, "job_runner": job_runner, "drive_creds": drive_creds}
    final_tests_folder, final_records_folder = final_folders or (
        os.environ.get("WARDOS_FINAL_TESTS_FOLDER_ID", ""),
        os.environ.get("WARDOS_FINAL_RECORDS_FOLDER_ID", ""))
    final_job = os.environ.get("WARDOS_FINAL_JOB", "")
    summary_configured = job_runner is not None or bool(final_job and final_tests_folder)

    def get_job_runner():
        if state["job_runner"] is None:
            state["job_runner"] = finals.cloud_job_runner(settings.project, os.environ.get(
                "WARDOS_FINAL_REGION", "asia-south1"), final_job)
        return state["job_runner"]

    def get_drive_creds():
        if state["drive_creds"] is None:
            from google.oauth2.credentials import Credentials
            state["drive_creds"] = Credentials.from_authorized_user_file(
                str(settings.drive_token), ["https://www.googleapis.com/auth/drive"])
        return state["drive_creds"]

    def get_model():
        if state["model"] is None:
            state["model"] = get_model_client(load_settings())
        return state["model"]

    def saved_reading(filename: str | None) -> str | None:
        import os
        import re
        from pathlib import Path
        folder = os.environ.get("WARDOS_CHAIN_SAVED_READINGS")
        hit = re.fullmatch(r"(case_[0-9a-f]{10})-(\d+)\.jpe?g", filename or "", flags=re.I)
        if not folder or not hit or os.environ.get("K_SERVICE"):
            return None
        from .testsets import image_blocks
        path = Path(folder) / hit.group(1) / "stage1_OCR.txt"
        return image_blocks(path.read_text(encoding="utf-8")).get(int(hit.group(2))) if path.is_file() else None

    alerts = Alerts(record, auth)

    def alert_snapshot(admission_id):
        try:
            return alerts.snapshot(admission_id) if alerts.settings.configured else {}
        except Exception:
            return None

    def notify(admission_id, before, report_ids=()):
        try:
            if before is not None:
                alerts.after_merge(admission_id, before, report_ids=report_ids)
        except Exception:
            app.state.push_failures += 1

    async def run_batch(admission_id: str, source_ids: list[str]) -> dict:
        before = await asyncio.to_thread(alert_snapshot, admission_id)
        report = await process_batch(record, get_model(), settings, admission_id, source_ids)
        logging.getLogger("uvicorn.error").info(
            "WardOS batch usage: sources=%d read_inr=%.6f extract_inr=%.6f seconds=%.1f (reading %.1f, model extracting %.1f, saving %.1f)",
            report.sources, report.read_cost_inr, report.extract_cost_inr, report.seconds, report.read_seconds, report.model_seconds, report.merge_seconds)
        for line in report.errors[:10]:
            logging.getLogger("uvicorn.error").warning("WardOS batch error: %s", line[:200])
        await asyncio.to_thread(notify, admission_id, before)
        return asdict(report)

    cloud_queue = None if guest else queue_from_env()
    queue = queue or cloud_queue or LocalQueue(run_batch)
    app = FastAPI(title="WardOS patient record", version="1", docs_url=None, redoc_url=None)
    install_auth(app, auth)
    app.state.record, app.state.queue, app.state.run_batch = record, queue, run_batch
    app.state.alerts, app.state.push_failures = alerts, 0
    app.state.open_tasks = list
    if not guest:
        from .agent_routes import install_agent
        install_agent(app, record, get_model=get_model, settings=settings, alert_snapshot=alert_snapshot, notify=notify, guest=guest)
        from .demo import demo_from_env
        auth.demo = demo(auth, settings, cloud_queue) if demo else demo_from_env(auth, settings, queue=cloud_queue)
        if auth.demo:
            auth.demo.install(app)

    def admission_or_404(admission_id: str) -> dict:
        try:
            admission = record.admission(admission_id)
        except (RecordError, KeyError, TypeError):
            admission = None
        if not admission:
            raise HTTPException(404, "no such admission")
        return admission

    def writable_admission(admission_id: str) -> dict:
        admission = admission_or_404(admission_id)
        if admission.get("archived") and admission.get("old_case"):
            raise HTTPException(409, "Old stays are view-and-ask only")
        return admission

    @app.get("/", include_in_schema=False)
    def screen():
        from pathlib import Path
        from fastapi.responses import FileResponse
        return FileResponse(Path(__file__).parent / "static" / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        from pathlib import Path
        from fastapi.responses import FileResponse
        return FileResponse(Path(__file__).parent / "static" / "manifest.webmanifest",
                            media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        from pathlib import Path
        from fastapi.responses import FileResponse
        return FileResponse(Path(__file__).parent / "static" / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/icons/{name}", include_in_schema=False)
    def install_icon(name: str):
        from pathlib import Path
        from fastapi.responses import FileResponse
        if name not in ICONS:
            raise HTTPException(404, "no such icon")
        return FileResponse(Path(__file__).parent / "static" / "icons" / name, media_type="image/png")

    @app.post("/push/subscribe")
    def push_subscribe(body: SubscribeIn, doctor: Doctor = Depends(signed_in)):
        return alerts.subscribe(doctor, body)

    @app.post("/push/unsubscribe")
    def push_unsubscribe(body: UnsubscribeIn, doctor: Doctor = Depends(signed_in)):
        return alerts.unsubscribe(doctor, body.endpoint)

    @app.post("/push/test")
    def push_test(doctor: Doctor = Depends(owner_only)):
        return alerts.test(doctor)

    @app.get("/health")
    def health():
        return {"ok": True, "store": settings.store, "files": settings.files}

    @app.post("/patients")
    def create_patient(body: PatientIn):
        return record.create_patient(name=body.name, uhid=body.uhid, sex=body.sex, age=body.age)

    @app.get("/patients")
    def find_patients(q: str = ""):
        return record.find_patients(q) if q else record.store.query(m.PATIENTS)

    @app.post("/admissions")
    def create_admission(body: AdmissionIn):
        return record.create_admission(body.patient_id, unit=body.unit, admitted_at=body.admitted_at,
                                       ward=body.ward, bed=body.bed)

    @app.post("/patients/admit")
    def new_stay(body: NewStayIn):
        try:
            patient, admission = record.create_stay(**body.model_dump())
        except ValueError as error:
            raise HTTPException(400, str(error))
        return {"patient": patient, "admission": admission, "today": build_today(admission, patient, [], [])}

    @app.get("/admissions")
    def list_admissions(status: str = "active", light: bool = False):
        from concurrent.futures import ThreadPoolExecutor
        if light:                                          # names and beds only, so the cards show at once; the full list follows
            quick = [a for a in record.store.query(m.ADMISSIONS, status=status) if not a.get("archived")]
            with ThreadPoolExecutor(max_workers=max(8, len(quick))) as pool:
                people = list(pool.map(lambda a: record.store.get(m.PATIENTS, a["patient_id"]) or {}, quick))
            open_now = open_agent_tasks()
            rows = [{"admission": a, "patient": p, "open_questions": 0,
                     "row": {"name": p.get("name") or "This patient", "age": p.get("age"), "sex": p.get("sex"), "bed": a.get("bed"), "day": None,
                             "diagnosis": [], "brief": {"lines": [p.get("name") or "This patient", "", "", ""]}, "severity": a.get("severity"),
                             "flag": None, "reason": "", "news": "", "needs": 0, "updated_at": a.get("updated_at"), **agent_lines(open_now, a["id"])}}
                    for a, p in zip(quick, people)]
            return sorted(rows, key=lambda r: r["admission"].get("updated_at") or "", reverse=True)

        import time as _t
        _t0 = _t.monotonic()

        def one(admission):
            patient = record.store.get(m.PATIENTS, admission["patient_id"]) or {}
            _s = _t.monotonic()
            try:
                return admission, patient, today_page(admission, patient, with_pages=False)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("ward row failed for %s", admission["id"])
                return admission, patient, None
        out, tasks = [], open_agent_tasks()
        _t1 = _t.monotonic()
        _list = [a for a in record.store.query(m.ADMISSIONS, status=status) if not a.get("archived")]
        _t2 = _t.monotonic()
        with ThreadPoolExecutor(max_workers=max(8, len(_list))) as pool:
            pages = list(pool.map(one, _list))
        logging.getLogger("uvicorn.error").info("WardOS timing /admissions: open_tasks %.1fs, admissions query %.1fs, %d rows built %.1fs",
                                                _t1 - _t0, _t2 - _t1, len(_list), _t.monotonic() - _t2)
        for admission, patient, page in pages:
            if page is None:
                out.append({"admission": admission, "patient": patient, "open_questions": 0,
                            "row": {"name": patient.get("name") or "This patient", "age": None, "sex": None,
                                    "bed": admission.get("bed"), "day": None, "diagnosis": [],
                                    "brief": {"lines": [patient.get("name") or "This patient",
                                                        "This page could not be built. Tell the owner.", "", ""]},
                                    "severity": admission.get("severity"), "flag": None, "reason": "",
                                    "news": "", "needs": 0, "updated_at": admission.get("updated_at"),
                                    **agent_lines(tasks, admission["id"])}})
                continue
            out.append({"admission": admission, "patient": patient, "open_questions": len(page["questions"]),
                        "row": {"name": page["patient"]["name"], "age": page["patient"]["age"],
                                "sex": page["patient"]["sex"], "bed": page["admission"]["bed"],
                                "day": page["admission"]["day"], "diagnosis": page["diagnosis"],
                                "brief": page["brief"],
                                "severity": admission.get("severity"), "flag": page["flag"]["flag"],
                                "reason": page["flag"]["reason"], "news": page["news"],
                                "needs": len(page["questions"]) + len(page["checks"]),
                                "updated_at": admission.get("updated_at"), **agent_lines(tasks, admission["id"])}})
        return sorted(out, key=lambda a: a["admission"].get("updated_at") or "", reverse=True)

    def open_agent_tasks() -> list[dict]:
        try:
            return app.state.open_tasks()
        except Exception:
            logging.getLogger("uvicorn.error").warning("WardOS: the open agent tasks could not be read")
            return []

    def agent_lines(tasks: list[dict], admission_id: str) -> dict:
        mine = [t for t in tasks if t.get("admission_id") == admission_id]
        watch = next((t for t in mine if t.get("kind") == "watch"), None)
        job = next((t for t in mine if t.get("kind") != "watch"), None)
        what = {"hospital_question": "Asking the hospital"}         # a patient card names the job, never the robot's step
        return {"watching": watch.get("test") if watch else None, "watch_id": watch.get("id") if watch else None,
                "job": what.get(job.get("kind"), "Fetching reports") if job else None, "job_id": job.get("id") if job else None}

    def today_page(admission: dict, patient: dict, *, with_pages: bool = True) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as reads:                 # three database reads at the same time, not one after another
            got_facts = reads.submit(record.facts, admission["id"])
            got_questions = reads.submit(record.questions, admission["id"])
            got_sources = reads.submit(record.store.query, m.SOURCES, admission_id=admission["id"])
            facts, questions, sources = got_facts.result(), got_questions.result(), got_sources.result()
        pages = {}
        if with_pages:
            wanted = {fid for q in questions for fid in q.get("fact_ids") or []} | \
                     {q["page_id"] for q in questions if q.get("kind") == m.Q_WRONG_PATIENT and q.get("page_id")}
            pages = {p["id"]: p for p in record.pages(admission["id"])} if wanted else {}
        return build_today(admission, patient, facts, questions, pages_by_id=pages, sources=sources)

    @app.get("/admissions/{admission_id}/today")
    def today(admission_id: str):
        admission = admission_or_404(admission_id)
        return {**today_page(admission, record.patient_of(admission_id)),
                **agent_lines(open_agent_tasks(), admission_id)}

    @app.get("/admissions/{admission_id}/record")
    def full_record(admission_id: str):
        from .record_view import record_sections
        admission = admission_or_404(admission_id)
        patient = record.patient_of(admission_id)
        return {"sections": record_sections(record.facts(admission_id), today_page(admission, patient, with_pages=False))}

    @app.get("/admissions/{admission_id}/reports")
    def reports_in_file_order(admission_id: str, day: str | None = None):
        admission_or_404(admission_id)
        facts = record.facts(admission_id)
        patient = record.patient_of(admission_id)
        sources = record.store.query(m.SOURCES, admission_id=admission_id)
        return reports_in_order(facts, day, _sex(patient, facts), sources)

    @app.patch("/admissions/{admission_id}")
    def change_bed(admission_id: str, body: BedIn, doctor: Doctor = Depends(signed_in)):
        from .beds import BedTaken
        writable_admission(admission_id)
        try:
            return record.set_bed(admission_id, doctor=doctor.name, bed=body.bed,
                                  confirm_occupied=body.confirm_occupied)
        except BedTaken as error:
            raise HTTPException(409, {"message": str(error), "occupied": [a["id"] for a in error.admissions]})
        except (RecordError, ValueError) as error:
            raise HTTPException(400, str(error))

    @app.patch("/admissions/{admission_id}/severity")
    def severity(admission_id: str, body: SeverityIn, doctor: Doctor = Depends(signed_in)):
        writable_admission(admission_id)
        try:
            return record.set_severity(admission_id, doctor=doctor.name, severity=body.severity)
        except RecordError as error:
            raise HTTPException(400, str(error))

    file_cache: dict[str, bytes] = {}
    FILE_CACHE_BYTES = 60 * 1024 * 1024

    @app.get("/sources/{source_id}/file")
    def source_file(source_id: str):
        from fastapi import Response
        try:
            source = record.source(source_id)
        except RecordError:
            raise HTTPException(404, "no such source")
        if source["kind"] == "text":
            raise HTTPException(404, "a typed note has no file")
        data = file_cache.get(source_id)
        if data is None:
            data = record.read_file(source)
            file_cache[source_id] = data
            if guest:
                file_cache.clear()
            while sum(len(v) for v in file_cache.values()) > FILE_CACHE_BYTES and len(file_cache) > 1:
                file_cache.pop(next(iter(file_cache)))
        page = lab_result_page(data) if source["kind"] == "portal_report" else None
        if page:
            return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "private, max-age=604800, immutable"})
        return Response(data, media_type=source.get("mime") or "application/octet-stream",
                        headers={"Cache-Control": "private, max-age=604800, immutable"})

    @app.post("/sources/{source_id}/retry")
    async def retry_source(source_id: str, doctor: Doctor = Depends(signed_in)):
        try:
            source = record.source(source_id)
        except RecordError:
            raise HTTPException(404, "no such source")
        await asyncio.to_thread(writable_admission, source["admission_id"])
        if source["status"] not in (m.SRC_READ, m.SRC_RECEIVED):
            raise HTTPException(409, "This photo is not waiting")
        await asyncio.to_thread(record.update_source, source_id, not_added=False)
        await queue.enqueue(source["admission_id"], [source_id], attempt=m.now_iso())
        return {"queued": 1}

    @app.get("/sources/{source_id}/pages")
    def source_pages(source_id: str):
        try:
            source = record.source(source_id)
        except RecordError:
            raise HTTPException(404, "no such source")
        return record.pages(source["admission_id"], source_id)

    @app.get("/admissions/{admission_id}")
    def overview(admission_id: str):
        admission = admission_or_404(admission_id)
        sources = record.store.query(m.SOURCES, admission_id=admission_id)
        status = {}
        for s in sources:
            status[s["status"]] = status.get(s["status"], 0) + 1
        return {"admission": admission, "patient": record.patient_of(admission_id), "sources": status,
                "not_added": sum(1 for s in sources if s["status"] == m.SRC_READ and s.get("not_added")),
                "facts": len(record.facts(admission_id)), "open_questions": len(record.questions(admission_id))}

    @app.post("/admissions/{admission_id}/uploads")
    async def upload(admission_id: str, files: list[UploadFile] = File(...),
                     doctor: Doctor = Depends(signed_in)):
        await asyncio.to_thread(writable_admission, admission_id)
        out, new, items = [], [], []
        for f in files:
            mime = (f.content_type or "").split(";")[0].lower()
            kind = MIME_KIND.get(mime)
            if not kind:
                out.append({"filename": f.filename, "error": f"not a photo or PDF ({mime or 'unknown type'})"})
                continue
            out.append(f)
            items.append(dict(kind=kind, data=await f.read(), mime=mime, filename=f.filename, author=doctor.name))
        stored = iter(await asyncio.to_thread(record.add_sources, admission_id, items))
        for i, entry in enumerate(out):
            if isinstance(entry, dict):
                continue
            source, duplicate = next(stored)
            out[i] = {"filename": entry.filename, "source_id": source["id"], "duplicate": duplicate,
                      "near_duplicate_of": source.get("near_duplicate_of")}
            if not duplicate:
                new.append(source["id"])
                saved = saved_reading(entry.filename)
                if saved:
                    await asyncio.to_thread(record.save_pages, source["id"], [{"number": 1, "text": saved}])
        if new:
            await queue.enqueue(admission_id, new)
        return {"files": out, "queued": len(new)}

    @app.post("/admissions/{admission_id}/import")
    async def import_read(admission_id: str, files: list[UploadFile] = File(...), readings: str = Form(...),
                          doctor: Doctor = Depends(signed_in)):
        import json
        await asyncio.to_thread(writable_admission, admission_id)
        texts = json.loads(readings)
        if not isinstance(texts, list) or len(texts) != len(files):
            raise HTTPException(400, "readings must be a JSON list with one text per file")
        out, new, items, readings_of = [], [], [], []
        for f, text in zip(files, texts):
            mime = (f.content_type or "").split(";")[0].lower()
            if MIME_KIND.get(mime) != "photo" or not str(text).strip():
                out.append({"filename": f.filename, "error": "needs a photo and its reading"})
                continue
            out.append(f)
            items.append(dict(kind="photo", data=await f.read(), mime=mime, filename=f.filename,
                              author=doctor.name))
            readings_of.append(str(text))

        def keep_reading(pair):
            (source, duplicate), text = pair
            if not duplicate and source["status"] == m.SRC_RECEIVED:
                record.save_pages(source["id"], [{"number": 1, "text": text}])
                record.update_source(source["id"], model="imported reading")
                return source["id"]
            return None

        def work():
            stored = record.add_sources(admission_id, items)
            return stored, record.run_parallel(keep_reading, list(zip(stored, readings_of)))
        stored, imported = await asyncio.to_thread(work)
        stored, imported = iter(stored), iter(imported)
        for i, entry in enumerate(out):
            if isinstance(entry, dict):
                continue
            source, duplicate = next(stored)
            out[i] = {"filename": entry.filename, "source_id": source["id"], "duplicate": duplicate}
            if next(imported):
                new.append(source["id"])
        if new:
            await queue.enqueue(admission_id, new)
        return {"files": out, "queued": len(new)}

    @app.post("/admissions/{admission_id}/notes")
    async def note(admission_id: str, body: NoteIn, doctor: Doctor = Depends(signed_in)):
        await asyncio.to_thread(writable_admission, admission_id)
        if not body.text.strip():
            raise HTTPException(400, "empty note")
        source, duplicate = await asyncio.to_thread(record.add_source, admission_id, "text", text=body.text, mime="text/plain",
                                                    author=doctor.name)
        if not duplicate:
            await queue.enqueue(admission_id, [source["id"]])
        return {"source_id": source["id"], "duplicate": duplicate}

    @app.post("/register/find")
    async def register_find(body: AskIn, doctor: Doctor = Depends(signed_in)):
        from . import register
        try:
            await asyncio.to_thread(register.sync, record, settings.drive_token)
            return register.card(await asyncio.to_thread(register.find, record, body.text))
        except Exception:
            raise HTTPException(503, "The old register could not be checked. Try again shortly.") from None

    @app.post("/register/{run_id}/pull-in")
    async def register_pull_in(run_id: str, doctor: Doctor = Depends(signed_in)):
        from . import register
        try:
            admission = await register.pull_in(record, run_id, doctor.name, model=get_model(), settings=settings)
        except RecordError as error:
            raise HTTPException(409, str(error)) from None
        return {"admission": admission, "old_case": True}

    last_rows: dict = {}

    @app.post("/ask")
    async def ask(body: AskIn, doctor: Doctor = Depends(signed_in)):
        if not body.text.strip():
            raise HTTPException(400, "empty ask")
        if body.direct:
            return await answer_record(body.text, body.admission_id, doctor)
        from .agent_jobs import MuseDidNotAnswer
        try:
            return await app.state.agent_command(body.text, body.admission_id, doctor, body.reply_to)
        except MuseDidNotAnswer as silent:
            raise HTTPException(502, str(silent)) from None

    async def answer_record(text: str, admission_id: str | None, doctor: Doctor) -> dict:
        from . import ask as asking
        body = AskIn(text=text, admission_id=admission_id)
        result = await asyncio.to_thread(asking.ask, record, body.text, body.admission_id, last_rows=last_rows)
        card = result["card"]
        if guest and result["intent"]["action"] == "old_patient":
            from .demo import OLD_CASES
            result["card"] = {"kind": "message", "title": "Old patients", "patient": None, "text": OLD_CASES}
            return result
        if result["intent"]["action"] == "old_patient":
            result["card"] = await register_find(body, doctor)
            return result
        if not guest and result["intent"]["how"] in ("none", "current") and card["kind"] in ("ai", "message"):
            from . import register
            try:
                found = await asyncio.to_thread(register.name_only_lookup, record, body.text)
            except Exception:
                found = []
            if found:
                result["intent"]["action"] = "old_patient"
                result["card"] = register.card(found, not_on_ward=True)
                return result
            if result["intent"]["action"] == "unknown" and card["kind"] == "message" and card.get("text") == asking.HELP:
                result["card"] = {"kind": "message", "title": "Not found", "patient": None,
                                  "text": f"Nobody on your ward or in the old patients matches that. Nothing was made up. {asking.HELP}"}
                return result
        if card["kind"] == "ai":
            from .askai import answer_anything
            try:
                card = await answer_anything(record, ask_model or get_model(), settings.model, card["patient"], card["question"])
            except Exception as error:
                card = {"kind": "message", "title": "Answer", "patient": card["patient"],
                        "text": f"The answer did not come ({type(error).__name__}). Try again."}
            result["card"] = card
        if card["kind"] == "note":
            await asyncio.to_thread(writable_admission, card["patient"]["id"])
            source, duplicate = await asyncio.to_thread(record.add_source, card["patient"]["id"], "text", text=card.pop("note_text"),
                                                        mime="text/plain", author=doctor.name)
            if not duplicate:
                await queue.enqueue(card["patient"]["id"], [source["id"]])
            card.update(kind="message", source_id=source["id"],
                        text=card["text"] if not duplicate else "That note is already in the record.")
        asking.remember_row(card, last_rows)
        return result

    app.state.answer_record = answer_record

    async def recording(file: UploadFile) -> tuple[bytes, str]:
        mime = (file.content_type or "").split(";")[0].strip().lower()
        if mime not in VOICE_MIMES:
            raise HTTPException(400, "Choose a voice recording")
        mime = "audio/wav" if mime in ("audio/x-wav", "audio/wave") else mime
        data = await file.read(VOICE_MAX_BYTES + 1)
        if len(data) > VOICE_MAX_BYTES:
            raise HTTPException(413, "the recording is larger than 10 MB")
        if not data:
            raise HTTPException(400, "empty recording")
        return data, mime

    @app.post("/voice/transcribe")
    async def voice_words(file: UploadFile = File(...), doctor: Doctor = Depends(signed_in)):
        from .voice import transcribe
        data, mime = await recording(file)
        try:
            text, usage = await transcribe(get_model(), data, mime, model_id=settings.model)
        except Exception:
            raise HTTPException(503, "Could not write down the recording. Try again.") from None
        return {"text": text, "cost_inr": usage.cost_inr}

    @app.post("/admissions/{admission_id}/voice")
    async def voice_note(admission_id: str, file: UploadFile = File(...), doctor: Doctor = Depends(signed_in)):
        await asyncio.to_thread(writable_admission, admission_id)
        mime = (file.content_type or "").split(";")[0].strip().lower()
        if mime not in VOICE_MIMES:
            raise HTTPException(400, f"not a voice recording ({mime or 'unknown type'})")
        mime = "audio/wav" if mime in ("audio/x-wav", "audio/wave") else mime
        data = await file.read(VOICE_MAX_BYTES + 1)
        if len(data) > VOICE_MAX_BYTES:
            raise HTTPException(413, "the recording is larger than 10 MB")
        if not data:
            raise HTTPException(400, "empty recording")
        try:
            source, duplicate = await asyncio.to_thread(record.add_source, admission_id, "voice", data=data,
                                                       mime=mime, filename=file.filename, author=doctor.name)
        except Exception:
            raise HTTPException(503, "The recording could not be stored. Try saving it again.") from None
        if not duplicate:
            await queue.enqueue(admission_id, [source["id"]])
        return {"source_id": source["id"], "duplicate": duplicate}

    @app.post("/admissions/{admission_id}/reports")
    async def portal_report(admission_id: str, file: UploadFile = File(...), portal: str | None = Form(None),
                            caller=Depends(any_caller)):
        from .agent_tasks import AGENT_AUTHOR, PORTAL_LABELS
        await asyncio.to_thread(writable_admission, admission_id)
        before = await asyncio.to_thread(alert_snapshot, admission_id)
        by_agent = bool(portal) or not isinstance(caller, Doctor)
        source, duplicate = await asyncio.to_thread(record.add_source, admission_id, "portal_report", data=await file.read(),
                                                    mime="application/pdf", filename=file.filename,
                                                    author=AGENT_AUTHOR if by_agent else caller.name)
        if not duplicate:
            if by_agent:
                site = portal or "ehospital"
                await asyncio.to_thread(record.update_source, source["id"], portal=site,
                                        label=PORTAL_LABELS.get(site, f"{site} report"))
            await queue.enqueue(admission_id, [source["id"]])
            await asyncio.to_thread(notify, admission_id, before, [] if by_agent else [source["id"]])
        return {"source_id": source["id"], "duplicate": duplicate}

    @app.get("/admissions/{admission_id}/timeline")
    def timeline(admission_id: str):
        admission_or_404(admission_id)
        return record.timeline(admission_id)

    @app.get("/admissions/{admission_id}/facts")
    def facts(admission_id: str, type: str | None = None):
        admission_or_404(admission_id)
        return [f for f in record.facts(admission_id) if not type or f["type"] == type]

    @app.get("/admissions/{admission_id}/questions")
    def questions(admission_id: str, status: str = "open"):
        admission_or_404(admission_id)
        return record.questions(admission_id, status=status)

    @app.get("/admissions/{admission_id}/medicines")
    def medicines(admission_id: str):
        admission_or_404(admission_id)
        return record.medicines(admission_id)

    @app.get("/admissions/{admission_id}/changes")
    def changes(admission_id: str, since: int = 0):
        admission_or_404(admission_id)
        return record.changes_since(admission_id, since)

    @app.get("/admissions/{admission_id}/events")
    def events(admission_id: str, since: str | None = None):
        admission_or_404(admission_id)
        return record.events(admission_id, since)

    @app.patch("/facts/{fact_id}")
    def change_fact(fact_id: str, body: FactChange, doctor: Doctor = Depends(signed_in)):
        fact = record.store.get(m.FACTS, fact_id)
        if fact:
            writable_admission(fact["admission_id"])
        if body.action not in ("correct", "confirm", "dismiss"):
            raise HTTPException(400, "action must be correct, confirm or dismiss")
        if body.action == "correct" and not body.text:
            raise HTTPException(400, "a correction needs text")
        try:
            if body.action == "correct":
                return record.correct_fact(fact_id, doctor=doctor.name, text=body.text, number=body.number,
                                           unit=body.unit)
            if body.action == "confirm":
                return record.confirm_fact(fact_id, doctor=doctor.name)
            return record.dismiss_fact(fact_id, doctor=doctor.name, reason=body.reason or "")
        except RecordError as error:
            raise HTTPException(404, str(error))

    @app.post("/questions/{question_id}/answer")
    async def answer(question_id: str, body: AnswerIn, doctor: Doctor = Depends(signed_in)):
        question = await asyncio.to_thread(record.store.get, m.QUESTIONS, question_id)
        if question:
            await asyncio.to_thread(writable_admission, question["admission_id"])
        held_page = bool(question and question.get("kind") == m.Q_WRONG_PATIENT and question.get("status") == "open")
        before = await asyncio.to_thread(alert_snapshot, question["admission_id"]) if held_page else None
        try:
            out = await asyncio.to_thread(record.answer_question, question_id, doctor=doctor.name, answer=body.answer,
                                          fact_id=body.fact_id)
        except RecordError as error:
            raise HTTPException(400 if "not part" in str(error) else 404, str(error))
        if held_page and out.get("added"):
            await asyncio.to_thread(notify, question["admission_id"], before)
        return out

    @app.post("/admissions/{admission_id}/reviewed")
    def reviewed(admission_id: str, doctor: Doctor = Depends(signed_in)):
        writable_admission(admission_id)
        return record.mark_reviewed(doctor.name, admission_id)

    @app.get("/admissions/{admission_id}/since-review")
    def since_review(admission_id: str, doctor: Doctor = Depends(signed_in)):
        admission_or_404(admission_id)
        return record.since_last_review(doctor.name, admission_id)

    @app.get("/admissions/{admission_id}/draft")
    def draft(admission_id: str):
        admission_or_404(admission_id)
        return {"text": summary_text(record, admission_id)}

    @app.post("/admissions/{admission_id}/draft")
    async def refresh_draft(admission_id: str):
        await asyncio.to_thread(writable_admission, admission_id)
        return await update_summary(record, get_model(), admission_id, model_id=settings.model)

    @app.post("/admissions/{admission_id}/final")
    async def make_final(admission_id: str):
        await asyncio.to_thread(writable_admission, admission_id)
        if not summary_configured:
            raise HTTPException(503, "summary service not configured")
        current = await asyncio.to_thread(finals.latest_final, record, admission_id)
        if current and current["status"] == "running":
            current = await asyncio.to_thread(finals.final_status, record, current["id"],
                                              drive_creds=get_drive_creds())
            if current["status"] == "running":
                raise HTTPException(409, "a final summary is already being made for this admission")
        if guest and len(record.store.query(m.FINALS)) >= DEMO_FINALS_PER_COPY:
            raise HTTPException(429, f"The demo makes up to {DEMO_FINALS_PER_COPY} summaries per visit.")
        try:
            return await asyncio.to_thread(
                finals.start_final, record, admission_id, drive_creds=get_drive_creds(),
                tests_folder_id=final_tests_folder, records_folder_id=final_records_folder,
                job_runner=get_job_runner())
        except finals.NoPhotos as error:
            raise HTTPException(409, str(error))
        except Exception as error:
            raise HTTPException(502, f"could not start the final summary: {error}"[:300])

    @app.post("/admissions/{admission_id}/final/cancel")
    async def cancel_final(admission_id: str):
        admission_or_404(admission_id)
        current = await asyncio.to_thread(finals.latest_final, record, admission_id)
        if current and current["status"] == "running":
            current = {**current, "status": "stopped", "finished_at": m.now_iso()}
            await asyncio.to_thread(record.store.put, m.FINALS, current)
        return current

    @app.get("/admissions/{admission_id}/final")
    async def get_final(admission_id: str):
        admission_or_404(admission_id)
        current = await asyncio.to_thread(finals.latest_final, record, admission_id)
        if current and current["status"] == "running":
            current = await asyncio.to_thread(finals.final_status, record, current["id"],
                                              drive_creds=get_drive_creds())
        return current

    @app.post("/internal/process")
    async def process(body: ProcessIn):
        await asyncio.to_thread(writable_admission, body.admission_id)
        return await run_batch(body.admission_id, body.source_ids)

    return app


def __getattr__(name):
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)

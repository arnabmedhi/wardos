"""Try the demo: every guest gets a private copy of the made-up demo ward that resets itself."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel

from . import model as m
from .agent_jobs import Conversations, portal_in_use, run_jobs
from .agent_words import plain_test
from .auth import Auth, Doctor, Machine, any_caller, signed_in
from .normalize import fact_key
from .settings import AuthSettings

GUEST_COOKIE = "wardos_guest"
GUEST_SECONDS = 7 * 24 * 3600
WARM_TARGET = int(os.environ.get("WARDOS_DEMO_WARM") or 0)           # ready-made demo wards kept waiting (0 = off)
WARM_FRESH_MIN = 90
WRITE_COOKIE = "wardos_gw"
GUESTS, TASKS, STATUS, BLOBS = "guests", "tasks", "status", "guest_files"
GUEST_EMAIL = "guest@demo.wardos"
GUEST_NAME = "Demo doctor"
OLD_CASES = "Old patients are not in the demo."
FINAL = "The discharge summary is shown in the video."
NOT_IN_DEMO = "This is not part of the demo."
REFUSED = ((re.compile(r"^/register(?:/|$)"), OLD_CASES), (re.compile(r"^/admissions/[^/]+/final$"), FINAL),
           (re.compile(r"^/doctors(?:/|$)"), NOT_IN_DEMO), (re.compile(r"^/push/test$"), NOT_IN_DEMO),
           (re.compile(r"^/internal(?:/|$)"), NOT_IN_DEMO), (re.compile(r"^/agent/worker(?:/|$)"), NOT_IN_DEMO),
           (re.compile(r"^/agent/watches/[^/]+/"), NOT_IN_DEMO),
           (re.compile(r"^/admissions/[^/]+/report-values$"), NOT_IN_DEMO))
ID_RE = re.compile(r"\b[a-z]+_[0-9a-f]{16}\b")
BLOB_CHUNK = 700_000
ALL_GUEST_COLLECTIONS = (*m.ALL_COLLECTIONS, "report_files", BLOBS, "notification_outbox", "push_sent")
PHOTO_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _now(clock) -> datetime:
    return datetime.fromtimestamp(clock(), timezone.utc)


def _clock_text(iso: str) -> str:
    try:
        from ..config import HOSPITAL_TZ
        return datetime.fromisoformat(iso).astimezone(HOSPITAL_TZ).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def watch_alert(test: str | None, steps: list[str]) -> dict | None:
    """A demo watch's alert, in the shape a real watch gives, read from its newest DANGER line."""
    for line in reversed(steps):
        hit = re.search(r"DANGER · (.+?) (\d[\d.]*)([^·]*)(.*)", line)
        if not hit:
            continue
        parts = [p.strip(" ·") for p in hit.group(4).split(" · ")]
        after = lambda word: next((p[len(word):].strip() for p in parts if p.startswith(word)), None)
        return {"test": test, "key": fact_key("lab_result", hit.group(1)), "label": hit.group(1), "value": hit.group(2),
                "unit": hit.group(3).strip() or None, "normal": after("normal "), "time": after("collected "),
                "earlier": [{"value": value.strip(), "time": when}
                            for value, when in re.findall(r"([^,]+?) \(([^()]*)\)", after("earlier:") or "")]}
    return None


class DemoFiles:
    scheme = "demo-blob:"

    def __init__(self, store, photos: Path):
        from ..hospital.demo_record import DemoTextFiles
        self.store, self.photos, self.text = store, Path(photos), DemoTextFiles(store)

    def save(self, parts, name, data):
        import base64
        ident = m.stable_id("blob", *parts, name)
        coded = base64.b64encode(data).decode()
        pieces = [coded[i:i + BLOB_CHUNK] for i in range(0, len(coded), BLOB_CHUNK)] or [""]
        for n, piece in enumerate(pieces):
            self.store.put(BLOBS, {"id": f"{ident}-{n}", "data": piece, "n": n, "total": len(pieces)})
        return self.scheme + ident

    def read(self, uri):
        import base64
        if uri.startswith("demo-photo:"):
            name = uri[len("demo-photo:"):]
            if not PHOTO_NAME.match(name):
                raise ValueError("Not a demo photo")
            return (self.photos / name).read_bytes()
        if uri.startswith("demo-text:"):
            return self.text.read(uri)
        if uri.startswith(self.scheme):
            ident, data, n = uri[len(self.scheme):], [], 0
            while True:
                row = self.store.get(BLOBS, f"{ident}-{n}")
                if not row:
                    raise ValueError("Demo file not found")
                data.append(row["data"])
                n += 1
                if n >= row["total"]:
                    return base64.b64decode("".join(data))
        raise ValueError("Only demo files can be opened here")


class DemoAskModel:

    def __init__(self, model):
        self.model = model

    async def generate(self, prompt, *, model=None, max_tokens=8192, as_json=True, timeout=240, hedge_after=None, **kw):
        return await self.model.generate(prompt, max_tokens=max_tokens, as_json=as_json, timeout=max(timeout, 60))


class LazyModel:
    def __init__(self):
        self._client = None

    def __getattr__(self, name):
        if self.__dict__.get("_client") is None:
            from ..config import load_settings
            from ..models import get_model_client
            self._client = get_model_client(load_settings())
        return getattr(self._client, name)


class GuestAuth(Auth):
    guest = True

    def __init__(self, store, final_ok: bool = False):
        super().__init__(AuthSettings(cookie_secret="", cookie_secure=True), store)
        self.identity = Doctor(GUEST_EMAIL, GUEST_NAME, "doctor")
        self.final_ok = final_ok

    def doctor_for(self, email, fallback_name=""):
        return self.identity if email == GUEST_EMAIL else None

    def gate(self, request):
        path = request.url.path
        for pattern, line in REFUSED:
            if pattern.match(path) and not (self.final_ok and line == FINAL):
                return None, JSONResponse({"detail": line}, status_code=403)
        return self.identity, None


class TaskIn(BaseModel):
    text: str = ""
    admission_id: str | None = None


class AlertIn(BaseModel):
    line: str


class GuestIn(BaseModel):
    guest: str


class GuestProcessIn(GuestIn):
    admission_id: str
    source_ids: list[str]


@dataclass
class Copy:
    app: FastAPI
    cache: object
    seen: str = ""


class GuestCloudQueue:
    def __init__(self, inner, guest):
        self.inner, self.guest = inner, guest

    async def enqueue(self, admission_id, source_ids, *, attempt=""):
        await self.inner.enqueue(admission_id, source_ids, attempt=attempt, path="/internal/demo-process", extra={"guest": self.guest})


class DemoSite:
    def __init__(self, *, auth: Auth, settings, base, data_dir: Path, model=None, ask_model=None, queue=None,
                 start_task=None, clock=time.time, idle_minutes: float | None = None, max_apps: int = 24):
        self.auth, self.settings, self.base, self.data_dir, self.clock = auth, settings, base, Path(data_dir), clock
        self.model = model if model is not None else LazyModel()
        self.ask_model = ask_model if ask_model is not None else DemoAskModel(self.model)
        self.queue, self.start_task = queue, start_task
        self.idle = timedelta(minutes=idle_minutes if idle_minutes is not None
                              else float(os.environ.get("WARDOS_DEMO_RESET_IDLE_MIN") or 30))
        self.max_apps = max_apps
        self._apps: OrderedDict[str, Copy] = OrderedDict()
        self._lock = threading.Lock()
        self._guest_locks: dict[str, threading.Lock] = {}
        self._warm_lock = threading.Lock()
        self._checked: dict[str, float] = {}
        self._generation: dict[str, int] = {}
        self._template = None
        self._cookie = URLSafeSerializer(auth.settings.cookie_secret, salt="wardos-guest") if auth.settings.cookie_secret else None

    def guest_of(self, request: Request) -> str | None:
        if self._cookie is None:
            return None
        try:
            data = self._cookie.loads(request.cookies.get(GUEST_COOKIE) or "")
            if isinstance(data, dict) and self.clock() - float(data["iat"]) <= GUEST_SECONDS:
                return str(data["g"])
        except (BadSignature, KeyError, TypeError, ValueError):
            pass
        return None

    def new_cookie(self, guest: str | None = None) -> str:
        return self._cookie.dumps({"g": guest or secrets.token_urlsafe(18), "iat": self.clock()})

    # Ready-made demo wards: copying a ward (6,000+ records) takes ~16 s, so a few are built ahead and a new visitor is handed one.
    def _fresh_pool(self) -> list[dict]:
        now = _now(self.clock)
        return [r for r in self.base.query(GUESTS, pool=True)
                if r.get("built_at") and now - datetime.fromisoformat(r["built_at"]) <= timedelta(minutes=WARM_FRESH_MIN)]

    def claim_warm(self) -> str | None:
        now = _now(self.clock)
        for row in self._fresh_pool():
            def take(tx, guest=row["id"]):
                current = tx.get(GUESTS, guest)
                if not current or not current.get("pool"):
                    return False
                tx.put(GUESTS, {**current, "pool": False, "last_used": now.isoformat()})
                return True
            if self.base.transact(take):
                return row["id"]
        return None

    def top_up(self) -> int:
        """Build ready wards until WARM_TARGET are waiting. Returns how many were built."""
        if not self._warm_lock.acquire(blocking=False):
            return 0
        built = 0
        try:
            for _ in range(WARM_TARGET - len(self._fresh_pool())):
                guest = "w" + secrets.token_urlsafe(12)
                now = _now(self.clock)
                self.base.put(GUESTS, {"id": guest, "guest": guest, "generation": 1, "last_used": now.isoformat(),
                                       "building_until": (now + timedelta(seconds=300)).isoformat()})
                self._replace(guest, 1)
                self.base.put(GUESTS, {"id": guest, "guest": guest, "generation": 1, "last_used": _now(self.clock).isoformat(),
                                       "building_until": None, "pool": True, "built_at": _now(self.clock).isoformat()})
                built += 1
        except Exception:
            pass
        finally:
            self._warm_lock.release()
        return built

    def wants(self, request: Request) -> str | None:
        path = request.url.path
        if path in ("/demo/start", "/demo/status", "/auth/login", "/auth/callback") or request.headers.get("authorization"):
            return None
        guest = self.guest_of(request)
        if guest is None or self.auth._doctor_from_cookie(request.cookies.get("wardos_session")) is not None:
            return None
        return guest

    async def serve(self, guest: str, scope, receive, send):
        request = Request(scope, receive)
        if request.url.path == "/auth/logout":
            response = RedirectResponse("/", status_code=302)
            response.delete_cookie(GUEST_COOKIE, path="/")
            return await response(scope, receive, send)
        static = request.url.path in ("/", "/sw.js", "/manifest.webmanifest", "/auth/me", "/openapi.json", "/health") \
            or request.url.path.startswith("/icons/")
        was_reset = False
        _t0 = time.monotonic()
        if not static:
            try:
                was_reset = await asyncio.to_thread(self.ensure_copy, guest)
            except HTTPException as error:
                return await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)
        _t1 = time.monotonic()
        copy = await asyncio.to_thread(self.app_of, guest)
        if time.monotonic() - _t0 > 2:
            logging.getLogger("uvicorn.error").info("WardOS timing guest %s: ensure_copy %.1fs, app build %.1fs", request.url.path, _t1 - _t0, time.monotonic() - _t1)
        if was_reset:
            copy.cache.clear()
        token = request.cookies.get(WRITE_COOKIE) or ""
        if token and token != copy.seen:
            copy.cache.clear()
            copy.seen = token
        writes = request.method not in ("GET", "HEAD", "OPTIONS")

        async def send_marked(message):
            if was_reset and message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"x-wardos-demo-reset", b"1")]}
            if writes and message["type"] == "http.response.start":
                stamp = Response()
                copy.seen = secrets.token_urlsafe(9)
                self.auth._set_cookie(stamp, WRITE_COOKIE, copy.seen, GUEST_SECONDS)
                message = {**message, "headers": [*message.get("headers", []), *stamp.raw_headers[-1:]]}
            await send(message)
        await copy.app(scope, receive, send_marked)

    def app_of(self, guest: str) -> Copy:
        with self._lock:
            copy = self._apps.get(guest)
            if copy:
                self._apps.move_to_end(guest)
                return copy
        copy = self._build_app(guest)
        with self._lock:
            copy = self._apps.setdefault(guest, copy)
            while len(self._apps) > self.max_apps:
                self._apps.popitem(last=False)
        return copy

    def _build_app(self, guest: str) -> Copy:
        from .api import create_app
        from .docstore import CachedDocStore
        from .record import Record
        from ..hospital.demo_record import GuestStore
        scoped = GuestStore(self.base, guest)
        cached = CachedDocStore(scoped, ttl=float(os.environ.get("WARDOS_CHAIN_CACHE_SECONDS") or 10))
        cached.guest = guest
        files = DemoFiles(scoped, self.data_dir / "photos")
        safe = replace(self.settings, drive_token=Path("/nonexistent"), drive_root="")
        queue = GuestCloudQueue(self.queue, guest) if self.queue is not None else None

        def never(*args, **kwargs):
            raise RuntimeError("Not available in the demo")
        job_runner, drive_creds, folders = never, never, None
        final_folder = os.environ.get("WARDOS_DEMO_FINAL_FOLDER_ID")
        if final_folder and self.final_creds() is not None:
            from .finals import cloud_job_runner
            job_runner = cloud_job_runner(self.settings.project, os.environ.get("WARDOS_FINAL_REGION", "asia-south1"),
                                          os.environ.get("WARDOS_FINAL_JOB", "wardos-final-summary"))
            drive_creds, folders = self.final_creds(), (final_folder, final_folder)
        app = create_app(record=Record(cached, files), model=self.model, settings=safe, queue=queue, job_runner=job_runner,
                         drive_creds=drive_creds, auth=GuestAuth(cached, final_ok=folders is not None), guest=True,
                         ask_model=self.ask_model, final_folders=folders)
        self._install_guest_routes(app, guest, cached)
        return Copy(app, cached)

    def final_creds(self):
        """The demo summaries are written by the service's Drive login into one private demo folder (never a doctor's)."""
        if not hasattr(self, "_final_creds"):
            try:
                from google.oauth2.credentials import Credentials
                self._final_creds = Credentials.from_authorized_user_file(
                    str(self.settings.drive_token), ["https://www.googleapis.com/auth/drive"])
            except Exception:
                self._final_creds = None
        return self._final_creds

    def template(self) -> dict:
        if self._template is None:
            self._template = json.loads((self.data_dir / "template.json").read_text(encoding="utf-8"))
        return self._template

    def _lock_of(self, guest: str) -> threading.Lock:
        with self._lock:
            if len(self._guest_locks) > 2000:
                self._guest_locks.clear()
            return self._guest_locks.setdefault(guest, threading.Lock())

    def ensure_copy(self, guest: str) -> bool:
        checked = self._checked.get(guest)
        if checked is not None and self.clock() - checked < 30:
            return False
        with self._lock_of(guest):
            for _ in range(240):
                action, generation = self._claim(guest)
                if action == "wait":
                    time.sleep(0.5)
                    continue
                if action == "build":
                    self._replace(guest, generation)
                seen = self._generation.get(guest)
                self._generation[guest], self._checked[guest] = generation, self.clock()
                return seen is not None and seen != generation
        raise HTTPException(503, "The demo ward is still being made. Try again in a moment.")

    def _robot_busy(self, guest: str, now: datetime) -> bool:
        for status in ("working", "waiting"):
            for task in self.base.query(TASKS, guest=guest, status=status):
                if now - datetime.fromisoformat(task["updated_at"]) < timedelta(minutes=20):
                    return True
        return False

    def _claim(self, guest: str) -> tuple[str, int]:
        now = _now(self.clock)
        busy = self._robot_busy(guest, now)

        def work(tx):
            row = tx.get(GUESTS, guest)
            if row and row.get("building_until"):
                if datetime.fromisoformat(row["building_until"]) > now:
                    return "wait", 0
            elif row and (busy or now - datetime.fromisoformat(row["last_used"]) < self.idle):
                if now - datetime.fromisoformat(row["last_used"]) > timedelta(seconds=60):
                    tx.put(GUESTS, {**row, "last_used": now.isoformat()})
                return "ready", row["generation"]
            generation = int((row or {}).get("generation") or 0) + 1
            tx.put(GUESTS, {"id": guest, "guest": guest, "generation": generation, "last_used": now.isoformat(),
                            "building_until": (now + timedelta(seconds=120)).isoformat()})
            return "build", generation
        return self.base.transact(work)

    def _replace(self, guest: str, generation: int) -> None:
        from .bulk import bulk_delete, bulk_put
        old, old_admissions = [], []
        if generation > 1:
            for collection in ALL_GUEST_COLLECTIONS:
                old.extend((collection, d["id"]) for d in self.base.query(collection, guest=guest))
            old_admissions = [d for d in self.base.query(m.ADMISSIONS, guest=guest)]
        fresh = make_copy(self.template(), guest, generation)
        bulk_put(self.base, [(m.ADMISSIONS, {**a, "status": "retired"}) for a in old_admissions])
        bulk_put(self.base, fresh)
        new_ids = {(c, d["id"]) for c, d in fresh}
        bulk_delete(self.base, [pair for pair in old if pair not in new_ids])
        now = _now(self.clock).isoformat()
        self.base.put(GUESTS, {"id": guest, "guest": guest, "generation": generation, "last_used": now, "building_until": None})

    def _install_guest_routes(self, app: FastAPI, guest: str, store) -> None:
        def view(task: dict) -> dict:
            steps = list(task.get("steps") or [])
            if task["status"] == "waiting" and not steps:
                steps = ["Waiting for the demo robot"]
            kind = {"watch": "watch", "hospital_question": "hospital_question"}.get(task["kind"], "fetch")
            created = task.get("created_at") or ""
            return {"id": task["id"], "kind": kind, "status": task["status"], "alert": watch_alert(task.get("test"), steps),
                    "active": task["status"] in ("waiting", "working"), "steps": steps,
                    "title": f"Watching bed {task['bed']}: {task.get('test') or 'potassium'}" if task["kind"] == "watch" else "",
                    "admission_id": task.get("admission_id"), "bed": task.get("bed"), "test": task.get("test"),
                    "job": {"watch": "Watch", "hospital_question": "Hospital question"}.get(task["kind"], "Fetch lab reports"),
                    "patient_label": f"Bed {task['bed']}" if task.get("bed") else "Ward", "question": task.get("question"),
                    "answer": task.get("answer"), "skill": task.get("skill"), "skill_state": task.get("skill_state"),
                    "created_at": created, "time": _clock_text(created), "updated_at": task.get("updated_at"),
                    "checks": task.get("checks"), "last_check_at": task.get("last_check_at")}

        def own_task(task_id: str) -> dict:
            task = self.base.get(TASKS, task_id)
            if not task or task.get("guest") != guest:
                raise HTTPException(404, "no such task")
            return task

        conversations = Conversations()

        async def command(text, admission_id, doctor, reply_to=None):
            from types import SimpleNamespace
            from .agent_words import ward_list
            ward = await asyncio.to_thread(ward_list, SimpleNamespace(store=store), m=m)
            rows = {str(a.get("bed")): a for a in await asyncio.to_thread(store.query, m.ADMISSIONS, status="active")}
            mine = await asyncio.to_thread(self.base.query, TASKS, guest=guest)
            running = [{"kind": t.get("kind"), "bed": t.get("bed"), "test": t.get("test"), "question": t.get("question")}
                       for t in mine if t["status"] in ("waiting", "working")]

            def say(line):
                return {"answer": {"kind": "message", "title": "Demo robot", "patient": None, "text": line}}

            async def execute(job, words):
                kind = job["kind"]
                if kind == "cannot":
                    return {"error": job["text"] or "I can't do that. I never order, send or change anything on the hospital site."}
                if kind == "record_question":
                    row = rows.get(str(job["bed"])) if job["bed"] else None
                    if job["bed"] and row is None:
                        return {"error": f"There is no bed {job['bed']} on this ward. Nothing was opened."}
                    question = job["text"] or words
                    if job["bed"] and str(job["bed"]) not in re.findall(r"\d+", question):
                        question += f" of bed {job['bed']}"
                    answered = await self_app.state.answer_record(question, row["id"] if row else admission_id, doctor)
                    return {"answer": answered["card"], "intent": answered["intent"]}
                if kind == "list_watches":
                    active = [t for t in mine if t["kind"] == "watch" and t["status"] in ("waiting", "working")]
                    if active:
                        return say(f"I am watching bed {active[0]['bed']}'s {active[0].get('test') or 'potassium'}. "
                                   "The demo watch stops by itself after ten minutes.")
                    return say("I am not watching anything now. Try: watch bed 12's potassium.")
                if kind == "stop_watch":
                    return say("The demo watch stops by itself after ten minutes.")
                if kind == "hospital_question":
                    question = " ".join((job["text"] or words).split())[:300]
                    task = await asyncio.to_thread(self.create_question, guest, question)
                    return {"task": view(task), "intent": {"action": "agent_hospital_question", "how": "none", "patient_id": None}}
                target = rows.get(str(job["bed"])) if job["bed"] else next((a for a in rows.values() if a["id"] == admission_id), None)
                if not target:
                    return {"error": "Say which bed: the robot works on beds 11 to 20."}
                if not target.get("demo_bed"):
                    return {"error": "In the demo the robot works on the ten patients on the demo hospital site: beds 11 to 20."}
                test = plain_test(job["test"])
                if kind == "watch" and not test:
                    return {"error": "Which test should I watch? Say it, for example: watch bed 12 potassium."}
                task = await asyncio.to_thread(self.create_task, guest, target, kind, test)
                return {"task": view(task), "intent": {"action": "agent", "how": "current", "patient_id": target["id"]}}

            return await run_jobs(text, admission_id, owner=guest, ward=ward, model=self.model, model_id=self.settings.model,
                                  portal=portal_in_use("openmrs"), running=running, conversations=conversations, reply_to=reply_to,
                                  execute=execute)

        self_app = app
        app.state.agent_command = command
        app.state.open_tasks = lambda: [view(t) for t in self.base.query(TASKS, guest=guest)
                                        if t["status"] in ("waiting", "working")]

        @app.get("/agent/tasks/{task_id}")
        def agent_task(task_id: str, doctor: Doctor = Depends(signed_in)):
            self.deliver(guest)
            return view(own_task(task_id))

        @app.get("/agent/tasks")
        def agent_task_list(limit: int = 30, doctor: Doctor = Depends(signed_in)):
            self.deliver(guest)
            rows = sorted(self.base.query(TASKS, guest=guest), key=lambda t: t.get("created_at") or "", reverse=True)
            return {"tasks": [view(t) for t in rows[:max(1, min(limit, 100))]]}

        @app.get("/agent/watches")
        def agent_watches(admission_id: str | None = None, include_ended: bool = False, doctor: Doctor = Depends(signed_in)):
            rows = [view(t) for t in self.base.query(TASKS, guest=guest, kind="watch") if t["status"] in ("waiting", "working")]
            return {"watches": rows}

        # The robot writes to the store directly, so this copy's short cache cannot know. The page calls this when a watch's alert
        # arrives: being a POST it also renews the write cookie, which empties the cache on the other servers of this site too.
        @app.post("/agent/tasks/{task_id}/fresh")
        def agent_fresh(task_id: str, doctor: Doctor = Depends(signed_in)):
            own_task(task_id)
            store.clear()
            return {"ok": True}

        @app.post("/agent/tasks/{task_id}/stop")
        def agent_stop(task_id: str, doctor: Doctor = Depends(signed_in)):
            task = own_task(task_id)
            if task["status"] in ("waiting", "working"):      # Cancel: stops at once; the robot ignores a task that is no longer open
                task = {**task, "status": "stopped", "updated_at": _now(self.clock).isoformat(), "steps": list(task.get("steps") or []) + ["Cancelled"]}
                self.base.put(TASKS, task)
            return view(task)

    def create_question(self, guest: str, question: str) -> dict:
        from ..hospital.demo_tasks import start_demo_task
        running = [t for t in self.base.query(TASKS, guest=guest, kind="hospital_question")
                   if t.get("status") in ("waiting", "working") and t.get("question") == question]
        if running:
            return running[0]
        now = _now(self.clock).isoformat()
        task = {"id": "task_" + uuid4().hex, "guest": guest, "kind": "hospital_question", "question": question,
                "status": "waiting", "steps": [], "created_at": now, "updated_at": now}
        self.base.put(TASKS, task)
        start = self.start_task if self.start_task is not None else (start_demo_task if os.environ.get("K_SERVICE") else None)
        if start is None:
            return task
        try:
            operation = start(task["id"])
            current = self.base.get(TASKS, task["id"]) or task
            self.base.put(TASKS, {**current, "operation": operation})
            return {**current, "operation": operation}
        except Exception:
            task = {**task, "status": "failed", "steps": ["The demo robot could not start. Please try again."]}
            self.base.put(TASKS, task)
            return task

    def create_task(self, guest: str, admission: dict, kind: str, test: str | None = None) -> dict:
        from ..hospital.demo_tasks import start_demo_task
        bed = str(admission["demo_bed"])
        running = [t for t in self.base.query(TASKS, guest=guest, admission_id=admission["id"])
                   if t.get("status") in ("waiting", "working") and t.get("kind") == kind
                   and ((kind != "watch" and not test) or t.get("test") == test)]
        if running:
            return running[0]
        now = _now(self.clock).isoformat()
        task = {"id": "task_" + uuid4().hex, "guest": guest, "admission_id": admission["id"], "bed": bed, "kind": kind,
                "test": (test or "potassium") if kind == "watch" else (test or None), "status": "waiting", "steps": [], "created_at": now,
                "updated_at": now}
        self.base.put(TASKS, task)
        start = self.start_task if self.start_task is not None else (start_demo_task if os.environ.get("K_SERVICE") else None)
        if start is None:
            return task
        try:
            task = {**task, "operation": start(task["id"])}
            current = self.base.get(TASKS, task["id"]) or task
            self.base.put(TASKS, {**current, "operation": task["operation"]})
            return {**current, "operation": task["operation"]}
        except Exception:
            task = {**task, "status": "failed", "steps": ["The demo robot could not start. Please try again."]}
            self.base.put(TASKS, task)
            return task

    def deliver(self, guest: str) -> int:
        sent = 0
        copy = self.app_of(guest)
        alerts = copy.app.state.alerts
        if not alerts.settings.configured:
            return 0
        rows = self.base.query("notification_outbox", guest=guest, status="waiting")
        for row in rows:
            def claim(tx):
                current = tx.get("notification_outbox", row["id"])
                if not current or current.get("status") != "waiting" or current.get("guest") != guest:
                    return False
                tx.put("notification_outbox", {**current, "status": "sent"})
                return True
            if not self.base.transact(claim):
                continue
            admission = self.base.get(m.ADMISSIONS, row["admission_id"]) or {}
            if admission.get("guest") != guest:
                continue
            event, bed = row["event"], re.sub(r"[^A-Za-z0-9 -]", "", str(admission.get("bed") or "?"))[:16] or "?"
            title = f"WardOS: Bed {bed} - dangerous value. Tap to open." if event["kind"] == "danger" else f"WardOS: Bed {bed} - new report."
            alerts._send(alerts._devices(), {"title": title, "silent": event["silent"], "tag": row["id"],
                                             "url": "/#/a/" + row["admission_id"]})
            sent += 1
        return sent

    def install(self, app: FastAPI) -> None:
        @app.get("/demo/start", include_in_schema=False)
        def start(request: Request):
            response = RedirectResponse("/?demo=1", status_code=302)                      # ?demo=1: this tab pressed the demo button
            if self.guest_of(request) is None or request.query_params.get("fresh"):      # /demo/start?fresh=1 = a new clean copy
                self.auth._set_cookie(response, GUEST_COOKIE, self.new_cookie(self.claim_warm()), GUEST_SECONDS)
                if WARM_TARGET:
                    threading.Thread(target=self.top_up, daemon=True).start()
            return response

        @app.get("/demo/status", include_in_schema=False)
        def status():
            return HTMLResponse(status_page(self.base.get(STATUS, "openmrs"), self.base.get(STATUS, "robot")),
                                headers={"Cache-Control": "no-store"})

        def machine(who=Depends(any_caller)):
            if not isinstance(who, Machine):
                raise HTTPException(403, "Machine token only.")
            return who

        @app.post("/internal/demo-alert")
        def demo_alert(body: AlertIn, who=Depends(machine)):
            line = re.sub(r"\s+", " ", body.line).strip()[:160]
            alerts = app.state.alerts
            if not alerts.settings.configured:
                return {"sent": 0, "failed": 0}
            devices = [d for d in alerts._devices() if d["doctor_email"] in self.auth.settings.owner_emails]
            return alerts._send(devices, {"title": line or "WardOS demo", "silent": False, "tag": "wardos-demo", "url": "/demo/status"})

        @app.post("/internal/demo-deliver")
        def demo_deliver(body: GuestIn, who=Depends(machine)):
            return {"sent": self.deliver(body.guest)}

        @app.post("/internal/demo-process")
        async def demo_process(body: GuestProcessIn, who=Depends(machine)):
            copy = await asyncio.to_thread(self.app_of, body.guest)
            copy.cache.clear()
            try:
                await asyncio.to_thread(copy.app.state.record.admission, body.admission_id)
            except Exception:
                return {"gone": True}
            return await copy.app.state.run_batch(body.admission_id, body.source_ids)


def make_copy(template: dict, guest: str, generation: int) -> list[tuple[str, dict]]:
    ids: dict[str, str] = {}
    for stay in template.values():
        for value in stay.values():
            for doc in value if isinstance(value, list) else [value]:
                old = doc["id"]
                ids[old] = m.stable_id(old.split("_")[0], guest, generation, old)

    def remap(value, key=None):
        if isinstance(value, str):
            return value if key == "file_uri" else ID_RE.sub(lambda hit: ids.get(hit.group(0), hit.group(0)), value)
        if isinstance(value, list):
            return [remap(v, key) for v in value]
        if isinstance(value, dict):
            return {k: remap(v, k) for k, v in value.items()}
        return value

    out = []
    from .today import MAX_QUESTIONS
    for stay in template.values():                       # a demo patient keeps at most 5 open questions
        qs = stay.get("questions") or []
        if isinstance(qs, list) and sum(1 for q in qs if q.get("status") == "open") > MAX_QUESTIONS:
            kept, drop = 0, set()
            for q in qs:
                if q.get("status") == "open":
                    kept += 1
                    if kept > MAX_QUESTIONS:
                        drop.add(q["id"])
            stay["questions"] = [q for q in qs if q["id"] not in drop]
    names = {"patient": m.PATIENTS, "admission": m.ADMISSIONS, "sources": m.SOURCES, "pages": m.PAGES, "facts": m.FACTS,
             "questions": m.QUESTIONS, "events": m.EVENTS, "held_facts": m.HELD}
    for stay in template.values():
        for field, collection in names.items():
            value = stay.get(field) or []
            for doc in value if isinstance(value, list) else [value]:
                copy = {**remap(doc), "guest": guest}
                if collection == m.SOURCES and "openmrs" in str(copy.get("label") or "").lower():
                    copy["label"] = "Lab report"
                out.append((collection, copy))
    return out


def status_page(openmrs: dict | None, robot: dict | None) -> str:
    def clean(value) -> str:
        return html.escape(str(value or "not yet"))
    rows = "".join(f"<tr><td>Bed {clean(b.get('bed'))}</td><td>{clean(b.get('reports'))} reports</td><td>{'OK' if b.get('ok') else 'Problem'}</td></tr>"
                   for b in (openmrs or {}).get("beds") or [])
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>WardOS demo status</title>"
            "<style>body{font:16px/1.5 system-ui;margin:24px;max-width:520px}td{padding:2px 14px 2px 0}</style><h1>WardOS demo</h1>"
            f"<p><b>Demo hospital check:</b> {clean((openmrs or {}).get('result'))}<br><small>{clean((openmrs or {}).get('checked_at'))}</small></p>"
            f"<table>{rows}</table>"
            f"<p><b>Last robot run:</b> {clean((robot or {}).get('result'))}<br><small>{clean((robot or {}).get('updated_at'))}</small></p>")


def demo_from_env(auth: Auth, settings, *, queue=None) -> DemoSite | None:
    folder = os.environ.get("WARDOS_DEMO_DIR")
    if not folder or not (Path(folder) / "template.json").is_file() or not auth.settings.cookie_secret:
        return None
    if settings.store == "firestore":
        from .docstore import FirestoreDocStore
        base = FirestoreDocStore(project=settings.project, prefix="wardos_demo_")
    else:
        from .docstore import SQLiteDocStore
        base = SQLiteDocStore(Path(settings.data_dir) / "demo" / "demo.sqlite3")
    return DemoSite(auth=auth, settings=settings, base=base, data_dir=Path(folder), queue=queue)

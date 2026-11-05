"""The task queue shared by the demo site and the demo robot."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
from uuid import uuid4

ID = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
TASKS, STATUS, LOCKS = "tasks", "status", "robot_locks"
DEMO_BEDS = {str(n) for n in range(11, 21)}      # the ten demo beds
JOB = "projects/your-gcp-project/locations/asia-south1/jobs/wardos-demo-robot"


def checked_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid demo id")
    return value


def utcnow():
    return datetime.now(timezone.utc)


def demo_store():
    from ..chain.docstore import FirestoreDocStore
    return FirestoreDocStore(project="your-gcp-project", prefix="wardos_demo_")


def start_demo_task(task_id: str, *, session=None) -> str:
    checked_id(task_id)
    job = os.environ.get("WARDOS_DEMO_JOB", JOB)
    if job != JOB:
        raise ValueError("Only the demo robot job may be started")
    if session is None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(credentials)
    reply = session.post(f"https://run.googleapis.com/v2/{job}:run", json={
        "overrides": {"taskCount": 1, "containerOverrides": [{"env": [
            {"name": "WARDOS_DEMO_TASK_ID", "value": task_id}]}]}}, timeout=30)
    if reply.status_code >= 400:
        raise RuntimeError(f"Demo job start failed (HTTP {reply.status_code})")
    return reply.json()["name"]


class TaskQueue:
    def __init__(self, store, *, now=utcnow):
        self.store, self.now = store, now

    def claim(self, task_id):
        checked_id(task_id)
        def work(tx):
            task = tx.get(TASKS, task_id)
            if not task or task.get("status") != "waiting":
                return None
            if task.get("kind") not in {"get_reports", "watch", "hospital_question"}:
                raise ValueError("Unknown demo task")
            checked_id(task.get("guest"))
            if task["kind"] == "hospital_question":
                question = task.get("question")
                if not isinstance(question, str) or not 3 <= len(question) <= 300 or re.search(r"\d{6,}", question):
                    raise ValueError("A demo question needs plain words and no identifiers")
            else:
                checked_id(task.get("admission_id"))
                if str(task.get("bed")) not in DEMO_BEDS:
                    raise ValueError("Only the ten demo beds are allowed")
            if task["kind"] == "watch" and not re.fullmatch(r"[a-z0-9][a-z0-9 +/.-]{0,39}", str(task.get("test") or "")):
                raise ValueError("A demo watch needs a plain test name")
            task = dict(task, status="working", worker=uuid4().hex,
                        updated_at=self.now().isoformat(), lease_until=(self.now() + timedelta(minutes=18)).isoformat())
            tx.put(TASKS, task)
            return task
        return self.store.transact(work)

    def step(self, task, line, *, terminal=None, **metadata):
        def work(tx):
            current = tx.get(TASKS, task["id"])
            if not current or current.get("worker") != task.get("worker") or current.get("status") != "working":
                raise RuntimeError("Demo task lease lost")
            lines = list(current.get("steps") or [])
            lines.append(line)
            current.update(steps=lines, updated_at=self.now().isoformat(), **metadata)
            if terminal:
                current["status"] = terminal
            tx.put(TASKS, current)
        self.store.transact(work)

    def acquire(self, name, owner, seconds=180):
        def work(tx):
            lock = tx.get(LOCKS, name)
            if lock and datetime.fromisoformat(lock["until"]) > self.now() and lock["owner"] != owner:
                return False
            tx.put(LOCKS, {"id": name, "owner": owner, "until": (self.now() + timedelta(seconds=seconds)).isoformat()})
            return True
        return self.store.transact(work)

    def held(self, name):
        lock = self.store.get(LOCKS, name)
        return bool(lock and datetime.fromisoformat(lock["until"]) > self.now())

    def release(self, name, owner):
        def work(tx):
            lock = tx.get(LOCKS, name)
            if lock and lock["owner"] == owner:
                tx.put(LOCKS, dict(lock, until=self.now().isoformat()))
        self.store.transact(work)

    def expire_stalled(self):
        for old in self.store.query(TASKS, status="working"):
            if datetime.fromisoformat(old["lease_until"]) < self.now():
                def work(tx):
                    task = tx.get(TASKS, old["id"])
                    if task and task.get("status") == "working" and task.get("lease_until") == old["lease_until"]:
                        tx.put(TASKS, dict(task, status="failed", updated_at=self.now().isoformat(),
                                          steps=list(task.get("steps") or []) + ["The demo robot stopped. Please try again."]))
                self.store.transact(work)

"""The agent's task board: what the doctor asked, what the laptop worker is doing, what it found."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
from uuid import uuid4

from ..config import HOSPITAL_TZ
from . import model as m
from .agent_words import clock_text, same_test, ward_list
from .normalize import fact_key
from .today import _sex, is_danger, outside

TASKS, STATUS = "agent_tasks", "agent_status"
AGENT_AUTHOR = "hospital-agent"
PORTAL_LABELS = {"ehospital": "eHospital report", "openmrs": "OpenMRS report"}
JOB_LABELS = {"get_reports": "Fetch lab reports", "watch": "Watch", "hospital_question": "Hospital question"}
OPEN, ENDED = ("waiting", "working"), ("done", "failed", "stopped")
LEASE = timedelta(minutes=12)
WAITING_LIMIT = timedelta(minutes=30)
WORKER_SILENT = timedelta(minutes=2)
ROBOT_SILENT = timedelta(seconds=60)
ROBOT_PREFIX = "robot:"
OLD_REPORT = timedelta(hours=24)
MAX_LINES = 1000
EARLIER_SHOWN = 3
NAME_MISMATCH = "eHospital shows a different name for this UHID. Nothing was downloaded."
RELATED_MEDS = {
    "potassium": r"kcl|k\s*cl|potassium|potchlor|spironolactone|aldactone|furosemide|lasix|torsemide|insulin|salbutamol|"
                 r"calcium gluconate|ramipril|enalapril|losartan|telmisartan|kayexalate",
    "sodium": r"saline|\bns\b|3\s*%|tolvaptan|desmopressin|furosemide|lasix|hydrochlorothiazide",
    "glucose": r"insulin|metformin|glimepiride|dextrose|\bd25\b|\bd10\b",
    "haemoglobin": r"prbc|packed|transfusion|blood|iron|ferric",
    "platelets": r"platelet|\brdp\b|\bsdp\b|heparin|enoxaparin|clopidogrel|aspirin",
    "inr": r"warfarin|acitrom|acenocoumarol|vitamin k|\bffp\b|heparin",
    "creatinine": r"furosemide|lasix|vancomycin|amikacin|gentamicin|colistin|contrast",
}


class TaskGone(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _when(iso: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=HOSPITAL_TZ)
    return moment


def _short(iso: str | None) -> str:
    moment = _when(iso)
    return moment.astimezone(HOSPITAL_TZ).strftime("%d/%m %H:%M") if moment else "time not shown"


class AgentDesk:
    def __init__(self, record, *, now=_utcnow):
        self.record, self.now = record, now
        self.cloud_robot = False  # True when the hospital robot is a cloud job started by the site, not a laptop window
        self.store = getattr(record.store, "inner", record.store)

    def _iso(self, moment: datetime | None = None) -> str:
        return (moment or self.now()).astimezone(timezone.utc).isoformat(timespec="seconds")

    def _clock(self) -> str:
        return self.now().astimezone(HOSPITAL_TZ).strftime("%H:%M")

    def _safe(self, text) -> str:
        return str(text or "")[:600]

    def _update(self, task_id: str, worker: str | None, change) -> dict:
        def work(tx):
            task = tx.get(TASKS, task_id)
            if not task:
                raise TaskGone("no such task")
            if worker is not None and (task.get("worker") != worker or task.get("status") not in OPEN):
                raise TaskGone("task is no longer this worker's")
            change(task)
            task["updated_at"] = self._iso()
            task["steps"] = list(task.get("steps") or [])[-MAX_LINES:]
            tx.put(TASKS, task)
            return task
        return self.store.transact(work)

    def create(self, kind: str, *, admission_id=None, bed=None, doctor="", first_line: str, **fields) -> dict:
        task = {"id": "t" + uuid4().hex[:20], "kind": kind, "admission_id": admission_id, "bed": bed, "status": "waiting",
                "steps": [first_line], "thought": "", "doctor": doctor, "created_at": self._iso(),
                "updated_at": self._iso(), **fields}
        self.store.put(TASKS, task)
        return task

    def create_watch(self, *, admission_id, bed, test, until: datetime, every_min: int, doctor="") -> tuple[dict, bool]:
        end = until.replace(tzinfo=HOSPITAL_TZ) if until.tzinfo is None else until
        fields = dict(until=self._iso(end), every_min=int(every_min), until_text=clock_text(end.astimezone(HOSPITAL_TZ)))
        for old in self.watches(admission_id=admission_id):
            if same_test(old.get("test"), test):
                def change(task):
                    task.update(fields)
                    task["steps"] = list(task.get("steps") or []) + [
                        f"{self._clock()} · watch changed · every {every_min} min until {fields['until_text']}"]
                return self._update(old["id"], None, change), False
        line = f"{self._clock()} · watch started · {test} · every {every_min} min · until {fields['until_text']}"
        return self.create("watch", admission_id=admission_id, bed=bed, doctor=doctor, first_line=line, test=test,
                           next_check_at=self._iso(), checks=0, problems=0, **fields), True

    def tag(self, task_id: str, *, batch_id: str, position: int, owner: str = "", words: str = "") -> dict:
        """Mark a new task as one job of a request: which request, in what order, whose, and the doctor's words for it."""
        def change(task):
            task.update(batch_id=batch_id, position=int(position), owner=owner, words=self._safe(words))
        return self._update(task_id, None, change)

    def note(self, task_id: str, line: str, **fields) -> dict:
        """A plain line (and optional fields) added to a task whatever its state; used by the planner."""
        line = self._safe(line)

        def change(task):
            task["steps"] = list(task.get("steps") or []) + [line]
            task.update(fields)
        return self._update(task_id, None, change)

    def stop(self, task_id: str, doctor="") -> dict:
        def change(task):
            if task.get("status") in OPEN:
                task["status"] = "stopped"
                task["stopped_by"] = doctor
                task["thought"] = ""
                line = "Stopped" if task["kind"] != "watch" else f"{self._clock()} · watch ended · told to stop"
                task["steps"] = list(task.get("steps") or []) + [line]
        return self._update(task_id, None, change)

    def get(self, task_id: str) -> dict | None:
        return self.store.get(TASKS, task_id)

    def watches(self, *, admission_id=None, include_ended=False) -> list[dict]:
        self.sweep()
        rows = [t for status in OPEN + (ENDED if include_ended else ()) for t in self.store.query(TASKS, status=status)
                if t.get("kind") == "watch" and (not admission_id or t.get("admission_id") == admission_id)]
        return sorted(rows, key=lambda t: t.get("created_at") or "", reverse=True)

    def worker_state(self) -> dict:
        row = self.store.get(STATUS, "worker") or {}
        seen = _when(row.get("seen_at"))
        return {"online": bool(seen and self.now() - seen <= WORKER_SILENT), "seen_at": row.get("seen_at"),
                "line": row.get("line") or ""}

    def view(self, task: dict, worker: dict | None = None) -> dict:
        worker = worker or self.worker_state()
        kind, status = task.get("kind"), task.get("status")
        out = {k: task.get(k) for k in ("id", "kind", "admission_id", "bed", "status", "steps", "thought", "summary",
                                        "new_reports", "attention", "answer", "test", "every_min", "until",
                                        "until_text", "checks", "problems", "last_check_at", "alert", "created_at",
                                        "updated_at", "question", "skill", "skill_state", "source_ids", "next_check_at", "batch_id", "position",
                                        "parent", "attempt", "ask")}
        out["job"] = JOB_LABELS.get(kind, kind)
        out["patient_label"] = f"Bed {task.get('bed')}" if task.get("bed") else "Ward"
        created = _when(task.get("created_at"))
        out["time"] = f"{created.astimezone(HOSPITAL_TZ):%H:%M}" if created else ""
        nxt = _when(task.get("next_check_at"))
        out["next_check"] = f"{nxt.astimezone(HOSPITAL_TZ):%H:%M}" if nxt and status in OPEN else ""
        seen_at = _when(task.get("last_check_at"))
        out["last_check"] = f"{seen_at.astimezone(HOSPITAL_TZ):%H:%M}" if seen_at else ""
        out["active"] = status in OPEN
        out["worker_online"] = worker["online"]
        out["health"] = ""
        last = _when(task.get("last_check_at"))
        if kind == "watch":
            out["title"] = f"Watching bed {task.get('bed') or '?'} · {task.get('test')} · every {task.get('every_min')} min until {task.get('until_text')}"
            if status in OPEN:
                said = f"Last check {last.astimezone(HOSPITAL_TZ):%H:%M}" if last else "No check yet"
                if not worker["online"]:
                    out["health"] = f"{said} · {self._robot()} is not answering"
                elif task.get("last_problem"):
                    out["health"] = f"{said} · {task['last_problem']}"
                else:
                    out["health"] = said
        elif status == "waiting" and not worker["online"]:
            out["health"] = ("The hospital robot is starting. This begins in about a minute." if self.cloud_robot
                             else "The laptop worker is off. This starts when it is on.")
        return out

    def _robot(self) -> str:
        return "The hospital robot" if self.cloud_robot else "The laptop worker"

    def sweep(self) -> None:
        now = self.now()
        for status in OPEN:
            for task in self.store.query(TASKS, status=status):
                if task.get("kind") == "watch":
                    end = _when(task.get("until"))
                    if end and now >= end and (_when(task.get("lease_until")) or now) <= now:
                        self._close_watch(task["id"], "end time reached")
                    continue
                created, lease = _when(task.get("created_at")), _when(task.get("lease_until"))
                if status == "waiting" and created and now - created > WAITING_LIMIT:
                    self._fail_stale(task["id"], status, f"{self._robot()} was off, so this did not run. Ask again." if not self.cloud_robot
                                     else "The hospital robot did not start in time, so this did not run. Ask again.")
                elif status == "working" and lease and lease < now and not task.get("requeued") and self._in_batch(task):
                    self._requeue(task["id"])
                elif status == "working" and lease and lease < now:
                    self._fail_stale(task["id"], status, "The laptop stopped while working. Ask again." if not self.cloud_robot
                                     else "The hospital robot stopped while working. Ask again.")

    def _in_batch(self, task: dict) -> bool:
        """True when the job is one of several of the same request (a lone job still ends with 'Ask again')."""
        return bool(task.get("batch_id")) and any(
            t["id"] != task["id"] for status in ("waiting", "working", *ENDED) for t in self.store.query(TASKS, batch_id=task["batch_id"], status=status))

    def _requeue(self, task_id: str) -> None:
        """A robot went silent in the middle of a job: the job waits for another robot, once."""
        def work(tx):
            task = tx.get(TASKS, task_id)
            if task and task.get("status") == "working":
                line = "The robot stopped while working. This job is waiting for another robot."
                task.update(status="waiting", requeued=True, thought="", updated_at=self._iso(),
                            steps=list(task.get("steps") or []) + [line])
                task.pop("worker", None)
                task.pop("lease_until", None)
                tx.put(TASKS, task)
        self.store.transact(work)

    def _fail_stale(self, task_id: str, was: str, line: str) -> None:
        def work(tx):
            task = tx.get(TASKS, task_id)
            if task and task.get("status") == was:
                task.update(status="failed", summary=line, thought="", updated_at=self._iso(),
                            steps=list(task.get("steps") or []) + [line])
                tx.put(TASKS, task)
        self.store.transact(work)

    def _close_watch(self, task_id: str, reason: str, *, status="done") -> None:
        def work(tx):
            task = tx.get(TASKS, task_id)
            if task and task.get("status") in OPEN:
                line = (f"{self._clock()} · watch ended · {reason} · checks {task.get('checks') or 0} · "
                        f"danger {task.get('dangers') or 0} · problems {task.get('problems') or 0}")
                task.update(status=status, thought="", updated_at=self._iso(), summary=reason,
                            steps=list(task.get("steps") or []) + [line])
                task.pop("lease_until", None)
                tx.put(TASKS, task)
        self.store.transact(work)

    def heartbeat(self, worker: str, line: str = "", batch_id: str | None = None) -> None:
        self.store.put(STATUS, {"id": "worker", "worker": worker, "seen_at": self._iso(), "line": line[:200]})
        row = self.store.get(STATUS, ROBOT_PREFIX + worker) or {}
        self.store.put(STATUS, {"id": ROBOT_PREFIX + worker, "worker": worker, "seen_at": self._iso(),
                                "batch_id": batch_id if batch_id is not None else row.get("batch_id")})

    def max_robots(self) -> int:
        """How many robots may work at once: WARDOS_MAX_ROBOTS (default 1), never more than a forced logout left allowed."""
        try:
            allowed = max(1, int(os.environ.get("WARDOS_MAX_ROBOTS") or 1))
        except ValueError:
            allowed = 1
        lowered = self.store.get(STATUS, "limits") or {}
        return min(allowed, int(lowered.get("max_robots") or allowed))

    def lower_robots(self, reason: str) -> None:
        """The site logged a robot out because of a second login: one robot at a time from now on."""
        self.store.put(STATUS, {"id": "limits", "max_robots": 1, "reason": reason[:200], "at": self._iso()})

    def robots(self, *, besides: str | None = None) -> list[dict]:
        """Robots seen in the last minute (each has its own heartbeat row and the batch it holds)."""
        now = self.now()
        rows = [r for r in self.store.query(STATUS) if str(r.get("id", "")).startswith(ROBOT_PREFIX)]
        return [r for r in rows if r.get("worker") != besides and (_when(r.get("seen_at")) or now - ROBOT_SILENT * 2) >= now - ROBOT_SILENT]

    def wants_robot(self) -> bool:
        """True when a robot may be started: fewer than the allowed robots run and a waiting batch is held by none."""
        robots = self.robots()
        if len(robots) >= self.max_robots():
            return False
        held = {r.get("batch_id") for r in robots if r.get("batch_id")}
        return any((t.get("batch_id") or t["id"]) not in held for t in self.store.query(TASKS, status="waiting"))

    def logins(self, worker: str, add: int | None = None, failed: bool = False) -> dict:
        """eHospital logins of the last 12 hours and failed-in-a-row count: one copy for every worker run."""
        row = self.store.get(STATUS, "logins") or {"id": "logins", "times": [], "failed": 0}
        now = self.now().timestamp()
        row["times"] = [t for t in row.get("times") or [] if now - t < 12 * 3600]
        if add is not None:
            row["times"] += [now] * max(0, int(add))
            row["failed"] = int(row.get("failed") or 0) + max(1, int(add)) if failed else 0
            self.store.put(STATUS, row)
        return {"times": row["times"], "failed": int(row.get("failed") or 0)}

    def claim_next(self, worker: str, batch_id: str | None = None) -> dict:
        """The next task for a robot. With batch_id it takes only that batch (its session stays open); an empty batch says
        batch_done. Without it, the oldest batch no other robot holds."""
        others = self.robots(besides=worker)
        if len(others) >= self.max_robots():
            return {"task": None, "refused": "Another laptop worker is already running." if self.max_robots() == 1
                    else "The most robots allowed are already running."}
        self.heartbeat(worker)
        self.sweep()
        now = self.now()
        held = {r.get("batch_id") for r in others if r.get("batch_id")}
        tasks = [t for t in self.store.query(TASKS, status="waiting") if t.get("kind") != "watch"]
        tasks = [t for t in tasks if (t.get("batch_id") or t["id"]) == batch_id] if batch_id             else [t for t in tasks if (t.get("batch_id") or t["id"]) not in held]
        first = {}
        for t in tasks:
            key = t.get("batch_id") or t["id"]
            first[key] = min(first.get(key) or "~", t.get("created_at") or "")
        tasks.sort(key=lambda t: (first[t.get("batch_id") or t["id"]], int(t.get("position") or 0), t.get("created_at") or ""))
        watches = [t for status in OPEN for t in self.store.query(TASKS, status=status) if t.get("kind") == "watch"]
        free = [t for t in watches if (_when(t.get("lease_until")) or now) <= now]
        due = [] if batch_id else sorted((t for t in free if (_when(t.get("next_check_at")) or now) <= now),
                                         key=lambda t: t.get("next_check_at") or "")
        for candidate in tasks + due:
            claimed = self._claim(candidate, worker, held)
            if claimed:
                key = None if claimed.get("kind") == "watch" else (claimed.get("batch_id") or claimed["id"])
                self.heartbeat(worker, batch_id=key or "")
                return {"task": claimed, "active_watches": len(watches), "batch_id": key, "others": len(others)}
        self.heartbeat(worker, batch_id="")
        if batch_id:
            return {"task": None, "batch_done": True, "active_watches": len(watches), "others": len(others)}
        ahead = [(_when(t.get("next_check_at")) - now).total_seconds() for t in free if _when(t.get("next_check_at"))]
        return {"task": None, "active_watches": len(watches), "next_due_in_s": max(0, min(ahead)) if ahead else None,
                "others": len(others)}

    def _claim(self, seen: dict, worker: str, held=frozenset()) -> dict | None:
        def work(tx):
            task = tx.get(TASKS, seen["id"])
            if not task or task.get("status") != seen.get("status") or task.get("lease_until") != seen.get("lease_until"):
                return None
            key = None if task.get("kind") == "watch" else (task.get("batch_id") or task["id"])
            now = self.now()
            if key and key not in held:
                for row in tx.query(STATUS):  # another robot may have taken this batch a moment ago
                    if (str(row.get("id", "")).startswith(ROBOT_PREFIX) and row.get("worker") != worker
                            and row.get("batch_id") == key and (_when(row.get("seen_at")) or now) >= now - ROBOT_SILENT):
                        return None
            task.update(status="working", worker=worker, lease_until=self._iso(now + LEASE), updated_at=self._iso())
            tx.put(TASKS, task)
            tx.put(STATUS, {"id": ROBOT_PREFIX + worker, "worker": worker, "seen_at": self._iso(), "batch_id": key or ""})
            return task
        return self.store.transact(work)

    def identity(self, task_id: str, worker: str) -> dict:
        task = self.get(task_id)
        if not task or task.get("worker") != worker or task.get("status") != "working" or not task.get("admission_id"):
            raise TaskGone("not this worker's task")
        patient = self.record.patient_of(task["admission_id"])
        return {"uhid": patient.get("portal_uhid") or patient.get("uhid"), "name": patient.get("portal_name") or patient.get("name")}

    def step(self, task_id: str, worker: str, line: str = "", thought: str | None = None) -> dict:
        line, thought = self._safe(line) if line else "", self._safe(thought) if thought is not None else None
        self.heartbeat(worker)

        def change(task):
            if line:
                task["steps"] = list(task.get("steps") or []) + [line]
            if thought is not None:
                task["thought"] = thought
            task["lease_until"] = self._iso(self.now() + LEASE)
        return self._update(task_id, worker, change)

    def finish(self, task_id: str, worker: str, *, status: str, line: str, **result) -> dict:
        if status not in ENDED:
            raise ValueError("a task ends done, failed or stopped")
        line = self._safe(line)
        safe = {k: (self._safe(v) if isinstance(v, str) else v) for k, v in result.items()}

        def change(task):
            task.update(status=status, thought="", summary=line, steps=list(task.get("steps") or []) + [line], **safe)
            task.pop("lease_until", None)
        return self._update(task_id, worker, change)

    def give_up(self, worker: str, line: str) -> int:
        line, ended = self._safe(line), 0
        for status in OPEN:
            for task in self.store.query(TASKS, status=status):
                if task.get("kind") == "watch":
                    self._close_watch(task["id"], line, status="failed")
                else:
                    self._fail_stale(task["id"], status, line)
                ended += 1
        return ended

    def _new_facts(self, admission_id: str, source_ids) -> list[dict]:
        ids = set(source_ids)
        return [f for f in self.record.facts(admission_id) if f.get("type") == "lab_result"
                and (f.get("source_id") in ids or ids & set(f.get("support") or []))]

    def tally(self, task: dict, source_ids) -> dict:
        pending = failed = 0
        for sid in source_ids:
            source = self.record.source(sid)
            if source.get("status") == m.SRC_FAILED or source.get("not_added"):
                failed += 1
            elif source.get("status") != m.SRC_MERGED:
                pending += 1
        admission_id = task["admission_id"]
        facts = self.record.facts(admission_id)
        sex = _sex(self.record.patient_of(admission_id), facts)
        new = self._new_facts(admission_id, source_ids)
        attention = [f for f in new if is_danger(f, sex) or outside(f, sex)[0]]
        return {"pending": pending, "failed": failed, "values": len(new), "attention": len(attention),
                "danger": sum(1 for f in new if is_danger(f, sex))}

    def _medicines_for(self, admission_id: str, test: str) -> list[str]:
        pattern = RELATED_MEDS.get(fact_key("lab_result", test))
        if not pattern:
            return []
        on = self.record.medicines(admission_id)
        return sorted(name for name, entry in on.items() if entry["state"] == "on" and re.search(pattern, name, re.I))

    def _alert(self, task: dict, fact: dict, same_key: list[dict], medicines: list[str]) -> dict:
        value = fact.get("value") or {}
        then = fact.get("clinical_time") or ""
        earlier = sorted((f for f in same_key if f["id"] != fact["id"] and (f.get("clinical_time") or "") <= then),
                         key=lambda f: f.get("clinical_time") or "", reverse=True)[:EARLIER_SHOWN]
        return {"test": task.get("test"), "label": fact.get("label"), "value": value.get("text"), "number": value.get("number"),
                "unit": value.get("unit"), "normal": value.get("reference"), "collected_at": then, "time": _short(then),
                "earlier": [{"value": (f.get("value") or {}).get("text"), "time": _short(f.get("clinical_time"))} for f in earlier],
                "medicines": medicines, "fact_id": fact["id"], "source_id": fact.get("source_id"),
                "report_url": f"/sources/{fact.get('source_id')}/file", "at": self._iso()}

    def finish_round(self, task_id: str, worker: str, *, outcome: str, source_ids=(), reports: int = 0,
                     problem: str = "") -> dict:
        task = self.get(task_id)
        if not task or task.get("worker") != worker or task.get("status") != "working":
            raise TaskGone("not this worker's watch")
        test, admission_id, clock = task["test"], task["admission_id"], self._clock()
        lines, alert, dangers = [], None, 0
        due, last = _when(task.get("next_check_at")), _when(task.get("last_check_at"))
        if due and last and self.now() - due > timedelta(minutes=int(task.get("every_min") or 15)):
            lines.append(f"{clock} · no checks since {last.astimezone(HOSPITAL_TZ):%H:%M} · the laptop or the internet was off")
        if outcome == "found":
            facts = self.record.facts(admission_id)
            sex = _sex(self.record.patient_of(admission_id), facts)
            key = fact_key("lab_result", test)
            same_key = [f for f in facts if f.get("type") == "lab_result" and f.get("key") == key]
            ids = set(source_ids)
            new = sorted((f for f in same_key if f.get("source_id") in ids or ids & set(f.get("support") or [])),
                         key=lambda f: f.get("clinical_time") or "")
            cutoff, older = self.now() - OLD_REPORT, 0
            for fact in new:
                value = fact.get("value") or {}
                what = f"{fact.get('label')} {value.get('text')}"
                then = _when(fact.get("clinical_time"))
                if then and then < cutoff:
                    older += 1
                elif fact.get("status") == m.DISPUTED:
                    lines.append(f"{clock} · checked · new {what} · differs from another page, a question is waiting")
                elif is_danger(fact, sex):
                    dangers += 1
                    alert = self._alert(task, fact, same_key, self._medicines_for(admission_id, test))
                    history = ", ".join(f"{e['value']} ({e['time']})" for e in alert["earlier"]) or "none"
                    normal = f" · normal {alert['normal']}" if alert["normal"] else ""
                    lines.append(f"{clock} · DANGER · {what}{normal} · collected {alert['time']} · earlier: {history}")
                else:
                    lines.append(f"{clock} · checked · new {what} · not dangerous")
            if older:
                lines.append(f"{clock} · checked · {older} older {test} value{'s' if older != 1 else ''} added, not new tonight")
            if not new:
                found = f"{reports} new report{'s' if reports != 1 else ''} · " if reports else ""
                lines.append(f"{clock} · checked · {found}no new {test}")
        elif outcome == "nothing_new":
            lines.append(f"{clock} · checked · no new {test}")
        elif outcome == "name_mismatch":
            lines.append(f"{clock} · {NAME_MISMATCH}")
        else:
            lines.append(f"{clock} · {self._safe(problem) or 'eHospital not answering'} · trying again next round")

        def change(task):
            started = self.now()
            task["steps"] = list(task.get("steps") or []) + [self._safe(line) for line in lines]
            task["checks"] = int(task.get("checks") or 0) + 1
            task["last_check_at"] = self._iso(started)
            task["next_check_at"] = self._iso(started + timedelta(minutes=int(task.get("every_min") or 15)))
            task["thought"] = ""
            task.pop("lease_until", None)
            if outcome in ("found", "nothing_new"):
                task["last_problem"] = ""
            else:
                task["problems"] = int(task.get("problems") or 0) + 1
                task["last_problem"] = "eHospital not answering" if outcome != "name_mismatch" else NAME_MISMATCH
            if alert:
                task["alert"] = alert
                task["dangers"] = int(task.get("dangers") or 0) + dangers
            if outcome == "name_mismatch":
                task["status"] = "failed"
                task["summary"] = NAME_MISMATCH
        updated = self._update(task_id, worker, change)
        end = _when(updated.get("until"))
        if updated.get("status") in OPEN and end and self.now() >= end:
            self._close_watch(task_id, "end time reached")
            updated = self.get(task_id)
        return updated

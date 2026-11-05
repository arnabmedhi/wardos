"""The agent's routes."""
from __future__ import annotations

import asyncio
import html
import json
import os

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import model as m
from .agent_tasks import AGENT_AUTHOR, ENDED, NAME_MISMATCH, OPEN, PORTAL_LABELS, TASKS, AgentDesk, TaskGone
from .agent_jobs import Conversations, reply_of, run_jobs
from .planner import Planner
from .agent_words import Command, bed_label, beds_in_words, resolve, same_test, ward_list
from .auth import Doctor, any_caller, signed_in
from .normalize import fact_key


class CommandIn(BaseModel):
    text: str
    admission_id: str | None = None


class WorkerIn(BaseModel):
    worker: str = Field(max_length=64)


class NextIn(WorkerIn):
    batch_id: str | None = Field(default=None, max_length=64)


class StepIn(WorkerIn):
    line: str = Field(default="", max_length=600)
    thought: str | None = Field(default=None, max_length=600)


class TallyIn(WorkerIn):
    source_ids: list[str] = []


class FinishIn(WorkerIn):
    status: str
    line: str = Field(max_length=600)
    new_reports: int | None = None
    attention: int | None = None
    answer: str | None = Field(default=None, max_length=4000)
    source_ids: list[str] | None = Field(default=None, max_length=200)
    skill: str | None = Field(default=None, max_length=120)
    skill_state: str | None = Field(default=None, pattern="^(used|learned|repaired|learning)$")


class RoundIn(WorkerIn):
    outcome: str
    source_ids: list[str] = []
    reports: int = 0
    problem: str = Field(default="", max_length=300)


class LoginsIn(WorkerIn):
    add: int | None = Field(default=None, ge=0, le=20)
    failed: bool = False


class GiveUpIn(WorkerIn):
    line: str = Field(max_length=300)


class PortalValue(BaseModel):
    test: str = Field(max_length=120)
    value: str = Field(max_length=120)
    number: float | None = None
    unit: str | None = Field(default=None, max_length=40)
    reference: str | None = Field(default=None, max_length=80)
    collected_at: str | None = Field(default=None, max_length=40)
    sample_no: str | None = Field(default=None, max_length=80)


class ValuesIn(BaseModel):
    portal: str = "openmrs"
    values: list[PortalValue] = Field(max_length=2000)


def add_portal_values(record, admission_id: str, values: list[dict], portal: str) -> tuple[list[str], int]:
    from .normalize import quote_found
    label = PORTAL_LABELS.get(portal, f"{portal} report")
    known = {(f["key"], (f.get("clinical_time") or "")[:16], (f.get("value") or {}).get("number")
              if (f.get("value") or {}).get("number") is not None else (f.get("value") or {}).get("text"))
             for f in record.facts(admission_id, live_only=False) if f.get("type") == "lab_result"}
    new, skipped = [], 0
    for value in values:
        key = fact_key("lab_result", value["test"])
        marker = (key, (value.get("collected_at") or "")[:16],
                  value.get("number") if value.get("number") is not None else value["value"])
        if marker in known:
            skipped += 1
            continue
        known.add(marker)
        payload = json.dumps({k: value.get(k) for k in ("test", "value", "unit", "reference", "collected_at")}, sort_keys=True)
        source, duplicate = record.add_source(admission_id, "portal_report", text=payload, mime="text/plain",
                                              filename=f"{portal}-report.txt", author=AGENT_AUTHOR)
        if duplicate:
            skipped += 1
            continue
        record.update_source(source["id"], label=label, portal=portal)
        page = record.save_pages(source["id"], [{"text": payload, "page_type": "lab_report"}])[0]
        fact = m.fact_doc(admission_id=admission_id, source_id=source["id"], page_id=page["id"], index=0,
                          type="lab_result", key=key, label=value["test"],
                          value={"text": value["value"], "number": value.get("number"), "unit": value.get("unit"),
                                 "reference": value.get("reference"), "lab": label, "sample_no": value.get("sample_no")},
                          clinical_time=value.get("collected_at"),
                          time_precision="exact" if value.get("collected_at") else "unknown", assertion="present",
                          quote=value["value"], quote_found=quote_found(value["value"], payload))
        record.apply(source["id"], [fact])
        new.append(source["id"])
    return new, skipped


def _card(view: dict, *, patient=None, title: str | None = None) -> dict:
    return {"kind": "agent", "title": title or view.get("title") or "Get the lab reports", "patient": patient, "task": view,
            "text": ""}


def install_agent(app: FastAPI, record, *, get_model, settings, alert_snapshot, notify, clock=None,
                  guest: bool = False) -> AgentDesk:
    desk = AgentDesk(record, **({"now": clock} if clock else {}))
    app.state.agent = desk
    robot_job = None if guest else os.environ.get("WARDOS_HOSPITAL_ROBOT_JOB")  # set only when the eHospital robot is a Cloud Run job
    desk.cloud_robot = bool(robot_job)
    started = {"at": 0.0}

    def kick_robot() -> None:
        """Start the cloud robot for a new task (best effort; the 10-minute scheduler is the safety net)."""
        if not robot_job or not desk.wants_robot():
            return
        import time
        if time.time() - started["at"] < 60:
            return
        started["at"] = time.time()
        try:
            from .finals import cloud_job_runner
            cloud_job_runner(settings.project, os.environ.get("WARDOS_HOSPITAL_ROBOT_REGION", "asia-south1"), robot_job)({})
        except Exception:
            pass
    min_every = int(os.environ.get("WARDOS_AGENT_MIN_EVERY_MIN") or 5)

    def patient_block(admission_id: str | None, bed) -> dict | None:
        return {"id": admission_id, "label": f"Bed {bed}" if bed else "This patient"} if admission_id else None

    def worker_only(request: Request, who=Depends(any_caller)):
        if isinstance(who, Doctor) and who.role != "owner":
            raise HTTPException(403, "Only the laptop worker can do this.")
        return who

    def open_task(task_id: str) -> dict:
        task = desk.get(task_id)
        if not task:
            raise HTTPException(404, "no such task")
        return task

    def message(text: str, command: Command, title="WardOS agent") -> dict:
        return {"text": text, "intent": {"action": f"agent_{command.action}", "patient_id": command.admission_id,
                                         "how": command.how, "bed": command.bed},
                "card": {"kind": "message", "title": title, "patient": patient_block(command.admission_id, command.bed),
                         "text": text}}

    def writable(admission_id: str) -> str | None:
        admission = record.store.get(m.ADMISSIONS, admission_id) or {}
        return "Old stays are view-and-ask only. Nothing was opened." if admission.get("old_case") else None

    def act(command: Command, doctor: Doctor, text: str) -> dict | None:
        if command.action == "ask_record":
            return None
        if command.message:
            return message(command.message, command)
        worker = desk.worker_state()
        if command.action == "get_reports":
            refusal = writable(command.admission_id)
            if refusal:
                return message(refusal, command)
            task = desk.create("get_reports", admission_id=command.admission_id, bed=command.bed, doctor=doctor.name, test=command.test,
                               first_line="Waiting for the laptop worker", understood_by=command.understood_by,
                               understand_cost_inr=command.cost_inr)
            kick_robot()
            view = desk.view(task, worker)
            return {"text": text, "intent": {"action": "agent_get_reports", "patient_id": command.admission_id,
                                             "how": command.how, "bed": command.bed},
                    "card": _card(view, patient=patient_block(command.admission_id, command.bed), title="Get the lab reports")}
        if command.action == "watch":
            refusal = writable(command.admission_id)
            if refusal:
                return message(refusal, command)
            task, _ = desk.create_watch(admission_id=command.admission_id, bed=command.bed, test=command.test,
                                        until=command.until, every_min=command.every_min, doctor=doctor.name)
            kick_robot()
            view = desk.view(task, worker)
            return {"text": text, "intent": {"action": "agent_watch", "patient_id": command.admission_id,
                                             "how": command.how, "bed": command.bed},
                    "card": _card(view, patient=patient_block(command.admission_id, command.bed))}
        if command.action == "list_watches":
            rows = [desk.view(t, worker) for t in desk.watches()]
            return {"text": text, "intent": {"action": "agent_list_watches", "patient_id": None, "how": "none", "bed": None},
                    "card": {"kind": "agent_watches", "title": "What I am watching", "patient": None, "watches": rows,
                             "text": "" if rows else "Nothing is being watched."}}
        if command.action == "stop_watch":
            rows = desk.watches(admission_id=command.admission_id)
            if command.test:
                rows = [t for t in rows if same_test(t.get("test"), command.test)]
            if not rows:
                return message("No watch like that is running.", command)
            if len(rows) > 1 and not command.admission_id:
                views = [desk.view(t, worker) for t in rows]
                return {"text": text, "intent": {"action": "agent_stop_watch", "patient_id": None, "how": "none", "bed": None},
                        "card": {"kind": "agent_watches", "title": "Which watch should stop?", "patient": None, "watches": views, "text": ""}}
            for row in rows:
                desk.stop(row["id"], doctor.name)
            names = ", ".join(f"bed {t.get('bed') or '?'} {t.get('test')}" for t in rows)
            return message(f"Stopped watching {names}.", command)
        if command.action == "hospital_question":
            task = desk.create("hospital_question", doctor=doctor.name, question=command.question,
                               first_line="Waiting for the laptop worker", understood_by=command.understood_by,
                               understand_cost_inr=command.cost_inr)
            kick_robot()
            return {"text": text, "intent": {"action": "agent_hospital_question", "patient_id": None, "how": "none", "bed": None},
                    "card": _card(desk.view(task, worker), title="Asking the hospital website")}
        return None

    conversations = Conversations()
    portal = os.environ.get("WARDOS_PORTAL") or "ehospital"

    def running_now() -> list[dict]:
        return [{"kind": t.get("kind"), "bed": t.get("bed"), "test": t.get("test"), "question": t.get("question")}
                for status in OPEN for t in record.store.query(TASKS, status=status)]

    async def run_command(text: str, admission_id: str | None, doctor: Doctor, reply_to: str | None = None) -> dict | None:
        ward = await asyncio.to_thread(ward_list, record, m=m)
        model = get_model()

        async def execute(job: dict, words: str) -> dict:
            kind = job["kind"]
            if kind == "cannot":
                return {"error": job["text"] or "I can't do that yet"}
            if kind == "record_question":
                row = None
                if job["bed"]:
                    row = next((r for r in ward if bed_label(r["bed"]) == bed_label(job["bed"])), None)
                    if row is None:
                        return {"error": f"There is no bed {job['bed']} on this ward. Nothing was opened."}
                question = job["text"] or words
                if job["bed"] and bed_label(job["bed"]).lower() not in beds_in_words(question):
                    question += f" of bed {job['bed']}"
                answered = await app.state.answer_record(question, row["id"] if row else admission_id, doctor)
                return {"answer": answered["card"], "intent": answered["intent"]}
            command = resolve(reply_of(job), words, ward, admission_id, min_every=min_every, job_text=job["text"],
                              cost=job["cost_inr"], understood_by=job["understood_by"])
            made = await asyncio.to_thread(act, command, doctor, words)
            if command.message:
                return {"error": command.message}
            if made is None:
                return {"error": "Nothing was started."}
            card = made["card"]
            if card.get("kind") == "agent":
                await asyncio.to_thread(desk.tag, card["task"]["id"], batch_id=job.get("batch_id") or card["task"]["id"],
                                        position=job.get("position") or 0, owner=doctor.email, words=job.get("text") or words)
            return {"intent": made["intent"], **({"task": card["task"]} if card.get("kind") == "agent" else {"answer": card})}

        return await run_jobs(text, admission_id, owner=doctor.email, ward=ward, model=model, model_id=settings.model, portal=portal,
                              running=await asyncio.to_thread(running_now), conversations=conversations, reply_to=reply_to, execute=execute)

    app.state.agent_command = run_command
    planner = Planner(desk, get_model=get_model, model_id=settings.model, portal=portal, conversations=conversations, kick=kick_robot)
    app.state.planner = planner

    def open_tasks() -> list[dict]:
        worker = desk.worker_state()
        return [desk.view(t, worker) for status in OPEN for t in record.store.query(TASKS, status=status)]

    app.state.open_tasks = open_tasks

    @app.post("/agent/command")
    async def agent_command(body: CommandIn, doctor: Doctor = Depends(signed_in)):
        if not body.text.strip():
            raise HTTPException(400, "empty command")
        result = await run_command(body.text, body.admission_id, doctor)
        return {"handled": result is not None, **(result or {})}

    @app.get("/agent/tasks/{task_id}")
    def agent_task(task_id: str, doctor: Doctor = Depends(signed_in)):
        desk.sweep()
        return desk.view(open_task(task_id))

    @app.post("/agent/tasks/{task_id}/stop")
    def agent_stop(task_id: str, doctor: Doctor = Depends(signed_in)):
        open_task(task_id)
        return desk.view(desk.stop(task_id, doctor.name))

    @app.get("/agent/tasks")
    def agent_task_list(limit: int = 30, doctor: Doctor = Depends(signed_in)):
        """Every agent job of this ward, newest first, with what the Tasks list needs (job, patient, status, time, answer)."""
        desk.sweep()
        worker = desk.worker_state()
        rows = [t for status in (*OPEN, *ENDED) for t in record.store.query(TASKS, status=status)]
        rows.sort(key=lambda t: t.get("created_at") or "", reverse=True)
        return {"worker": worker, "tasks": [desk.view(t, worker) for t in rows[:max(1, min(limit, 100))]]}

    @app.get("/agent/watches")
    def agent_watches(admission_id: str | None = None, include_ended: bool = False, doctor: Doctor = Depends(signed_in)):
        worker = desk.worker_state()
        return {"worker": worker, "watches": [desk.view(t, worker)
                                              for t in desk.watches(admission_id=admission_id, include_ended=include_ended)]}

    @app.get("/agent/worker")
    def agent_worker(doctor: Doctor = Depends(signed_in)):
        return desk.worker_state()

    def night_log(task_id: str) -> dict:
        task = open_task(task_id)
        if task.get("kind") != "watch":
            raise HTTPException(404, "not a watch")
        return desk.view(task)

    @app.get("/agent/watches/{task_id}/log")
    def agent_log(task_id: str, doctor: Doctor = Depends(signed_in)):
        view = night_log(task_id)
        lines = []
        for raw in view["steps"] or []:
            when, _, what = raw.partition(" · ")
            lines.append({"time": when, "text": what, "danger": what.startswith("DANGER")} if _ else
                         {"time": "", "text": raw, "danger": False})
        return {**{k: view[k] for k in ("id", "title", "bed", "test", "status", "active", "health", "alert", "checks",
                                        "problems", "last_check_at", "worker_online")}, "lines": lines}

    @app.get("/agent/watches/{task_id}/night-log", include_in_schema=False)
    def agent_night_page(task_id: str, doctor: Doctor = Depends(signed_in)):
        view = night_log(task_id)
        rows = "".join(f'<p class="{"d" if " · DANGER · " in line else ""}">{html.escape(line)}</p>' for line in view["steps"] or [])
        return HTMLResponse(
            '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="refresh" content="30"><title>WardOS night log</title><style>'
            'body{margin:0;padding:6vh 6vw;background:#0b1512;color:#d8efe6;font:500 clamp(16px,1.7vw,26px)/1.65 Figtree,system-ui,sans-serif}'
            'h1{font-size:1.25em;margin:0 0 .3em}.h{color:#8fb5a7;margin:0 0 1.5em}p{margin:0}.d{color:#ff8f8f;font-weight:700}</style>'
            f'<h1>{html.escape(view.get("title") or "Watch")}</h1><div class="h">{html.escape(view.get("health") or view.get("status") or "")}</div>{rows}',
            headers={"Cache-Control": "no-store"})

    def guarded(work):
        try:
            return work()
        except TaskGone as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/agent/worker/next")
    def worker_next(body: NextIn, who=Depends(worker_only)):
        answer = desk.claim_next(body.worker, body.batch_id)
        return {**answer, "task": _for_worker(answer["task"])}

    def _for_worker(task: dict | None) -> dict | None:
        if not task:
            return None
        out = {k: task.get(k) for k in ("id", "kind", "admission_id", "bed", "test", "question", "steps", "checks")}
        hint = task.get("skill_hint")
        if hint and out["question"]:  # the planner's choice reaches the hospital brain as one plain line in the question
            out["question"] += (" (Do not use a saved skill; explore the site to find it.)" if hint == "explore"
                                else f" (Use the saved skill {hint}.)")
        return out

    @app.post("/agent/worker/heartbeat")
    def worker_heartbeat(body: StepIn, who=Depends(worker_only)):
        desk.heartbeat(body.worker, body.line)
        return {"ok": True}

    @app.post("/agent/worker/tasks/{task_id}/patient")
    def worker_patient(task_id: str, body: WorkerIn, who=Depends(worker_only)):
        return guarded(lambda: desk.identity(task_id, body.worker))

    @app.post("/agent/worker/tasks/{task_id}/step")
    def worker_step(task_id: str, body: StepIn, who=Depends(worker_only)):
        guarded(lambda: desk.step(task_id, body.worker, body.line, body.thought))
        return {"ok": True}

    @app.post("/agent/worker/tasks/{task_id}/tally")
    def worker_tally(task_id: str, body: TallyIn, who=Depends(worker_only)):
        task = open_task(task_id)
        if task.get("worker") != body.worker:
            raise HTTPException(409, "not this worker's task")
        return desk.tally(task, body.source_ids)

    @app.post("/agent/worker/tasks/{task_id}/finish")
    async def worker_finish(task_id: str, body: FinishIn, who=Depends(worker_only)):
        extra = {k: v for k, v in (("new_reports", body.new_reports), ("attention", body.attention), ("answer", body.answer),
                                  ("skill", body.skill), ("skill_state", body.skill_state),
                                  ("source_ids", body.source_ids))
                 if v is not None}
        try:
            await asyncio.to_thread(guarded, lambda: desk.finish(task_id, body.worker, status=body.status, line=body.line, **extra))
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        try:
            await planner.after(task_id)
        except Exception:
            pass  # the planner only adds a next step; it never makes a finished job fail
        return {"ok": True}

    @app.post("/agent/worker/tasks/{task_id}/round")
    def worker_round(task_id: str, body: RoundIn, who=Depends(worker_only)):
        task = guarded(lambda: desk.finish_round(task_id, body.worker, outcome=body.outcome, source_ids=body.source_ids,
                                                 reports=body.reports, problem=body.problem))
        return {"status": task.get("status"), "line": (task.get("steps") or [""])[-1], "danger": bool(task.get("alert"))}

    @app.post("/agent/worker/tasks/{task_id}/logged-out")
    async def worker_logged_out(task_id: str, body: WorkerIn, who=Depends(worker_only)):
        """The site logged this robot out (a second login on the same account): one robot at a time from now on."""
        desk.lower_robots("logged out by the site")
        line = "Logged out by the site. Only one robot will work at a time now."
        try:
            await asyncio.to_thread(guarded, lambda: desk.finish(task_id, body.worker, status="failed", line=line))
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        try:
            await planner.after(task_id)  # the planner can retry it; with one robot allowed the retry runs alone
        except Exception:
            pass
        return {"ok": True, "max_robots": desk.max_robots()}

    @app.post("/agent/worker/logins")
    def worker_logins(body: LoginsIn, who=Depends(worker_only)):
        return desk.logins(body.worker, body.add, body.failed)

    @app.post("/agent/worker/give-up")
    def worker_give_up(body: GiveUpIn, who=Depends(worker_only)):
        return {"ended": desk.give_up(body.worker, body.line)}

    @app.post("/admissions/{admission_id}/report-values")
    def report_values(admission_id: str, body: ValuesIn, caller=Depends(any_caller)):
        admission = record.store.get(m.ADMISSIONS, admission_id)
        if not admission:
            raise HTTPException(404, "no such admission")
        if admission.get("archived") and admission.get("old_case"):
            raise HTTPException(409, "Old stays are view-and-ask only")
        before = alert_snapshot(admission_id)
        new, skipped = add_portal_values(record, admission_id, [v.model_dump() for v in body.values], body.portal)
        if new:
            notify(admission_id, before)
        return {"source_ids": new, "skipped": skipped}

    return desk


__all__ = ["install_agent", "add_portal_values", "NAME_MISMATCH"]

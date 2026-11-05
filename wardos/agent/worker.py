"""The laptop worker: asks WardOS for the next job, does it with its hands, reports every step."""
from __future__ import annotations

import asyncio
import time

from .client import ServerError, TaskGone

FAILED_LOGINS_LIMIT = 3
LOGINS_LIMIT, LOGINS_WINDOW_S = 12, 12 * 3600
READ_WAIT_S = 180
KEEP_ALIVE_AFTER_S = 12 * 60
LOGIN_STOPPED = "eHospital login failed 3 times. Stopped so the account is not locked. Ask again to retry."
TOO_MANY_LOGINS = "Too many eHospital logins in 12 hours. Stopped so the OTP mailbox is not filled. Ask again to retry."


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


class Worker:
    def __init__(self, client, hands, *, poll_s: float = 5.0, sleep=asyncio.sleep, clock=time.monotonic, wall=time.time,
                 alive_s: float = 30.0):
        self.client, self.hands, self.poll_s, self.sleep, self.clock, self.wall = client, hands, poll_s, sleep, clock, wall
        self.alive_s = alive_s
        self.batch = False  # cloud robot: keep claiming the same batch inside one hospital login
        self.batch_id: str | None = None
        self.others = 0
        self.failed_logins = 0
        self.login_times: list[float] = []
        self.stopped = False
        self.logged_out = False
        self.last_visit = clock()
        self.last_answer: dict = {}

    def _count_logins(self, fetched_logins: int, failed: bool) -> None:
        now = self.wall()
        self.login_times = [t for t in self.login_times if now - t < LOGINS_WINDOW_S] + [now] * fetched_logins
        self.failed_logins = self.failed_logins + max(1, fetched_logins) if failed else 0
        note = getattr(self.client, "note_logins", None)
        if note:
            try:
                shared = note(fetched_logins, failed)
                if isinstance(shared, dict) and "times" in shared:  # the server's copy counts every robot
                    self.login_times, self.failed_logins = list(shared["times"]), int(shared.get("failed") or 0)
            except Exception:
                pass  # the count stays in this process; the server copy catches up at the next login

    def _guard_line(self) -> str | None:
        if self.failed_logins >= FAILED_LOGINS_LIMIT:
            return LOGIN_STOPPED
        return TOO_MANY_LOGINS if len(self.login_times) >= LOGINS_LIMIT else None

    def _trip_if_needed(self) -> bool:
        line = self._guard_line()
        if not line:
            return False
        self.stopped = True
        self.client.give_up(line)
        print("Login guard: everything open was ended; waiting for a new command.", flush=True)
        return True

    def _load_guard(self) -> None:
        """A cloud robot starts fresh for each stretch of work: take the logins already made from the server."""
        load = getattr(self.client, "logins", None)
        if not load:
            return
        try:
            kept = load()
            now = self.wall()
            self.login_times = [t for t in kept.get("times") or [] if now - t < LOGINS_WINDOW_S]
            self.failed_logins = int(kept.get("failed") or 0)
        except Exception:
            pass

    async def run_resident(self, *, idle_exit_s: float, max_run_s: float) -> str:
        """Cloud job: work while there is anything to do, leave after idle_exit_s of nothing, or at the time limit."""
        started = last_active = self.clock()
        ever_worked, failures = False, 0
        self._load_guard()
        self.batch = True
        try:
            return await self._resident_loop(started, last_active, ever_worked, failures, idle_exit_s, max_run_s)
        finally:
            await self._end_batch()

    async def _resident_loop(self, started, last_active, ever_worked, failures, idle_exit_s, max_run_s) -> str:
        while self.clock() - started < max_run_s:
            try:
                worked = await self.run_once()
                failures = 0
            except Exception as error:
                import requests
                if not isinstance(error, (ServerError, OSError, requests.RequestException)):
                    print(f"Worker problem ({type(error).__name__}).", flush=True)
                worked, failures = False, failures + 1
                if failures >= 5:
                    return "lost WardOS"
            answer = self.last_answer
            if worked:
                ever_worked = last_active = self.clock()
                if self.stopped:
                    return "login guard"
                if self.logged_out:
                    return "logged out by the site"
                continue
            if answer.get("refused"):
                return "another worker is running"
            if answer.get("active_watches"):
                last_active = self.clock()
            elif self.clock() - last_active >= (idle_exit_s if ever_worked else 0) and not failures:
                return "nothing to do"
            await self.sleep(self.poll_s)
        return "time limit"

    async def run_forever(self) -> None:
        failures = 0
        while True:
            try:
                worked = await self.run_once()
                failures = 0
            except (ServerError, OSError) as error:
                worked = False
                failures += 1
                if failures in (1, 12):
                    print(f"Cannot reach WardOS ({type(error).__name__}). Still trying.", flush=True)
            except Exception as error:
                worked = False
                import requests
                if not isinstance(error, requests.RequestException):
                    print(f"Worker problem ({type(error).__name__}). Still running.", flush=True)
            if not worked:
                await self.sleep(self.poll_s)

    async def _end_batch(self) -> None:
        self.batch_id = None
        if hasattr(self.hands, "batching"):
            self.hands.batching = False
        end = getattr(self.hands, "end_batch", None)
        if end:
            try:
                await end()
            except Exception:
                pass

    async def run_once(self) -> bool:
        answer = self.client.next(self.batch_id) if self.batch_id else self.client.next()
        if self.batch_id and answer.get("batch_done"):  # the batch is empty: close its hospital login, look for another batch
            await self._end_batch()
            answer = self.client.next()
        self.last_answer = answer
        self.others = int(answer.get("others") or 0)
        if answer.get("refused"):
            print(answer["refused"], flush=True)
            await self.sleep(30)
            return False
        task = answer.get("task")
        if not task:
            await self._end_batch()
            await self._keep_alive(answer)
            return False
        if self.batch:
            self.batch_id = answer.get("batch_id")
            if not self.batch_id:  # a watch round does not belong to a batch
                await self._end_batch()
            else:
                self.hands.batching = True
        if self.stopped:
            self.stopped = False
            self.failed_logins = FAILED_LOGINS_LIMIT - 1
            self.login_times = []
        print(f"Job {task['id']}: {task['kind']}", flush=True)
        kinds = {"get_reports": self._get_reports, "watch": self._watch_round, "hospital_question": self._hospital_question}
        job = asyncio.ensure_future(kinds[task["kind"]](task))
        alive = asyncio.ensure_future(self._stay_alive(task["id"], job))
        try:
            await job
        except (TaskGone, asyncio.CancelledError):
            print(f"Job {task['id']}: stopped by the doctor.", flush=True)
        except (ServerError, OSError) as error:
            print(f"Job {task['id']}: lost WardOS ({type(error).__name__}).", flush=True)
        finally:
            alive.cancel()
        self.last_visit = self.clock()
        if getattr(self.hands, "session_lost", False) and self.others:
            await self._logged_out(task["id"])
            return True
        self._trip_if_needed()
        return True

    async def _logged_out(self, task_id: str) -> None:
        """A login happened again in the middle of a batch while another robot works: the site logged us out."""
        self.hands.session_lost = False
        self.logged_out = True
        try:
            self.client.logged_out(task_id)
        except Exception:
            pass  # the task is already ended or gone; the server still lowers the robots on the next report
        await self._end_batch()
        print("Logged out by the site: this robot stops, one robot at a time from now on.", flush=True)

    async def _stay_alive(self, task_id: str, job) -> None:
        while not job.done():
            await self.sleep(self.alive_s)
            try:
                await asyncio.to_thread(self.client.step, task_id)
            except TaskGone:
                job.cancel()
                return
            except Exception:
                continue

    async def _keep_alive(self, answer: dict) -> None:
        if not answer.get("active_watches") or self.stopped:
            return
        due = answer.get("next_due_in_s")
        if self.clock() - self.last_visit < KEEP_ALIVE_AFTER_S or (due is not None and due < 180):
            return
        self.last_visit = self.clock()
        try:
            self._count_logins(await self.hands.visit(), False)
        except Exception:
            self._count_logins(1, True)
        self._trip_if_needed()

    async def _into_record(self, task: dict, who: dict, fetched) -> list[str]:
        site, admission = self.hands.name, task["admission_id"]
        try:
            if fetched.values:
                return self.client.post_values(admission, fetched.values, site)["source_ids"]
            return [self.client.post_report(admission, path, site)["source_id"] for path in fetched.pdfs]
        except Exception:
            self.hands.forget(who, fetched.keys)
            raise

    async def _wait_read(self, task_id: str, source_ids: list[str]) -> dict:
        end = self.clock() + READ_WAIT_S
        while True:
            tally = self.client.tally(task_id, source_ids)
            if not tally["pending"] or self.clock() >= end:
                return tally
            await self.sleep(3)

    def _who(self, task: dict, say) -> dict | None:
        who = self.client.patient(task["id"])
        if not who.get("uhid"):
            say("This patient has no UHID in the record. Nothing was opened.")
            return None
        if not who.get("name"):
            say("This patient has no name in the record, so eHospital's name cannot be checked. Nothing was opened.")
            return None
        return who

    async def _get_reports(self, task: dict) -> None:
        task_id = task["id"]
        self.client.step(task_id, "Laptop worker started", thought="Opening eHospital for this patient")
        ended = []
        who = self._who(task, ended.append)
        if not who:
            self.client.finish(task_id, "failed", ended[0])
            return
        def say(line):
            try:
                self.client.step(task_id, line)
            except ServerError:
                pass
        fetched = await self.hands.fetch_new(who, say)
        self._count_logins(fetched.logins, fetched.login_failed)
        if fetched.status == "NAME_MISMATCH":
            self.client.finish(task_id, "failed", "eHospital shows a different name for this UHID. Nothing was downloaded.")
        elif fetched.status == "NOTHING_NEW":
            self.client.step(task_id, f"{_plural(fetched.found, 'report')} found, 0 new")
            self.client.finish(task_id, "done", "No new reports", new_reports=0, attention=0)
        elif fetched.status == "NOT_FOUND":
            self.client.finish(task_id, "done", "eHospital shows no reports for this patient", new_reports=0, attention=0)
        elif fetched.status == "FOUND":
            self.client.step(task_id, f"{_plural(fetched.found, 'report')} found, {fetched.new} new",
                             thought="Reading the new reports")
            source_ids = await self._into_record(task, who, fetched)
            self.client.step(task_id, "Reading the new reports")
            tally = await self._wait_read(task_id, source_ids)
            if tally["pending"] or tally["failed"]:
                line = "The reports are saved but not all are read yet. Look again in a minute."
            else:
                line = f"{_plural(fetched.new, 'new report')} · {tally['attention']} need attention"
            self.client.finish(task_id, "done", line, new_reports=fetched.new, attention=tally["attention"],
                               source_ids=source_ids)
        else:
            self.client.finish(task_id, "failed", f"{fetched.problem or 'eHospital check did not finish'}. Nothing new was added.")

    async def _watch_round(self, task: dict) -> None:
        task_id = task["id"]
        self.client.step(task_id, thought=f"Checking eHospital for new {task.get('test')}")
        ended = []
        who = self._who(task, ended.append)
        if not who:
            self.client.round(task_id, "problem", problem=ended[0])
            return
        fetched = await self.hands.fetch_new(who, lambda line: None)
        self._count_logins(fetched.logins, fetched.login_failed)
        if fetched.status == "FOUND":
            source_ids = await self._into_record(task, who, fetched)
            await self._wait_read(task_id, source_ids)
            self.client.round(task_id, "found", source_ids=source_ids, reports=fetched.new)
        elif fetched.status in ("NOTHING_NEW", "NOT_FOUND"):
            self.client.round(task_id, "nothing_new")
        elif fetched.status == "NAME_MISMATCH":
            self.client.round(task_id, "name_mismatch")
        else:
            self.client.round(task_id, "problem", problem=fetched.problem)

    async def _hospital_question(self, task: dict) -> None:
        task_id = task["id"]
        self.client.step(task_id, "Laptop worker started", thought="Asking the hospital website")
        def say(line):
            try:
                self.client.step(task_id, line)
            except ServerError:
                pass
        try:
            answer, logins = await self.hands.ask(task.get("question") or "", say)
            self._count_logins(logins, False)
        except TaskGone:
            raise
        except Exception as error:
            self._count_logins(1, "Session" in type(error).__name__)
            self.client.finish(task_id, "failed", f"eHospital did not answer ({type(error).__name__}).")
            return
        skill = getattr(self.hands, "last_skill", None) or {}
        extra = {k: v for k, v in (("skill", skill.get("name")), ("skill_state", skill.get("state"))) if v}
        self.client.finish(task_id, "done", answer[:600], answer=answer[:4000], **extra)

"""The hands: one hospital site, seen as a few plain jobs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import re

from ..hospital.runlog import RunLog

SKILL = "lis.fetch_lab_reports_learned"
TRIES = 2


@dataclass
class Fetched:
    status: str
    found: int = 0
    new: int = 0
    pdfs: list[str] = field(default_factory=list)
    values: list[dict] = field(default_factory=list)
    problem: str = ""
    logins: int = 0
    login_failed: bool = False
    keys: list[str] = field(default_factory=list)


_SAY = ((re.compile(r"^Logging in"), lambda _: "Signing in to eHospital"),
        (re.compile(r"^Login finished"), lambda _: "Signed in to eHospital"),
        (re.compile(r"^Hospital session is healthy"), lambda _: "Signed in to eHospital"),
        (re.compile(r"^Running skill lis\."), lambda _: "Opening lab reports"))


class _StepLog(RunLog):
    say_to = None
    said: set

    def say(self, message: str) -> None:
        for pattern, line in _SAY:
            if pattern.search(message) and self.say_to and line(message) not in self.said:
                self.said.add(line(message))
                self.say_to(line(message))


class EHospitalHands:
    name = "ehospital"

    def __init__(self, *, headless: bool = True, settings=None):
        from ..config import load_settings
        self.settings, self.headless = settings or load_settings(), headless
        self.batching = False      # True while a batch of jobs shares one hospital login
        self.session_lost = False  # a login had to happen again in the middle of a batch
        self._held = None

    @property
    def ledger_dir(self) -> Path:
        return self.settings.private_dir / "agent" / "ledger"

    def _keeper(self, models, otp, *, read_like=False):
        from ..hospital.portals import get_portal
        from ..hospital.session import SessionKeeper
        keeper_class = SessionKeeper
        if read_like:
            from ..hospital.brain import ReadSessionKeeper as keeper_class
        return keeper_class(self.settings, get_portal(self.name), models, otp, headless=self.headless,
                            firewall_mode="enforce", firewall_read_like=read_like, keep_login=True, max_recoveries=1,
                            console=False, ledger_dir=self.ledger_dir)

    def _tools(self):
        from ..hospital.otp import ImapOtpProvider
        from ..models import get_model_client
        settings = self.settings
        return get_model_client(settings), ImapOtpProvider(settings.otp_config_file, settings.otp_source_file,
                                                           settings.otp_window_seconds)

    async def _open(self, read_like: bool) -> dict:
        """The browser and login for one job: the batch's open one if it fits, else a new one (saved login reused)."""
        if self._held and self._held["read_like"] == read_like:
            return self._held
        await self.end_batch()
        models, otp = self._tools()
        held = dict(read_like=read_like, models=models, otp=otp, keeper=self._keeper(models, otp, read_like=read_like), counted=0)
        try:
            await held["keeper"].start()
        except BaseException:
            await self._close(held)
            raise
        if self.batching:
            self._held = held
        return held

    @staticmethod
    async def _close(held: dict) -> None:
        await held["keeper"].stop()
        await held["otp"].close()
        if hasattr(held["models"], "aclose"):
            await held["models"].aclose()

    async def end_batch(self) -> None:
        held, self._held = self._held, None
        if held:
            await self._close(held)

    async def _done_with(self, held, *, broken: bool = False) -> None:
        if held is None or (held is self._held and not broken):
            return
        if held is self._held:
            self._held = None
        await self._close(held)

    def _new_logins(self, held) -> int:
        """Logins made since the last job reported; a login after the first in a batch means the site dropped the session."""
        if held is None:
            return 0
        made = held["keeper"].logins - held["counted"]
        if made and held["counted"] and self._held is held:
            self.session_lost = True
        held["counted"] += made
        return made

    async def fetch_new(self, who: dict, say) -> Fetched:
        from ..hospital.session import SessionError
        held, broken = None, False
        try:
            held = await self._open(False)
            keeper = held["keeper"]
            log = _StepLog(RunLog.create(self.settings.runs_dir, "agent-fetch", console=False).directory, False)
            log.say_to, log.said = say, set()
            result = None
            for attempt in range(TRIES):
                result = await keeper.run_skill(SKILL, {"uhid": who["uhid"], "patient_name": who["name"], "select": "new"}, log=log)
                if result.status != "STEP_FAILED":
                    break
                if attempt + 1 < TRIES:
                    say("The page was slow. Trying once more")
                    await asyncio.sleep(5)
            return self._fetched(result, self._new_logins(held))
        except SessionError as error:
            broken = True
            busy = "another WardOS job" in str(error)
            return Fetched("PROBLEM", problem="Another eHospital job is running" if busy else "eHospital login failed",
                           logins=self._new_logins(held), login_failed=not busy)
        except Exception as error:
            broken = True
            return Fetched("PROBLEM", problem=f"eHospital not answering ({type(error).__name__})", logins=self._new_logins(held))
        finally:
            await self._done_with(held, broken=broken)

    @staticmethod
    def _fetched(result, logins: int) -> Fetched:
        status, outputs = result.status, result.outputs or {}
        rows = outputs.get("rows") or []
        base = dict(found=len(rows), logins=logins)
        if status == "FOUND":
            pdfs = [a["path"] for a in result.artifacts if a.get("check", {}).get("ok")]
            keys = list(outputs.get("selected_keys") or [])
            return Fetched("FOUND", new=len(keys), pdfs=pdfs, keys=keys, **base)
        if status in {"NOTHING_NEW", "NOT_FOUND", "NAME_MISMATCH"}:
            return Fetched(status, **base)
        if status == "PDF_NOT_VERIFIED":
            return Fetched("PROBLEM", problem="eHospital's PDF did not match this patient", **base)
        return Fetched("PROBLEM", problem="eHospital check did not finish", **base)

    def forget(self, who: dict, keys: list[str]) -> None:
        from ..hospital.ledger import Ledger
        Ledger(self.ledger_dir, self.name, who["uhid"]).forget(keys)

    async def visit(self) -> int:
        held = None
        try:
            held = await self._open(False)
            await held["keeper"].ensure_healthy(RunLog.create(self.settings.runs_dir, "agent-visit", console=False))
            return self._new_logins(held)
        finally:
            await self._done_with(held)

    async def ask(self, question: str, say=None) -> tuple[str, int]:
        from ..hospital.brain import HospitalBrain
        from ..hospital.skill_notes import end_lines
        self.last_skill = {}
        held, broken = None, True
        try:
            held = await self._open(True)
            brain = HospitalBrain(held["keeper"], self.settings)
            brain.on_note = lambda line, state: (say(line) if say else None, self.last_skill.update(state=state))
            answer = await brain.run(question, minutes=5, max_steps=25)
            lines, state = end_lines(answer)
            for line in lines:
                if say:
                    say(line)
            self.last_skill = {"name": answer.skill, "state": state or self.last_skill.get("state")}
            broken = False
            return answer.answer or answer.note or "eHospital did not give an answer.", self._new_logins(held)
        finally:
            await self._done_with(held, broken=broken)

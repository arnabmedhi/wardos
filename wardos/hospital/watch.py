"""Watch one test for one patient, only when told."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import re

from ..chain.today import is_danger
from ..config import hospital_now
from ..record.labs import LabRecord, normalize

MAX_LOGINS = 6
EARLIER_SHOWN = 3
OLD_REPORT = timedelta(hours=24)


def parse_every(text: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh])\s*", text.lower())
    if not match or float(match.group(1)) <= 0:
        raise ValueError(f"use a time like 15m, 1h or 90s, not {text!r}")
    return float(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


def parse_until(text: str, now: datetime) -> datetime:
    moment = datetime.strptime(text.strip(), "%H:%M")
    end = now.replace(hour=moment.hour, minute=moment.minute, second=0, microsecond=0)
    return end if end > now else end + timedelta(days=1)


def is_dangerous(row: dict) -> bool:
    name = re.sub(r"^(?:serum|plasma|s\.)\s+", "", str(row["analyte"]).strip(), flags=re.I)      # "Serum potassium" is potassium
    fact = {"key": normalize(name), "type": "lab_result",
            "value": {"number": row.get("number"), "text": row.get("value"), "unit": row.get("unit"),
                      "reference": row.get("reference")}}
    return is_danger(fact)


def _row_id(row: dict) -> tuple:
    return (row.get("sample_no"), row.get("analyte"), row.get("value"), row.get("collected_at"))


def _short_time(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return "time not shown"


@dataclass
class Watch:
    test: str
    every_s: float
    until: datetime
    record: LabRecord
    log_path: Path
    stop_path: Path
    fetch: object
    visit: object
    ingest: object
    logins: object = lambda: 0
    otps: object = lambda: 0
    keep_alive_s: float | None = None
    max_logins: int = MAX_LOGINS
    now: object = hospital_now
    sleep: object = asyncio.sleep
    rounds: int = 0
    danger_lines: int = 0
    problems: int = 0
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    def write(self, text: str) -> None:
        line = f"{self.now():%H:%M} · {text}"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)

    def _known(self) -> dict[tuple, dict]:
        return {_row_id(row): row for rows in self.record.find(self.test).values() for row in rows}

    def _report(self, known_before: dict, reports: int) -> None:
        current = self._known()
        new = [row for key, row in current.items() if key not in known_before]
        new.sort(key=lambda r: r.get("collected_at") or r.get("verified_at") or "")
        if not new:
            found = f"{reports} new report{'s' if reports != 1 else ''} · " if reports else ""
            self.write(f"checked · {found}no new {self.test}")
            return
        for row in new:
            what = f"{row['analyte']} {row['value']}{' ' + row['unit'] if row.get('unit') else ''}"
            collected = row.get("collected_at") or row.get("verified_at")
            try:
                old = bool(collected) and datetime.fromisoformat(collected) < self.now() - OLD_REPORT
            except ValueError:
                old = False
            if old:
                self.write(f"checked · older report, not new tonight · {what} ({_short_time(collected)})")
            elif is_dangerous(row):
                self.danger_lines += 1
                earlier = sorted((r for key, r in current.items()
                                  if r.get("analyte") == row["analyte"] and _row_id(r) != _row_id(row)
                                  and (r.get("collected_at") or "") <= (collected or "")),
                                 key=lambda r: r.get("collected_at") or "", reverse=True)[:EARLIER_SHOWN]
                history = ", ".join(f"{r['value']} ({_short_time(r.get('collected_at'))})" for r in earlier) or "none"
                normal = f" · normal {row['reference']}" if row.get("reference") else ""
                self.write(f"DANGER · {what}{normal} · collected {_short_time(collected)} · earlier: {history}")
            else:
                self.write(f"checked · new {what} · not dangerous")

    async def _round(self) -> None:
        self.rounds += 1
        known_before = self._known()
        status, paths = await self.fetch()
        if status == "FOUND":
            self.ingest(paths)
            self._report(known_before, len(paths))
        elif status in {"NOTHING_NEW", "NOT_FOUND"}:
            self.write(f"checked · no new {self.test}")
        else:
            self.problems += 1
            self.write(f"eHospital check did not finish ({status}) · trying again next round")

    def _stopped(self) -> bool:
        return self._stop.is_set() or self.stop_path.exists() or self.now() >= self.until

    async def _wait(self, seconds: float) -> None:
        end = self.now() + timedelta(seconds=seconds)
        while not self._stopped() and self.now() < end:
            await self.sleep(min(5.0, max(0.05, (end - self.now()).total_seconds())))

    async def _guarded(self, work, what: str) -> None:
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.problems += 1
            self.write(f"eHospital not answering ({type(exc).__name__}) during {what} · trying again next round")

    async def run(self) -> int:
        self.stop_path.unlink(missing_ok=True)
        self.write(f"watch started · {self.test} · every {self.every_s / 60:g} min · until {self.until:%d/%m %H:%M}")
        reason = "end time reached"
        while not self._stopped():
            started = self.now()
            await self._guarded(self._round, "a check")
            if self.logins() > self.max_logins:
                reason = f"stopped after {self.max_logins} logins, so the mailbox is not filled with OTPs"
                break
            next_round = started + timedelta(seconds=self.every_s)
            while not self._stopped() and self.now() < next_round:
                left = (next_round - self.now()).total_seconds()
                if self.keep_alive_s and left > self.keep_alive_s:
                    await self._wait(self.keep_alive_s)
                    if not self._stopped():
                        await self._guarded(self.visit, "a keep-alive visit")
                else:
                    await self._wait(left)
        if self.stop_path.exists() or self._stop.is_set():
            reason = "told to stop"
        self.stop_path.unlink(missing_ok=True)
        self.write(f"watch ended · {reason} · rounds {self.rounds} · danger lines {self.danger_lines} · "
                   f"problems {self.problems} · logins {self.logins()} · OTPs {self.otps()}")
        return 0

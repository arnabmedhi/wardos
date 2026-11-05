"""The Watch on the OpenMRS demo. No model calls in a round.

OpenMRS has no PDFs, so one round runs the skill ``lab.results.latest`` and turns the rows on the Results screen into
lab values. The danger rule and the log lines are the Watch's own (``hospital/watch.py``); only fetching differs.

    python tools/openmrs_watch.py --bed 12 --test potassium --every 1m --minutes 10
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from wardos.record.labs import LabRecord, LabValue, normalize  # noqa: E402

_VALUE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(.*?)\s*$", re.S)


def _when(text: str, now: datetime) -> datetime | None:
    text = " ".join(text.split())
    for word, back in (("today", 0), ("yesterday", 1)):
        if text.lower().startswith(word + ","):
            try:
                clock = datetime.strptime(text.split(",", 1)[1].strip(), "%I:%M %p")
            except ValueError:
                return None
            return (now - timedelta(days=back)).replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    day, dash, rest = text.partition("-")
    month, dash2, rest = rest.partition("-")
    text = f"{day}-{month[:3]}-{rest}" if dash and dash2 else text   # OpenMRS writes September as "Sept"
    try:
        return datetime.strptime(text, "%d-%b-%Y, %I:%M %p")
    except ValueError:
        return None


def values_from_rows(rows: list[dict], now: datetime) -> tuple[list[LabValue], int]:
    values, unread = [], 0
    for row in rows:
        cells = row["cells"]
        name, when, shown = cells.get("Test name"), cells.get("Result date"), cells.get("Value")
        match = _VALUE.match((shown or "").replace("↓", " ").replace("↑", " "))
        if not (name and match):
            continue
        moment = _when(when or "", now)
        if moment is None:
            unread += 1
            continue
        collected = moment.isoformat()
        unit = " ".join(match.group(2).split()) or None
        reference = (cells.get("Reference range") or "").strip()
        values.append(LabValue(
            analyte=name, value=match.group(1), number=float(match.group(1)), flag=None, unit=unit,
            reference=reference if re.search(r"\d", reference) else None, panel=None, lab="OpenMRS", sample_no=collected,
            collected_at=collected, verified_at=None, page=0,
            source_sha256=hashlib.sha256(f"{name}|{collected}".encode()).hexdigest()))
    return values, unread


def new_values(values: list[LabValue], known: list[dict]) -> list[LabValue]:
    seen = {(normalize(k["analyte"]), k["collected_at"], k["value"]) for k in known}
    return [v for v in values if (normalize(v.analyte), v.collected_at, v.value) not in seen]


async def run(args) -> int:
    import openmrs_seed as seed
    from wardos import cli
    from wardos.config import hospital_now, load_settings
    from wardos.hospital.watch import Watch, parse_every

    people = seed.load_people()
    if args.bed not in people:
        print(f"No bed {args.bed}. Beds: {', '.join(people)}")
        return 2
    patient = seed.Site().find_patient(people[args.bed])
    if patient is None:
        print(f"Bed {args.bed} is not on the site: run openmrs_seed.py --seed first.")
        return 2
    identifier = patient["display"].split(" - ")[0]
    every = parse_every(args.every)
    settings = load_settings()
    folder = settings.private_dir / "watch"
    record = LabRecord(folder / "openmrs-records", "openmrs", identifier)
    record.path.unlink(missing_ok=True)
    namespace = argparse.Namespace(portal="openmrs", firewall="enforce", read_like=True, headless=args.headless,
                                   uhid=None, uhid_file=None)

    async def work(_settings, keeper):
        async def read_screen() -> tuple[str, list[LabValue]]:
            result = await keeper.run_skill("lab.results.latest", {"uhid": identifier})
            if result.status != "OK":
                return result.status, []
            values, unread = values_from_rows(result.outputs.get("answer_rows") or [], hospital_now().replace(tzinfo=None))
            if unread:
                return f"{unread} rows not readable", []
            return ("OK", values) if values else ("no results on the screen", [])

        async def fetch():
            status, values = await read_screen()
            if status != "OK":
                return status, []
            fresh = new_values(values, record.load()["values"])
            return ("FOUND", fresh) if fresh else ("NOTHING_NEW", [])

        async def visit():
            await keeper.run_skill("nav.home", {})

        status, values = await read_screen()
        if status != "OK":
            print(f"Could not read the starting values ({status}).")
            return 2
        record.add(values, Path("openmrs-results-screen"))
        now = hospital_now()
        watch = Watch(test=args.test, every_s=every, until=now + timedelta(minutes=args.minutes), record=record,
                      log_path=folder / f"{now:%Y%m%d-%H%M}-openmrs-bed{args.bed}.log",
                      stop_path=folder / "STOP-openmrs", fetch=fetch, visit=visit,
                      ingest=lambda fresh: record.add(fresh, Path("openmrs-results-screen")))
        print(f"Starting values: {len(values)}. Log: {watch.log_path}")
        return await watch.run()

    return await cli._live(namespace, work)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bed", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--every", default="1m")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--headless", action="store_true")
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())

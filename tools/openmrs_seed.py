"""Put 5 patients' lab reports on the demo OpenMRS, safely and repeatably.
The site is our own private OpenMRS (address: WARDOS_OPENMRS_URL, login: OPENMRS_USERNAME / OPENMRS_PASSWORD).

The site is meant to be shown to judges, so only test, value, unit and the time gap between reports go there. The private data file
(``private/openmrs/patients.json``) holds nothing else: names, sex and birth dates are made up here, and no real date,
UHID, note or medicine is ever copied into it.

    python tools/openmrs_seed.py --build-data            read the record (read-only), write the private file
    python tools/openmrs_seed.py --seed                  add what is missing on the site (safe to run again)
    python tools/openmrs_seed.py --check                 are our patients, beds and reports still there?
    python tools/openmrs_seed.py --add-held-back 12      the bed's last report arrives now
    python tools/openmrs_seed.py --add-potassium 12 6.8  a made-up potassium for showing the danger line
    python tools/openmrs_seed.py --reset-held-back 12    take those back off, to rehearse again
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wardos.config import openmrs_login, openmrs_rest_base            # noqa: E402  (after the path line above)

BASE = openmrs_rest_base()
DATA_FILE = Path(os.environ["WARDOS_SEED_FILE"]) if os.environ.get("WARDOS_SEED_FILE") else ROOT / "private" / "openmrs" / "patients.json"
PAUSE_S = 0.25

FAMILY = "Wardos"
PEOPLE = {      # made-up identities for the ten demo beds; sex and birth year match the record's ages
    "11": ("Sarita", "F", "1959-05-11"),
    "12": ("Meena", "F", "1981-03-14"),
    "13": ("Usha", "F", "1994-08-02"),
    "14": ("Kamla", "F", "1996-01-19"),
    "15": ("Geeta", "F", "1975-07-23"),
    "16": ("Anita", "F", "1996-11-05"),
    "17": ("Nisha", "F", "1996-04-27"),
    "18": ("Radha", "F", "2001-09-16"),
    "19": ("Vikas", "M", "2008-02-08"),
    "20": ("Arjun", "M", "2001-06-30"),
}
SELECTION_FILE = ROOT / "private" / "demo10" / "selection.json"      # bed -> which saved case it was built from (private)

# The made-up hospital: ward name, number of beds, bed-number prefix, how many beds are full (demo patients included).
WARDS = {"Surgery": (40, "SUR", 31), "Medicine": (40, "MED", 29), "ObsGyn": (20, "OBG", 15),
         "Paediatrics": (20, "PED", 14), "ENT": (20, "ENT", 14)}
WARD_OF = {"11": "Medicine", "12": "Medicine", "13": "ObsGyn", "14": "ObsGyn", "15": "Medicine", "16": "ObsGyn",
           "17": "ObsGyn", "18": "ObsGyn", "19": "Medicine", "20": "Surgery"}
WARD_TAGS = ["Admission Location", "Transfer Location", "Login Location"]
DISCHARGE_ENCOUNTER = "181820aa-88c9-479b-9077-af92f5364329"
LEGACY_NAMES = [("Harish", "Wardos"), ("Meera", "Wardos"), ("Gopal", "Wardos"), ("Dinesh", "Wardos"), ("Mohan", "Wardos")]
FILLER_GIVEN = ["Aashvi", "Bimla", "Chandra", "Deepa", "Esha", "Farida", "Gauri", "Hema", "Indu", "Jaya", "Kiran", "Leela", "Manju",
                "Nalini", "Om", "Padma", "Qamar", "Ranjika", "Seema", "Tara", "Uma", "Vimla", "Yash", "Zoya", "Amit", "Bhanu",
                "Chetan", "Dev", "Ejaz", "Farhan", "Girish", "Hari", "Imran", "Jatin", "Kabir", "Lalit", "Manoj", "Naveen",
                "Omkar", "Pawan", "Ravi", "Sanjay", "Tarun", "Umesh", "Vijay", "Waman", "Yogesh", "Zubin"]
FILLER_FAMILY = ["Demo", "Sample", "Trial", "Mock", "Test"]

WARD = "5a7f3c53-6bb4-448b-a966-5e65b397b9f3"
VISIT_LOCATION = "ba685651-ed3b-4e63-9b35-78893060758a"
VISIT_TYPE = "7b0f5697-27e3-40c4-8bae-f4049abfb4ed"
LAB_ENCOUNTER = "3596fafb-6f6f-4396-8c87-6e63a0f1bd71"
ADMISSION_ENCOUNTER = "e22e39fd-7db2-45e7-80f1-60fa0d5a4378"
OPENMRS_ID_TYPE = "05a29f94-c0ed-11e2-94be-8c13b969e334"
OPENMRS_ID_SOURCE = "8549f706-7e85-4c1d-9424-217d50a2988b"
POTASSIUM = "1133AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def concept(number: str) -> str:
    return number + "A" * (36 - len(number))


TESTS = {
    "haemoglobin": (concept("21"), "g/dL", 1, 1, 1, 25),
    "tlc": (concept("678"), "10^3/uL", 0.001, 2, 0.1, 200),
    "platelets": (concept("729"), "10^3/mL", 0.001, 0, 1, 2000),
    "rbc": (concept("679"), "10^6/uL", 1, 2, 0.5, 10),
    "hct": (concept("1015"), "%", 1, 1, 5, 75),
    "haematocrit": (concept("1015"), "%", 1, 1, 5, 75),
    "mcv": (concept("851"), "fL", 1, 0, 40, 150),
    "mch": (concept("1018"), "pg", 1, 1, 10, 50),
    "mchc": (concept("1017"), "g/dL", 1, 1, 20, 45),
    "neu%": (concept("1022"), "%", 1, 0, 0, 100),
    "lym%": (concept("1021"), "%", 1, 0, 0, 100),
    "creatinine": (concept("790"), "umol/L", 88.4, 1, 0.05, 30),
    "urea": (concept("163699"), "mg/dL", 1, 0, 2, 500),
    "sodium": (concept("1132"), "mmol/L", 1, 0, 90, 190),
    "potassium": (POTASSIUM, "mmol/L", 1, 2, 1, 12),
    "chloride": (concept("1134"), "mmol/L", 1, 0, 60, 150),
    "calcium": (concept("159497"), "mg/dL", 1, 2, 2, 20),
    "s. calcium": (concept("159497"), "mg/dL", 1, 2, 2, 20),
    "bilirubin": (concept("655"), "umol/L", 17.1, 1, 0, 60),
    "direct bilirubin": (concept("1297"), "umol/L", 17.1, 0, 0, 40),
    "sgot": (concept("653"), "IU/L", 1, 0, 1, 20000),
    "sgpt": (concept("654"), "IU/L", 1, 1, 1, 20000),
    "alp": (concept("785"), "U/L", 1, 0, 1, 10000),
    "albumin": (concept("848"), "g/dL", 1, 2, 0.5, 8),
    "total protein": (concept("717"), "g/dL", 1, 2, 1, 15),
    "uric acid": (concept("159825"), "mg/dL", 1, 2, 0.2, 30),
    "glucose": (concept("887"), "mg/dl", 1, 0, 10, 1500),
    "blood glucose": (concept("887"), "mg/dl", 1, 0, 10, 1500),
    "inr test": (concept("161482"), "", 1, 2, 0.5, 20),
    "c reactive protein": (concept("161500"), "mg/L", 1, 1, 0, 700),
}


def site_value(key: str, base: float | None) -> float | None:
    row = TESTS.get(key)
    if row is None or base is None:
        return None
    _, _, factor, decimals, low, high = row
    shown = base if key in ("bilirubin", "direct bilirubin", "creatinine") else base * factor
    if not low <= shown <= high:
        return None
    value = round(base * factor, decimals)
    return int(value) if decimals == 0 else value


def in_record_unit(key: str, base: float | None, fact: dict) -> float | None:
    text = str((fact.get("value") or {}).get("text") or "").lower()
    if base is not None and key in ("bilirubin", "direct bilirubin") and "mol/l" in text:
        return base / 17.1
    return base


def clock(fact: dict) -> datetime | None:
    text = str(fact.get("clinical_time") or "")
    try:
        when = datetime.fromisoformat(text if len(text) > 10 else text + "T08:00:00")
    except ValueError:
        return None
    return when.replace(tzinfo=None)


def reports_from_facts(facts: list[dict], live: tuple, base_of) -> tuple[list[dict], int]:
    grouped: dict[datetime, dict] = {}
    dropped = 0
    for fact in facts:
        if (fact.get("type") != "lab_result" or fact.get("status") not in live or fact.get("assertion") != "present"
                or fact.get("time_precision") not in ("exact", "day") or fact.get("key") not in TESTS):
            continue
        when, value = clock(fact), site_value(fact["key"], in_record_unit(fact["key"], base_of(fact), fact))
        if when is None or value is None or fact.get("quote_found") is False:
            dropped += 1
            continue
        uuid, unit = TESTS[fact["key"]][:2]
        grouped.setdefault(when, {}).setdefault(uuid, {"key": fact["key"], "concept": uuid, "value": value, "unit": unit})
    if not grouped:
        return [], dropped
    first = min(grouped)
    return ([{"offset_s": int((when - first).total_seconds()), "results": list(grouped[when].values())}
             for when in sorted(grouped)], dropped)


def split_held_back(reports: list[dict]) -> tuple[list[dict], dict | None]:
    return (reports[:-1], reports[-1]) if len(reports) > 1 else (reports, None)   # a single report stays in the record


def build_data() -> int:
    """Lab values for the ten demo beds, from the cases saved in private/demo10/raw (not from the ward record)."""
    from wardos.chain import model as m
    from wardos.chain.today import _base, _number
    raw = ROOT / "private" / "demo10"
    ident = json.loads((raw / "ident.json").read_text(encoding="utf-8")) if (raw / "ident.json").is_file() else {}
    real_words = {w for one in ident.values() for w in one.get("words", [])}
    if {g.lower() for g, *_ in PEOPLE.values()} & real_words or FAMILY.lower() in real_words:
        print("A made-up name matches a real name word. Change it in PEOPLE.")
        return 2
    out = {}
    for bed in PEOPLE:
        stay = json.loads((raw / "raw" / bed / "stay.json").read_text(encoding="utf-8"))
        left_out = {s["id"] for s in stay["sources"] if s.get("held_reason")}
        mine = [f for f in stay["facts"] if f.get("source_id") not in left_out]
        reports, dropped = reports_from_facts(mine, m.LIVE_STATUSES, lambda f: _base(f["key"], _number(f), f))
        kept, held = split_held_back(reports)
        given, sex, born = PEOPLE[bed]
        out[bed] = {"given": given, "family": FAMILY, "gender": sex, "birthdate": born, "reports": kept, "held_back": held}
        print(f"bed {bed}: {len(kept)} reports, {sum(len(r['results']) for r in kept)} values, "
              f"held back {len(held['results']) if held else 0} values, dropped {dropped}, "
              f"{len({x['key'] for r in reports for x in r['results']})} tests, "
              f"span {reports[-1]['offset_s'] // 86400 if reports else 0} days")
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"Saved privately: {DATA_FILE}")
    return 0


class Site:
    def __init__(self):
        import httpx
        self.http = httpx.Client(auth=openmrs_login(), timeout=60)

    def _call(self, method, path, **kw):
        time.sleep(PAUSE_S)
        reply = self.http.request(method, BASE + path, **kw)
        if reply.status_code >= 400:
            raise RuntimeError(f"{method} {path.split('?')[0]} -> {reply.status_code}: {reply.text[:300]}")
        return reply.json() if reply.content else {}

    def get(self, path, **params):
        return self._call("GET", path, params=params)

    def post(self, path, body):
        return self._call("POST", path, json=body)

    def delete(self, path):
        return self._call("DELETE", path)

    def find_patient(self, person: dict) -> dict | None:
        name = f"{person['given']} {person['family']}"
        rows = self.get("/patient", q=name, limit=10, v="custom:(uuid,display,voided,person:(display))")["results"]
        return next((r for r in rows if not r.get("voided") and r["person"]["display"].lower() == name.lower()), None)

    def active_visit(self, patient: str) -> dict | None:
        rows = self.get("/visit", patient=patient, includeInactive="false",
                        v="custom:(uuid,startDatetime,stopDatetime)")["results"]
        return rows[0] if rows else None

    def lab_encounters(self, patient: str) -> list[dict]:
        rows = self.get("/encounter", patient=patient, encounterType=LAB_ENCOUNTER, limit=100,
                        v="custom:(uuid,encounterDatetime,obs:(concept:(uuid),value))")["results"]
        return rows

    def beds(self) -> dict[str, dict]:
        return self.all_beds()

    def make_patient(self, person: dict) -> dict:
        number = self.post(f"/idgen/identifiersource/{OPENMRS_ID_SOURCE}/identifier", {})["identifier"]
        types = self.get("/patientidentifiertype", v="custom:(uuid,required)")["results"]
        extras = [{"identifier": luhn_number(uuid4().int % 10**9), "identifierType": t["uuid"], "location": VISIT_LOCATION}
                  for t in types if t["required"] and t["uuid"] != OPENMRS_ID_TYPE]
        return self.post("/patient", {
            "person": {"names": [{"givenName": person["given"], "familyName": person["family"], "preferred": True}],
                       "gender": person["gender"], "birthdate": person["birthdate"]},
            "identifiers": [{"identifier": number, "identifierType": OPENMRS_ID_TYPE,
                             "location": VISIT_LOCATION, "preferred": True}] + extras})

    def make_bed(self, number: str, bed_type: str, position: int, ward: str = WARD) -> dict:
        return self.post("/bed", {"bedNumber": number, "bedType": bed_type, "row": 2 if number.startswith("WOS") else 1, "column": position,
                                  "status": "AVAILABLE", "locationUuid": ward})

    def ward_uuid(self, name: str) -> str:
        title = f"{name} Ward"
        rows = self.get("/location", q=title, limit=20, v="custom:(uuid,name,retired)")["results"]
        found = next((r for r in rows if r["name"] == title and not r.get("retired")), None)
        if found:
            return found["uuid"]
        known = {tag["name"]: tag["uuid"] for tag in self.get("/locationtag", limit=100, v="custom:(uuid,name)")["results"]}
        return self.post("/location", {"name": title, "tags": [known[name] for name in WARD_TAGS if name in known],
                                       "description": "Demo ward (made up)"})["uuid"]

    def all_beds(self) -> dict[str, dict]:
        out, start = {}, 0
        while True:
            part = self.get("/bed", startIndex=start, limit=100, v="custom:(id,uuid,bedNumber,status)")["results"]
            out.update({b["bedNumber"]: b for b in part})
            if len(part) < 100:
                return out
            start += len(part)

    def add_labs(self, patient: str, visit: str, when: datetime, results: list[dict]) -> dict:
        stamp = iso(when)
        return self.post("/encounter", {
            "patient": patient, "visit": visit, "location": WARD, "encounterType": LAB_ENCOUNTER,
            "encounterDatetime": stamp,
            "obs": [{"concept": r["concept"], "value": r["value"], "obsDatetime": stamp} for r in results]})


def luhn_number(body: int) -> str:
    digits = str(body)
    total = sum(int(d) if i % 2 else sum(divmod(int(d) * 2, 10)) for i, d in enumerate(reversed(digits)))
    return f"{digits}-{-total % 10}"


def iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%f%z")


def visit_start(now: datetime, person: dict) -> datetime:
    last = person["held_back"]["offset_s"] if person.get("held_back") else person["reports"][-1]["offset_s"]
    return now - timedelta(seconds=last + 3600) - timedelta(hours=1)


def missing_reports(person: dict, start: datetime, existing: list[dict]) -> list[tuple[datetime, list[dict]]]:
    have = {parse(e["encounterDatetime"]).replace(microsecond=0) for e in existing}
    out = []
    for report in person["reports"]:
        when = start + timedelta(hours=1, seconds=report["offset_s"])
        if when.replace(microsecond=0) not in have:
            out.append((when, report["results"]))
    return out


def later_than_kept(person: dict, start: datetime, existing: list[dict]) -> list[dict]:
    last = start + timedelta(hours=1, seconds=person["reports"][-1]["offset_s"])
    return [e for e in existing if parse(e["encounterDatetime"]) > last]


def load_people() -> dict:
    if not DATA_FILE.is_file():
        raise SystemExit(f"No data file yet ({DATA_FILE.name}): run --build-data first.")
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def seed(site: Site, people: dict) -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    beds = site.beds()
    types = site.get("/bedtype", limit=1, v="custom:(name)")["results"]
    if not types:
        site.post("/bedtype", {"name": "Standard", "displayName": "Standard", "description": "Standard ward bed"})
        types = site.get("/bedtype", limit=1, v="custom:(name)")["results"]
    bed_type = types[0]["name"]
    for position, (bed, person) in enumerate(people.items(), start=1):
        patient = site.find_patient(person)
        created = patient is None
        if created:
            patient = site.make_patient(person)
        uuid = patient["uuid"]
        visit = site.active_visit(uuid)
        if visit is None:
            start = visit_start(now, person)
            visit = site.post("/visit", {"patient": uuid, "visitType": VISIT_TYPE, "location": VISIT_LOCATION,
                                         "startDatetime": iso(start)})
            visit = site.active_visit(uuid)
        start = parse(visit["startDatetime"])
        existing = site.lab_encounters(uuid)
        to_add = missing_reports(person, start, existing)
        for when, results in to_add:
            site.add_labs(uuid, visit["uuid"], when, results)

        number = f"WOS{bed}"
        ward = site.ward_uuid(WARD_OF[bed]) if bed in WARD_OF else WARD
        if number not in beds:
            beds[number] = site.make_bed(number, bed_type, position, ward)
        admitted = site.get("/encounter", patient=uuid, encounterType=ADMISSION_ENCOUNTER, limit=1,
                            v="custom:(uuid)")["results"]
        if not admitted:
            encounter = site.post("/encounter", {"patient": uuid, "visit": visit["uuid"], "location": ward,
                                                 "encounterType": ADMISSION_ENCOUNTER,
                                                 "encounterDatetime": iso(start + timedelta(minutes=5))})
            site.post(f"/beds/{beds[number].get('id') or beds[number]['uuid']}",
                      {"patientUuid": uuid, "encounterUuid": encounter["uuid"]})
        print(f"bed {bed}: patient {'made' if created else 'found'}, reports added {len(to_add)} "
              f"(already there {len(person['reports']) - len(to_add)}), bed {number}")
    return 0


def check(site: Site, people: dict) -> int:
    problems = 0
    beds = site.beds()
    for bed, person in people.items():
        patient = site.find_patient(person)
        if patient is None:
            print(f"bed {bed}: PATIENT GONE (run --seed)")
            problems += 1
            continue
        visit = site.active_visit(patient["uuid"])
        if visit is None:
            print(f"bed {bed}: visit closed by someone (run --seed)")
            problems += 1
            continue
        start = parse(visit["startDatetime"])
        existing = site.lab_encounters(patient["uuid"])
        missing = missing_reports(person, start, existing)
        extra = later_than_kept(person, start, existing)
        number = f"WOS{bed}"
        print(f"bed {bed}: {patient['display'].split(' - ')[0]} ok, reports {len(person['reports']) - len(missing)}/"
              f"{len(person['reports'])}, added later {len(extra)}, "
              f"bed {number} {beds[number]['status'] if number in beds else 'GONE'}")
        problems += bool(missing) + (number not in beds)
    print("All there." if not problems else f"{problems} problem(s). Run --seed.")
    return 0 if not problems else 2


def filler_plan() -> list[dict]:
    """The made-up patients that fill the other beds. Always the same list (seeded), so a re-run finds what is there."""
    import random
    rnd = random.Random(20261103)
    names = [(g, f) for f in FILLER_FAMILY for g in FILLER_GIVEN]
    rnd.shuffle(names)
    plan, used = [], 0
    for ward, (beds, prefix, full) in WARDS.items():
        demo = sum(1 for w in WARD_OF.values() if w == ward)
        for n in range(1, full - demo + 1):
            given, family = names[used]
            used += 1
            today = rnd.random() < 0.15
            hours = rnd.randint(1, 14) if today else rnd.randint(24, 24 * 10)
            low = 2005 if ward != "Paediatrics" else 2018
            plan.append({"ward": ward, "bed": f"{prefix}-{n:02d}", "given": given, "family": family,
                         "gender": "F" if ward == "ObsGyn" else rnd.choice("MF"),
                         "birthdate": f"{rnd.randint(1950 if ward != 'Paediatrics' else 2010, low)}"
                                      f"-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}",
                         "hours_ago": hours, "discharged_hours_ago": None})
        for _ in range(2):         # two people already sent home today: no bed, a closed visit
            given, family = names[used]
            used += 1
            plan.append({"ward": ward, "bed": None, "given": given, "family": family,
                         "gender": "F" if ward == "ObsGyn" else rnd.choice("MF"),
                         "birthdate": f"{rnd.randint(1950, 2005)}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}",
                         "hours_ago": rnd.randint(48, 24 * 6), "discharged_hours_ago": rnd.randint(1, 6)})
    return plan


def known_names(site: Site) -> set[str]:
    out = set()
    for family in FILLER_FAMILY:
        start = 0
        while True:
            part = site.get("/patient", q=family, startIndex=start, limit=100, v="custom:(display,voided)")["results"]
            out |= {r["display"].split(" - ")[0].lower() for r in part if not r.get("voided")}
            if len(part) < 100:
                break
            start += len(part)
    return out


def seed_hospital(site: Site) -> int:
    """Wards with their beds, and made-up filler patients so the counts look like a busy hospital. Safe to run again."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    types = site.get("/bedtype", limit=1, v="custom:(name)")["results"]
    if not types:
        site.post("/bedtype", {"name": "Standard", "displayName": "Standard", "description": "Standard ward bed"})
        types = site.get("/bedtype", limit=1, v="custom:(name)")["results"]
    bed_type = types[0]["name"]
    beds = site.beds()
    wards = {name: site.ward_uuid(name) for name in WARDS}
    made_beds = 0
    for name, (count, prefix, _) in WARDS.items():
        regular = count - sum(1 for w in WARD_OF.values() if w == name)
        for n in range(1, regular + 1):
            number = f"{prefix}-{n:02d}"
            if number not in beds:
                beds[number] = site.make_bed(number, bed_type, n, wards[name])
                made_beds += 1
    existing = known_names(site)
    made = admitted = discharged = 0
    for row in filler_plan():
        person = {k: row[k] for k in ("given", "family", "gender", "birthdate")}
        full = f"{row['given']} {row['family']}".lower()
        if full in existing:
            continue
        patient = site.make_patient(person)
        start = now - timedelta(hours=row["hours_ago"])
        visit = site.post("/visit", {"patient": patient["uuid"], "visitType": VISIT_TYPE, "location": VISIT_LOCATION,
                                     "startDatetime": iso(start)})
        encounter = site.post("/encounter", {"patient": patient["uuid"], "visit": visit["uuid"], "location": wards[row["ward"]],
                                             "encounterType": ADMISSION_ENCOUNTER, "encounterDatetime": iso(start + timedelta(minutes=5))})
        made += 1
        if row["discharged_hours_ago"] is None:
            bed = beds[row["bed"]]
            site.post(f"/beds/{bed.get('id') or bed['uuid']}", {"patientUuid": patient["uuid"], "encounterUuid": encounter["uuid"]})
            admitted += 1
        else:
            stop = now - timedelta(hours=row["discharged_hours_ago"])
            site.post("/encounter", {"patient": patient["uuid"], "visit": visit["uuid"], "location": wards[row["ward"]],
                                     "encounterType": DISCHARGE_ENCOUNTER, "encounterDatetime": iso(stop)})
            site.post("/visit/" + visit["uuid"], {"stopDatetime": iso(stop)})
            discharged += 1
    print(f"hospital: {made_beds} beds made, {made} filler patients made ({admitted} in beds, {discharged} discharged today)")
    return 0


def retire_legacy(site: Site) -> int:
    """The five older demo patients go; their beds are renamed so the new ten can take WOS11-WOS20."""
    gone = 0
    for given, family in LEGACY_NAMES:
        patient = site.find_patient({"given": given, "family": family})
        if patient is None:
            continue
        for bed in site.get("/beds", patientUuid=patient["uuid"]).get("results", []):
            site.delete(f"/beds/{bed.get('bedId') or bed.get('id')}?patientUuid={patient['uuid']}")
        site.delete(f"/patient/{patient['uuid']}?reason=demo+patients+replaced")
        gone += 1
    for number, bed in site.beds().items():
        if number in {f"WOS{n}" for n in range(11, 16)} and bed["status"] != "OCCUPIED":
            site.post(f"/bed/{bed['uuid']}", {"bedNumber": "OLD" + number[3:]})
    print(f"retired {gone} old demo patients")
    return 0


def hospital_check(site: Site) -> int:
    beds = site.beds()
    problems = 0
    for name, (count, prefix, full) in WARDS.items():
        regular = count - sum(1 for w in WARD_OF.values() if w == name)
        mine = [b for number, b in beds.items() if number.startswith(prefix + "-")]
        demo = [b for number, b in beds.items() if number.startswith("WOS") and WARD_OF.get(number[3:]) == name]
        occupied = sum(1 for b in mine + demo if b["status"] == "OCCUPIED")
        print(f"{name}: {len(mine) + len(demo)} of {count} beds, {occupied} full (aim {full})")
        problems += (len(mine) != regular) + (abs(occupied - full) > 1)
    return 0 if not problems else 2


def _one(site: Site, people: dict, bed: str) -> tuple[dict, str, dict, datetime, list[dict]]:
    if bed not in people:
        raise SystemExit(f"No bed {bed}. Beds: {', '.join(people)}")
    person = people[bed]
    patient = site.find_patient(person)
    visit = site.active_visit(patient["uuid"]) if patient else None
    if not visit:
        raise SystemExit(f"Bed {bed} is not on the site: run --seed first.")
    return person, patient["uuid"], visit, parse(visit["startDatetime"]), site.lab_encounters(patient["uuid"])


def add_held_back(site: Site, people: dict, bed: str) -> int:
    person, uuid, visit, start, existing = _one(site, people, bed)
    if later_than_kept(person, start, existing):
        print(f"Bed {bed} already has its later report (--reset-held-back {bed} takes it off).")
        return 0
    results = person["held_back"]["results"]
    site.add_labs(uuid, visit["uuid"], datetime.now(timezone.utc).replace(microsecond=0), results)
    print(f"Bed {bed}: new report on the site now, {len(results)} values.")
    return 0


def add_potassium(site: Site, people: dict, bed: str, value: float) -> int:
    _, uuid, visit, _, _ = _one(site, people, bed)
    site.add_labs(uuid, visit["uuid"], datetime.now(timezone.utc).replace(microsecond=0),
                  [{"concept": POTASSIUM, "value": value}])
    print(f"Bed {bed}: potassium {value} on the site now (made up, for the demo).")
    return 0


def reset_held_back(site: Site, people: dict, bed: str) -> int:
    person, _, _, start, existing = _one(site, people, bed)
    extra = later_than_kept(person, start, existing)
    for encounter in extra:
        site.delete(f"/encounter/{encounter['uuid']}")
    print(f"Bed {bed}: {len(extra)} later report(s) taken off.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--build-data", action="store_true")
    group.add_argument("--seed-hospital", action="store_true")
    group.add_argument("--retire-legacy", action="store_true")
    group.add_argument("--check-hospital", action="store_true")
    group.add_argument("--seed", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--add-held-back", metavar="BED")
    group.add_argument("--add-potassium", nargs=2, metavar=("BED", "VALUE"))
    group.add_argument("--reset-held-back", metavar="BED")
    args = ap.parse_args(argv)
    if args.build_data:
        return build_data()
    if args.seed_hospital or args.retire_legacy or args.check_hospital:
        site = Site()
        try:
            return (seed_hospital if args.seed_hospital else retire_legacy if args.retire_legacy else hospital_check)(site)
        except RuntimeError as problem:
            print(f"The site refused: {problem}")
            return 2
    site, people = Site(), load_people()
    started = time.monotonic()
    try:
        if args.seed:
            code = seed(site, people)
        elif args.check:
            code = check(site, people)
        elif args.add_held_back:
            code = add_held_back(site, people, args.add_held_back)
        elif args.add_potassium:
            code = add_potassium(site, people, args.add_potassium[0], float(args.add_potassium[1]))
        else:
            code = reset_held_back(site, people, args.reset_held_back)
    except RuntimeError as problem:
        print(f"The site refused: {problem}")
        return 2
    print(f"({time.monotonic() - started:.0f} s)")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

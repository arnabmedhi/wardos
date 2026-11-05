"""Maintenance of the five demo patients on the private OpenMRS."""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path

from ..config import openmrs_rest_base
from .demo_tasks import checked_id, utcnow

HOSPITAL_TZ = timezone(timedelta(hours=5, minutes=30))
DANGER_POTASSIUM = 2.6      # the one planned value that arrives during a demo watch (low)
TAG = "WARDOS_DEMO_TASK:"
SITE_DOWN = "The demo hospital site is not answering. Try again in a few minutes."


class MaintenanceBusy(RuntimeError):
    pass


class DemoHTTPError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(SITE_DOWN)


def seed_module():
    import openmrs_seed
    return openmrs_seed


def quiet(fn, *args):
    with redirect_stdout(io.StringIO()):
        return fn(*args)


def load_people(path):
    people = json.loads(Path(path).read_text(encoding="utf-8"))
    seed = seed_module()
    if set(people) != set(seed.PEOPLE):
        raise ValueError("Expected the existing five demo patients")
    for bed, person in people.items():
        if (person.get("given") != seed.PEOPLE[bed][0] or person.get("family") != seed.FAMILY
                or person.get("gender") != seed.PEOPLE[bed][1] or person.get("birthdate") != seed.PEOPLE[bed][2]):
            raise ValueError("Only the approved made-up demo identities are allowed")
        if set(person) != {"given", "family", "gender", "birthdate", "reports", "held_back"}:
            raise ValueError("Unexpected private demo data fields")
        for report in [*person["reports"], *([person["held_back"]] if person.get("held_back") else [])]:
            if set(report) != {"offset_s", "results"}:
                raise ValueError("Unexpected demo report fields")
            for result in report["results"]:
                if (not {"concept", "value"} <= set(result) or set(result) - {"key", "concept", "value", "unit"}
                        or not isinstance(result["value"], (int, float))):
                    raise ValueError("Only numeric lab values belong in the demo seed")
    for person in people.values():
        held = person.get("held_back")
        if held:
            person["photos_until_offset"] = person["reports"][-1]["offset_s"]
            person["reports"] = [*person["reports"], held]
            person["held_back"] = None
    return people


def make_site():
    seed = seed_module()
    class DemoSite(seed.Site):
        def _call(self, method, path, **kw):
            if seed.BASE != openmrs_rest_base() or not path.startswith("/") or "://" in path:
                raise ValueError("Only our own private OpenMRS demo is allowed")
            try:
                reply = self.http.request(method, seed.BASE + path, **kw)
                if reply.status_code >= 400:
                    raise DemoHTTPError(reply.status_code)
                return reply.json() if reply.content else {}
            except DemoHTTPError:
                raise
            except Exception:
                raise RuntimeError(SITE_DOWN) from None

        def lab_encounters(self, patient):
            rows, start = [], 0
            while True:
                part = self.get("/encounter", patient=patient, encounterType=seed.LAB_ENCOUNTER,
                                startIndex=start, limit=100,
                                v="custom:(uuid,encounterDatetime,obs:(uuid,concept:(uuid),value,comment))")["results"]
                rows.extend(part)
                if len(part) < 100:
                    return rows
                start += len(part)

    return DemoSite()


def tagged(encounter, task_id=None):
    wanted = TAG + checked_id(task_id) if task_id else None
    return any((o.get("comment") == wanted if wanted else str(o.get("comment") or "").startswith(TAG))
               for o in encounter.get("obs", []))


def observations(encounter):
    return sorted((o["concept"]["uuid"], float(o["value"])) for o in encounter.get("obs", [])
                  if not o.get("voided"))


class Maintainer:
    def __init__(self, site, people, queue, *, alert=lambda line: None, now=utcnow):
        self.site, self.people, self.queue, self.alert, self.now = site, people, queue, alert, now
        self._ready = {}

    def find_patient(self, bed, person):
        patient = self.site.find_patient(person)
        known = self.queue.store.get("robot_patients", bed)
        if patient is None and known:
            try:
                patient = self.site.get("/patient/" + known["uuid"], v="custom:(uuid,display,voided,person:(uuid))")
                if patient.get("voided"):
                    patient = None
            except DemoHTTPError as error:
                if error.status != 404:
                    raise
        if patient:
            self.queue.store.put("robot_patients", {"id": bed, "uuid": patient["uuid"]})
        return patient

    def profile_problem(self, patient, person):
        if not patient or not {"given", "family", "gender", "birthdate"} <= set(person):
            return None
        profile = self.site.get("/patient/" + patient["uuid"],
                                v="custom:(person:(uuid,gender,birthdate,names:(uuid,givenName,familyName,preferred)))")["person"]
        preferred = next((n for n in profile["names"] if n.get("preferred")), profile["names"][0])
        if (preferred.get("givenName") != person["given"] or preferred.get("familyName") != person["family"]
                or profile.get("gender") != person["gender"] or str(profile.get("birthdate", ""))[:10] != person["birthdate"]):
            return profile, preferred
        return None

    def notify(self, line):
        try:
            self.alert(line)
            self.queue.store.put("status", {"id": "owner_alert", "updated_at": self.now().isoformat(), "result": "Sent"})
        except Exception:
            self.queue.store.put("status", {"id": "owner_alert", "updated_at": self.now().isoformat(), "result": "Failed"})

    def bed_state(self, bed, person, beds):
        seed = seed_module()
        patient = self.find_patient(bed, person)
        profile = self.profile_problem(patient, person)
        visit = self.site.active_visit(patient["uuid"]) if patient else None
        rows = self.site.lab_encounters(patient["uuid"]) if visit else []
        missing, bad = [], []
        if visit:
            start = seed.parse(visit["startDatetime"])
            by_time = {seed.parse(e["encounterDatetime"]).replace(microsecond=0): e for e in rows}
            for report in person["reports"]:
                when = (start + timedelta(hours=1, seconds=report["offset_s"])).replace(microsecond=0)
                actual = by_time.get(when)
                if actual is None:
                    missing.append(report)
                elif observations(actual) != sorted((r["concept"], float(r["value"])) for r in report["results"]):
                    bad.append((actual, when, report["results"]))
        number = "WOS" + bed
        broken = not patient or not visit or number not in beds or beds[number].get("status") != "OCCUPIED" or bool(missing or bad or profile)
        return patient, visit, rows, bad, profile, broken

    def inspect(self):
        beds, counts, changed = self.site.beds(), [], []
        for bed, person in self.people.items():
            patient, visit, rows, bad, profile, broken = self.bed_state(bed, person, beds)
            counts.append({"bed": bed, "reports": len(rows), "ok": not broken})
            if broken:
                changed.append((bed, patient, visit, bad, profile))
        return counts, changed

    def bed_ready(self, bed):
        bed = str(bed)
        try:
            if self.queue.held("maintenance"):
                return False
            patient, visit, rows, _, _, broken = self.bed_state(bed, self.people[bed], self.site.beds())
            seed = seed_module()
            old = self.now() - timedelta(minutes=15)
            if broken or any(tagged(e) and seed.parse(e["encounterDatetime"]) < old for e in rows):
                return False
        except Exception:
            return False
        self._ready[bed] = (patient, visit)
        return True

    def cleanup(self, *, task_id=None):
        seed = seed_module()
        removed = 0
        for bed, person in self.people.items():
            patient = self.find_patient(bed, person)
            if not patient:
                continue
            for encounter in self.site.lab_encounters(patient["uuid"]):
                if tagged(encounter, task_id) and (task_id or seed.parse(encounter["encounterDatetime"]) < self.now() - timedelta(minutes=15)):
                    self.site.delete("/encounter/" + encounter["uuid"])
                    removed += 1
        return removed

    def check(self):
        owner = "check-" + self.now().isoformat()
        if not self.queue.acquire("maintenance", owner, seconds=300):
            raise MaintenanceBusy("Another demo check is in progress")
        result = {"id": "openmrs", "checked_at": self.now().isoformat()}
        try:
            self.cleanup()
            seed = seed_module()
            quiet(seed.check, self.site, self.people)
            counts, changed = self.inspect()
            if changed:
                for bed, patient, visit, bad, profile in changed:
                    if profile:
                        person, preferred = profile
                        expected = self.people[bed]
                        self.site.post("/person/" + person["uuid"] + "/name/" + preferred["uuid"],
                                       {"givenName": expected["given"], "familyName": expected["family"], "preferred": True})
                        self.site.post("/person/" + person["uuid"],
                                       {"gender": expected["gender"], "birthdate": expected["birthdate"]})
                    for encounter, when, values in bad:
                        self.site.delete("/encounter/" + encounter["uuid"])
                        self.site.add_labs(patient["uuid"], visit["uuid"], when, values)
                quiet(seed.seed, self.site, self.people)
                beds = self.site.beds()
                for bed, person in self.people.items():
                    number = "WOS" + bed
                    if beds[number].get("status") != "OCCUPIED":
                        patient = self.site.find_patient(person)
                        admissions = self.site.get("/encounter", patient=patient["uuid"],
                                                   encounterType=seed.ADMISSION_ENCOUNTER, limit=1,
                                                   v="custom:(uuid)")["results"]
                        self.site.post("/beds/" + str(beds[number].get("id") or beds[number]["uuid"]),
                                       {"patientUuid": patient["uuid"], "encounterUuid": admissions[0]["uuid"]})
                if quiet(seed.check, self.site, self.people) != 0:
                    raise RuntimeError("Demo put-back did not pass its check")
                counts, remaining = self.inspect()
                if remaining:
                    raise RuntimeError("Demo values still differ after put-back")
            line = "Put back" if changed else "All there"
            result.update(result=line, beds=counts)
            self.queue.store.put("status", result)
            if changed:
                self.notify("The demo hospital was put back.")
            return result
        except Exception:
            result.update(result="Site not answering", beds=[])
            self.queue.store.put("status", result)
            self.notify(SITE_DOWN)
            raise RuntimeError(SITE_DOWN) from None
        finally:
            self.queue.release("maintenance", owner)

    def only_new(self, bed, values):
        person = self.people.get(str(bed)) or {}
        if person.get("photos_until_offset") is None:
            return values
        seed = seed_module()
        ready = self._ready.get(str(bed))
        patient = ready[0] if ready else self.find_patient(str(bed), person)
        visit = ready[1] if ready else (self.site.active_visit(patient["uuid"]) if patient else None)
        if not visit:
            raise RuntimeError(SITE_DOWN)
        until = seed.parse(visit["startDatetime"]) + timedelta(hours=1, seconds=person["photos_until_offset"])

        def later(value):
            when = datetime.fromisoformat(value.collected_at)
            return (when if when.tzinfo else when.replace(tzinfo=HOSPITAL_TZ)) > until
        return [v for v in values if later(v)]

    def add_potassium(self, task):
        seed = seed_module()
        person, uuid, visit, _, _ = seed._one(self.site, self.people, str(task["bed"]))
        stamp = seed.iso(self.now().replace(microsecond=0))
        return self.site.post("/encounter", {"patient": uuid, "visit": visit["uuid"], "location": seed.WARD,
            "encounterType": seed.LAB_ENCOUNTER, "encounterDatetime": stamp, "obs": [
                {"concept": seed.POTASSIUM, "value": DANGER_POTASSIUM, "obsDatetime": stamp,
                 "comment": TAG + checked_id(task["id"])}]})

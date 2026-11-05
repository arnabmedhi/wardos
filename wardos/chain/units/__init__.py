"""Unit definitions: the fields of each unit's discharge summary and the writing rule of each field."""
from __future__ import annotations

import json
from pathlib import Path
import re

RULES_DIR = Path(__file__).parent / "rules"
UNIT_FILES = {
    "Surgery Unit 2": "surgery_unit2", "Surgery Unit 1": "surgery_unit1", "Surgery Unit 3": "surgery_unit3",
    "Medicine Unit 1": "medicine_unit1", "Medicine Unit 2": "medicine_unit2",
    "ENT Unit 1": "ent", "ENT Unit 2": "ent_unit2",
    "ObsGyn Unit 2": "obs_unit2", "ObsGyn Unit 3": "obs_unit3", "Pediatrics": "pediatrics",
}

CODE_FIELDS = {"{{LAB_ANCHOR}}", "{{RADIO_ANCHOR}}", "{{SPECIAL_INV_ANCHOR}}", "{{labs_json}}"}

_FEEDS = [
    (r"patient_name|^age$|gender|uhid|address|contact|header_id", {"identity"}),
    (r"^doa$|^dod$|^dop$|consultant|resident|unit_team|^sr_\d", {"admin", "identity"}),
    (r"complaint|^hpi$|^history$", {"complaint", "history", "observation", "vital"}),
    (r"past_|treatment_history|personal|family|addiction|menstrual|allerg", {"history", "allergy", "med_event"}),
    (r"^dis_|discharge_condition", {"vital", "observation", "decision"}),
    (r"pulse|^bp$|^rr$|temp|spo2|height|weight|bmi|^gc$|gcs|pallor|icterus|cyanosis|clubbing|edema|lymph|jvp|"
     r"_exam|pa_|dre|oral_cavity|build|pupils|fundoscopy|higher_mental|cranial|motor|meningeal|neuro|cns|"
     r"perception|monofilament|biothesiometry|cans_", {"vital", "observation"}),
    (r"_cs_|culture|fluid", {"investigation_result", "lab_result"}),
    (r"radiology", {"investigation_result", "investigation_request"}),
    (r"procedure|management|intraop", {"procedure", "plan", "decision", "diagnosis"}),
    (r"hospital_course|treatment_given", {"med_event", "procedure", "decision", "plan", "observation", "vital",
                                          "lab_result", "investigation_result", "diagnosis"}),
    (r"diagnosis", {"diagnosis", "procedure"}),
    (r"advice|follow_up", {"plan", "med_event", "decision"}),
]
_ALL = {"identity", "admin", "complaint", "history", "observation", "vital", "lab_result", "investigation_request",
        "investigation_result", "med_event", "procedure", "diagnosis", "plan", "decision", "allergy"}


def field_name(placeholder: str) -> str:
    return placeholder.strip("{}")


def feeds(placeholder: str) -> set[str]:
    name = field_name(placeholder)
    for pattern, types in _FEEDS:
        if re.search(pattern, name):
            return types
    return {"lab_result", "investigation_result"} if name else _ALL


def load_unit(unit: str) -> dict:
    name = UNIT_FILES.get(unit)
    if not name:
        raise KeyError(f"no summary definition for unit {unit!r} yet (have: {', '.join(UNIT_FILES)})")
    data = json.loads((RULES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    fields = []
    for placeholder, rule in data["rules"].items():
        fields.append({"placeholder": placeholder, "rule": rule, "feeds": sorted(feeds(placeholder)),
                       "by_code": placeholder in CODE_FIELDS})
    return {"unit": unit, "fields": fields}

"""Bed commands and occupancy warnings."""
from __future__ import annotations

import re


def clean_bed(value: str | None) -> str | None:
    value = re.sub(r"^bed\s*", "", str(value or "").strip(), flags=re.I).strip().upper()
    if not value:
        return None
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9 /-]{0,19}", value):
        raise ValueError("Use a bed number or label, up to 20 characters")
    return value


def bed_command(norm: str) -> tuple[str, str] | None:
    beds = list(re.finditer(r"\bbed\s*(?:no|number)?\s*([a-z]?\d+[a-z]?)\b", norm))
    if not beds:
        return None
    moves = re.search(r"\b(?:move|shift|transfer|set|change|assign|pe|par|में|पर|शिफ्ट|बदलो)\b", norm)
    if len(beds) >= 2 and moves:
        dest = beds[-1]
        return norm[:dest.start()], dest[1].upper()
    if len(beds) == 1 and (moves or (beds[0].start() > 0 and beds[0].end() == len(norm))):
        dest = beds[0]
        who = norm[:dest.start()] + " " + norm[dest.end():]
        return who, dest[1].upper()
    return None


def occupants(admission: dict, bed: str | None, admissions: list[dict]) -> list[dict]:
    key = lambda x: re.sub(r"[^a-z0-9]", "", str(x or "").lower())
    return [a for a in admissions if bed and a["id"] != admission["id"] and a.get("status") == "active"
            and not a.get("archived") and a.get("unit") == admission.get("unit")
            and (a.get("ward") or "") == (admission.get("ward") or "")
            and key(a.get("bed")) == key(bed)]


class BedTaken(ValueError):
    def __init__(self, admissions: list[dict]):
        self.admissions = admissions
        super().__init__("This bed is already assigned to another patient on this ward. Change it anyway?")

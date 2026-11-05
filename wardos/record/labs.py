"""Read eHospital LIS result PDFs into values, store them per patient, and answer "latest X"."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib

from ..config import HOSPITAL_TZ
import json
from pathlib import Path
import re

DT = r"\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?"
HEADER = {
    "registration": re.compile(r"Registration\s*No\s*:\s*(\d{6,})", re.I),
    "lab": re.compile(r"Lab\s*Cent(?:er|re)\s*Name\s*:\s*(.+?)(?:\s+Lab\s*Incharge|$)", re.I),
    "collected": re.compile(r"Sample\s*Collection[^\d]{0,40}?(" + DT + ")", re.I),
    "sample_no": re.compile(r"Sample\s*No\s*:\s*(\S+)", re.I),
    "verified": re.compile(r"Verification[^\d]{0,40}?(" + DT + ")", re.I),
}
PAGE_BOILERPLATE = re.compile(r"^\s*(All India Institute of Medical Sciences|Page\s+\d+(\s+of\s+\d+)?\s*$)", re.I)
BODY_START = re.compile(r"^\s*Test\s*Name\s+Result\s+Unit\s+Reference", re.I)
BODY_END = re.compile(r"^\s*(Note\s*:|Authori[sz]ed\s+Signatory|Kindly\s+correlate|\*+\s*End)", re.I)
NUMBER = re.compile(r"[<>]?\d+(?:\.\d+)?")
PANEL_WORDS = re.compile(r"\b(test|profile|panel|function|examination|culture|count|study|markers?|screen|picture|"
                         r"analysis|hormones?|lipid|electrolytes?)\b", re.I)
FLAGS = {"H", "L", "HH", "LL", "*H", "*L", "H*", "L*"}
QUALITATIVE = {"positive", "negative", "reactive", "nonreactive", "non-reactive", "nil", "absent", "present",
               "trace", "detected", "notdetected", "normal", "abnormal"}

SYNONYMS = {
    "creatinine": ["serum creatinine", "creatinine", "s creatinine"],
    "urea": ["blood urea", "urea", "serum urea"],
    "sodium": ["serum na+", "na+", "sodium", "serum sodium"],
    "potassium": ["serum k+", "k+", "potassium", "serum potassium"],
    "chloride": ["serum chloride", "chloride"],
    "calcium": ["s calcium", "calcium", "serum calcium"],
    "bilirubin": ["total bilirubin"],
    "direct bilirubin": ["direct bilirubin"],
    "sgot": ["sgot", "ast"],
    "sgpt": ["sgpt", "alt"],
    "alp": ["alkaline phosphatase", "alp"],
    "hb": ["haemoglobin", "hemoglobin", "hb", "hgb"],
    "haemoglobin": ["haemoglobin", "hemoglobin", "hb", "hgb"],
    "tlc": ["total leucocyte count", "total leukocyte count", "tlc", "wbc"],
    "platelets": ["platelet count", "platelets", "plt"],
    "albumin": ["albumin", "serum albumin"],
    "uric acid": ["serum uric acid", "uric acid"],
}


@dataclass
class LabValue:
    analyte: str
    value: str
    number: float | None
    flag: str | None
    unit: str | None
    reference: str | None
    panel: str | None
    lab: str | None
    sample_no: str | None
    collected_at: str | None
    verified_at: str | None
    page: int
    source_sha256: str
    kind: str = "value"
    fetched_at: str | None = None


def _dt(text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return None


def parse_result_line(line: str) -> dict | None:
    tokens = line.split()
    for i in range(1, len(tokens)):
        token = tokens[i]
        is_number = bool(NUMBER.fullmatch(token))
        if not is_number and token.lower().replace(" ", "") not in QUALITATIVE:
            continue
        analyte = " ".join(tokens[:i]).strip()
        if not re.search(r"[A-Za-z]", analyte):
            return None
        rest = tokens[i + 1:]
        flag = rest.pop(0) if rest and rest[0].upper() in FLAGS else None
        unit = None
        if rest and not NUMBER.fullmatch(rest[0]):
            unit = rest.pop(0)
            if unit == "-":
                unit = None
        reference = " ".join(rest).strip() or None
        number = float(token.lstrip("<>")) if is_number else None
        return {"analyte": analyte, "value": token, "number": number,
                "flag": flag.strip("*").upper() if flag else None, "unit": unit, "reference": reference}
    return None


def parse_lis_pdf(path: Path, expected_registration: str | None = None) -> tuple[list[LabValue], list[str]]:
    import pdfplumber
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    values: list[LabValue] = []
    warnings: list[str] = []
    previous: dict | None = None
    panel: str | None = None
    with pdfplumber.open(str(path)) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            header = {k: (m.group(1).strip() if (m := rx.search(text)) else None) for k, rx in HEADER.items()}
            continuation = not header["lab"] and not header["sample_no"]
            if continuation:
                if previous is None:
                    warnings.append(f"page {number}: continuation page without a preceding report - skipped")
                    continue
                header = previous
            else:
                previous, panel = header, None
            if expected_registration and header["registration"] and header["registration"] != expected_registration:
                warnings.append(f"page {number}: different patient (Registration No mismatch) - skipped")
                continue
            in_body = continuation
            for line in text.splitlines():
                if PAGE_BOILERPLATE.match(line):
                    continue
                if BODY_START.match(line):
                    in_body = True
                    continue
                if not in_body:
                    continue
                if BODY_END.match(line):
                    break
                parsed = parse_result_line(line)
                common = dict(panel=panel, lab=header["lab"], sample_no=header["sample_no"],
                              collected_at=_dt(header["collected"]), verified_at=_dt(header["verified"]),
                              page=number, source_sha256=sha)
                if parsed:
                    values.append(LabValue(**parsed, **common))
                elif len(line.split()) <= 7 and PANEL_WORDS.search(line) and not line.strip().startswith("("):
                    panel = line.strip()
                elif line.strip():
                    values.append(LabValue(analyte="note", value=line.strip(), number=None, flag=None, unit=None,
                                           reference=None, kind="note", **common))
            if in_body and not continuation and not header["collected"]:
                warnings.append(f"page {number}: no Sample Collection date-time found")
    return values, warnings


def normalize(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\(.*?\)", " ", name)
    name = re.sub(r"[^a-z0-9+ ]", " ", name)
    return " ".join(name.split())


class LabRecord:

    def __init__(self, directory: Path, portal: str, patient: str):
        key = hashlib.sha256(f"{portal}|{patient}".encode()).hexdigest()[:20]
        self.path = directory / f"labs-{key}.json"

    def load(self) -> dict:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"values": [], "sources": {}}

    def add(self, values: list[LabValue], source: Path) -> dict:
        data = self.load()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def base_of(v: dict) -> tuple:
            sample = v["sample_no"] or f"{v['source_sha256'][:12]}:p{v['page']}"
            return (sample, v["panel"] or "", normalize(v["analyte"]), v["kind"], v["value"] if v["kind"] == "note" else "")

        index = {base_of(v) + (v.get("occurrence", 0),): i for i, v in enumerate(data["values"])}
        seen_in_doc: dict[tuple, int] = {}
        added = corrected = 0
        for value in values:
            row = asdict(value)
            base = base_of(row)
            row["occurrence"] = seen_in_doc.get(base, 0)
            seen_in_doc[base] = row["occurrence"] + 1
            key = base + (row["occurrence"],)
            if key in index:
                old = data["values"][index[key]]
                if row["kind"] == "value" and old["value"] != row["value"]:
                    row["fetched_at"] = now
                    row["corrects"] = old["value"]
                    data["values"][index[key]] = row
                    corrected += 1
                continue
            row["fetched_at"] = now
            data["values"].append(row)
            index[key] = len(data["values"]) - 1
            added += row["kind"] == "value"
        data["sources"][values[0].source_sha256 if values else "none"] = {"file": str(source), "added_at": now}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return {"added": added, "corrected": corrected}

    def find(self, query: str) -> dict[str, list[dict]]:
        q = normalize(query)
        names = {normalize(n) for n in SYNONYMS.get(q, [])} | {q}
        groups: dict[str, list[dict]] = {}
        for v in self.load()["values"]:
            if v["kind"] != "value":
                continue
            a = normalize(v["analyte"])
            if a in names or (len(q) >= 3 and q in a):
                groups.setdefault(v["analyte"], []).append(v)
        for rows in groups.values():
            rows.sort(key=lambda r: (r["collected_at"] or r["verified_at"] or ""), reverse=True)
        return groups


def _local(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if moment.tzinfo is not None:
        moment = moment.astimezone(HOSPITAL_TZ)
    return moment.strftime("%d %b %Y %H:%M")


def describe(row: dict) -> str:
    flag = {"H": " (HIGH)", "L": " (LOW)"}.get(row.get("flag") or "", "")
    unit = f" {row['unit']}" if row.get("unit") else ""
    ref = f", range {row['reference']}" if row.get("reference") else ""
    if row.get("collected_at"):
        when = f"collected {_local(row['collected_at'])}"
    else:
        when = f"collection time not printed; verified {_local(row.get('verified_at'))}"
    where = f" [{row['panel']}]" if row.get("panel") else ""
    return f"{row['value']}{unit}{flag}{ref} - {when}{where}"


def _when(row: dict) -> datetime | None:
    for key in ("collected_at", "verified_at"):
        if row.get(key):
            try:
                moment = datetime.fromisoformat(row[key])
            except ValueError:
                continue
            return moment.astimezone(HOSPITAL_TZ).replace(tzinfo=None) if moment.tzinfo else moment
    return None


def answer_window(record: LabRecord, query: str | None, *, since: datetime, until: datetime,
                  checked_at: str | None, portal_note: str | None = None) -> str:
    groups = record.find(query) if query else {}
    if not query:
        for v in record.load()["values"]:
            if v["kind"] == "value":
                groups.setdefault(v["analyte"], []).append(v)
    lines = [f"Time window: {since:%d %b %Y %H:%M} to {until:%d %b %Y %H:%M}."]
    shown = 0
    for analyte, rows in sorted(groups.items()):
        inside = [r for r in rows if (w := _when(r)) is not None and since <= w <= until]
        inside.sort(key=_when, reverse=True)
        if not inside:
            continue
        shown += 1
        lines.append(f"{analyte}: {describe(inside[0])}")
        for r in inside[1:]:
            lines.append(f"   also: {describe(r)}")
    if not shown:
        latest = next(iter(groups.values()), [])
        lines.append((f"No {query} result in this window yet." if query else
                      "No result in this window yet.")
                     + (f" Latest stored {query}: {describe(latest[0])}" if query and latest else ""))
    lines.append(f"Hospital system last checked: {_local(checked_at) if checked_at else 'never'}"
                 + (f" ({portal_note})" if portal_note else ""))
    return "\n".join(lines)


def answer_latest(record: LabRecord, query: str, *, checked_at: str | None, new_since: str | None,
                  portal_note: str | None = None) -> str:
    groups = record.find(query)
    lines = []
    if not groups:
        lines.append(f"No stored result for '{query}'.")
    for analyte, rows in groups.items():
        latest = rows[0]
        lines.append(f"{analyte}: {describe(latest)}")
        if len(rows) > 1:
            lines.append(f"   previous: {describe(rows[1])}")
        if new_since:
            late = [r for r in rows[1:] if (r.get("fetched_at") or "") >= new_since]
            for r in late:
                lines.append(f"   also new since last check (older sample, released late): {describe(r)}")
    lines.append(f"Hospital system last checked: {_local(checked_at) if checked_at else 'never'}"
                 + (f" ({portal_note})" if portal_note else ""))
    return "\n".join(lines)

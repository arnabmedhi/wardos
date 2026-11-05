"""What the doctor's screen shows for one patient today, worked out in plain code (no model)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
import re

from . import model as m
from .merge import medicine_states
from .normalize import clean_diagnosis, is_future_time, not_a_medicine, parse_clinical_time, split_flag

IST = timezone(timedelta(hours=5, minutes=30))
STUCK_AFTER_SECONDS = 300      # same as worker.STUCK_AFTER_SECONDS (not imported: the demo robot image has no worker.py)
MAX_QUESTIONS = 5
DANGER_SD = 2
ALERT_BY_RANGE = True         # False = the old rule (range edge +/- 2 steps of a quarter of the range width)


def ist_today() -> date:
    return datetime.now(IST).date()

LAB_RANGES: dict[str, tuple] = {
    "haemoglobin": ("Hb", 12.0, 17.0, "g/dL", {"m": (13.0, 17.0), "f": (12.0, 15.0)}),
    "tlc": ("TLC", 4000, 11000, "/µL", {}),
    "platelets": ("Platelets", 150000, 450000, "/µL", {}),
    "creatinine": ("Creatinine", 0.6, 1.3, "mg/dL", {}),
    "urea": ("Urea", 15, 45, "mg/dL", {}),
    "sodium": ("Sodium", 135, 145, "mmol/L", {}),
    "potassium": ("Potassium", 3.5, 5.0, "mmol/L", {}),
    "chloride": ("Chloride", 98, 107, "mmol/L", {}),
    "calcium": ("Calcium", 8.5, 10.5, "mg/dL", {}),
    "bilirubin": ("Bilirubin (total)", None, 1.2, "mg/dL", {}),
    "direct bilirubin": ("Bilirubin (direct)", None, 0.3, "mg/dL", {}),
    "sgot": ("SGOT", None, 40, "U/L", {}),
    "sgpt": ("SGPT", None, 40, "U/L", {}),
    "alp": ("ALP", None, 130, "U/L", {}),
    "albumin": ("Albumin", 3.5, 5.0, "g/dL", {}),
    "total protein": ("Total protein", 6.0, 8.3, "g/dL", {}),
    "uric acid": ("Uric acid", 3.5, 7.2, "mg/dL", {}),
    "blood glucose": ("Blood sugar", 70, 140, "mg/dL", {}),
}
LAB_ORDER = list(LAB_RANGES)

VITAL_RANGES: dict[str, tuple] = {
    "pulse": ("Pulse", 60, 100, lambda v: v > 100),
    "blood pressure": ("BP", 90, 140, lambda v: v < 90),
    "temperature": ("Temp", 97.0, 99.5, lambda v: v >= 100.4),
    "spo2": ("SpO₂", 94, 100, lambda v: v < 94),
    "respiratory rate": ("Resp. rate", 12, 20, lambda v: v > 24),
}
VITAL_ORDER = ["pulse", "blood pressure", "temperature", "spo2", "respiratory rate"]

POSITIVE = re.compile(r"\b(positive|reactive|detected|seen|present|isolated|grown)\b", re.I)
NEGATIVE = re.compile(r"\b(non[\s-]?reactive|negative|not\s+detected|absent|nil|no\s+growth|sterile|not\s+seen)\b",
                      re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)")
RECENT_DAYS = 1


def _day(fact_or_time) -> date | None:
    t = fact_or_time.get("clinical_time") if isinstance(fact_or_time, dict) else fact_or_time
    if not t:
        return None
    try:
        return date.fromisoformat(str(t)[:10])
    except ValueError:
        return None


def _time_key(fact: dict) -> str:
    t = fact.get("clinical_time") or ""
    return t if len(t) > 10 else (t + "T00:00" if t else "")


def _number(fact: dict) -> float | None:
    value = fact.get("value") or {}
    n = value.get("number")
    if isinstance(n, (int, float)):
        return float(n)
    hit = _NUMBER.search(str(value.get("text") or "").replace(",", ""))
    return float(hit.group()) if hit else None


def _systolic(fact: dict) -> tuple[float | None, float | None]:
    hit = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", str((fact.get("value") or {}).get("text") or ""))
    if hit:
        return float(hit.group(1)), float(hit.group(2))
    return _number(fact), None


def _base(key: str, n: float | None, fact: dict) -> float | None:
    if n is None:
        return None
    text = str((fact.get("value") or {}).get("text") or "").lower()
    if key == "tlc" and n < 200:
        return n * 1000
    if key == "platelets":
        if "lakh" in text or n < 10:
            return n * 100000
        if n < 2000:
            return n * 1000
    if key == "temperature" and n < 45:
        return n * 9 / 5 + 32
    return n


def _sex(patient: dict, facts: list[dict]) -> str | None:
    text = patient.get("sex") or next((((f.get("value") or {}).get("text")) for f in reversed(facts)
                                        if f["type"] == "identity" and f["key"] in ("sex", "gender")), None)
    first = (text or "").strip().lower()[:1]
    return first if first in ("m", "f") else None


def _unit_fits(table_unit: str, unit: str | None) -> bool:
    u = (unit or "").strip().lower().replace(" ", "")
    if not u:
        return True
    if table_unit in ("mg/dL", "g/dL"):
        return "mol" not in u and u not in ("g/l", "gm/l") and not (table_unit == "g/dL" and u.startswith("mg"))
    return True


def _glucose_mmol(fact: dict) -> bool:
    value = fact.get("value") or {}
    unit = str(value.get("unit") or "").lower().replace(" ", "")
    n = _number(fact)
    return "mmol" in unit or (not unit and n is not None and 0 < n < 25)   # mg/dL values are never this small


def normal_range(fact: dict, sex: str | None = None) -> tuple[float | None, float | None] | None:
    key = fact.get("key") or ""
    ref = str((fact.get("value") or {}).get("reference") or "")
    hit = _RANGE.search(ref.replace(",", ""))
    if hit:
        lo, hi = float(hit.group(1)), float(hit.group(2))
        return _base(key, lo, {"value": {"text": ref}}), _base(key, hi, {"value": {"text": ref}})
    if fact.get("type") == "vital" and key in VITAL_RANGES:
        return VITAL_RANGES[key][1], VITAL_RANGES[key][2]
    if key in LAB_RANGES:
        _, lo, hi, unit, by_sex = LAB_RANGES[key]
        if key == "blood glucose" and _glucose_mmol(fact):
            return 3.9, 7.8
        if key == "blood glucose" and (_number(fact) or 99) < 25:
            return None        # "9.3 mg/dl" is a misread unit or digit, so no LOW/HIGH is claimed; the value stays as written
        if not _unit_fits(unit, (fact.get("value") or {}).get("unit")):
            return None
        return by_sex.get(sex or "", (lo, hi))
    return None


def standard_unit(fact: dict) -> str:
    key = fact.get("key") or ""
    if fact.get("type") != "lab_result" or key not in LAB_RANGES:
        return ""
    n = _number(fact)
    if key == "blood glucose" and _glucose_mmol(fact):
        return "mmol/L"
    base = _base(key, n, fact)
    if n and base and abs(base - n) > 1e-9:
        return "lakh/µL" if key == "platelets" and round(base / n) == 100000 else "×10³/µL"
    return LAB_RANGES[key][3]


def shown_range(fact: dict, sex: str | None = None) -> list | None:
    rng = normal_range(fact, sex)
    if not rng:
        return None
    key = fact.get("key") or ""
    n = _number(fact)
    base = _base(key, n, fact)
    factor = base / n if n and base else 1.0

    def tidy(x):
        if x is None:
            return None
        x = x / factor
        return int(round(x)) if abs(x - round(x)) < 1e-6 else round(x, 2)
    return [tidy(rng[0]), tidy(rng[1])]


def outside(fact: dict, sex: str | None = None) -> tuple[str | None, float]:
    value = fact.get("value") or {}
    flag = str(value.get("flag") or "").strip().upper()[:1]
    if not flag and fact.get("type") == "lab_result":
        n0 = value.get("number")
        flag = (split_flag(str(value.get("text") or ""), n0 if isinstance(n0, (int, float)) else None)[1] or "")[:1]
    key = fact.get("key") or ""
    if fact.get("type") == "vital" and key == "blood pressure":
        n = _systolic(fact)[0]
    else:
        n = _base(key, _number(fact), fact)
    rng = normal_range(fact, sex)
    if n is not None and rng:
        lo, hi = rng
        if lo is not None and n < lo:
            return "low", (lo - n) / lo if lo else 0.0
        if hi is not None and n > hi:
            return "high", (n - hi) / hi if hi else 0.0
        if not flag:
            return None, 0.0
    if flag == "H":
        return "high", 0.01
    if flag == "L":
        return "low", 0.01
    if fact.get("type") == "lab_result" and n is None:
        text = str(value.get("text") or "")
        if POSITIVE.search(text) and not NEGATIVE.search(text):
            return "abnormal", 0.01
    return None, 0.0


def danger_limits(lo: float | None, hi: float | None) -> tuple[float | None, float | None]:
    if lo is None and hi is None:
        return None, None
    if hi is None:
        return lo / 2, None
    sd = (hi - (lo or 0.0)) / 4
    danger_hi = hi + DANGER_SD * sd
    if lo is None:
        return None, danger_hi
    danger_lo = lo - DANGER_SD * sd
    return (danger_lo if danger_lo > 0 else lo / 2), danger_hi


def is_danger(fact: dict, sex: str | None = None) -> bool:
    key = fact.get("key") or ""
    if fact.get("type") == "vital":
        spec = VITAL_RANGES.get(key)
        n = _systolic(fact)[0] if key == "blood pressure" else _base(key, _number(fact), fact)
        return bool(spec and n is not None and spec[3](n))
    n = _base(key, _number(fact), fact)
    rng = normal_range(fact, sex)
    if n is None or not rng:
        return False
    low, high = rng if ALERT_BY_RANGE else danger_limits(*rng)       # rule: outside the normal range = alert
    return bool((low is not None and n < low) or (high is not None and n > high))


def latest_and_previous(facts: list[dict], key: str, type_: str | None = None) -> tuple[dict | None, dict | None]:
    rows = [f for f in facts if f.get("key") == key and (type_ is None or f["type"] == type_)
            and f.get("status") in m.LIVE_STATUSES]
    dated = sorted((f for f in rows if f.get("clinical_time")), key=_time_key)
    if not dated:
        undated = sorted(rows, key=lambda f: f.get("created_at") or "")
        return (undated[-1] if undated else None), None
    latest = dated[-1]
    earlier = [f for f in dated if _day(f) and _day(latest) and _day(f) < _day(latest)]
    return latest, (earlier[-1] if earlier else None)


NOT_CLINICAL_DAY = ("history", "admin", "identity")


def as_of_day(facts: list[dict], today: date | None = None) -> date:
    limit = (today or ist_today()) + timedelta(days=1)
    days = [d for d in (_day(f) for f in facts if f["type"] not in NOT_CLINICAL_DAY and not f.get("time_doubtful"))
            if d and d <= limit]
    return max(days) if days else (today or ist_today())


_LEAD_DATE = re.compile(r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*[-–:/]?\s*(?=\d)", re.I)


def _unjunk(text: str) -> str:
    """A date word read into the value ("aug - 8") is dropped from what is shown; the number itself is kept."""
    return _LEAD_DATE.sub("", text, count=1).strip() or text


def _show(fact: dict | None) -> str:
    if not fact:
        return ""
    value = fact.get("value") or {}
    text = str(value.get("text") or "").strip()
    n = value.get("number")
    if fact.get("type") == "vital" and fact.get("key") == "blood pressure":
        s, d = _systolic(fact)
        return f"{s:g}/{d:g}" if s and d else text
    if fact.get("type") == "vital" and fact.get("key") in VITAL_RANGES and _number(fact) is not None:
        return f"{_number(fact):g}"
    if not text:
        return f"{n:g}" if isinstance(n, (int, float)) else ""
    if fact.get("type") == "lab_result":
        text = _unjunk(split_flag(text, n if isinstance(n, (int, float)) else None)[0])
    unit = str(value.get("unit") or "").strip()
    if unit and text.lower().endswith(unit.lower()):
        text = text[:-len(unit)].strip()
    text = re.sub(r"\s*(mg\s*/\s*dl|g\s*/\s*dl|mmol\s*/\s*l?|u\s*/\s*l?|/\s*min|mm ?hg)$", "", text, flags=re.I).strip()
    return re.sub(r"\s+[HL]$", "", text).strip()


def _when(t: str | None) -> str:
    d = _day(t)
    if not d:
        return ""
    out = f"{d.day} {d:%b}"
    if t and len(t) > 10:
        hour, minute = int(t[11:13]), int(t[14:16])
        if hour or minute:
            h12 = hour % 12 or 12
            out += f", {h12}{f':{minute:02d}' if minute else ''} {'am' if hour < 12 else 'pm'}"
    return out


_MONTH_JUNK = re.compile(r"(?:" + "|".join(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"]) + r")[a-z]*" + chr(92) + r".?(?:" + chr(92) + r"s*[-–/]?" + chr(92) + r"s*" + chr(92) + r"d{1,4})?" + chr(92) + r"s*$", re.I)
_TAIL_JUNK = re.compile(r"" + chr(92) + r"s*[-–:/]" + chr(92) + r"s*" + chr(92) + r"d{1,4}" + chr(92) + r"s*$")


def clean_name(label: str) -> str:
    """A test name read from a sheet column can carry the date next to it ("Hb aug - 8"). Show only the name."""
    name = " ".join(str(label or "").split())
    for _ in range(3):
        before = name
        name = _TAIL_JUNK.sub("", name).strip(" -–:/.,")
        hit = _MONTH_JUNK.search(name)
        if hit and hit.start() > 0:
            name = name[:hit.start()].strip(" -–:/.,")
        if name == before:
            break
    return name or " ".join(str(label or "").split())


def _label(fact: dict) -> str:
    key = fact.get("key") or ""
    if fact.get("type") == "vital" and key in VITAL_RANGES:
        return VITAL_RANGES[key][0]
    if key in LAB_RANGES:
        return LAB_RANGES[key][0]
    label = clean_name((fact.get("label") or key or "").strip())
    if len(label) <= 4 and label.isalpha():
        return label.upper()
    return label[:1].upper() + label[1:]


_ACRONYMS = {"usg", "ct", "mri", "ecg", "ekg", "cxr", "hrct", "mrcp", "echo", "eeg", "emg", "pet", "ercp", "kub",
             "ugie", "cbc", "lft", "kft", "rft", "tft", "abg", "pt", "inr", "hba1c", "crp", "esr"}


def _nice(label: str) -> str:
    return " ".join(w.upper() if w.lower() in _ACRONYMS else w for w in (label or "").split())


def _ref(fact: dict | None) -> dict | None:
    if not fact:
        return None
    return {"fact_id": fact["id"], "source_id": fact.get("source_id"), "page_id": fact.get("page_id"),
            "quote": fact.get("quote") or "", "quote_found": bool(fact.get("quote_found")),
            "time": fact.get("clinical_time"),
            "when": "date unclear" if fact.get("time_doubtful") else _when(fact.get("clinical_time")),
            "date_doubtful": bool(fact.get("time_doubtful")),
            "by_doctor": fact.get("status") == m.DOCTOR_LOCKED, "disputed": fact.get("status") == m.DISPUTED}


def vitals_rows(facts: list[dict], as_of: date) -> list[dict]:
    out = []
    for key in VITAL_ORDER:
        latest, previous = latest_and_previous(facts, key, "vital")
        if not latest:
            continue
        name, lo, hi, danger = VITAL_RANGES[key]
        series = sorted((f for f in facts if f["type"] == "vital" and f["key"] == key and f.get("clinical_time")
                         and f.get("status") in m.LIVE_STATUSES), key=_time_key)[-6:]
        def val(f):
            return _systolic(f)[0] if key == "blood pressure" else _base(key, _number(f), f)
        now = val(latest)
        out.append({"key": key, "label": name, "value": _show(latest), "number": now,
                    "unit": {"pulse": "/min", "blood pressure": "mmHg",
                             "temperature": "°C" if (_number(latest) or 99) < 45 else "°F",
                             "spo2": "%", "respiratory rate": "/min"}[key],
                    "yesterday": _show(previous) or None, "was_bad": bool(previous and outside(previous)[0]), "range": [lo, hi],
                    "bad": outside(latest)[0] is not None, "danger": bool(now is not None and danger(now)),
                    "recent": bool(_day(latest) and (as_of - _day(latest)).days <= RECENT_DAYS),
                    "series": [{"v": val(f), "text": _show(f), "day": _when(f.get("clinical_time")).split(",")[0], **_ref(f)}
                               for f in series if val(f) is not None],
                    **_ref(latest)})
    return out


def _lab_series(facts: list[dict], key: str, latest: dict) -> list[dict]:
    number = _number(latest)
    if number is None:
        return []
    base = _base(key, number, latest)
    factor = base / number if number and base else 1.0
    rows: dict[tuple, dict] = {}
    for f in sorted((f for f in facts if f["type"] == "lab_result" and f.get("key") == key and f.get("clinical_time")
                     and f.get("status") in m.LIVE_STATUSES), key=_time_key):
        value = _base(key, _number(f), f)
        if value is not None:
            rows[(f["clinical_time"], _show(f))] = {"v": round(value / factor, 3), "text": _show(f),
                                                    "day": _when(f["clinical_time"]).split(",")[0], **_ref(f)}
    return list(rows.values())


def lab_rows(facts: list[dict], sex: str | None, as_of: date) -> list[dict]:
    keys = {f["key"] for f in facts if f["type"] == "lab_result" and f.get("status") in m.LIVE_STATUSES}
    ordered = [k for k in LAB_ORDER if k in keys] + sorted(k for k in keys if k not in LAB_RANGES)
    out = []
    for key in ordered:
        latest, previous = latest_and_previous(facts, key, "lab_result")
        if not latest:
            continue
        direction, far = outside(latest, sex)
        was_dir, was_far = outside(previous, sex) if previous else (None, 0.0)
        out.append({"key": key, "label": _label(latest), "value": _show(latest),
                    "unit": (latest.get("value") or {}).get("unit") or "", "dir": direction, "far": round(far, 3),
                    "unit_std": "" if (latest.get("value") or {}).get("unit") else standard_unit(latest),
                    "was": _show(previous) or None, "was_when": _when((previous or {}).get("clinical_time")) or None,
                    "was_dir": was_dir, "was_far": round(was_far, 3), "danger": is_danger(latest, sex),
                    "range": shown_range(latest, sex),
                    "numeric": _number(latest) is not None,
                    "recent": bool(_day(latest) and (as_of - _day(latest)).days <= RECENT_DAYS),
                    "series": _lab_series(facts, key, latest),
                    **_ref(latest)})
    newest: dict[str, dict] = {}
    for r in out:
        name = r["label"].lower()
        if name not in newest or (r["time"] or "") > (newest[name]["time"] or ""):
            newest[name] = r
    out = [r for r in out if newest[r["label"].lower()] is r]
    for r in out:
        r["main"] = r["key"] in LAB_RANGES or bool(r["dir"])
    return [r for r in out if r["numeric"]] + [r for r in out if not r["numeric"]]


MED_NAMES = {
    "merol": "Meropenem", "pantop": "Pantoprazole", "azee": "Azithromycin", "potclor": "Potassium chloride",
    "teicol": "Teicoplanin", "pcm": "Paracetamol", "diclo": "Diclofenac", "hcq": "Hydroxychloroquine",
    "kcl": "Potassium chloride", "mgso4": "Magnesium sulphate", "iprabent": "Ipratropium", "ciplar": "Propranolol",
    "neomercazole": "Carbimazole", "thyronorm": "Levothyroxine", "looz": "Lactulose", "emeset": "Ondansetron",
    "emset": "Ondansetron", "metrogyl": "Metronidazole", "monocef": "Ceftriaxone", "prbc": "Packed red blood cells",
    "rdp": "Random donor platelets", "ffp": "Fresh frozen plasma", "ns": "Normal saline", "rl": "Ringer lactate",
}
MED_FORMS = {"inj": "Inj.", "injection": "Inj.", "iv": "Inj.", "tab": "Tab.", "t": "Tab.", "tablet": "Tab.",
             "syp": "Syp.", "syr": "Syp.", "syrup": "Syp.", "cap": "Cap.", "capsule": "Cap.", "gel": "Gel",
             "oint": "Oint.", "ointment": "Oint.", "neb": "Neb.", "nebulisation": "Neb."}
MED_GROUPS = ("Injections", "Tablets and syrups", "Drips, fluids and blood", "Other")
_FILLER = {"also", "with", "and", "the", "of", "given", "taking", "regularly", "stat", "transfusion", "transfused"}
_DRIP = re.compile(r"\b(ns|rl|dns|saline|ringer)\b|\bover\s+\d+\s*(hrs?|hours?|h)\b|infusion|drip|meq/hr", re.I)
_BLOOD = re.compile(r"prbc|red blood|platelet|\brdp\b|plasma|\bffp\b|transfus|whole blood", re.I)


def _words_like(a: str, b: str) -> bool:
    a, b = a.lower().strip(".,"), b.lower().strip(".,")
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.75))


def display_medicine(label: str, key: str, text: str = "") -> tuple[str, str]:
    tokens = [t for t in re.split(r"\s+", (label or key or "").strip()) if t]
    form = None
    if tokens and tokens[0].lower().strip(".") in MED_FORMS:
        form, tokens = MED_FORMS[tokens[0].lower().strip(".")], tokens[1:]
    elif tokens and tokens[-1].lower().strip(".") in MED_FORMS:
        form, tokens = MED_FORMS[tokens[-1].lower().strip(".")], tokens[:-1]
    tokens = [t for i, t in enumerate(tokens) if not (i == 0 and t.lower() in _FILLER)
              and t.lower() not in ("transfusion", "transfused")]
    if not tokens or len(tokens) > 3 or any(re.search(r"\d", t) for t in tokens):
        tokens = [str(key or label or "").strip()]
    name = " ".join(MED_NAMES.get(t.lower().strip(".,"), t) for t in tokens).strip()
    if name.isupper() and len(name) > 4:
        name = name.title()
    name = name[:1].upper() + name[1:]
    shown = f"{form} {name}".strip() if form and form != "Gel" else (f"{name} gel" if form == "Gel" else name)
    blob = f"{label} {key} {text}"
    if _BLOOD.search(blob) or _DRIP.search(blob):
        group = MED_GROUPS[2]
    elif form == "Inj.":
        group = MED_GROUPS[0]
    elif form in ("Tab.", "Syp.", "Cap.", "Gel", "Oint."):
        group = MED_GROUPS[1]
    else:
        group = MED_GROUPS[3]
    return shown, group


def _dose_line(value: dict, label: str, key: str) -> str:
    dose = str(value.get("dose") or "").strip()
    if dose:
        extra = [str(value.get(k) or "").strip() for k in ("route", "frequency")]
        dose = " ".join([dose] + [x for x in extra if x and x.lower() not in dose.lower().split()])
    else:
        dose = str(value.get("text") or "").strip()
    own = [w for w in re.split(r"\s+", f"{label} {key}") if w] + list(MED_FORMS)
    kept, seen = [], set()
    for word in dose.split():
        low = word.lower().strip(".,")
        if low in seen or low in _FILLER or any(_words_like(low, o) for o in own if len(o) > 1 or low == o.lower()):
            continue
        seen.add(low)
        kept.append(word)
    return " ".join(kept).strip(" .,-:")


def medicine_rows(facts: list[dict]) -> list[dict]:
    rows = []
    for key, state in medicine_states(facts).items():
        events = [e for e in state["events"] if (e.get("value") or {}).get("event") != "discharge_med"]
        if not events:
            continue
        last = state["last"] or events[-1]
        started = next((e for e in events if e.get("clinical_time")), None)
        value = last.get("value") or {}
        raw = (last.get("label") or key).strip()
        if not_a_medicine(raw, value.get("text")):
            continue
        shown, group = display_medicine(raw, key, str(value.get("text") or ""))
        rows.append({"key": key, "label": shown, "raw_label": raw, "group": group, "dose": _dose_line(value, raw, key),
                     "state": {"on": "on", "off": "stopped"}.get(state["state"], "check"),
                     "since": _when((started or {}).get("clinical_time")) or None, **_ref(last)})
    order = {"on": 0, "check": 1, "stopped": 2}
    return sorted(rows, key=lambda r: (order[r["state"]], MED_GROUPS.index(r["group"]), r["label"].lower()))


def plan_rows(facts: list[dict]) -> list[dict]:
    live = [f for f in facts if f.get("status") in m.LIVE_STATUSES]
    plans = sorted((f for f in live if f["type"] == "plan"), key=_time_key)
    rows = []
    if plans:
        newest = _day(plans[-1])
        rows += [{"text": str((f.get("value") or {}).get("text") or f.get("label") or f["key"]), "kind": "plan",
                  **_ref(f)} for f in plans if _day(f) == newest][:5]
    results = {f["key"] for f in live if f["type"] in ("lab_result", "investigation_result")}
    seen = set()
    for req in sorted((f for f in live if f["type"] == "investigation_request"), key=_time_key, reverse=True):
        key = req["key"]
        if key in seen or key in results or any(key in r or r in key for r in results if len(r) > 3):
            continue
        seen.add(key)
        since = _when(req.get("clinical_time")).split(",")[0]
        rows.append({"text": f"{_nice(_label(req))} pending" + (f" since {since}" if since else ""), "kind": "pending",
                     "name": _nice(_label(req)), "since": since or None, **_ref(req)})
        if len(seen) >= 4:
            break
    return rows


NOT_KEPT_NOTE = ("This page was held before the app kept what it read, so its values cannot be added to the record "
                 "from here, even if you say it is this patient.")


def question_rows(record_questions: list[dict], facts_by_id: dict, pages_by_id: dict, sources_by_id: dict) -> list[dict]:
    out = []
    for q in sorted(record_questions, key=lambda q: q.get("created_at") or ""):
        options = []
        if q.get("kind") in (m.Q_CONFLICT, m.Q_DOCTOR_CONFLICT):
            for fid in q.get("fact_ids") or []:
                fact = facts_by_id.get(fid)
                if not fact or fact.get("status") not in m.LIVE_STATUSES:
                    continue
                where = _source_word(fact, pages_by_id, sources_by_id)
                when = _when(fact.get("clinical_time"))
                options.append({"fact_id": fid, "value": _show(fact) or "(blank)",
                                "where": ", ".join(x for x in (where, when) if x),
                                "page_id": fact.get("page_id"), "source_id": fact.get("source_id")})
        elif q.get("kind") == m.Q_WRONG_PATIENT:
            options = [{"fact_id": None, "value": m.HOLD_IT, "where": ""},
                       {"fact_id": None, "value": m.THIS_PATIENT, "where": ""}]
        first = facts_by_id.get((q.get("fact_ids") or [None])[0]) if q.get("fact_ids") else None
        short = _label(first) if first else {"wrong_patient": "a page of another patient"}.get(q.get("kind"), "a check")
        identity = bool(first and first["type"] in ("identity", "admin"))
        text = q.get("text") or ""
        if q.get("kind") in (m.Q_CONFLICT, m.Q_DOCTOR_CONFLICT) and options:
            when = _when((first or {}).get("clinical_time"))
            n = len(options)
            title = (f"{short} on {when}: {n} values written" if when else f"{short}: {n} versions written")
            if q.get("kind") == m.Q_DOCTOR_CONFLICT:
                title = f"{short}: a new page differs from your value"
            elif not identity:
                text = _conflict_sentence(short, when, options, facts_by_id, pages_by_id, sources_by_id)
        else:
            title = text
        if q.get("kind") == m.Q_WRONG_PATIENT:
            title = text = f"This page shows a different {'UHID' if 'UHID' in text else 'name'}. Hold it?"
        note = ""
        if q.get("kind") == m.Q_WRONG_PATIENT and not q.get("values_kept"):
            note = NOT_KEPT_NOTE
        out.append({"id": q["id"], "kind": q.get("kind"), "text": text, "title": title, "short": short,
                    "identity": identity, "options": options, "note": note,
                    "page_id": q.get("page_id") or (options[0].get("page_id") if options else None),
                    "source_id": q.get("source_id") or (options[0].get("source_id") if options else None)
                    or (pages_by_id.get(q.get("page_id")) or {}).get("source_id")})
    return out


def _conflict_sentence(short: str, when: str, options: list[dict], facts_by_id: dict, pages_by_id: dict,
                       sources_by_id: dict) -> str:
    values = [o["value"] for o in options]
    joined = " and ".join([", ".join(values[:-1]), values[-1]] if len(values) > 2 else values)
    count = {2: "two", 3: "three", 4: "four"}.get(len(values), str(len(values)))
    pages = {o["page_id"] for o in options if o.get("page_id")}
    first = facts_by_id.get(options[0]["fact_id"]) or {}
    if len(pages) == 1 and first:
        word = _source_word(first, pages_by_id, sources_by_id) or "page"
        who = {"referral": "The referral letter", "discharge card": "The discharge card"}.get(
            ((pages_by_id.get(first.get("page_id") or "") or {}).get("page_type") or "").replace("_", " "),
            f"The {word}")
        read_as = f", both read as {when}" if when and len(values) == 2 else (f", all read as {when}" if when else "")
        return f"{who} shows {count} {short} values, {joined}{read_as}. Which one is the newest?"
    on = f" on {when}" if when else ""
    return f"The pages disagree about {short}{on}: {joined}. Which one is right?"


def _source_word(fact: dict, pages_by_id: dict, sources_by_id: dict) -> str:
    if fact.get("created_by") == "doctor":
        return "your entry"
    page = pages_by_id.get(fact.get("page_id") or "")
    kind = (page or {}).get("page_type")
    label = (sources_by_id.get(fact.get("source_id") or "") or {}).get("label")
    if label:
        return label
    if kind:
        return {"rounds_note": "round note", "admission_note": "admission note", "drug_chart": "drug chart",
                "vitals_chart": "nursing chart", "lab_report": "lab report"}.get(kind, kind.replace("_", " "))
    source = sources_by_id.get(fact.get("source_id") or "") or {}
    return {"text": "typed note", "voice": "voice note", "portal_report": "lab report", "pdf": "PDF",
            "photo": "photo"}.get(source.get("kind"), "")


def build_alerts(vitals: list[dict], labs: list[dict]) -> list[dict]:
    out = []
    for lab in labs:
        if not lab["recent"] or not lab["danger"] or lab["disputed"]:
            continue
        word = {"low": "low", "high": "high"}.get(lab["dir"] or "", "")
        out.append({"kind": "danger", "title": f"{lab['label']} {word}".strip(), "name": lab["label"],
                    "value": lab["value"], "unit": lab["unit"], "was": lab["was"],
                    "sub": f"was {lab['was']}" if lab["was"] else "first result",
                    "go": "tests", "key": lab["key"], "far": lab["far"],
                    **{k: lab[k] for k in ("fact_id", "source_id", "page_id", "quote", "quote_found", "time", "when",
                                           "date_doubtful", "by_doctor", "disputed")}})
    for v in vitals:
        if not v["recent"] or not v["danger"] or v["disputed"]:
            continue
        lo, hi = v["range"]
        far = ((v["number"] - hi) / hi if v["number"] > hi else (lo - v["number"]) / lo) if v["number"] else 0.0
        out.append({"kind": "danger", "title": f"{v['label']} past danger line", "name": v["label"],
                    "value": v["value"], "unit": v["unit"], "was": v["yesterday"],
                    "sub": f"yesterday {v['yesterday']}" if v["yesterday"] else "newest reading",
                    "go": "vitals", "key": v["key"], "far": far,
                    **{k: v[k] for k in ("fact_id", "source_id", "page_id", "quote", "quote_found", "time", "when",
                                         "date_doubtful", "by_doctor", "disputed")}})
    out.sort(key=lambda a: (a.get("time") or "", a["far"]), reverse=True)
    for a in out:
        a.pop("far", None)
    return out


def build_changes(vitals: list[dict], labs: list[dict], alerts: list[dict]) -> list[dict]:
    danger = {(a["go"], a["key"]) for a in alerts}
    out = []

    def add(row, go, rank, was, *, bad=False, good=False, alert=False):
        out.append({"key": row["key"], "label": row["label"], "was": was, "value": row["value"], "unit": row["unit"],
                    "rank": rank, "far": row.get("far") or 0.0, "bad": bad, "good": good, "alert": alert,
                    "go": f"{go}:{row['key']}", "series": row["series"]})
    for lab in labs:
        alert = ("tests", lab["key"]) in danger
        was, now, before = lab["was"], lab["dir"], lab["was_dir"]
        if not lab["recent"] or (not alert and (was == lab["value"] or (was is None and not now))):
            continue
        flipped = bool(now and before and now != before)
        if alert or (now and not before) or flipped:
            add(lab, "value", 9 if alert else 5, was, bad=bool(now), alert=alert)
        elif now and lab["far"] > lab["was_far"]:
            add(lab, "value", 4, was, bad=True)
        elif before and lab["far"] < lab["was_far"]:
            add(lab, "value", 1, was, good=True)
        elif now:
            add(lab, "value", 1, was, bad=True)
    for v in vitals:
        alert = ("vitals", v["key"]) in danger
        was = v["yesterday"]
        if not v["recent"] or (not alert and (was == v["value"] or (was is None and not v["bad"]))):
            continue
        if alert or (v["bad"] and not v["was_bad"]):
            add(v, "vital", 9 if alert else 3, was, bad=True, alert=alert)
        elif v["was_bad"] and not v["bad"]:
            add(v, "vital", 1, was, good=True)
    return sorted(out, key=lambda c: (-c["rank"], -c["far"]))


def news_line(admission: dict, changes: list[dict], waiting: int, stuck: int, entries: int) -> str:
    plural = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"
    alerts = sum(1 for c in changes if c["alert"])
    parts = [plural(alerts, "alert") if alerts else "Marked unstable" if admission.get("severity") == "unstable" else "",
             (plural(min(waiting, 5), "question")
              + (" for you" if waiting == 1 and len(changes) == alerts else "")) if waiting else "",   # the 5 most important show
             plural(len(changes) - alerts, "change") if len(changes) > alerts else "",
             f"{plural(stuck, 'photo')} not read" if stuck else ""]
    return ", ".join(p for p in parts if p) or ("No recent changes" if entries else "Nothing read yet")


def build_notices(facts: list[dict], sources_by_id: dict, as_of: date, alerts: list[dict], sex: str | None,
                  questions: list[dict]) -> list[dict]:
    out, seen = [], set()
    for f in sorted((f for f in facts if f["type"] == "investigation_result" and _day(f) == as_of
                     and f.get("status") in m.LIVE_STATUSES), key=_time_key, reverse=True):
        if f["key"] in seen:
            continue
        seen.add(f["key"])
        out.append({"kind": "info", "title": f"New report: {_nice(_label(f))}", "name": _nice(_label(f)),
                    "sub": str((f.get("value") or {}).get("text") or "")[:80], "go": "record", "key": f["key"],
                    **_ref(f)})
    by_source: dict[str, list[dict]] = {}
    for f in facts:
        source = sources_by_id.get(f.get("source_id") or "") or {}
        if f["type"] == "lab_result" and f.get("status") in m.LIVE_STATUSES and source.get("kind") in ("portal_report", "pdf") \
                and _day(f) and (as_of - _day(f)).days <= RECENT_DAYS:
            by_source.setdefault(f["source_id"], []).append(f)
    for sid, group in by_source.items():
        if any(is_danger(f, sex) for f in group):
            continue
        newest = max(group, key=_time_key)
        out.append({"kind": "info", "title": "New report: lab tests", "name": "lab tests",
                    "sub": f"{len(group)} value{'s' if len(group) != 1 else ''}", "go": "tests", "key": f"report:{sid}",
                    **_ref(newest)})
    if questions:
        out.insert(0, {"kind": "need", "title": "Needs your answer", "name": "Needs your answer",
                       "value": str(len(questions)), "sub": ", ".join(dict.fromkeys(q["short"] for q in questions)),
                       "go": "questions", "key": "questions"})
    return out


def ward_flag(admission: dict, alerts: list[dict], questions: list[dict]) -> dict:
    if admission.get("severity") == "unstable":
        return {"flag": "red", "reason": "Marked unstable" + (f" by {admission['severity_by']}"
                                                               if admission.get("severity_by") else "")}
    if alerts:
        a = alerts[0]
        unit = f" {a['unit']}" if a.get("unit") else ""
        return {"flag": "red", "reason": f"{a['title']} {a['value']}{unit}".strip()}
    if questions:
        n = len(questions)
        return {"flag": "amber", "reason": f"{min(n, 5)} answer{'s' if n != 1 else ''} needed: "
                                           f"{', '.join(list(dict.fromkeys(q['short'] for q in questions))[:5])}"}
    return {"flag": None, "reason": ""}


def fold_lines(vitals: list[dict], labs: list[dict], meds: list[dict], plan: list[dict]) -> dict:
    vital_parts = [{"text": f"{v['label']} {v['value']}", "bad": v["bad"] and v["recent"]} for v in vitals
                   if v["key"] in ("pulse", "blood pressure", "temperature", "spo2")
                   and (v["recent"] or not v["time"])]
    abnormal = sorted((x for x in labs if x["dir"]), key=lambda x: -x["far"])
    if abnormal:
        test_parts = [{"text": f"{x['label']} {x['value']}", "word": x["dir"] if x["dir"] != "abnormal" else "",
                       "bad": True} for x in abnormal[:3]]
        if len(abnormal) > 3:
            test_parts.append({"text": f"+{len(abnormal) - 3} more", "bad": False})
    elif labs:
        test_parts = [{"text": f"All {len(labs)} in range", "bad": False}]
    else:
        test_parts = [{"text": "No tests yet", "bad": False}]
    counts = {s: sum(1 for x in meds if x["state"] == s) for s in ("on", "check", "stopped")}
    med_text = ", ".join(f"{n} {w}" for n, w in ((counts["on"], "on"), (counts["check"], "to check"),
                                                  (counts["stopped"], "stopped")) if n) or "No medicines yet"
    plan_text = ", ".join(p["text"] for p in plan[:3]) or "No plan written yet"
    return {"vitals": vital_parts or [{"text": "No vitals yet", "bad": False}], "tests": test_parts,
            "medicines": med_text, "plan": plan_text}


def _admin(facts: list[dict], *keys: str) -> str | None:
    for f in sorted((f for f in facts if f["type"] in ("admin", "identity") and f["key"] in keys
                     and f.get("status") in m.LIVE_STATUSES), key=_time_key, reverse=True):
        text = str((f.get("value") or {}).get("text") or "").strip()
        if text:
            return text
    return None


def admitted_on(admission: dict, facts: list[dict]) -> date | None:
    d = _day(admission.get("admitted_at"))
    if d:
        return d
    keys = ("date of admission", "admission date", "doa", "admitted on", "date of admission time")
    pages = sorted((f for f in facts if f["type"] == "admin" and f["key"] in keys and f.get("status") in m.LIVE_STATUSES
                    and _day(f)), key=_time_key)
    if pages:
        return _day(pages[-1])
    text = _admin(facts, *keys)
    ref = _day(admission.get("reference_date"))
    when, _ = parse_clinical_time(text, around=ref) if text else (None, "unknown")
    return _day(when)


def build_brief(patient: dict, admission: dict, diagnosis: list[str], alerts: list[dict], plan: list[dict]) -> dict:
    who = [f"Bed {admission['bed']}" if admission.get("bed") else None,
           patient["name"] if patient.get("name") and patient["name"] != "Name not read yet" else None,
           str(patient["age"]) if patient.get("age") else None, patient.get("sex")]
    line1 = ", ".join(x for x in who if x)
    if admission.get("admitted_text"):
        line1 = f"{line1}. {admission['admitted_text']}" if line1 else admission["admitted_text"]
    line2 = "; ".join(diagnosis) if diagnosis else "Diagnosis not written yet"
    dangers = [(f"{a['name']} {a['was']} → {a['value']}" if a.get("was") and a["go"] == "tests" else f"{a['name']} {a['value']}")
               + (f" {a['unit']}" if a.get("unit") else "") for a in alerts]
    unstable = "Marked unstable" if admission.get("severity") == "unstable" else ""
    line3 = ". ".join(x for x in (unstable, f"Dangerous: {', '.join(dangers)}" if dangers else "") if x) \
        or "Nothing dangerous"
    pending = [p["text"] for p in plan if p.get("kind") == "pending"]
    line4 = ", ".join(pending) if pending else "Nothing pending"
    if admission.get("read_only"):
        line1 = ", ".join(x for x in (patient.get("name"), str(patient.get("age") or ""), patient.get("sex")) if x)
        line3 = f"Out of range in that stay: {', '.join(dangers)}" if dangers else ""
        line4 = ""
    lines = [line1, line2, line3, line4]
    return {"lines": lines, "text": "\n".join(lines)}


REPORT_ORDER: list[tuple[str, list[str]]] = [
    ("CBC", ["haemoglobin", "hb", "tlc", "total leucocyte count", "differential leucocyte count", "neu%",
             "neutrophil percentage", "lym%", "lymphocyte percentage", "mono%", "monocyte percentage", "eos%",
             "eosinophil percentage", "baso%", "basophil percentage", "platelets", "platelet count", "platelet",
             "haematocrit", "hct", "rbc", "red blood cell count", "mcv", "mch", "mchc", "rdw-cv", "reticulocyte percentage"]),
    ("Kidney tests and electrolytes", ["urea", "creatinine", "sodium", "potassium", "chloride", "calcium", "s. calcium",
                                       "bicarbonate", "uric acid", "phosphorus", "phoshorus", "magnesium"]),
    ("Liver tests", ["bilirubin", "direct bilirubin", "sgpt", "sgot", "alp", "ggt", "ggt gamma glutamyl transferase",
                     "total protein", "protein", "albumin", "globulin", "a/g ratio", "amylase", "lipase"]),
    ("Clotting", ["prothrombin time", "pt test", "pt control", "inr", "inr test", "inr control", "pt/inr", "aptt",
                  "aptt control"]),
    ("Sugar", ["blood glucose", "glucose", "fasting blood sugar", "hba1c"]),
    ("Urine", ["urine osmolality", "urine sodium", "urine potassium", "urine chloride", "urine ph", "urine culture",
               "specific gravity", "ketone", "nitrate", "urobilinogen", "blood"]),
]
_OTHERS = "Other tests"


def report_group(key: str) -> tuple[int, int]:
    low = (key or "").lower()
    for n, (_, keys) in enumerate(REPORT_ORDER):
        if low in keys:
            return n, keys.index(low)
    if low.startswith("urine") or "24 hour urine" in low:
        return len(REPORT_ORDER) - 1, 99
    return len(REPORT_ORDER), 0


def reports_in_order(facts: list[dict], day: str | None = None, sex: str | None = None,
                     sources: list[dict] | None = None) -> dict:
    by_id = {s["id"]: s for s in sources or []}
    facts = [_unsure_time(f, by_id) for f in facts]
    live = [f for f in facts if f["type"] == "lab_result" and f.get("status") in m.LIVE_STATUSES
            and f.get("clinical_time") and not f.get("time_doubtful")]
    days = sorted({_day(f).isoformat() for f in live if _day(f)}, reverse=True)
    chosen = day if day in days else (days[0] if days else None)
    newest: dict[str, dict] = {}
    for f in sorted((f for f in live if _day(f) and _day(f).isoformat() == chosen), key=_time_key):
        newest[f["key"]] = f
    groups: dict[int, list[dict]] = {}
    for key, f in newest.items():
        direction, _ = outside(f, sex)
        n, place = report_group(key)
        groups.setdefault(n, []).append({"key": key, "label": _label(f), "value": _show(f),
                                         "unit": (f.get("value") or {}).get("unit") or "", "dir": direction,
                                         "unit_std": "" if (f.get("value") or {}).get("unit") else standard_unit(f),
                                         "danger": is_danger(f, sex), "range": shown_range(f, sex),
                                         "_place": place, **_ref(f)})
    out = []
    for n in sorted(groups):
        rows = sorted(groups[n], key=lambda r: (r["_place"], r["label"].lower()))
        for r in rows:
            r.pop("_place")
        out.append({"name": REPORT_ORDER[n][0] if n < len(REPORT_ORDER) else _OTHERS, "rows": rows})
    return {"day": chosen, "days": days, "groups": out}


def _unsure_time(fact: dict, sources_by_id: dict) -> dict:
    if fact.get("time_doubtful") or (fact.get("clinical_time") and fact.get("assertion") != "planned" and is_future_time(
            fact["clinical_time"], (sources_by_id.get(fact.get("source_id") or "") or {}).get("received_at"))):
        return dict(fact, clinical_time=None, time_precision="unknown", time_doubtful=True,
                    time_written=fact.get("time_written") or fact.get("clinical_time"))
    return fact


_GRADE_ONLY = re.compile(r"^(?:nyha|asa|gcs|killip|child[- ]?pugh|meld|grade|stage|class|score|ecog|mrs|wagner)" + chr(92) + r"b[^a-z]*(?:class|grade|stage)?[^a-z]*(?:[ivx]+|" + chr(92) + r"d+|[a-d])?[^a-z]*$", re.I)


def _diagnosis_lines(facts: list[dict]) -> list[str]:
    diagnoses = sorted((f for f in facts if f["type"] == "diagnosis" and f.get("assertion") in (None, "present",
                                                                                                "uncertain")),
                       key=_time_key)
    if not diagnoses:
        return []
    named = [f for f in diagnoses if not _GRADE_ONLY.match(str((f.get("value") or {}).get("text") or f.get("label") or f["key"]).strip())]
    diagnoses = named or diagnoses       # a bare grade ("NYHA 4") is not the diagnosis when a real one exists
    newest = _day(diagnoses[-1])
    todays = [f for f in diagnoses if _day(f) == newest]

    def rank(f):
        text = str((f.get("value") or {}).get("text") or f.get("label") or f["key"])
        if _GRADE_ONLY.match(text.strip()):
            return 3
        return 2 if re.search(r"resolved|ruled out", text, re.I) else (1 if f.get("assertion") == "uncertain" else 0)
    todays.sort(key=rank)
    return list(dict.fromkeys(clean_diagnosis(str((f.get("value") or {}).get("text") or f.get("label") or f["key"]))
                              for f in todays))[:2]


def effective_bed(admission: dict, facts: list[dict]) -> str | None:
    return (admission.get("bed") if admission.get("bed_set_at") else
            admission.get("bed") or _admin(facts, "bed", "bed number", "bed no"))


def build_today(admission: dict, patient: dict, facts: list[dict], questions: list[dict], *,
                pages_by_id: dict | None = None, sources: list[dict] | None = None,
                today: date | None = None) -> dict:
    pages_by_id = pages_by_id or {}
    today = today or ist_today()
    sources = sorted(sources or [], key=lambda s: s.get("received_at") or "")
    sources_by_id = {s["id"]: s for s in sources}
    facts = [_unsure_time(f, sources_by_id) for f in facts]
    facts_by_id = {f["id"]: f for f in facts}
    sex = _sex(patient, facts)
    as_of = as_of_day(facts, today)
    vitals = vitals_rows(facts, as_of)
    labs = lab_rows(facts, sex, as_of)
    all_q = question_rows(questions, facts_by_id, pages_by_id, sources_by_id)
    all_q = sorted(all_q, key=lambda q: {"wrong_patient": 0, "doctor_conflict": 1, "conflict": 2}.get(q.get("kind"), 3))[:MAX_QUESTIONS]   # the 5 that matter most; no screen ever sees more
    qrows = [q for q in all_q if not q["identity"]]
    checks = [q for q in all_q if q["identity"]]
    alerts = build_alerts(vitals, labs)
    notices = build_notices(facts, sources_by_id, as_of, alerts, sex, qrows)
    flag = ward_flag(admission, alerts, qrows + checks)
    meds = medicine_rows(facts)
    plan = plan_rows(facts)
    changes = build_changes(vitals, labs, alerts)

    admitted = admitted_on(admission, facts)
    age = patient.get("age") or _admin(facts, "age")
    age_years = re.match(r"\s*(\d{1,3})", age or "")
    dx = _diagnosis_lines(facts)
    files = [s for s in sources if s.get("kind") in ("photo", "pdf", "portal_report")]
    photo_no = {s["id"]: i + 1 for i, s in enumerate(files)}
    # A read page waits for its values to be added (often 1-3 minutes); call it "not read" only when the worker would.
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STUCK_AFTER_SECONDS)).isoformat(timespec="seconds")
    stuck = [{"id": s["id"], "kind": s["kind"]} for s in files
             if s.get("status") == m.SRC_READ and (s.get("not_added") or (s.get("received_at") or "9") < cutoff)]
    day_no = (today - admitted).days + 1 if admitted and today >= admitted else None
    patient_block = {"name": patient.get("name") or _admin(facts, "name", "patient name") or "Name not read yet",
                     "age": int(age_years.group(1)) if age_years else None,
                     "sex": {"m": "male", "f": "female"}.get(sex or "") or ("other" if str(patient.get("sex") or "").strip().lower().startswith("o") else None), "uhid": patient.get("uhid"),
                     "other_hospital_number": (patient.get("other_hospital_numbers") or [None])[0]}
    admission_block = {"id": admission["id"], "unit": admission.get("unit"),
                       "bed": effective_bed(admission, facts),
                       "ward": admission.get("ward") or _admin(facts, "ward"),
                       "admitted_on": admitted.isoformat() if admitted else None, "day": day_no,
                       "admitted_text": f"Admitted {admitted.day} {admitted:%b} (day {day_no})" if day_no else None,
                       "severity": admission.get("severity"), "severity_by": admission.get("severity_by"),
                       "severity_at": admission.get("severity_at"), "updated_at": admission.get("updated_at"),
                       "version": admission.get("version"), "demo_bed": admission.get("demo_bed")}
    old_stay = bool(admission.get("archived") and admission.get("old_case"))
    if old_stay:
        days = sorted({_day(f).isoformat() for f in facts if _day(f)})
        first = days[0] if days else admission.get("old_run_date")
        last = days[-1] if days else first
        label = f"Old stay · Summary service · {first or 'Date not written'} to {last or 'Date not written'}"
        admission_block.update(read_only=True, first_date=first, last_date=last, stay_label=label,
                               admitted_text=None, admitted_on=None, day=None, severity=None)
        flag = {"flag": None, "reason": ""}
        notices = changes = []
    return {
        "oldStay": admission_block if old_stay else None,
        "as_of": as_of.isoformat(),
        "patient": patient_block,
        "admission": admission_block,
        "diagnosis": dx,
        "brief": build_brief(patient_block, admission_block, dx, alerts, plan),
        "flag": flag,
        "alerts": alerts,
        "changes": changes,
        "news": "" if old_stay else news_line(admission, changes, len(qrows) + len(checks), len(stuck), len(facts)),
        "stuck": stuck,
        "notices": notices,
        "questions": qrows,
        "checks": checks,
        "vitals": vitals,
        "labs": labs,
        "medicines": meds,
        "plan": plan,
        "fold_lines": fold_lines(vitals, labs, meds, plan),
        "sources": {s["id"]: {"n": photo_no.get(s["id"]), "of": len(files), "kind": s.get("kind"),
                              "received_at": s.get("received_at"), "status": s.get("status"),
                              **({"label": s["label"]} if s.get("label") else {})} for s in sources},
        "counts": {"photos": len(files), "entries": len(facts),
                   "days": len({_day(f) for f in facts if _day(f)}),
                   "doubtful_dates": sum(1 for f in facts if f.get("time_doubtful")),
                   "reading": sum(1 for s in sources if s.get("status") in (m.SRC_RECEIVED, m.SRC_READ))},
    }

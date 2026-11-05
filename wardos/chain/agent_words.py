"""One job read from the doctor's words becomes a checked agent command."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

from ..config import hospital_now
from .normalize import fact_key, name_words, words_match

DEFAULT_EVERY_MIN = 15
LONGEST_WATCH = timedelta(hours=24)

_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
         "eighteen": 18, "nineteen": 19,
         "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": 6, "saat": 7,
         "sat": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12, "baarah": 12, "terah": 13,
         "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19, "bees": 20}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

_GET = re.compile(r"\b(?:get|fetch|download|pull|bring|laao|lao|le\s*aao|nikalo|mangao|mangwao)\b.*\b(?:reports?|riport|results?)\b"
                  r"|\b(?:reports?|riport|results?)\b.*\b(?:fetch|download|laao|lao|nikalo|mangao|mangwao)\b"
                  r"|\b(?:reports?|riport|results?)\b.*\b(?:from|on)\s+(?:the\s+)?(?:portal|e\s*hospital|lis|website|hospital\s+site)\b"
                  r"|\b(?:check|search|look)\b.*\b(?:e\s*hospital|portal|lis)\b", re.I)
_WATCH = re.compile(r"\b(?:watch|watching|monitor|monitoring|track|keep\s+(?:an\s+)?eye|nazar|wake\s+me|alert\s+me)\b", re.I)
_STOP = re.compile(r"\b(?:stop|cancel|end|band|rok)\b.*\b(?:watch|watching|monitor|monitoring|nazar|tracking)\b", re.I)
_LIST = re.compile(r"\b(?:what|which|list|show)\b.*\b(?:watching|watches|monitoring)\b", re.I)
_HOSPITAL = re.compile(r"\b(?:e\s*hospital|portal|hospital\s+(?:site|website|system)|vacant|free\s+beds?|empty\s+beds?|"
                       r"bed\s+occupancy|occupied\s+beds?|(?:free|vacant|empty)\s+beds?\s+in|"
                       r"how\s+many\s+(?:\w+\s+){0,3}?(?:are\s+|were\s+|have\s+been\s+)?(?:admitted|discharged)|"
                       r"(?:who|which\s+patients?)\s+(?:was|were|got|is|are)\s+(?:admitted|discharged)|"
                       r"(?:admitted|discharged)\s+(?:today|yesterday|this\s+week))\b", re.I)
_CANNOT = re.compile(r"^\s*(?:please\s+|pls\s+)?(?:book|order|cancel|prescribe|schedule|send|call|phone|transfer|refer|"
                     r"submit|sign|delete|remove|pay|email|whatsapp|start|stop|give(?!\s+me))\b", re.I)
_TEST = re.compile(r"\b(potassium|sodium|chloride|calcium|magnesium|creatinine|urea|hb|hgb|ha?emoglobin|tlc|wbc|platelets?|"
                   r"plt|bilirubin|sgot|sgpt|alp|albumin|inr|crp|procalcitonin|lactate|troponin|glucose|sugar|k\+?|na\+?)\b", re.I)
_SHORT_TEST = {"k": "potassium", "k+": "potassium", "na": "sodium", "na+": "sodium", "sugar": "glucose", "hgb": "hb",
               "plt": "platelets", "wbc": "tlc"}


def spoken_numbers(text: str | None) -> str:
    words = str(text or "").translate(str.maketrans("०१२३४५६७८९", "0123456789"))
    words = re.sub(r"[''’]s\b", "", words)

    def tens(hit):
        return str(_TENS[hit.group(1).lower()] + (_ONES.get((hit.group(2) or "").lower(), 0)))
    words = re.sub(r"\b(" + "|".join(_TENS) + r")(?:[\s-]+(one|two|three|four|five|six|seven|eight|nine))?\b", tens, words, flags=re.I)

    def after_bed(hit):
        return hit.group(1) + str(_ONES[hit.group(2).lower()])
    words = re.sub(r"\b(bed\s*(?:no\.?|number)?\s*)(" + "|".join(_ONES) + r")\b", after_bed, words, flags=re.I)
    return re.sub(r"\b(" + "|".join(k for k, v in _ONES.items() if k in ("one two three four five six seven eight nine ten eleven "
                  "twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()) + r")\b",
                  lambda hit: str(_ONES[hit.group(1).lower()]), words, flags=re.I)


def _squash(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def bed_label(bed) -> str:
    return re.sub(r"^bed\s*", "", str(bed or "").strip(), flags=re.I).strip()


def ward_list(record, *, m) -> list[dict]:
    rows = []
    for admission in record.store.query(m.ADMISSIONS, status="active"):
        if admission.get("archived"):
            continue
        patient = record.store.get(m.PATIENTS, admission["patient_id"]) or {}
        rows.append({"id": admission["id"], "bed": bed_label(admission.get("bed")), "name": patient.get("name") or "", "name_words": name_words(patient.get("name"))})
    return rows


def _named(text: str, ward: list[dict]) -> list[dict]:
    tokens = [t.lower() for t in re.findall(r"[A-Za-z]{3,}", text)]
    return [row for row in ward if any(words_match(t, n) for t in tokens for n in row.get("name_words") or [])]


def beds_in_words(text: str) -> set[str]:
    words = spoken_numbers(text).lower()
    return {_squash(x) for x in re.findall(r"\b[a-z]?\d+[a-z]?\b", words)} | \
        {_squash(x) for x in re.findall(r"\bbed\s*(?:no\.?|number)?\s*([a-z0-9-]+)", words)}


def plain_test(value) -> str | None:
    text = " ".join(str(value or "").lower().split())
    text = _SHORT_TEST.get(text, text)
    if not text or not re.fullmatch(r"[a-z0-9][a-z0-9 +/.-]{0,39}", text):
        return None
    return text


def clock_text(when: datetime) -> str:
    hour = when.hour % 12 or 12
    return f"{hour}{':%02d' % when.minute if when.minute else ''} {'AM' if when.hour < 12 else 'PM'}"


def parse_clock(value) -> tuple[int, int] | None:
    hit = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*", str(value or "").lower())
    if not hit:
        return None
    hour, minute = int(hit.group(1)), int(hit.group(2) or 0)
    if hit.group(3):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if hit.group(3).startswith("p") else 0)
    return (hour, minute) if hour < 24 and minute < 60 else None


def _next(now: datetime, hour: int, minute: int) -> datetime:
    end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return end if end > now else end + timedelta(days=1)


def watch_end(until, for_minutes, now: datetime) -> datetime:
    end = None
    try:
        minutes = float(for_minutes) if for_minutes not in (None, "") else 0
    except (TypeError, ValueError):
        minutes = 0
    if minutes > 0:
        end = now + timedelta(minutes=minutes)
    else:
        clock = parse_clock(until)
        if clock:
            end = _next(now, *clock)
    if end is None:
        end = _next(now, 8, 0)
    return min(end, now + LONGEST_WATCH)


@dataclass
class Command:
    action: str
    admission_id: str | None = None
    bed: str | None = None
    test: str | None = None
    until: datetime | None = None
    every_min: int = DEFAULT_EVERY_MIN
    question: str | None = None
    message: str | None = None
    how: str = "none"
    cost_inr: float = 0.0
    understood_by: str = "model"


def resolve(reply: dict, text: str, ward: list[dict], current_id: str | None, *, now: datetime | None = None,
            min_every: int = 5, job_text: str | None = None, cost: float = 0.0, understood_by: str = "model") -> Command:
    """One job read by Muse (or by the code reading) becomes a checked command: the bed must exist on this ward."""
    now = now or hospital_now()
    safe_words = text
    action = reply["action"]
    done = Command(action, cost_inr=cost, understood_by=understood_by)
    if action in ("ask_record", "cannot", "list_watches"):
        if action == "cannot":
            done.message = str(reply.get("question") or "I can't do that yet")[:300]
        return done
    if action == "hospital_question":
        question = str(reply.get("question") or job_text or safe_words)
        done.question = question[:300]
        return done

    said = beds_in_words(text)
    asked_bed = bed_label(reply.get("bed")) or None
    named = _named(job_text or "", ward) or _named(text, ward)
    row = None
    if asked_bed:
        if _squash(asked_bed) not in said:
            asked_bed = None
        else:
            row = next((r for r in ward if _squash(r["bed"]) == _squash(asked_bed)), None)
            if row is None:
                done.message = f"There is no bed {asked_bed} on this ward. Nothing was opened."
                return done
            done.how = "bed"
    if row is None and len(named) == 1:
        row, done.how = named[0], "name"
    if row is None and len(named) > 1:
        done.message = "More than one patient has that name. Say the bed number. Nothing was opened."
        return done
    if row is None and current_id:
        row = next((r for r in ward if r["id"] == current_id), None)
        if row is not None:
            done.how = "current"
    if row is None and action == "stop_watch":
        return done
    if row is None:
        done.message = "Which bed? Say the bed number. Nothing was opened."
        return done
    done.admission_id, done.bed = row["id"], row["bed"] or None

    if action == "watch":
        test = plain_test(reply.get("test"))
        if not test:
            done.message = "Which test should I watch? Say it, for example: watch bed 12 potassium. Nothing was opened."
            return done
        done.test = test
        done.until = watch_end(reply.get("until"), reply.get("for_minutes"), now)
        try:
            every = int(float(reply.get("every_minutes") or DEFAULT_EVERY_MIN))
        except (TypeError, ValueError):
            every = DEFAULT_EVERY_MIN
        done.every_min = max(min_every, min(every, 240))
    if action == "get_reports" and reply.get("test"):
        done.test = plain_test(reply.get("test")) or None
    if action == "stop_watch" and reply.get("test"):
        done.test = plain_test(reply.get("test"))
    return done


def same_test(a: str | None, b: str | None) -> bool:
    return bool(a and b) and fact_key("lab_result", a) == fact_key("lab_result", b)


_MISHEARD = re.compile(r"\b(?:web|bat|bad|bid|bet|bud|bread|bedd|ped|bag)\s*(?:no\.?|number)?\s*(?=\d)", re.I)


def fix_bed_words(text: str) -> str:
    """The microphone hears "bed 13" as "web 13" or "bat 13": a bed number after such a word is read as a bed."""
    return _MISHEARD.sub("bed ", text or "")

"""Plain-code helpers: names to keys, values to comparable form, times to slots, photo fingerprints, quotes."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import io
import re

from ..record.labs import SYNONYMS as LAB_SYNONYMS
from .model import SINGLE_VALUED

try:
    from rapidfuzz import fuzz as _fuzz

    def _partial(a: str, b: str) -> float:
        return _fuzz.partial_ratio(a, b)

    def _token_set(a: str, b: str) -> float:
        return _fuzz.token_set_ratio(a, b)
except ImportError:                      # pragma: no cover - depends on the environment
    from difflib import SequenceMatcher

    def _partial(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        best = 0.0
        step = max(1, len(short) // 4)
        for start in range(0, max(1, len(long_) - len(short) + 1), step):
            best = max(best, SequenceMatcher(None, short, long_[start:start + len(short)]).ratio())
        return best * 100

    def _token_set(a: str, b: str) -> float:
        sa, sb = set(a.split()), set(b.split())
        if not sa or not sb:
            return 0.0
        return 100 * len(sa & sb) / min(len(sa), len(sb))


_SYNONYM_TO_KEY = {}
for _key, _names in LAB_SYNONYMS.items():
    for _name in _names:
        _SYNONYM_TO_KEY.setdefault(_name, "haemoglobin" if _key == "hb" else _key)
_SYNONYM_TO_KEY.update({
    "hb": "haemoglobin", "hgb": "haemoglobin", "hemoglobin": "haemoglobin",
    "pr": "pulse", "pulse rate": "pulse", "heart rate": "pulse", "hr": "pulse",
    "bp": "blood pressure", "blood pressure": "blood pressure",
    "rr": "respiratory rate", "resp rate": "respiratory rate", "respiratory rate": "respiratory rate",
    "temp": "temperature", "temperature": "temperature", "t": "temperature",
    "spo2": "spo2", "sp o2": "spo2", "oxygen saturation": "spo2", "sao2": "spo2",
    "grbs": "blood glucose", "rbs": "blood glucose", "random blood sugar": "blood glucose",
    "s creatinine": "creatinine", "serum creatinine": "creatinine", "s. creatinine": "creatinine",
})
_DRUG_PREFIX = re.compile(r"^(inj|injection|tab|tablet|cap|capsule|syp|syr|syrup|oint|ointment|neb|nebulisation|"
                          r"nebulization|iv|po|oral|t|c)\.?\s+", re.I)
_STRENGTH = re.compile(r"\b\d+(\.\d+)?\s*(mg|gm|g|mcg|µg|ml|iu|units?|%)\b", re.I)


def clean(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^\w%+/.\- ]", " ", text)
    return " ".join(text.split())


def fact_key(type_: str, key: str | None, label: str | None = None) -> str:
    base = clean((key or label or "").replace("_", " "))
    if type_ == "med_event":
        return drug_key(base)
    base = base.strip(" .-")
    return _SYNONYM_TO_KEY.get(base, base)


def drug_key(name: str) -> str:
    name = clean(name)
    for _ in range(2):
        name = _DRUG_PREFIX.sub("", name)
    name = _STRENGTH.sub(" ", name)
    return " ".join(name.split()).strip(" .-")


_NUM = re.compile(r"[<>]?\d+(?:\.\d+)?")


def value_signature(value: dict | None) -> str:
    value = value or {}
    number = value.get("number")
    if isinstance(number, (int, float)):
        return f"{float(number):g} {clean(value.get('unit'))}".strip()
    text = clean(value.get("text"))
    return text


_LEAD_FLAG = re.compile(r"^(HH|LL|H|L)\s*:?\s*(?=[<>]?[+-]?\d)")
_TAIL_FLAG = re.compile(r"\s*[(\[]?\b(HH|LL|H|L)[)\]]?\s*$")


def split_flag(text: str | None, number: float | None = None) -> tuple[str, str | None]:
    t = str(text or "").strip()

    def agrees(rest: str) -> bool:
        rest = rest.replace(",", "")
        first = re.match(r"[<>]?[+-]?\d+(?:\.\d+)?", rest)
        if not first:
            return False
        after = rest[first.end():]
        if re.match(r"\s*[-/:]\s*\d", after) or len(after) > 24:
            return False
        if number is None:
            return True
        try:
            return abs(float(first.group().lstrip("<>")) - float(number)) < 1e-9
        except ValueError:
            return False
    hit = _LEAD_FLAG.match(t)
    if hit:
        rest = t[hit.end():].strip()
        if agrees(rest):
            return rest, hit.group(1)
    hit = _TAIL_FLAG.search(t)
    if hit and hit.start() > 0:
        rest = t[:hit.start()].strip()
        if agrees(rest) and not rest.endswith("/"):
            return rest, hit.group(1)
    return t, None


def _same_identity(a: dict, b: dict, key: str) -> bool | None:
    ta, tb = str((a or {}).get("text") or ""), str((b or {}).get("text") or "")
    if key in ("name", "patient name"):
        na, nb = squash_name(ta), squash_name(tb)
        return bool(na and nb) and (na == nb or na in nb or nb in na)
    if key in ("sex", "gender"):
        sa, sb = clean(ta)[:1], clean(tb)[:1]
        return bool(sa) and sa == sb
    if key == "age":
        ya, yb = re.match(r"\s*(\d{1,3})", ta), re.match(r"\s*(\d{1,3})", tb)
        return bool(ya and yb) and abs(int(ya.group(1)) - int(yb.group(1))) <= 1
    return None


def same_value(a: dict, b: dict, type_: str, key: str | None = None) -> bool:
    sa, sb = value_signature(a), value_signature(b)
    if sa == sb:
        return True
    if type_ == "identity" and key:
        same = _same_identity(a, b, key)
        if same is not None:
            return same
    if type_ in SINGLE_VALUED:
        na, nb = (a or {}).get("number"), (b or {}).get("number")
        if isinstance(na, (int, float)) and isinstance(nb, (int, float)):
            return abs(float(na) - float(nb)) < 1e-9
        return False
    return bool(sa and sb) and _token_set(sa, sb) >= 90


def time_slot(type_: str, clinical_time: str | None, precision: str) -> str | None:
    if type_ in ("identity",):
        return "static"
    if not clinical_time or precision == "unknown":
        return None
    if precision == "exact":
        return clinical_time[:16]
    return clinical_time[:10]


def same_moment(a: dict, b: dict) -> str:
    if a["type"] == "identity":
        return "same"
    ta, tb = a.get("clinical_time"), b.get("clinical_time")
    pa, pb = a.get("time_precision", "unknown"), b.get("time_precision", "unknown")
    if not ta or not tb or "unknown" in (pa, pb):
        return "unknown"
    if ta[:10] != tb[:10]:
        return "different"
    if pa == pb == "exact":
        return "same" if ta[:16] == tb[:16] else "different"
    if pa == pb == "day":
        return "same"
    return "overlap"


_DATE_PATTERNS = (
    (re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b"), "dmy4"),
    (re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2})\b"), "dmy2"),
    (re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})\b"), "dm"),
)
_TIME = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm|AM|PM)?")


def parse_clinical_time(text: str | None, *, around: date | None = None) -> tuple[str | None, str]:
    if not text:
        return None, "unknown"
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        day, month = int(m.group(1)), int(m.group(2))
        if kind == "dmy4":
            year = int(m.group(3))
        elif kind == "dmy2":
            year = 2000 + int(m.group(3))
        else:
            year = (around or date.today()).year
        try:
            when = date(year, month, day)
        except ValueError:
            continue
        if around and kind == "dm" and (when - around).days > 200:
            when = date(year - 1, month, day)
        if around and abs((when - around).days) > 200:
            return None, "unknown"
        rest = text[m.end():]
        t = _TIME.search(rest)
        if t:
            hour, minute = int(t.group(1)), int(t.group(2))
            ampm = (t.group(3) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            if hour < 24 and minute < 60:
                return datetime(when.year, when.month, when.day, hour, minute).isoformat(timespec="minutes"), "exact"
        return when.isoformat(), "day"
    return None, "unknown"


def check_iso_time(value: str | None) -> tuple[str | None, str]:
    if not value or not isinstance(value, str):
        return None, "unknown"
    value = value.strip().replace(" ", "T")
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat(), "day"
        return datetime.fromisoformat(value[:16]).isoformat(timespec="minutes"), "exact"
    except ValueError:
        return None, "unknown"


def photo_fingerprint(data: bytes) -> str | None:
    try:
        from PIL import Image
        picture = Image.open(io.BytesIO(data)).convert("L").resize((9, 8))
    except Exception:
        return None
    pixels = list(picture.tobytes())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return f"{bits:016x}"


def fingerprint_distance(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def quote_found(quote: str | None, page_text: str | None, threshold: float = 90) -> bool:
    q, page = clean(quote), clean(page_text)
    if not q or not page:
        return False
    if q in page:
        return True
    return _partial(q, page) >= threshold


def squash_name(name: str | None) -> str:
    name = clean(name)
    if re.search(r"illegible|unreadable|unclear|not legible|unknown|\?", name):
        return ""
    name = re.sub(r"\b(mr|mrs|ms|miss|smt|shri|sri|baby|master|km|kumari|late)\b\.?", " ", name)
    return re.sub(r"[^a-z]", "", name)


def same_patient(expected: dict, seen: dict) -> bool | None:
    exp_uhid = re.sub(r"\D", "", str(expected.get("uhid") or ""))
    seen_uhid = re.sub(r"\D", "", str(seen.get("uhid") or ""))
    by_name = _same_name(expected, seen)
    if len(exp_uhid) == 11 and len(seen_uhid) == 11:
        if exp_uhid == seen_uhid:
            return True
        digits_off = sum(x != y for x, y in zip(exp_uhid, seen_uhid))
        return digits_off <= 2 and by_name is True
    return by_name


def _same_name(expected: dict, seen: dict) -> bool | None:
    a, b = squash_name(expected.get("name")), squash_name(seen.get("name"))
    if len(a) < 3 or len(b) < 3:
        return None
    if a in b or b in a:
        return True
    score = max(_partial(a, b), _token_set(clean(expected.get("name")), clean(seen.get("name"))))
    if score >= 70:
        return True
    return False if score < 50 else None


UHID_KEYS = {"uhid", "uhid no", "uhid number"}
NAME_KEYS = {"name", "patient name"}
OTHER_NUMBER_KEY = "other_hospital_number"
IDENTITY_AUTO_KEYS = UHID_KEYS | NAME_KEYS | {OTHER_NUMBER_KEY}
OUTSIDE_PAGE_TYPES = {"referral", "discharge_card"}
HOSPITAL_UHID_LENGTH = 11


def norm_uhid(text: str | None) -> str:
    return re.sub(r"[\s.\-]", "", str(text or "")).upper()


def edit_distance_at_most_one(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def classify_uhids(votes: dict[str, int], outside: set[str] | None = None, locked: str | None = None) -> dict:
    out = {"winner": None, "misread": {}, "other": [], "ask": []}
    outside = {v for v in (outside or set()) if v}
    own = {v: n for v, n in votes.items() if v and v not in outside}
    letters = [v for v in own if not v.isdigit()]
    digits = {v: n for v, n in own.items() if v.isdigit()}
    other = set(outside) | set(letters)
    winner = locked or None
    contested: list[str] = []
    if not winner and digits:
        ranked = sorted(digits, key=lambda v: (-digits[v], len(v) != HOSPITAL_UHID_LENGTH, v))
        winner = ranked[0]
        top = [v for v in ranked if digits[v] == digits[winner]
               and (len(v) == HOSPITAL_UHID_LENGTH) == (len(winner) == HOSPITAL_UHID_LENGTH)]
        if len(top) > 1:
            contested = top
    if contested:
        out["ask"] = contested
        winner = None
    out["winner"] = winner
    for v in digits:
        if v == winner or v in contested:
            continue
        if winner and edit_distance_at_most_one(v, winner):
            out["misread"][v] = "misread, one digit off" if len(v) == len(winner) else \
                ("misread, one digit missing" if len(v) < len(winner) else "misread, one digit extra")
        elif len(v) != HOSPITAL_UHID_LENGTH:
            other.add(v)
        elif winner and digits[v] == 1 and digits.get(winner, 0) >= 2:
            out["misread"][v] = "misread, seen on one page only"
        elif winner:
            out["ask"] = sorted({*out["ask"], v, winner})
            out["winner"] = None
        else:
            out["ask"] = sorted({*out["ask"], v})
    kept: list[str] = []
    for v in sorted(other, key=lambda v: (-own.get(v, 0), v)):
        if not any(_edit_distance(v, k) <= 2 for k in kept):
            kept.append(v)
    out["other"] = kept
    return out


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


_TITLES = {"mr", "mrs", "ms", "miss", "smt", "shri", "sri", "baby", "master", "km", "kumari", "late", "dr", "w/o",
           "s/o", "d/o", "c/o"}


def name_words(name: str | None) -> list[str]:
    text = clean(name)
    if not text or re.search(r"illegible|unreadable|unclear|not legible|unknown|\?", text):
        return []
    words = [w for w in re.sub(r"[^a-z ]", " ", text).split() if w not in _TITLES and len(w) > 1]
    return words if 0 < len(words) <= 4 else []


def words_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return _edit_distance(a, b) <= 1 or (len(a) >= 6 and len(b) >= 6 and _edit_distance(a, b) <= 2)


def names_overlap(a: list[str], b: list[str]) -> bool:
    return any(words_match(x, y) for x in a for y in b)


def group_names(votes: dict[str, int]) -> list[dict]:
    items = [(written, name_words(written), n) for written, n in votes.items()]
    items = [i for i in items if i[1]]
    groups: list[list[tuple]] = []
    for item in sorted(items, key=lambda i: -i[2]):
        joined = [g for g in groups if any(names_overlap(item[1], other[1]) for other in g)]
        if not joined:
            groups.append([item])
            continue
        merged = [item] + [x for g in joined for x in g]
        groups = [g for g in groups if g not in joined] + [merged]

    def seen(word: str, group: list[tuple]) -> int:
        return sum(n * sum(words_match(word, w) for w in words) for _, words, n in group)

    def order_agreement(cand: tuple, group: list[tuple]) -> int:
        score = 0
        for other in group:
            if other is cand:
                continue
            places = [next((k for k, w in enumerate(cand[1]) if words_match(w, ow)), None) for ow in other[1]]
            pairs = [(a, b) for i, a in enumerate(places) for b in places[i + 1:] if a is not None and b is not None]
            score += other[2] * sum(1 if a < b else -1 for a, b in pairs)
        return score

    out = []
    for group in groups:
        best = max(group, key=lambda i: (len(i[1]), order_agreement(i, group), sum(seen(w, group) for w in i[1]),
                                          i[2], i[0]))
        out.append({"names": {written: n for written, _, n in group}, "votes": sum(n for _, _, n in group),
                    "canonical": " ".join(w.capitalize() for w in best[1]), "best": best[0]})
    return sorted(out, key=lambda g: -g["votes"])


IST_OFFSET_HOURS = 5.5


def received_day(received_at: str | None) -> date | None:
    try:
        when = datetime.fromisoformat(str(received_at))
    except ValueError:
        return None
    if when.tzinfo:
        when = when.astimezone(timezone(timedelta(hours=IST_OFFSET_HOURS)))
    return when.date()


def is_future_time(clinical_time: str | None, received_at: str | None, slack_days: int = 1) -> bool:
    try:
        day = date.fromisoformat(str(clinical_time)[:10])
    except ValueError:
        return False
    arrived = received_day(received_at)
    return bool(arrived and day > arrived + timedelta(days=slack_days))


_NOT_A_DRUG = re.compile(r"\b(eggs?|(chewing\s+)?gum|diet|milk|biscuits?|bread|rice|dal|khichdi|fruits?|juice|soup|"
                         r"tea|coffee|curd|banana)\b", re.I)


def not_a_medicine(label: str | None, text: str | None = None) -> bool:
    return bool(_NOT_A_DRUG.search(f"{label or ''} {text or ''}"))


_RULE_OUT = re.compile(r"^\s*(?:\?+\s*|r\s*/\s*o\.?\s+|to\s+rou?t\.?\s+|to\s+rule\s+out\s+|rule\s+out\s+)", re.I)


def clean_diagnosis(text: str | None) -> str:
    text = " ".join(str(text or "").split())
    text = re.sub(r"\(\s*illegible\s*\)\s*/?", "", text, flags=re.I).strip(" /")
    hit = _RULE_OUT.match(text)
    if hit:
        rest = text[hit.end():].strip()
        return f"Rule out {rest[:1].upper()}{rest[1:]}" if rest else text
    return text[:1].upper() + text[1:]

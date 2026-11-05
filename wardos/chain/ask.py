"""The ask bar: a typed or spoken sentence becomes one answer card, worked out in code where it can be."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import difflib
import re

from . import model as m
from .normalize import fact_key, name_words, words_match
from .today import (MED_GROUPS, REPORT_ORDER, _day, _label, _ref, _sex, _show, _time_key, _unsure_time, build_today,
                    is_danger, normal_range, outside, report_group, reports_in_order, shown_range, standard_unit)

ACTIONS = ("reports", "tests", "vitals", "medicines", "plan", "brief", "photo", "summary", "note", "get_reports",
           "new_patient", "old_patient", "set_bed", "anything", "unknown")
HELP = ("Ask me about a patient by bed or name. For example: lowest potassium for bed 12, medicines of bed 14, "
        "or how is Sunil. I can show: reports, tests, vitals, medicines, plan, summary")
NO_ADVICE = ("I answer only from what is in a patient's record, so I don't give treatment advice. "
             "Ask about a patient, for example: potassium of bed 12.")
PORTAL_MESSAGE = "The portal agent isn't connected to this screen yet"
ACTION_WORDS = {"reports": "reports", "tests": "tests", "vitals": "vitals", "medicines": "medicines", "plan": "plan",
                "brief": "brief", "photo": "photo", "summary": "discharge summary", "note": "note",
                "get_reports": "get reports", "new_patient": "new patient"}


_DEVANAGARI = {"दवा": "dawa", "दवाई": "dawai", "रिपोर्ट": "report", "जांच": "janch", "जाँच": "janch",
               "योजना": "plan", "फोटो": "photo", "नब्ज": "pulse", "बिस्तर": "bed", "बेड": "bed",
               "नया": "new", "नई": "new", "नए": "new", "मरीज": "patient", "मरीज़": "patient",
               "मरीज़": "patient", "पेशेंट": "patient", "रोगी": "patient", "जोड़ो": "add", "जोड़ो": "add",
               "नाम": "name", "उम्र": "age", "आयु": "age", "साल": "years", "वर्ष": "years",
               "पुरुष": "male", "महिला": "female", "मेल": "male", "फीमेल": "female", "न्यू": "new", "यूनिट": "unit", "मेडिसिन": "medicine",
               "सर्जरी": "surgery", "है": "is", "में": "in", "पर": "par", "शिफ्ट": "shift"}

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("new_patient", re.compile(r"\b(?:new|naya|nayi|add|admit|register|create|make)\s+(?:a\s+)?(?:new\s+)?"
                               r"(?:patient|case|admission|marij|mareez|mariz)\b|\bnew\s+admission\b|"
                               r"^\s*admit\b")),
    ("note", re.compile(r"^\s*(?:(?:add|save|write|make|take)\s+(?:a\s+)?)?(?:note|likho|remember)\b|"
                        r"\bnote\s*(?:kar|karo|likh)\b")),
    ("summary", re.compile(r"\b(?:discharge|summary|summery|summry|saransh|final\s+summary)\b")),
    ("get_reports", re.compile(r"\b(?:get|fetch|download|pull|laao|lao|nikalo|mangao|mangwao)\b.*\b(?:reports?|riport)\b|"
                               r"\b(?:reports?|riport)\b.*\b(?:from|on|of)\s+(?:the\s+)?(?:portal|ehospital|e\s*hospital|"
                               r"website|hospital\s+site)\b")),
    ("photo", re.compile(r"\b(?:photos?|photograph|picture|pic|image|page|tasveer|foto|fotos)\b")),
    ("vitals", re.compile(r"\b(?:vitals?|vitel|vitles|bp|blood\s*pressure|pulse|temp|temperature|spo2|sat|saturation|"
                          r"heart\s*rate|resp(?:iratory)?\s*rate|nabz|bukhar|fever)\b")),
    ("medicines", re.compile(r"\b(?:medicines?|medicin|medcine|medicene|medications?|meds?|drugs?|dawai|dawa|davai|dava|"
                             r"dawaiyan|tablets?|injections?|syrups?)\b")),
    ("plan", re.compile(r"\b(?:plan|plans|pending|todo|to\s*do|advice|advise|kya\s*karna|next\s*steps?)\b")),
    ("tests", re.compile(r"\b(?:tests?|testing|labs?|investigations?|janch|jaanch|blood|lft|kft|rft|cbc|liver|kidney|renal|"
                         r"electrolytes?|clotting|coag|sugar|glucose|urine|haemogram|hemogram)\b")),
    ("reports", re.compile(r"\b(?:reports?|riport|ripot|results?)\b")),
    ("brief", re.compile(r"\b(?:brief|about|overview|who|status|how\s+is|kaisa|kaisi|kaise|haal)\b")),
]

_GROUPS: list[tuple[int, re.Pattern]] = [
    (0, re.compile(r"\b(?:cbc|blood\s+count|haemogram|hemogram|counts?)\b")),
    (1, re.compile(r"\b(?:kft|rft|renal|kidney|electrolytes?)\b")),
    (2, re.compile(r"\b(?:lft|liver)\b")),
    (3, re.compile(r"\b(?:clotting|coag|coagulation)\b")),
    (4, re.compile(r"\b(?:sugar|glucose|hba1c)\b")),
    (5, re.compile(r"\burine\b")),
]
_GROUP_TITLE = {0: "Blood count (CBC)", 1: "Kidney tests and electrolytes", 2: "Liver tests", 3: "Clotting",
                4: "Sugar", 5: "Urine"}

_TEST_NAMES = re.compile(r"\b(?:tlc|wbc|hb|hgb|haemoglobin|hemoglobin|platelets?|plt|sodium|potassium|chloride|calcium|"
                         r"creatinine|urea|bilirubin|sgot|sgpt|alp|ast|alt|albumin|uric|inr|esr|crp|hba1c|ferritin|"
                         r"ggt|amylase|lipase)\b")

_STOP = {"the", "of", "a", "an", "for", "show", "me", "tell", "give", "what", "whats", "is", "are", "on", "in", "to", "and",
         "patient", "please", "pls", "ka", "ki", "ke", "ko", "hai", "hain", "dikhao", "dikha", "batao", "bata", "dekho",
         "bed", "number", "no", "new", "all", "latest", "today", "todays", "my", "with", "his", "her", "s", "make",
         "get", "add", "from", "any", "now", "can", "you", "i", "want", "see", "open", "show", "wala", "wali", "ye",
         "yeh", "kya", "kaun", "me", "mein", "se", "ka", "file", "order", "mr", "mrs", "ms", "dr", "hi", "hello"}


def _command_words(text: str | None) -> str:
    text = str(text or "")
    text = text.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
    for word, latin in sorted(_DEVANAGARI.items(), key=lambda item: -len(item[0])):
        text = re.sub(r"(?<![\w\u0900-\u097f])" + re.escape(word) + r"(?![\w\u0900-\u097f])", latin, text)
    return text


def _norm(text: str | None) -> str:
    text = _command_words(text)
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"'s\b", "", text)
    return " ".join(re.sub(r"[^a-z0-9ऀ-ॿ ]", " ", text).split())


def _short_name(name: str | None) -> str:
    words = name_words(name)
    return words[0].capitalize() if words else ""


def ward_entry(admission: dict, patient: dict, page: dict) -> dict:
    bed = str(page["admission"].get("bed") or admission.get("bed") or "").strip()
    bed = re.sub(r"^bed\s*", "", bed, flags=re.I).strip()
    name = page["patient"].get("name")
    name = None if not name or name == "Name not read yet" else name
    short = _short_name(name)
    label = " · ".join(x for x in (f"Bed {bed}" if bed else "", short) if x) or "This patient"
    return {"id": admission["id"], "bed": bed, "name": name, "short": short, "label": label,
            "bed_label": f"Bed {bed}" if bed else (short or "this patient"), "page": page}


def build_ward(record) -> list[dict]:
    def one(admission):
        patient = record.store.get(m.PATIENTS, admission["patient_id"]) or {}
        facts, questions = record.facts(admission["id"]), record.questions(admission["id"])
        sources = record.store.query(m.SOURCES, admission_id=admission["id"])
        pages = {p["id"]: p for p in record.pages(admission["id"])} if questions else {}
        page = build_today(admission, patient, facts, questions, pages_by_id=pages, sources=sources)
        return ward_entry(admission, patient, page)
    def safe(admission):
        try:
            return one(admission)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("ward entry failed for %s", admission.get("id"))
            return None
    live = [a for a in record.store.query(m.ADMISSIONS, status="active") if not a.get("archived")]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [w for w in pool.map(safe, live) if w]


def _bed_of(text: str) -> str | None:
    hit = re.search(r"\bbed\s*(?:no|number|num|nmbr)?\s*([a-z]?\d+[a-z]?)\b", text) or \
        re.search(r"\b(\d+[a-z]?)\s*(?:number\s*)?bed\b", text)
    return hit.group(1) if hit else None


_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                             "dec"))}


_IST = timezone(timedelta(hours=5, minutes=30))


def _asked_day(norm: str, days: list[str]) -> str | None:
    year = int(days[0][:4]) if days else datetime.now(_IST).year
    found = []
    for hit in re.finditer(r"\b(\d{4}) (\d{1,2}) (\d{1,2})\b", norm):
        found.append((int(hit.group(1)), int(hit.group(2)), int(hit.group(3))))
    for hit in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3})[a-z]*\b", norm):
        if hit.group(2) in _MONTHS:
            found.append((year, _MONTHS[hit.group(2)], int(hit.group(1))))
    for hit in re.finditer(r"\b([a-z]{3})[a-z]* (\d{1,2})\b", norm):
        if hit.group(1) in _MONTHS:
            found.append((year, _MONTHS[hit.group(1)], int(hit.group(2))))
    if not found:
        for hit in re.finditer(r"(?<!\d )\b(\d{1,2}) (\d{1,2})\b(?! \d)", norm):
            found.append((year, int(hit.group(2)), int(hit.group(1))))
    for y, mo, d in found:
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    if re.search(r"\b(?:yesterday|kal)\b", norm):
        return (datetime.now(_IST).date() - timedelta(days=1)).isoformat()
    if re.search(r"\b(?:previous|earlier|before|pichla|pichhla|pichli|purana|purani|last time)\b", norm):
        return days[1] if len(days) > 1 else None
    return None


def _known_units() -> list[str]:
    from .units import UNIT_FILES
    return list(UNIT_FILES)


_UNIT = re.compile(r"\b(medicine|surgery|surgical)\s*(?:unit|department|dept)?\s*(\d|one|two|three|1|2|3)?\b", re.I)
_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3"}
_FILLER = {"new", "naya", "nayi", "add", "admit", "admitted", "register", "create", "make", "a", "an", "the", "patient",
           "case", "admission", "marij", "mareez", "mariz", "name", "named", "called", "naam", "hai", "is", "aged",
           "age", "years", "year", "yrs", "yr", "old", "saal", "sal", "male", "female", "man", "woman", "boy", "girl",
           "lady", "purush", "mahila", "mard", "aurat", "sex", "gender", "uhid", "cr", "no", "number", "bed", "in",
           "on", "of", "for", "with", "and", "ward", "today", "yesterday", "kal", "aaj", "unit", "medicine",
           "surgery", "surgical", "department", "dept", "one", "two", "three", "please", "ka", "ki", "ke", "se",
           "mein", "me", "to", "at", "from", "m", "f", "date", "dated", "jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "sept", "oct", "nov", "dec", "january", "february", "march", "april", "june", "july",
           "august", "september", "october", "november", "december", "mr", "mrs", "ms", "smt", "shri", "sri"}


def parse_new_patient(text: str, ward: list[dict]) -> dict:
    raw = _command_words(text)
    t = raw.replace(",", " , ")
    out: dict = {"name": None, "bed": None, "age": None, "sex": None, "uhid": None, "unit": None, "admitted_at": None}
    spans: list[tuple[int, int]] = []

    def take(pattern, flags=re.I):
        hit = re.search(pattern, t, flags)
        if hit:
            spans.append(hit.span())
        return hit
    hit = take(r"\b(?:uhid|cr\s*no|cr\s*number|registration\s*(?:no|number)?)\s*(?:no\.?|number)?\s*[:\-]?\s*(\d[\d\s-]{5,16}\d)")
    if not hit:
        hit = take(r"\b(\d{4}[\s-]?\d{3,4}[\s-]?\d{3,5})\b")
        if hit and len(re.sub(r"\D", "", hit.group(1))) < 8:
            spans.pop(); hit = None
    if hit:
        out["uhid"] = re.sub(r"\D", "", hit.group(1))
    hit = take(r"\bbed\s*(?:no\.?|number|num)?\s*([A-Za-z]?\d{1,3}[A-Za-z]?)\b")
    if hit:
        out["bed"] = hit.group(1).upper()
    n_before = len(spans)
    hit = take(r"\b(\d{1,3})\s*(?:years?|yrs?|yr|y|yo|saal|sal)\b(?:\s*old)?") or take(r"\bage[d]?\s*(?:is)?\s*(\d{1,3})\b") \
        or take(r"\b(\d{1,3})\s*(?=(?:/|,)?\s*(?:male|female|m|f|man|woman|boy|girl)\b)")
    if hit and 0 < int(hit.group(1)) < 120:
        out["age"] = hit.group(1)
    hit = take(r"\b(male|man|boy|purush|mard|female|woman|girl|lady|mahila|aurat)\b") or \
        take(r"(?<=\d)\s*/?\s*([mf])\b") or take(r"\b([mf])\b(?=\s*(?:,|$|bed|uhid|medicine|surgery))")
    if hit:
        out["sex"] = "F" if hit.group(1).lower() in ("female", "woman", "girl", "lady", "mahila", "aurat", "f") else "M"
    age_sex_at = min((a for a, _ in spans[n_before:]), default=None)
    hit = take(r"\b(medicine|surgery|surgical)\s*(?:unit|department|dept)?\s*(\d|one|two|three)?\b")
    units = _known_units()
    if hit:
        kind = "Surgery" if hit.group(1).lower().startswith("surg") else "Medicine"
        n = _NUMBER_WORDS.get((hit.group(2) or "").lower(), hit.group(2))
        exact = f"{kind} Unit {n}" if n else None
        out["unit"] = exact if exact in units else next((u for u in units if u.startswith(kind)), None)
    if not out["unit"]:
        seen = [w["page"]["admission"].get("unit") for w in ward if w["page"]["admission"].get("unit")]
        out["unit"] = max(set(seen), key=seen.count) if seen else (units[0] if units else None)
    day = _asked_day(_norm(raw), [])
    if re.search(r"\b(?:admitted|admit|admission|date|dated|kal|yesterday)\b", raw, re.I) and day:
        out["admitted_at"] = day
    out["admitted_at"] = out["admitted_at"] or datetime.now(_IST).date().isoformat()
    keep = []
    for i, ch in enumerate(t):
        keep.append(" " if any(a <= i < b for a, b in spans) else ch)
    left = re.sub(r"\d+", " ", "".join(keep))
    hit = re.search(r"\b(?:name|named|called|naam)\s*(?:is|hai)?\s+(.+)", left, re.I)
    if hit:
        left = hit.group(1)
    elif age_sex_at is not None:
        first = re.sub(r"\d+", " ", "".join(keep[:age_sex_at]))
        if [w for w in re.findall(r"[A-Za-zऀ-ॿ][A-Za-z.'ऀ-ॿ]*", first) if w.lower().strip(".") not in _FILLER]:
            left = first
    words = [w for w in re.findall(r"[A-Za-z\u0900-\u097F][A-Za-z.'\u0900-\u097F]*", left)
             if w.lower().strip(".") not in _FILLER]
    if words:
        out["name"] = " ".join(w.capitalize() if w.islower() or w.isupper() else w for w in words[:5])
    taken = next((w for w in ward if out["bed"] and _same_bed(w["bed"], out["bed"])), None)
    out["bed_taken_by"] = taken["label"] if taken else None
    return out


def _same_bed(a: str, b: str) -> bool:
    squash = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
    return bool(squash(a)) and squash(a) == squash(b)


_REAL_QUESTION = re.compile(r"\b(?:lowest|highest|minimum|maximum|min|max|peak|worst|best|trend|trending|when|why|"
                            r"which|how\s+many|how\s+much|how\s+long|first|last\s+time|ever|compare|compared|"
                            r"change[ds]?|since|before|after|reason|cause|supplements?|kab|kyun|kitna|kitne|"
                            r"worse|worsening|better|improv\w*|getting|rising|falling|going\s+(?:up|down)|"
                            r"increas\w*|decreas\w*|high|low|normal|okay|how\s+old|age|sex|gender)\b")
_ABNORMAL = re.compile(r"\b(?:abnormal|deranged|out\s+of\s+range|not\s+normal)\b")
_MONTH = r"(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)"
_DATE_NAMED = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)? {_MONTH}\b|\b{_MONTH} \d{{1,2}}\b")
_ORDERING = re.compile(r"\b(?:can|could|will|would)\s+you\s+(?:please\s+)?(?:order|book|prescribe|schedule|give|send|start|stop)\b")
_NORMAL_RANGE = re.compile(r"\bnormal\s+(?:range|value|level|limit)s?\b|\breference\s+range\b")
_WARD_ALWAYS = re.compile(r"\b(?:which|what|whose|who\s+has|who\s+is)\s+(?:patients?|beds?)\b|\b(?:all|my)\s+patients\b|"
                          r"\bhow\s+many\s+patients\b|\b(?:list|show)\s+(?:of\s+)?(?:the\s+)?patients\b|\bsickest\b|"
                          r"\bwho\s+(?:is|needs|has)\s+(?:the\s+)?(?:sick|critical|serious|unstable|worst|highest|lowest|attention)\w*\b|"
                          r"\bwhose\b")
_WARD_WORDS = re.compile(r"\b(?:sick|critical|serious|unstable|dangers?|dangerous|alerts?|ward|patients)\b")
_TREATMENT = re.compile(r"\b(?:should\s+i|what\s+to\s+give|treat|treatment|treated|manage|management|dose|dosage|kya\s+de[nt]a?)\b")
_TEST_WORDS = ("potassium", "sodium", "chloride", "calcium", "creatinine", "urea", "bilirubin", "haemoglobin", "hemoglobin",
               "platelets", "albumin", "ferritin", "amylase", "lipase")
_TEST_KEYS = {"hb": "haemoglobin", "hgb": "haemoglobin", "hemoglobin": "haemoglobin", "platelet": "platelets", "plt": "platelets",
              "wbc": "tlc", "sugar": "blood glucose", "glucose": "blood glucose"}
_NOT_A_NAME = {"patient", "doctor", "today", "yesterday", "morning", "evening", "night", "week", "month", "last", "that", "it",
               "what", "who", "here", "there", "let", "he", "she", "his", "her", "this", "bed", "ward", "hospital", "sir", "madam",
               "aaj", "kal", "iska", "uska", "mera", "mere", "meri"}


def _spoken_bed(norm: str) -> str:
    if not re.search(r"\bbed (?:no |number )?[a-z]", norm):
        return norm
    from .agent_words import spoken_numbers
    return _norm(spoken_numbers(norm))


def test_words(norm: str) -> str:
    norm = re.sub(r"(?<!vitamin )\bk\b(?! cit\b)", "potassium", norm)
    norm = re.sub(r"\bna\b(?= (?:of|for|in|level)\b)|(?<=\bbed )(\w+ )na$", lambda hit: (hit.group(1) or "") + "sodium", norm)
    out = []
    for word in norm.split():
        if len(word) >= 5 and word not in _TEST_WORDS:
            close = difflib.get_close_matches(word, _TEST_WORDS, n=1, cutoff=0.82)
            word = close[0] if close else word
        out.append(word)
    return " ".join(out)


def _named_test(norm: str) -> str | None:
    hit = _TEST_NAMES.search(norm)
    if not hit:
        return None
    return _TEST_KEYS.get(hit.group(0), fact_key("lab_result", hit.group(0)))


def _stranger(text: str, ward: list[dict]) -> str | None:
    for hit in re.finditer(r"\b([A-Za-z]{3,})(?:['’]s\b|\s+(?:ka|ki|ke)\b)", str(text)):
        word = hit.group(1).lower()
        if word in _NOT_A_NAME or _is_action_word(word) or _is_name_word(word, ward) or _TEST_NAMES.search(word) \
                or word in _TEST_WORDS or word in _FILLER or _GROUPS_WORD.search(word):
            continue
        rest = _norm(str(text)[hit.end():])
        if not (_TEST_NAMES.search(rest) or any(_is_action_word(t) and t not in _STOP for t in rest.split())):
            continue
        return f"There is no patient called {hit.group(1).capitalize()} on your list. Nothing was opened."
    return None


_GROUPS_WORD = re.compile(r"^(?:cbc|kft|rft|lft|liver|kidney|renal|sugar|urine|blood|heart|chest|lung|fever|pain|stool|bp|spo2)$")


def interpret(text: str, ward: list[dict], current_id: str | None = None, *, _allow_bed: bool = True) -> dict:
    norm = test_words(_spoken_bed(_norm(text)))
    action = next((name for name, pattern in _PATTERNS if pattern.search(norm)), None)
    group = next((n for n, pattern in _GROUPS if pattern.search(norm)), None)
    if action == "reports" and group is not None and re.search(r"\btests?\b", norm):
        action = "tests"
    if action == "tests" and re.search(r"\b(?:reports?|riport|ripot)\b", norm) \
            and not re.search(r"\b(?:tests?|testing|labs?|investigations?|janch|jaanch)\b", norm):
        action = "reports"
    if action in (None, "reports", "brief") and _TEST_NAMES.search(norm):
        action = "tests"
    if action == "new_patient":
        return {"action": action, "patient_id": None, "how": "none", "bed": _bed_of(norm), "group": None,
                "what_words": [], "note_text": None, "ambiguous": [], "text": str(text)}
    from .beds import bed_command
    move = bed_command(norm) if _allow_bed and action is None else None
    if move:
        who, destination = move
        picked = interpret((who + " brief").strip(), ward, current_id, _allow_bed=False)
        explicit = re.search(r"\b(?:move|shift|transfer|set|change|assign|pe|par|में|पर|शिफ्ट|बदलो)\b", norm)
        candidates = [w for w in ward if w["id"] == picked["patient_id"] or
                      w["id"] in {c["id"] for c in picked["ambiguous"]}]
        only_name = any(all(t in _STOP or any(words_match(t, n) for n in name_words(w["name"]))
                            for t in who.split()) for w in candidates)
        if explicit or (picked["how"] in ("name", "ambiguous") and only_name):
            remaining = [t for t in who.split() if t not in _STOP and t not in
                         {"move", "shift", "transfer", "set", "change", "assign", "pe", "par", "में", "पर", "शिफ्ट", "बदलो"}]
            if picked["how"] == "current" and remaining:
                picked.update(patient_id=None, how="none")
            return dict(picked, action="set_bed", destination_bed=destination, text=str(text))
    bed = _bed_of(norm)
    patient_id, how, ambiguous = None, "none", []
    if bed:
        hit = next((w for w in ward if _same_bed(w["bed"], bed)), None)
        patient_id, how = (hit["id"], "bed") if hit else (None, "missing_bed")
    tokens = [t for t in norm.split() if (len(t) >= 3 or t == "hb") and t not in _STOP and not t.isdigit()]
    if how == "none":
        score: dict[str, int] = {}
        for token in tokens:
            if _is_action_word(token):
                continue
            for w in ward:
                if any(words_match(token, nw) for nw in name_words(w["name"])):
                    score[w["id"]] = score.get(w["id"], 0) + 1
        best = max(score.values(), default=0)
        hits = {w["id"]: w for w in ward if best and score.get(w["id"]) == best}
        if len(hits) == 1:
            patient_id, how = next(iter(hits)), "name"
        elif len(hits) > 1:
            how, ambiguous = "ambiguous", [{"id": w["id"], "label": w["label"]} for w in hits.values()]
    from .register import OLD_WORDS, lookup_requested, query_parts
    if OLD_WORDS.search(norm) and lookup_requested(norm):
        wanted, _ = query_parts(norm)
        full = [w for w in ward if wanted and all(any(words_match(t, n) for n in name_words(w["name"])) for t in wanted)]
        if not full and how in ("name", "ambiguous"):
            patient_id, how, ambiguous = None, "none", []
    uhid = re.search(r"\b\d{11}\b", norm)
    if uhid:
        hit = next((w for w in ward if (w["page"].get("patient") or {}).get("uhid") == uhid[0]), None)
        patient_id, how = (hit["id"], "name") if hit else (None, "none")
    old_lookup = (how == "none" and lookup_requested(norm)) or (uhid and not patient_id)
    if old_lookup:
        return {"action": "old_patient", "patient_id": None, "how": "none", "bed": None, "group": None,
                "what_words": [], "note_text": None, "ambiguous": [], "text": str(text)}
    if how == "none" and current_id and any(w["id"] == current_id for w in ward):
        patient_id, how = current_id, "current"
    what = [t for t in tokens if not _is_action_word(t) and not _is_name_word(t, ward)]
    plain = {"action": action, "patient_id": None, "how": "none", "bed": bed, "group": None, "what_words": what,
             "note_text": None, "ambiguous": [], "text": str(text)}
    if action not in ("note", "summary", "get_reports") and _NORMAL_RANGE.search(norm) and _named_test(norm):
        return {**plain, "action": "normal_range", "patient_id": patient_id, "test": _named_test(norm)}
    if how in ("none", "current") and (_WARD_ALWAYS.search(norm) or (how == "none" and _WARD_WORDS.search(norm))):
        return {**plain, "action": "ward", "test": _named_test(norm)}
    if _ORDERING.search(norm):
        return {**plain, "action": "no_order"}
    stranger = _stranger(text, ward) if how in ("none", "current") and action not in ("note", "summary") else None
    if stranger:
        return {**plain, "action": "stranger", "stranger": stranger}
    if patient_id and action in (None, "tests", "reports", "brief") and _ABNORMAL.search(norm):
        action, what = "tests", []
    elif patient_id and action in (None, "tests", "reports", "brief", "vitals", "medicines", "plan")             and (
            _REAL_QUESTION.search(norm) or (action in (None, "tests", "vitals") and _DATE_NAMED.search(norm))):
        action = "anything"
    if action is None and how == "none" and _TREATMENT.search(norm):
        return {**plain, "action": "no_advice"}
    if action is None:
        action = ("anything" if patient_id and what else
                  "brief" if patient_id and how in ("bed", "name") else "unknown")
    note_text = None
    if action == "note":
        note_text = re.sub(r"^\s*(?:(?:add|save|write|make|take)\s+(?:a\s+)?)?(?:note|likho|remember)\b[\s:,\-]*", "",
                           str(text), flags=re.I).strip()
        note_text = re.sub(r"^(?:for\s+)?bed\s*(?:no\.?|number)?\s*[a-z]?\d+[a-z]?\s*[:,\-]?\s*", "", note_text,
                           flags=re.I).strip()
    return {"action": action, "patient_id": patient_id, "how": how, "bed": bed, "group": group, "what_words": what,
            "note_text": note_text, "ambiguous": ambiguous, "text": str(text)}


_ACTION_TOKENS = {t for _, pattern in _PATTERNS for t in re.findall(r"[a-z0-9]{3,}", pattern.pattern)} | {
    "photo", "report", "reports", "test", "tests", "vital", "vitals", "medicine", "medicines", "plan", "brief",
    "discharge", "summary", "note", "fetch", "download", "portal", "image", "picture", "page"}


def _is_action_word(token: str) -> bool:
    return token in _ACTION_TOKENS or token in _STOP


def _is_name_word(token: str, ward: list[dict]) -> bool:
    return any(words_match(token, nw) for w in ward for nw in name_words(w["name"]))


def lab_table(facts: list[dict], sources: list[dict], sex: str | None, max_dates: int = 8) -> dict:
    by_id = {s["id"]: s for s in sources}
    live = [f for f in (_unsure_time(f, by_id) for f in facts)
            if f["type"] == "lab_result" and f.get("status") in m.LIVE_STATUSES and f.get("clinical_time")
            and not f.get("time_doubtful") and _day(f)]
    dates = sorted({_day(f).isoformat() for f in live}, reverse=True)[:max_dates]
    cells: dict[str, dict[str, dict]] = {}
    for f in sorted(live, key=_time_key):
        day = _day(f).isoformat()
        if day in dates:
            cells.setdefault(f["key"], {})[day] = f
    rows = []
    for key, by_day in cells.items():
        sample = next(iter(by_day.values()))
        row_cells = []
        for day in dates:
            f = by_day.get(day)
            if not f:
                row_cells.append(None)
                continue
            direction, _ = outside(f, sex)
            row_cells.append({"value": _show(f), "dir": direction, "danger": is_danger(f, sex), **_ref(f)})
        group, place = report_group(key)
        rows.append({"key": key, "label": _label(sample), "unit": (sample.get("value") or {}).get("unit") or "",
                     "unit_std": "" if (sample.get("value") or {}).get("unit") else standard_unit(sample),
                     "range": shown_range(sample, sex), "cells": row_cells, "group": group, "_place": place})
    rows.sort(key=lambda r: (r["group"], r["_place"], r["label"].lower()))
    for r in rows:
        r.pop("_place")
    return {"dates": dates, "rows": rows, "groups": [g for g, _ in REPORT_ORDER] + ["Other tests"]}


def _patient_block(w: dict) -> dict:
    return {"id": w["id"], "bed": w["bed"], "name": w["name"], "label": w["label"], "bed_label": w["bed_label"]}


def _message(text: str, w: dict | None = None, title: str = "") -> dict:
    return {"kind": "message", "title": title, "text": text, "patient": _patient_block(w) if w else None}


def _ai_card(w: dict, text: str) -> dict:
    return {"kind": "ai", "title": "Answer", "patient": _patient_block(w), "question": str(text)}


def _find_value(page: dict, words: list[str]) -> dict | None:
    if not words:
        return None
    wanted = {w.lower() for w in words} | {" ".join(words[i:i + 2]).lower() for i in range(len(words) - 1)}
    keys = {fact_key("lab_result", w) for w in wanted} | wanted
    for row in page["labs"] + page["vitals"]:
        names = {row["key"].lower(), row["label"].lower()}
        if keys & names:
            return row
    for row in page["labs"] + page["vitals"]:
        if any(len(w) >= 3 and (w in row["key"].lower() or w in row["label"].lower()) for w in wanted):
            return row
    for row in page["medicines"]:
        if any(len(w) >= 4 and (w in row["label"].lower() or w in row["key"].lower()) for w in wanted):
            return row
    return None


_PANELS = [(0, re.compile(r"\b(?:cbc|complete blood count|blood count|hemogram|haemogram)\b", re.I)),
           (1, re.compile(r"\b(?:rft|kft|kidney|renal|electrolytes?)\b", re.I)),
           (2, re.compile(r"\b(?:lft|liver)\b", re.I)),
           (3, re.compile(r"\b(?:coagulation|clotting|pt/?inr)\b", re.I))]


def _panel_row(page: dict, text: str | None) -> dict | None:
    from .today import report_group
    for group, pattern in _PANELS:
        if pattern.search(text or ""):
            rows = [r for r in page["labs"] if report_group(r["key"])[0] == group and r.get("source_id")]
            return max(rows, key=lambda r: r.get("time") or "", default=None)
    return None


_POINTING = {"that", "this", "same", "one", "value", "uska", "iska", "usko", "isko"}


def _normal_range_card(key: str, w: dict | None) -> dict:
    fact = {"key": key, "type": "lab_result", "value": {}}
    rng = normal_range(fact)
    name = key.capitalize() if key != "tlc" else "TLC"
    if not rng:
        return _message(f"I don't hold a normal range for {name}.", w, "Normal range")
    lo, hi = rng
    unit = standard_unit(fact)
    if key in ("tlc", "platelets"):
        lo, hi, unit = lo / 1000, hi / 1000, "K/µL"
    show = lambda x: f"{x:g}"
    return _message(f"{name}: normal is {show(lo)} to {show(hi)} {unit}. This is the range WardOS marks values against; "
                    "a lab's own printed range is used first when the report has one.", w, "Normal range")


def _ward_card(ward: list[dict], test: str | None) -> dict:
    if not ward:
        return _message("No patients on your list yet.", title="Your ward")
    rows = sorted(ward, key=lambda w: (-len(w["page"].get("alerts") or []), _bed_sort(w["bed"])))
    if test:
        lines, name = [], test
        for w in sorted(ward, key=lambda x: _bed_sort(x["bed"])):
            row = next((r for r in w["page"].get("labs") or [] if r.get("key") == test), None)
            if row:
                name = row.get("label") or test
                note = f" ({row['dir']})" if row.get("dir") else ""
                lines.append(f"{w['label']}: {row.get('value')}{' ' + row['unit'] if row.get('unit') else ''}{note}, {row.get('when') or 'date not written'}")
            else:
                lines.append(f"{w['label']}: no {test} in the record")
        return _message("Newest value for each patient:\n" + "\n".join(lines), title=str(name))
    lines = []
    for w in rows:
        alerts = [a.get("title") or "" for a in w["page"].get("alerts") or []]
        words = f"{len(alerts)} alert{'' if len(alerts) == 1 else 's'}: " + ", ".join(a for a in alerts[:3] if a) if alerts else "no alerts"
        lines.append(f"{w['label']}: {words}" + (f" and {len(alerts) - 3} more" if len(alerts) > 3 else ""))
    return _message(f"{len(ward)} patient{'' if len(ward) == 1 else 's'}, most alerts first:\n" + "\n".join(lines), title="Your ward")


def _bed_sort(bed) -> tuple:
    digits = re.sub(r"\D", "", str(bed or ""))
    return (0, int(digits)) if digits else (1, 0)


def answer(intent: dict, ward: list[dict], record=None, last_rows: dict | None = None) -> dict:
    action, how = intent["action"], intent["how"]
    w = next((x for x in ward if x["id"] == intent["patient_id"]), None)
    if how == "ambiguous":
        retry = (f"set bed {intent['destination_bed']}" if action == "set_bed" else
                 " ".join(x for x in (ACTION_WORDS.get(action, ""), " ".join(intent["what_words"])) if x))
        return {"kind": "choose", "title": "Which patient?", "text": "More than one patient fits. Which one?",
                "choices": intent["ambiguous"], "retry_text": retry, "patient": None}
    if how == "missing_bed":
        return _message(f"I can't find Bed {intent['bed']} on your list.")
    if action == "stranger":
        return _message(intent["stranger"])
    if action == "no_order":
        return _message("I can't do that. I never order, send or change anything. I can get reports, watch a test, ask the hospital website a question, or answer from the record.")
    if action == "no_advice":
        return _message(NO_ADVICE)
    if action == "normal_range":
        return _normal_range_card(intent["test"], w)
    if action == "ward":
        return _ward_card(ward, intent.get("test"))
    if action == "anything" or (action == "unknown" and w):
        return _ai_card(w, intent["text"])
    if action == "unknown":
        return _message(HELP, w)
    if action == "get_reports":
        return _message(PORTAL_MESSAGE, w, "Get the lab reports")
    if action == "new_patient":
        fields = parse_new_patient(intent["text"], ward)
        if not any(fields.get(k) for k in ("name", "bed", "age", "sex", "uhid")):
            return _message("Tell me the new patient: bed, name, age, sex and the problem. For example: "
                            "new patient bed 16 Ramesh Kumar 45 male fever. Or tap Patients, then New patient.")
        return {"kind": "new_patient", "title": "New patient", "patient": None, "fields": fields,
                "units": _known_units(),
                "text": "Add this patient? Anything not said is read from the photos when you add them."}
    if w is None:
        return _message("Which patient? Say a bed number or a name, or tap Patients.")
    page, patient = w["page"], _patient_block(w)
    if page["admission"].get("read_only") and action in ("set_bed", "summary", "note", "get_reports"):
        return _message("Old stays are view-and-ask only.", w)
    if action == "set_bed":
        from .beds import occupants
        admissions = [dict(x["page"]["admission"], status="active") for x in ward]
        taken = occupants(dict(page["admission"], status="active"), intent["destination_bed"], admissions)
        labels = [x["label"] for x in ward if x["id"] in {a["id"] for a in taken}]
        return {"kind": "set_bed", "title": "Change bed", "patient": patient,
                "bed": intent["destination_bed"], "occupied": labels,
                "text": f"Move {w['label']} to Bed {intent['destination_bed']}?"}
    if action == "brief":
        return {"kind": "brief", "title": "Brief", "patient": patient, "lines": page["brief"]["lines"],
                "read_only": bool(page["admission"].get("read_only")),
                "alerts": [a["title"] for a in page["alerts"]], "questions": len(page["questions"]) + len(page["checks"])}
    if action == "summary":
        return {"kind": "confirm", "title": "Discharge summary", "patient": patient, "confirm": "summary",
                "text": f"Make the discharge summary for {w['bed_label']}? It takes about 4-6 minutes.",
                "yes": "Yes, make it", "no": "No"}
    if action == "note":
        if not intent.get("note_text"):
            return _message("Say the note after the word note, for example: note bed 5 plan CT tomorrow.", w)
        return {"kind": "note", "title": "Note", "patient": patient, "note_text": intent["note_text"],
                "text": f"Note saved for {w['bed_label']}."}
    if action == "vitals":
        if not page["vitals"]:
            return _message(f"No vitals written yet for {w['bed_label']}.", w, "Vitals")
        return {"kind": "vitals", "title": "Vitals", "patient": patient, "rows": page["vitals"]}
    if action == "medicines":
        rows = page["medicines"]
        if not rows:
            return _message(f"No medicines written yet for {w['bed_label']}.", w, "Medicines")
        live = [r for r in rows if r["state"] != "stopped"]
        groups = [{"name": g, "rows": [r for r in live if r["group"] == g]} for g in MED_GROUPS]
        return {"kind": "medicines", "title": "Medicines", "patient": patient,
                "groups": [g for g in groups if g["rows"]], "stopped": [r for r in rows if r["state"] == "stopped"]}
    if action == "plan":
        rows = sorted(page["plan"], key=lambda r: 0 if r.get("kind") == "pending" else 1)
        if not rows:
            return _message(f"No plan written yet for {w['bed_label']}.", w, "Plan")
        return {"kind": "plan", "title": "Plan", "patient": patient, "rows": rows}
    facts = record.facts(w["id"]) if record else []
    sources = record.store.query(m.SOURCES, admission_id=w["id"]) if record else []
    sex = _sex(record.patient_of(w["id"]), facts) if record else None
    if action == "photo":
        words = [t for t in intent["what_words"] if t not in _POINTING]
        row = _find_value(page, words) or _panel_row(page, intent.get("text"))
        if not row and not words and intent["what_words"]:
            row = (last_rows or {}).get(w["id"])
        if not row:
            if words:
                return _ai_card(w, intent["text"])
            return _message("Which value? For example: photo of the TLC.", w, "Photo")
        return {"kind": "photo", "title": f"Photo: {row['label']}", "patient": patient,
                "row": {k: row.get(k) for k in ("key", "label", "value", "unit", "dir", "fact_id", "source_id", "page_id",
                                                "quote", "quote_found", "when", "time", "by_doctor", "date_doubtful")}}
    if action == "reports":
        data = reports_in_order(facts, None, sex, sources)
        day = _asked_day(_norm(intent.get("text")), data["days"])
        if day and day != data["day"]:
            if day in data["days"]:
                data = reports_in_order(facts, day, sex, sources)
            else:
                data = dict(data, missing_day=day)
        if intent["group"] is not None:
            name = REPORT_ORDER[intent["group"]][0] if intent["group"] < len(REPORT_ORDER) else None
            data = dict(data, groups=[g for g in data["groups"] if g["name"] == name])
        if not data["groups"]:
            return _message(f"No lab values written yet for {w['bed_label']}.", w, "Reports")
        return {"kind": "reports", "title": "Reports", "patient": patient, **data}
    table = lab_table(facts, sources, sex)
    labs = page["labs"]
    single = _find_value({**page, "vitals": [], "medicines": []}, intent["what_words"]) if intent["what_words"] else None
    title = "Blood tests"
    if intent["group"] is not None:
        labs = [r for r in labs if report_group(r["key"])[0] == intent["group"]]
        title = _GROUP_TITLE[intent["group"]]
    elif single:
        labs, title = [r for r in labs if r["key"] == single["key"]], single["label"]
    if not labs:
        return _message(f"No {title.lower()} written yet for {w['bed_label']}.", w, title)
    ordered = sorted(labs, key=lambda r: (0 if r["dir"] else 1, report_group(r["key"])))
    filtered = intent["group"] is not None or bool(single)
    if filtered:
        table = dict(table, rows=[r for r in table["rows"] if r["key"] in {x["key"] for x in labs}])
    return {"kind": "tests", "title": title, "patient": patient, "filtered": filtered,
            "abnormal": [r for r in ordered if r["dir"]], "in_range": [r for r in ordered if not r["dir"]],
            "table": table}


def remember_row(card: dict, last_rows: dict) -> None:
    pid = (card.get("patient") or {}).get("id")
    row = None
    if card.get("kind") == "answer" and card.get("sources"):
        row = card["sources"][0]
    elif card.get("kind") == "tests" and card.get("filtered"):
        row = next(iter((card.get("abnormal") or []) + (card.get("in_range") or [])), None)
    elif card.get("kind") == "photo":
        row = card.get("row")
    if pid and row and row.get("source_id"):
        if len(last_rows) > 2000:
            last_rows.clear()
        last_rows[pid] = row


def ask(record, text: str, current_id: str | None = None, ward: list[dict] | None = None, last_rows: dict | None = None) -> dict:
    from .register import lookup_intent, lookup_requested
    intent = lookup_intent(record, text) if ward is None else None
    ward = ward if ward is not None else ([] if intent else build_ward(record))
    if current_id and not lookup_requested(text) and not any(w["id"] == current_id for w in ward):
        admission = record.store.get(m.ADMISSIONS, current_id)
        if admission and admission.get("old_case"):
            patient = record.patient_of(current_id)
            page = build_today(admission, patient, record.facts(current_id), record.questions(current_id),
                               sources=record.store.query(m.SOURCES, admission_id=current_id))
            ward = [*ward, ward_entry(admission, patient, page)]
    intent = intent or interpret(text, ward, current_id)
    card = ({"kind": "old_case", "title": "Old stays", "patient": None, "matches": [], "text": ""}
            if intent["action"] == "old_patient" else answer(intent, ward, record, last_rows))
    keep = ("action", "patient_id", "how", "bed", "group", "what_words", "ambiguous")
    return {"text": intent["text"], "intent": {k: intent[k] for k in keep}, "card": card}

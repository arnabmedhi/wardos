"""How close a summary is to the doctor's final document, on a fixed checklist."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..models import ModelClient, Usage
from .normalize import quote_found

PROMPTS = Path(__file__).parent / "prompts"
SECTIONS = ("diagnosis", "history", "examination", "investigations", "course_and_treatment", "discharge")
CHECKLIST_FILE = "checklist.json"
VERDICTS = {"c": "correct", "w": "wrong", "m": "missing"}
JUDGE_MAX_TOKENS = 32768


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _sha(*texts: str) -> str:
    return hashlib.sha256("\x00".join(texts).encode("utf-8")).hexdigest()[:16]


def numbered(statements: list[dict]) -> str:
    return "\n".join(f"{s['id']}. [{s['s']}] {s['t']}" for s in statements)


async def make_statements(model: ModelClient, truth: str, *, model_id: str) -> tuple[list[dict], Usage]:
    raw, usage = await model.generate(_prompt("statements.md").replace("<<TRUTH>>", truth), model=model_id, max_tokens=JUDGE_MAX_TOKENS, as_json=True, timeout=600)
    out = []
    for item in raw.get("statements") or []:
        text = " ".join(str((item or {}).get("t") or "").split())
        if text:
            section = str(item.get("s") or "").strip().lower()
            out.append({"id": len(out) + 1, "s": section if section in SECTIONS else "history", "t": text})
    return out, usage


async def locate(model: ModelClient, statements: list[dict], notes: str, *, model_id: str) -> tuple[dict, Usage]:
    prompt = _prompt("locate.md").replace("<<STATEMENTS>>", numbered(statements)).replace("<<NOTES>>", notes)
    raw, usage = await model.generate(prompt, model=model_id, max_tokens=JUDGE_MAX_TOKENS,
                                      as_json=True, timeout=600)
    found = {}
    for item in raw.get("found") or []:
        try:
            sid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        quote = str(item.get("q") or "").strip()
        if str(item.get("f") or "").lower() != "yes":
            found[sid] = ("no", "")
        else:
            found[sid] = ("yes" if quote_found(quote, notes) else "unsure", quote)
    return found, usage


async def build_checklist(model: ModelClient, truth: str, notes: str, *, model_id: str) -> tuple[dict, Usage]:
    statements, usage = await make_statements(model, truth, model_id=model_id)
    found, u2 = await locate(model, statements, notes, model_id=model_id)
    for s in statements:
        s["in_notes"], s["q"] = found.get(s["id"], ("unsure", ""))
    key = _sha(truth, notes, _prompt("statements.md"), _prompt("locate.md"), model_id)
    return {"key": key, "model": model_id, "statements": statements}, usage.add(u2)


async def load_checklist(case_dir: Path, model: ModelClient, *, model_id: str) -> tuple[dict, Usage | None]:
    truth = (case_dir / "doctor.txt").read_text(encoding="utf-8")
    notes = (case_dir / "stage1_OCR.txt").read_text(encoding="utf-8")
    path = case_dir / CHECKLIST_FILE
    key = _sha(truth, notes, _prompt("statements.md"), _prompt("locate.md"), model_id)
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("key") == key:
            return saved, None
    checklist, usage = await build_checklist(model, truth, notes, model_id=model_id)
    path.write_text(json.dumps(checklist, indent=1, ensure_ascii=False), encoding="utf-8")
    return checklist, usage


async def grade(model: ModelClient, checklist: dict, candidate: str, *, model_id: str) -> tuple[dict, Usage]:
    statements = checklist["statements"]
    verdicts: dict[int, dict] = {}
    extra: list[str] = []
    usage = Usage(model_id, cost_inr=0.0)
    todo = statements
    for _ in range(2):
        prompt = (_prompt("grade.md").replace("<<STATEMENTS>>", numbered(todo))
                  .replace("<<CANDIDATE>>", candidate or "(empty)"))
        raw, u = await model.generate(prompt, model=model_id, max_tokens=JUDGE_MAX_TOKENS,
                                      as_json=True, timeout=600)
        usage = usage.add(u)
        wanted = {s["id"] for s in todo}
        for item in raw.get("verdicts") or []:
            try:
                sid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            v = str(item.get("v") or "").lower()[:1]
            if sid in wanted and v in VERDICTS:
                verdicts[sid] = {"v": VERDICTS[v], "n": str(item.get("n") or "") if v == "w" else ""}
        if not extra:
            extra = [str(x) for x in (raw.get("extra") or []) if str(x).strip()][:25]
        todo = [s for s in statements if s["id"] not in verdicts]
        if not todo:
            break
    return {"verdicts": verdicts, "extra": extra}, usage


async def grade_cached(path: Path, model: ModelClient, checklist: dict, candidate: str, *,
                       model_id: str) -> tuple[dict, Usage | None]:
    key = _sha(checklist["key"], candidate, _prompt("grade.md"), model_id)
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("key") == key:
            return {"verdicts": {int(k): v for k, v in saved["verdicts"].items()}, "extra": saved["extra"]}, None
    graded, usage = await grade(model, checklist, candidate, model_id=model_id)
    path.write_text(json.dumps({"key": key, **graded}, indent=1, ensure_ascii=False), encoding="utf-8")
    return graded, usage


def tally(checklist: dict, graded: dict) -> dict:
    keys = ("truth", "correct", "wrong", "missing", "missing_in_notes", "missing_not_in_notes", "missing_unsure",
            "ungraded", "correct_not_located")
    out = {name: dict.fromkeys(keys, 0) for name in SECTIONS}
    for s in checklist["statements"]:
        row = out[s["s"]]
        row["truth"] += 1
        verdict = graded["verdicts"].get(s["id"], {}).get("v")
        if verdict is None:
            row["ungraded"] += 1
            continue
        row[verdict] += 1
        if verdict == "correct" and s["in_notes"] == "no":
            row["correct_not_located"] += 1
        if verdict == "missing":
            row[{"yes": "missing_in_notes", "no": "missing_not_in_notes"}.get(s["in_notes"], "missing_unsure")] += 1
    total = {k: sum(row[k] for row in out.values()) for k in keys}
    total["extra"] = len(graded["extra"])
    total["in_notes"] = sum(1 for s in checklist["statements"] if s["in_notes"] == "yes")
    return {"sections": out, "total": total}

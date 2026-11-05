"""The summary scorer: one fixed checklist per case, quotes and counts checked by code."""
from __future__ import annotations

import asyncio
import json

from wardos.chain import score
from wardos.models import Usage

NOTES = "C/O swelling left inguinal region x 3 months. No DM / HTN. Hb 13.3 (14/10)"
TRUTH = "Swelling in left inguinal region since 3 months. No DM, HTN. Married with 7 children. Hb 13.3"


class FakeJudge:

    def __init__(self, verdicts=None, skip_first=()):
        self.calls = []
        self.verdicts = verdicts or {}
        self.skip_first = set(skip_first)

    async def generate(self, prompt, **kwargs):
        u = Usage("fake", cost_inr=1.0)
        if "into a numbered checklist" in prompt:
            self.calls.append("statements")
            return {"statements": [{"s": "history", "t": "Swelling left inguinal region 3 months"},
                                   {"s": "history", "t": "No diabetes mellitus"},
                                   {"s": "history", "t": "Married with 7 children"},
                                   {"s": "investigations", "t": "Hb 13.3 on 14/10"},
                                   {"s": "nonsense", "t": "  "}]}, u
        if "say whether the notes contain it" in prompt:
            self.calls.append("locate")
            return {"found": [{"id": 1, "f": "yes", "q": "swelling left inguinal region x 3 months"},
                              {"id": 2, "f": "yes", "q": "No diabetes at all"},
                              {"id": 3, "f": "no", "q": ""},
                              {"id": 4, "f": "yes", "q": "Hb 13.3 (14/10)"}]}, u
        self.calls.append("grade")
        ids = [int(line.split(".")[0]) for line in prompt.split("=== CHECKLIST ===")[1]
               .split("=== SUMMARY")[0].strip().splitlines()]
        out = [{"id": i, "v": self.verdicts.get(i, "c"), "n": "other value"} for i in ids
               if not (self.calls.count("grade") == 1 and i in self.skip_first)]
        return {"verdicts": out, "extra": ["Hernia reducible"]}, u


def _case(tmp_path):
    (tmp_path / "doctor.txt").write_text(TRUTH, encoding="utf-8")
    (tmp_path / "stage1_OCR.txt").write_text(NOTES, encoding="utf-8")
    return tmp_path


def test_checklist_made_once_and_quotes_checked(tmp_path):
    case, judge = _case(tmp_path), FakeJudge()
    checklist, usage = asyncio.run(score.load_checklist(case, judge, model_id="fake"))
    assert usage is not None and [s["id"] for s in checklist["statements"]] == [1, 2, 3, 4]
    assert [s["in_notes"] for s in checklist["statements"]] == ["yes", "unsure", "no", "yes"]
    again, usage2 = asyncio.run(score.load_checklist(case, judge, model_id="fake"))
    assert usage2 is None and again == checklist and judge.calls == ["statements", "locate"]
    (case / "doctor.txt").write_text(TRUTH + " Reformed smoker", encoding="utf-8")
    asyncio.run(score.load_checklist(case, judge, model_id="fake"))
    assert judge.calls.count("statements") == 2


def test_tally_splits_missing_by_photos(tmp_path):
    case = _case(tmp_path)
    checklist, _ = asyncio.run(score.load_checklist(case, FakeJudge(), model_id="fake"))
    judge = FakeJudge(verdicts={2: "m", 3: "m", 4: "w"})
    graded, _ = asyncio.run(score.grade(judge, checklist, "summary", model_id="fake"))
    t = score.tally(checklist, graded)["total"]
    assert (t["truth"], t["correct"], t["wrong"], t["missing"]) == (4, 1, 1, 2)
    assert (t["missing_in_notes"], t["missing_not_in_notes"], t["missing_unsure"], t["extra"]) == (0, 1, 1, 1)
    assert graded["verdicts"][4]["n"] == "other value" and graded["verdicts"][1]["n"] == ""


def test_skipped_statements_asked_again(tmp_path):
    case = _case(tmp_path)
    checklist, _ = asyncio.run(score.load_checklist(case, FakeJudge(), model_id="fake"))
    judge = FakeJudge(skip_first={3, 4})
    graded, _ = asyncio.run(score.grade(judge, checklist, "summary", model_id="fake"))
    assert judge.calls == ["grade", "grade"] and sorted(graded["verdicts"]) == [1, 2, 3, 4]


def test_frozen_grade_reused(tmp_path):
    case = _case(tmp_path)
    checklist, _ = asyncio.run(score.load_checklist(case, FakeJudge(), model_id="fake"))
    judge, path = FakeJudge(), tmp_path / "frozen_grade.json"
    first, u1 = asyncio.run(score.grade_cached(path, judge, checklist, "draft", model_id="fake"))
    second, u2 = asyncio.run(score.grade_cached(path, judge, checklist, "draft", model_id="fake"))
    assert u1 is not None and u2 is None and first == second and judge.calls == ["grade"]
    assert json.loads(path.read_text(encoding="utf-8"))["key"]

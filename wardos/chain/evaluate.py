"""Write the summary from a replayed record and score it against the doctor's final document."""
from __future__ import annotations

import argparse
import asyncio
import json

from ..config import load_settings
from ..models import get_model_client
from . import model as m
from .replay import open_run
from .render import summary_text, update_summary
from .score import SECTIONS, grade, grade_cached, load_checklist, tally
from .settings import load_chain_settings


def _cost(*usages) -> float:
    return sum(float(u.cost_inr or 0) for u in usages if u is not None)


async def evaluate(replay_name: str, *, judge_frozen: bool = True, reuse_summary: bool = False) -> dict:
    settings = load_chain_settings()
    run_dir = settings.data_dir / "replays" / replay_name
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    case_dir = settings.data_dir / "testsets" / report["case"]
    model = get_model_client(load_settings())
    try:
        summary_file = run_dir / "summary.txt"
        if reuse_summary and summary_file.exists():
            written, ours = None, summary_file.read_text(encoding="utf-8")
        else:
            record = open_run(run_dir, json.loads((run_dir / "report.json").read_text(encoding="utf-8")).get("backend"))
            (admission,) = record.store.query(m.ADMISSIONS)
            written = await update_summary(record, model, admission["id"], model_id=settings.model)
            ours = summary_text(record, admission["id"])
            summary_file.write_text(ours, encoding="utf-8")
        checklist, u_check = await load_checklist(case_dir, model, model_id=settings.model)
        ours_graded, u_ours = await grade(model, checklist, ours, model_id=settings.model)
        result = {"summary_write": written, "checklist_new": u_check is not None,
                  "ours": tally(checklist, ours_graded), "ours_graded": ours_graded}
        u_frozen = None
        if judge_frozen:
            frozen = (case_dir / "frozen.txt").read_text(encoding="utf-8")
            frozen_graded, u_frozen = await grade_cached(case_dir / "frozen_grade.json", model, checklist, frozen,
                                                         model_id=settings.model)
            result.update(frozen=tally(checklist, frozen_graded), frozen_graded=frozen_graded)
        result["judge_cost_inr"] = round(_cost(u_check, u_ours, u_frozen), 2)
    finally:
        await model.aclose()
    (run_dir / "evaluation.json").write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str),
                                             encoding="utf-8")
    (run_dir / "scorecard.txt").write_text(scorecard(checklist, result), encoding="utf-8")
    return result


def scorecard(checklist: dict, result: dict) -> str:
    ours = result["ours_graded"]["verdicts"]
    frozen = (result.get("frozen_graded") or {}).get("verdicts", {})
    lines = ["id | section | in photos | WardOS | summary service | statement"]
    for s in checklist["statements"]:
        o, f = ours.get(s["id"], {}), frozen.get(s["id"], {})
        note = "; ".join(x for x in (f"ours: {o['n']}" if o.get("n") else "",
                                      f"DD: {f['n']}" if f.get("n") else "") if x)
        lines.append(f"{s['id']} | {s['s']} | {s['in_notes']} | {o.get('v', '-')} | {f.get('v', '-')} | {s['t']}"
                     + (f"   <{note}>" if note else ""))
    lines += ["", "EXTRA in WardOS:"] + [f"- {x}" for x in result["ours_graded"]["extra"]]
    if result.get("frozen_graded"):
        lines += ["", "EXTRA in summary service:"] + [f"- {x}" for x in result["frozen_graded"]["extra"]]
    return "\n".join(lines) + "\n"


def _line(name: str, t: dict) -> str:
    return (f"{name:14} of {t['truth']:3}: correct {t['correct']:3} | wrong {t['wrong']:3} | missing {t['missing']:3}"
            f" (in photos {t['missing_in_notes']}, not in photos {t['missing_not_in_notes']}, unsure "
            f"{t['missing_unsure']}) | extra {t['extra']:3}" + (f" | ungraded {t['ungraded']}" if t["ungraded"] else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m wardos.chain.evaluate")
    parser.add_argument("--replay", required=True, help="folder name under private/chain/replays/")
    parser.add_argument("--no-frozen", action="store_true")
    parser.add_argument("--reuse-summary", action="store_true", help="score the saved summary.txt, do not rewrite")
    args = parser.parse_args(argv)
    result = asyncio.run(evaluate(args.replay, judge_frozen=not args.no_frozen, reuse_summary=args.reuse_summary))
    w = result["summary_write"]
    if w:
        print(f"summary: {w['written']} fields written, Rs {w['cost_inr']:.2f}, {w['seconds']:.0f} s")
    t = result["ours"]["total"]
    print(f"checklist: {t['truth']} statements, {t['in_notes']} found in the photos"
          f"{' (new)' if result['checklist_new'] else ' (saved)'}; judging Rs {result['judge_cost_inr']:.2f}")
    print(_line("WardOS", t))
    if "frozen" in result:
        print(_line("summary service", result["frozen"]["total"]))
    for section in SECTIONS:
        o = result["ours"]["sections"][section]
        f = result.get("frozen", {}).get("sections", {}).get(section)
        print(f"  {section:22} of {o['truth']:3}  ours c{o['correct']:3} w{o['wrong']:3} m{o['missing']:3}"
              + (f"   | DD c{f['correct']:3} w{f['wrong']:3} m{f['missing']:3}" if f else ""))


if __name__ == "__main__":
    main()

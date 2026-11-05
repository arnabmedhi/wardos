"""Replay a past case into a fresh record, batch by batch, and print counts only."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import dataclasses
from datetime import datetime
import json
from pathlib import Path

from ..config import load_settings
from ..models import get_model_client
from . import model as m
from .record import Record
from .settings import load_chain_settings, open_record
from .testsets import image_blocks
from .worker import process_batch


def plan_batches(meta: dict, mode: str) -> list[list[int]]:
    batches = [list(b["photos"]) for b in meta["batches"]]
    if mode == "all":
        return [[n for b in batches for n in b]]
    if mode == "late" and len(batches) >= 2 and batches[1]:
        late = batches[1].pop(0)
        batches = [b for b in batches if b] + [[late]]
    if mode == "dup" and batches and batches[0]:
        batches[-1] = batches[-1] + [batches[0][0]]
    return [b for b in batches if b]


def summarise(record: Record, admission_id: str) -> dict:
    facts = record.facts(admission_id)
    meds = record.medicines(admission_id)
    return {
        "facts": len(facts),
        "by_type": dict(Counter(f["type"] for f in facts).most_common()),
        "quote_found": round(sum(f["quote_found"] for f in facts) / len(facts), 3) if facts else None,
        "dated": round(sum(1 for f in facts if f.get("clinical_time")) / len(facts), 3) if facts else None,
        "disputed": sum(1 for f in facts if f["status"] == m.DISPUTED),
        "multi_source": sum(1 for f in facts if len(f.get("support") or []) > 1),
        "open_questions": dict(Counter(q["kind"] for q in record.questions(admission_id))),
        "medicines": {"on": sum(1 for v in meds.values() if v["state"] == "on"),
                      "off": sum(1 for v in meds.values() if v["state"] == "off"),
                      "unknown": sum(1 for v in meds.values() if v["state"] == "unknown"),
                      "discharge_list": sum(1 for v in meds.values() if v["discharge"])},
        "version": record.admission(admission_id)["version"],
    }


def open_run(run_dir: Path, backend: dict | None = None):
    backend = backend or {"store": "sqlite", "files": "local"}
    settings = dataclasses.replace(load_chain_settings(), store=backend["store"], files=backend["files"],
                                   prefix=backend.get("prefix") or "wardos_")
    return open_record(settings, local_dir=run_dir)


async def replay(case: str, mode: str, saved_ocr: bool, gap_minutes: float, cloud: bool = False) -> dict:
    settings = load_chain_settings()
    case_dir = settings.data_dir / "testsets" / case
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    blocks = image_blocks((case_dir / "stage1_OCR.txt").read_text(encoding="utf-8")) if saved_ocr else {}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = settings.data_dir / "replays" / f"{stamp}-{case}-{mode}{'-saved' if saved_ocr else ''}"
    backend = ({"store": "firestore", "files": "drive", "prefix": f"wardosreplay_{stamp.replace('-', '')}_"}
               if cloud else {"store": "sqlite", "files": "local", "prefix": settings.prefix})
    run_dir.mkdir(parents=True, exist_ok=True)
    record = open_run(run_dir, backend)
    patient = record.create_patient(name=None)
    admission = record.create_admission(patient["id"], unit=f"{meta['department']} {meta['unit']}",
                                        reference_date=meta["run_date"])
    model = get_model_client(load_settings())
    reports = []
    try:
        for i, batch in enumerate(plan_batches(meta, mode)):
            if i and gap_minutes:
                await asyncio.sleep(gap_minutes * 60)
            ids, dups = [], 0
            for n in batch:
                data = (case_dir / "photos" / f"{n:02d}.jpg").read_bytes()
                src, dup = record.add_source(admission["id"], "photo", data=data, mime="image/jpeg",
                                             filename=f"{n:02d}.jpg", author="replay")
                ids.append(src["id"])
                dups += dup
            saved = {sid: blocks[n] for sid, n in zip(ids, batch) if n in blocks} if saved_ocr else None
            rep = await process_batch(record, model, settings, admission["id"], ids, saved_text=saved)
            rep.duplicates = dups
            reports.append(dataclasses.asdict(rep) | {"photos": len(batch)})
            print(f"batch {i + 1}: photos {len(batch)} (dup {dups}) | read {rep.pages_read} | proposed "
                  f"{rep.facts_proposed}, quotes found {rep.quotes_found} | added {rep.added}, same-again "
                  f"{rep.corroborated}, disputed {rep.disputed}, questions {rep.questions}, held {rep.held_pages} | "
                  f"Rs {rep.read_cost_inr + rep.extract_cost_inr:.2f} | {rep.seconds:.0f} s"
                  + (f" | ERRORS {len(rep.errors)}" if rep.errors else ""))
            for err in rep.errors:
                print("   error:", err)
    finally:
        await model.aclose()
    result = {"case": case, "mode": mode, "saved_ocr": saved_ocr, "backend": backend, "unit": meta["unit"],
              "department": meta["department"], "batches": reports, "summary": summarise(record, admission["id"]),
              "cost_inr": round(sum(r["read_cost_inr"] + r["extract_cost_inr"] for r in reports), 2),
              "seconds": round(sum(r["seconds"] for r in reports), 1), "record_dir": str(run_dir)}
    (run_dir / "report.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m wardos.chain.replay")
    parser.add_argument("--case", required=True)
    parser.add_argument("--mode", choices=["batches", "all", "late", "dup"], default="batches")
    parser.add_argument("--read-photos", action="store_true",
                        help="pay to read the photos again (costs model calls); default: the summary service's saved reading")
    parser.add_argument("--gap-minutes", type=float, default=0)
    parser.add_argument("--cloud", action="store_true", help="record in Firestore, files in Drive (own prefix per run)")
    args = parser.parse_args(argv)
    result = asyncio.run(replay(args.case, args.mode, not args.read_photos, args.gap_minutes, args.cloud))
    s = result["summary"]
    print(f"TOTAL: facts {s['facts']} | quotes found {s['quote_found']:.0%} | dated {s['dated']:.0%} | disputed "
          f"{s['disputed']} | seen on 2+ pages {s['multi_source']} | questions {s['open_questions']} | medicines "
          f"{s['medicines']} | Rs {result['cost_inr']} | {result['seconds']} s")
    print("types:", s["by_type"])
    print("report:", Path(result["record_dir"]).name)


if __name__ == "__main__":
    main()

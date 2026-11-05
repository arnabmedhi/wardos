"""The final discharge summary built from the record."""
from __future__ import annotations

from . import model as m
from .record import Record


def ordered_pages(record: Record, admission_id: str) -> list[dict]:
    sources = {s["id"]: s for s in record.store.query(m.SOURCES, admission_id=admission_id)}
    pages = [p for p in record.pages(admission_id) if not p.get("held") and p.get("text")]
    pages.sort(key=lambda p: (sources[p["source_id"]].get("received_at") or "", p["source_id"], p["number"]))
    keyed, last = [], ""
    for upload_order, page in enumerate(pages):
        day = (page.get("page_date") or "")[:10] or last
        last = day or last
        keyed.append((day or "0000-00-00", upload_order, page))
    return [page for _, _, page in sorted(keyed, key=lambda k: (k[0], k[1]))]


def doctor_notes(record: Record, admission_id: str) -> list[str]:
    lines = []
    for f in record.facts(admission_id):
        if f["status"] == m.DOCTOR_LOCKED:
            lines.append(f"{f.get('label') or f['key']}: {(f.get('value') or {}).get('text')}"
                         + (f" ({f['clinical_time'][:10]})" if f.get("clinical_time") else ""))
    for q in record.questions(admission_id, status="answered"):
        lines.append(f"{q.get('text')} -> {q.get('answer')}")
    return lines


def final_reading(record: Record, admission_id: str) -> str:
    parts = [f"**Image {i}:**\n\n{page['text'].strip()}" for i, page in enumerate(ordered_pages(record, admission_id), 1)]
    notes = doctor_notes(record, admission_id)
    if notes:
        parts.append("**DOCTOR'S CORRECTIONS (entered by the treating doctor in WardOS; where they differ from the "
                     "pages above, these are correct):**\n\n" + "\n".join(f"- {line}" for line in notes))
    return "\n\n---\n\n".join(parts)


def export(record: Record, admission_id: str, out_dir) -> dict:
    import json
    from pathlib import Path
    from .units import UNIT_FILES
    out = Path(out_dir)
    (out / "photos").mkdir(parents=True, exist_ok=True)
    photos, seen = [], set()
    for page in ordered_pages(record, admission_id):
        source = record.source(page["source_id"])
        if source["id"] in seen or source["kind"] != "photo":
            continue
        seen.add(source["id"])
        name = f"photos/{len(photos) + 1:02d}.jpg"
        (out / name).write_bytes(record.read_file(source))
        photos.append(name)
    (out / "reading.txt").write_text(final_reading(record, admission_id), encoding="utf-8")
    unit = record.admission(admission_id)["unit"]
    meta = {"unit": unit, "module": UNIT_FILES[unit], "photos": photos}
    (out / "input.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def main(argv=None):
    import argparse
    import json
    from .replay import open_run
    from .settings import load_chain_settings
    parser = argparse.ArgumentParser(prog="python -m wardos.chain.final")
    parser.add_argument("--replay", required=True)
    args = parser.parse_args(argv)
    run_dir = load_chain_settings().data_dir / "replays" / args.replay
    record = open_run(run_dir, json.loads((run_dir / "report.json").read_text(encoding="utf-8")).get("backend"))
    (admission,) = record.store.query(m.ADMISSIONS)
    meta = export(record, admission["id"], run_dir / "final_input")
    print(f"final input written: {run_dir / 'final_input'} ({len(meta['photos'])} photos, unit {meta['unit']})")


if __name__ == "__main__":
    main()

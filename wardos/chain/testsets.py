"""Build test cases from past discharge-summary runs."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import difflib
import hashlib
import io
import json
from pathlib import Path
import re

from ..config import REPO_DIR
from .normalize import parse_clinical_time
from .settings import load_chain_settings

USAGE_SHEET_ID = ""
SCOPES = ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive",
          "https://www.googleapis.com/auth/spreadsheets"]
IMAGE_BLOCK = re.compile(r"^\*\*Image\s+(\d+):\*\*", re.M)
PAGE_NAME = re.compile(r"^Page_(\d+)_", re.I)


def _services(token: Path):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    return (build("sheets", "v4", credentials=creds, cache_discovery=False),
            build("drive", "v3", credentials=creds, cache_discovery=False))


def _runs(sheets) -> list[dict]:
    rows = sheets.spreadsheets().values().get(spreadsheetId=USAGE_SHEET_ID, range="Runs!A1:AZ").execute()["values"]
    head = rows[0]
    return [{h: (r[i].strip() if i < len(r) and r[i] else "") for i, h in enumerate(head)} for r in rows[1:]]


def _children(drive, folder_id: str) -> list[dict]:
    out, token = [], None
    while True:
        res = drive.files().list(q=f"'{folder_id}' in parents and trashed=false", pageToken=token, pageSize=200,
                                 fields="nextPageToken, files(id,name,mimeType,size)").execute()
        out += res.get("files", [])
        token = res.get("nextPageToken")
        if not token:
            return out


def _walk(drive, folder_id: str, depth: int = 0) -> list[dict]:
    files = []
    for f in _children(drive, folder_id):
        if f["mimeType"] == "application/vnd.google-apps.folder":
            if depth < 2:
                files += _walk(drive, f["id"], depth + 1)
        else:
            files.append(f)
    return files


def _download(drive, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, drive.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def _doc_text(drive, link: str) -> str:
    m = re.search(r"/d/([A-Za-z0-9_-]+)", link)
    if not m:
        raise ValueError("not a document link")
    return drive.files().export(fileId=m.group(1), mimeType="text/plain").execute().decode("utf-8", "replace")


def image_blocks(ocr: str) -> dict[int, str]:
    marks = list(IMAGE_BLOCK.finditer(ocr))
    return {int(mk.group(1)): ocr[mk.end(): (marks[i + 1].start() if i + 1 < len(marks) else len(ocr))].strip()
            for i, mk in enumerate(marks)}


def page_date(text: str, around: date) -> str | None:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for line in lines[:6]:
        when, _ = parse_clinical_time(line, around=around)
        if when:
            return when[:10]
    seen = Counter(w[:10] for w, _ in (parse_clinical_time(ln, around=around) for ln in lines) if w)
    return seen.most_common(1)[0][0] if seen else None


def make_batches(blocks: dict[int, str], around: date, max_batches: int = 4) -> list[dict]:
    numbers = sorted(blocks)
    dates: dict[int, str | None] = {}
    last = None
    for n in numbers:
        d = page_date(blocks[n], around) or last
        dates[n] = d
        last = d or last
    first_known = next((d for d in dates.values() if d), None)
    for n in numbers:
        if dates[n] is None:
            dates[n] = first_known
    days = sorted({d for d in dates.values() if d}) or [None]
    groups: list[list[str | None]] = [[] for _ in range(min(max_batches, len(days)))]
    for i, day in enumerate(days):
        groups[min(i * len(groups) // len(days), len(groups) - 1)].append(day)
    return [{"days": g, "photos": [n for n in numbers if dates[n] in g]} for g in groups if g]


def _key(run_id: str) -> str:
    return "case_" + hashlib.sha256(run_id.encode()).hexdigest()[:10]


def build(units: list[str], per_unit: int, min_photos: int, token: Path, out_dir: Path) -> list[dict]:
    sheets, drive = _services(token)
    runs = [r for r in _runs(sheets) if r.get("Status") == "Success" and r.get("AI draft (frozen) link")
            and r.get("Doctor's document link") and r.get("Case folder")]
    runs.sort(key=lambda r: (r.get("Run date", ""), r.get("Run time", "")), reverse=True)
    made = []
    for unit in units:
        department, unit_name = unit.split("|")
        count = 0
        for run in runs:
            if count >= per_unit:
                break
            if run.get("Department") != department or run.get("Unit") != unit_name:
                continue
            key = _key(run["Run ID"])
            folder = out_dir / key
            if (folder / "meta.json").is_file():
                made.append(json.loads((folder / "meta.json").read_text(encoding="utf-8")))
                count += 1
                continue
            m = re.search(r"folders/([A-Za-z0-9_-]+)", run["Case folder"])
            if not m:
                continue
            files = _walk(drive, m.group(1))
            photos = sorted(((int(PAGE_NAME.match(f["name"]).group(1)), f) for f in files
                             if PAGE_NAME.match(f["name"]) and f["name"].lower().endswith((".jpg", ".jpeg"))),
                            key=lambda x: x[0])
            ocr_file = next((f for f in files if "stage1_OCR" in f["name"]), None)
            if len(photos) < min_photos or not ocr_file:
                continue
            ocr = _download(drive, ocr_file["id"]).decode("utf-8", "replace")
            blocks = image_blocks(ocr)
            if sorted(blocks) != [n for n, _ in photos]:
                continue
            try:
                frozen, doctor = _doc_text(drive, run["AI draft (frozen) link"]), _doc_text(drive, run["Doctor's document link"])
            except Exception:
                continue
            similarity = difflib.SequenceMatcher(None, frozen, doctor, autojunk=False).quick_ratio()
            if similarity > 0.995:
                continue
            (folder / "photos").mkdir(parents=True, exist_ok=True)
            for n, f in photos:
                (folder / "photos" / f"{n:02d}.jpg").write_bytes(_download(drive, f["id"]))
            (folder / "stage1_OCR.txt").write_text(ocr, encoding="utf-8")
            (folder / "frozen.txt").write_text(frozen, encoding="utf-8")
            (folder / "doctor.txt").write_text(doctor, encoding="utf-8")
            run_day = date.fromisoformat(run["Run date"][:10])
            meta = {"key": key, "run_id": run["Run ID"], "department": department, "unit": unit_name,
                    "run_date": run["Run date"][:10], "photos": len(photos),
                    "doctor_vs_frozen_similarity": round(similarity, 3),
                    "summary_cost_inr": run.get("AI cost (₹)"),
                    "batches": make_batches(blocks, run_day)}
            (folder / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
            made.append(meta)
            count += 1
    return made


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m wardos.chain.testsets")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--units", nargs="+", default=["Surgery|Unit 2", "Medicine|Unit 1"])
    b.add_argument("--per-unit", type=int, default=2)
    b.add_argument("--min-photos", type=int, default=12)
    b.add_argument("--token", default=str(REPO_DIR / "token.json"))
    args = parser.parse_args(argv)
    out_dir = load_chain_settings().data_dir / "testsets"
    made = build(args.units, args.per_unit, args.min_photos, Path(args.token), out_dir)
    print(f"{'case':16} {'unit':20} {'photos':>6} {'batches':>7} {'edited':>7}")
    for meta in made:
        print(f"{meta['key']:16} {meta['department'] + ' ' + meta['unit']:20} {meta['photos']:>6} "
              f"{len(meta['batches']):>7} {1 - meta['doctor_vs_frozen_similarity']:>7.1%}")


if __name__ == "__main__":
    main()

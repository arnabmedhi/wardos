"""The Make final summary button: export the stay to Drive and start the final-summary job."""
from __future__ import annotations

import json
import mimetypes
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from . import final as final_export
from . import model as m
from .record import Record

FOLDER = "application/vnd.google-apps.folder"
RESULT_FIELDS = ("doc_id", "doc_url", "cost", "warnings", "error")
JOB_GIVE_UP_MINUTES = 25


class NoPhotos(ValueError):
    """The stay has no read photo to build a summary from."""


def _drive(drive_creds):
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=drive_creds, cache_discovery=False)


def _make_folder(drive, name: str, parent: str) -> str:
    return drive.files().create(body={"name": name, "parents": [parent], "mimeType": FOLDER},
                                fields="id").execute()["id"]


def _find_folder(drive, name: str, parent: str) -> str | None:
    found = drive.files().list(q=f"'{parent}' in parents and name = '{name}' and trashed = false "
                                 f"and mimeType = '{FOLDER}'", fields="files(id)", pageSize=2).execute().get("files", [])
    return found[0]["id"] if found else None


UPLOAD_THREADS = 8


def upload_input(drive, input_dir: Path, run_id: str, tests_folder_id: str, *, new_drive=None) -> str:
    """Put the stay's reading and photos in Drive. Photos go up side by side (one Drive client per worker thread):
    sent one after another, 17 photos kept the Make button waiting ~97 s on 2 Nov 2026."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from googleapiclient.http import MediaFileUpload
    inputs = _find_folder(drive, "inputs", tests_folder_id) or _make_folder(drive, "inputs", tests_folder_id)
    run_folder = _make_folder(drive, run_id, inputs)
    photos_folder = _make_folder(drive, "photos", run_folder)
    files = [input_dir / "reading.txt", input_dir / "input.json", *sorted((input_dir / "photos").glob("*.jpg"))]
    local = threading.local()

    def send(f: Path) -> None:
        client = drive
        if new_drive is not None:
            if not hasattr(local, "drive"):
                local.drive = new_drive()
            client = local.drive
        parent = photos_folder if f.parent.name == "photos" else run_folder
        mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        client.files().create(body={"name": f.name, "parents": [parent]},
                              media_body=MediaFileUpload(str(f), mimetype=mime), fields="id").execute()
    with ThreadPoolExecutor(max_workers=UPLOAD_THREADS if new_drive is not None else 1) as pool:
        for _ in pool.map(send, files):
            pass
    return run_folder


def start_final(record: Record, admission_id: str, *, drive_creds, tests_folder_id: str, records_folder_id: str,
                job_runner: Callable[[dict], str]) -> dict:
    run_id = m.new_id("final").replace("_", "-")
    work = Path(tempfile.gettempdir()) / "wf" / run_id[-8:]
    try:
        meta = final_export.export(record, admission_id, work)
        if not meta["photos"]:
            raise NoPhotos("No photo of this patient has been read yet, so there is nothing to write the summary from. "
                           "Wait for the reading to finish, or take the photos again.")
        folder_id = upload_input(_drive(drive_creds), work, run_id, tests_folder_id,
                                 new_drive=lambda: _drive(drive_creds))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    execution = job_runner({"WARDOS_FINAL_INPUT_FOLDER_ID": folder_id,
                            "WARDOS_FINAL_RECORDS_FOLDER_ID": records_folder_id,
                            "WARDOS_FINAL_RUN_ID": run_id})
    doc = {"id": run_id, "admission_id": admission_id, "folder_id": folder_id, "execution": execution,
           "status": "running", "started_at": m.now_iso(),
           "record_version": len(record.store.query(m.FACTS, admission_id=admission_id))}
    record.store.put(m.FINALS, doc)
    return doc


def _read_result(drive, folder_id: str) -> dict | None:
    found = drive.files().list(q=f"'{folder_id}' in parents and name = 'result.json' and trashed = false",
                               fields="files(id)", pageSize=1).execute().get("files", [])
    if not found:
        return None
    return json.loads(drive.files().get_media(fileId=found[0]["id"]).execute())


def _minutes_since(iso: str | None) -> float:
    from datetime import datetime, timezone
    if not iso:
        return 0.0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 60


def final_status(record: Record, final_id: str, *, drive_creds) -> dict | None:
    doc = record.store.get(m.FINALS, final_id)
    if not doc or doc["status"] != "running":
        return doc
    result = _read_result(_drive(drive_creds), doc["folder_id"])
    if result is None:
        if _minutes_since(doc.get("started_at")) > JOB_GIVE_UP_MINUTES:
            doc = {**doc, "status": "error", "finished_at": m.now_iso(),
                   "error": f"the job did not finish (no result after {JOB_GIVE_UP_MINUTES} min)"}
            record.store.put(m.FINALS, doc)
        return doc
    doc = {**doc, "status": "done" if result.get("status") == "done" else "error",
           **{k: result.get(k) for k in RESULT_FIELDS}, "finished_at": m.now_iso()}
    record.store.put(m.FINALS, doc)
    return doc


def latest_final(record: Record, admission_id: str) -> dict | None:
    docs = record.store.query(m.FINALS, admission_id=admission_id)
    return max(docs, key=lambda d: d.get("started_at") or "") if docs else None


def cloud_job_runner(project: str, region: str, job: str) -> Callable[[dict], str]:
    session = {}

    def run(env: dict) -> str:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        if "s" not in session:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            session["s"] = AuthorizedSession(credentials)
        body = {"overrides": {"containerOverrides": [{"env": [{"name": k, "value": v} for k, v in env.items()]}]}}
        response = session["s"].post(f"https://run.googleapis.com/v2/projects/{project}/locations/{region}"
                                     f"/jobs/{job}:run", json=body, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"Cloud Run refused the job: {response.status_code} {response.text[:200]}")
        data = response.json()
        return (data.get("metadata") or {}).get("name") or data.get("name") or ""
    return run

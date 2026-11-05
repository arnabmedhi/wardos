"""The worker's only line to WardOS: plain HTTPS calls out."""
from __future__ import annotations

from pathlib import Path


class TaskGone(RuntimeError):
    pass


class ServerError(RuntimeError):
    pass


class ServerClient:
    def __init__(self, base: str, worker: str, *, session=None, token=None):
        self.base, self.worker = base.rstrip("/"), worker
        self._token = token
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        if token is None and self.base.startswith("https://"):
            self._token = _id_token_source(self.base)

    def _headers(self) -> dict:
        return {"Authorization": "Bearer " + self._token()} if self._token else {}

    def _post(self, path: str, *, timeout=30, json=None, data=None, files=None) -> dict:
        reply = self.session.post(self.base + path, headers=self._headers(), timeout=timeout, json=json, data=data, files=files)
        if reply.status_code == 409:
            raise TaskGone(path)
        if reply.status_code >= 400:
            raise ServerError(f"{path.split('/tasks/')[0]}: HTTP {reply.status_code}")
        return reply.json()

    def next(self, batch_id: str | None = None) -> dict:
        return self._post("/agent/worker/next", json={"worker": self.worker, "batch_id": batch_id})

    def logged_out(self, task_id: str) -> None:
        self._post(f"/agent/worker/tasks/{task_id}/logged-out", json={"worker": self.worker})

    def heartbeat(self, line: str = "") -> None:
        self._post("/agent/worker/heartbeat", json={"worker": self.worker, "line": line})

    def patient(self, task_id: str) -> dict:
        return self._post(f"/agent/worker/tasks/{task_id}/patient", json={"worker": self.worker})

    def step(self, task_id: str, line: str = "", thought: str | None = None) -> None:
        self._post(f"/agent/worker/tasks/{task_id}/step", json={"worker": self.worker, "line": line, "thought": thought})

    def tally(self, task_id: str, source_ids: list[str]) -> dict:
        return self._post(f"/agent/worker/tasks/{task_id}/tally", json={"worker": self.worker, "source_ids": source_ids})

    def finish(self, task_id: str, status: str, line: str, **result) -> None:
        self._post(f"/agent/worker/tasks/{task_id}/finish", json={"worker": self.worker, "status": status, "line": line, **result})

    def round(self, task_id: str, outcome: str, *, source_ids=(), reports: int = 0, problem: str = "") -> dict:
        return self._post(f"/agent/worker/tasks/{task_id}/round",
                          json={"worker": self.worker, "outcome": outcome, "source_ids": list(source_ids),
                                "reports": reports, "problem": problem})

    def logins(self) -> dict:
        """The login times (epoch seconds, last 12 hours) and failed-in-a-row count, kept on the server so a restarted
        robot cannot forget them."""
        return self._post("/agent/worker/logins", json={"worker": self.worker})

    def note_logins(self, count: int, failed: bool) -> dict:
        return self._post("/agent/worker/logins", json={"worker": self.worker, "add": count, "failed": failed})

    def give_up(self, line: str) -> None:
        self._post("/agent/worker/give-up", json={"worker": self.worker, "line": line})

    def post_report(self, admission_id: str, path: str, portal: str) -> dict:
        with open(path, "rb") as handle:
            return self._post(f"/admissions/{admission_id}/reports", timeout=180, data={"portal": portal},
                              files={"file": (Path(path).name, handle, "application/pdf")})

    def post_values(self, admission_id: str, values: list[dict], portal: str) -> dict:
        return self._post(f"/admissions/{admission_id}/report-values", timeout=120, json={"portal": portal, "values": values})


def _id_token_source(audience: str):
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    from ..config import load_settings
    path = load_settings().google_credentials
    if not path:
        def cloud_token() -> str:
            from google.oauth2 import id_token
            return id_token.fetch_id_token(Request(), audience)  # the job's own service account, from the metadata server
        return cloud_token
    credentials = service_account.IDTokenCredentials.from_service_account_file(str(path), target_audience=audience)

    def token() -> str:
        if not credentials.valid:
            credentials.refresh(Request())
        return credentials.token
    return token

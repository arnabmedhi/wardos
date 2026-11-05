"""Per-patient memory of what was already downloaded from a portal."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Ledger:
    directory: Path
    portal: str
    patient: str

    @property
    def path(self) -> Path:
        key = hashlib.sha256(f"{self.portal}|{self.patient}".encode()).hexdigest()[:20]
        return self.directory / f"{key}.json"

    def _load(self) -> dict:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"rows": {}, "pdfs": {}, "checks": []}

    def _save(self, data: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def seen_rows(self) -> set[str]:
        return set(self._load()["rows"])

    def remember(self, row_keys: list[str], pdf_sha256s: list[str], skill: str) -> None:
        data = self._load()
        stamp = _now()
        for key in row_keys:
            data["rows"].setdefault(key, {"first_downloaded": stamp, "skill": skill})
        for sha in pdf_sha256s:
            data["pdfs"].setdefault(sha, {"first_downloaded": stamp, "rows": row_keys})
        self._save(data)

    def forget(self, row_keys: list[str]) -> None:
        data = self._load()
        for key in row_keys:
            data["rows"].pop(key, None)
        self._save(data)

    def record_check(self, skill: str, status: str, new_rows: int) -> None:
        data = self._load()
        data["checks"] = (data["checks"] + [{"at": _now(), "skill": skill, "status": status, "new_rows": new_rows}])[-50:]
        self._save(data)

    def last_checked(self) -> str | None:
        checks = self._load()["checks"]
        return checks[-1]["at"] if checks else None

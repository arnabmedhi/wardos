"""Per-run folder and event log."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import uuid


@dataclass
class RunLog:
    directory: Path
    console: bool = True
    started: float = field(default_factory=time.monotonic)

    @classmethod
    def create(cls, runs_dir: Path, label: str, console: bool = True) -> "RunLog":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = runs_dir / f"{stamp}-{label}-{uuid.uuid4().hex[:6]}"
        directory.mkdir(parents=True, exist_ok=False)
        return cls(directory, console)

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 2)

    def event(self, name: str, **data) -> None:
        row = {"t": self.elapsed, "event": name, **data}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, default=str) + "\n")

    def say(self, message: str) -> None:
        if self.console:
            sys.stdout.write(f"[{self.elapsed:7.1f}s] {message}\n")
            sys.stdout.flush()

    def write_json(self, name: str, data) -> Path:
        path = self.directory / name
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path


class NullLog:

    def event(self, name: str, **data) -> None:
        pass

    def say(self, message: str) -> None:
        pass

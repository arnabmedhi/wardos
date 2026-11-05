"""Load and validate skills (saved, versioned step lists per portal)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re

from ..config import SKILLS_DIR

ACTIONS = {"goto", "home", "click", "click_until", "fill", "select", "check", "press", "wait",
           "read_table", "select_rows", "select_rows_paged", "capture_pdf", "end_if", "call_skill"}
NEEDS_TARGET = {"click", "click_until", "fill", "select", "check", "capture_pdf"}
DATE_EXPR = re.compile(r"^today(?:\s*([+-])\s*(\d+)\s*d)?$")


class SkillError(ValueError):
    pass


def validate(skill: dict) -> None:
    for key in ("skill", "portal", "version", "kind", "steps"):
        if key not in skill:
            raise SkillError(f"skill missing '{key}'")
    if skill["kind"] not in {"read", "write"}:
        raise SkillError("kind must be 'read' or 'write'")
    seen = set()
    for step in skill["steps"]:
        sid = step.get("id")
        if not sid or sid in seen:
            raise SkillError(f"step id missing or duplicated: {sid!r}")
        seen.add(sid)
        action = step.get("action")
        if action not in ACTIONS:
            raise SkillError(f"step {sid}: unknown action {action!r}")
        if action in NEEDS_TARGET and "target" not in step:
            raise SkillError(f"step {sid}: action {action} needs a target")
        if action == "call_skill" and "skill" not in step:
            raise SkillError(f"step {sid}: call_skill needs 'skill'")
        if action == "end_if" and ("condition" not in step or "status" not in step):
            raise SkillError(f"step {sid}: end_if needs 'condition' and 'status'")


class SkillLibrary:
    def __init__(self, root: Path = SKILLS_DIR):
        self.root = root

    def path(self, portal: str, name: str) -> Path:
        return self.root / portal / f"{name}.json"

    def load(self, portal: str, name: str) -> dict:
        path = self.path(portal, name)
        if not path.is_file():
            raise SkillError(f"no skill {name!r} for portal {portal!r}")
        skill = json.loads(path.read_text(encoding="utf-8"))
        validate(skill)
        return skill

    def list(self, portal: str) -> list[dict]:
        folder = self.root / portal
        skills = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            skill = json.loads(path.read_text(encoding="utf-8"))
            validate(skill)
            skills.append(skill)
        return skills


class LayeredSkillLibrary(SkillLibrary):

    def __init__(self, roots: list[Path]):
        super().__init__(roots[-1])
        self.roots = roots

    def path(self, portal: str, name: str) -> Path:
        for root in self.roots:
            candidate = root / portal / f"{name}.json"
            if candidate.is_file():
                return candidate
        return self.roots[-1] / portal / f"{name}.json"

    def list(self, portal: str) -> list[dict]:
        found: dict[str, dict] = {}
        for root in reversed(self.roots):
            for skill in SkillLibrary(root).list(portal):
                found[skill["skill"]] = skill
        return [found[name] for name in sorted(found)]


def parse_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().lower()
    match = DATE_EXPR.match(text)
    if match:
        delta = int(match.group(2) or 0) * (-1 if match.group(1) == "-" else 1)
        return date.today() + timedelta(days=delta)
    return date.fromisoformat(text)


def resolve_inputs(skill: dict, given: dict) -> dict:
    values = dict(given)
    for name, spec in (skill.get("inputs") or {}).items():
        spec = spec if isinstance(spec, dict) else {"type": spec}
        if values.get(name) in (None, "") and "default" in spec:
            values[name] = spec["default"]
        if values.get(name) in (None, ""):
            if spec.get("required"):
                raise SkillError(f"input '{name}' is required")
            continue
        if spec.get("type") == "date":
            values[name] = parse_date(values[name])
        elif spec.get("type") == "string":
            values[name] = str(values[name]).strip()
        if "choices" in spec and values[name] not in spec["choices"]:
            base, sep, window = str(values[name]).partition(",")
            if not (sep and base in spec["choices"] and re.fullmatch(r"(?:(?:days|hours|from|to):[^,]+,?)+", window)):
                raise SkillError(f"input '{name}' must be one of {spec['choices']}")
    return values

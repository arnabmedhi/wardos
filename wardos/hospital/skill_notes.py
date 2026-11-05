"""Plain step texts that say which saved skill the agent used, learned or repaired (shown on the task)."""
from __future__ import annotations

import re

USED, LEARNING, SAVED, REPAIRED = "used", "learning", "learned", "repaired"


def skill_title(name: str | None) -> str:
    """'bed_occupancy.count_vacant_beds' -> 'count vacant beds'; 'beds.vacant.count' -> 'beds vacant count'."""
    name = (name or "").strip()
    last = name.rsplit(".", 1)[-1] if name.count(".") == 1 else name
    return re.sub(r"[._]+", " ", last).strip() or "this task"


def start_line(mode: str, name: str | None) -> str | None:
    if mode == "run":
        return f"Used saved skill: {skill_title(name)}"
    if mode == "explore":
        return "Learning a new skill…"
    return None


def end_lines(answer) -> tuple[list[str], str | None]:
    """Lines to add after the brain has finished, and the skill state (used / learned / repaired / None)."""
    title = skill_title(getattr(answer, "skill", None))
    if getattr(answer, "learned", False):
        return [f"Saved new skill: {title}"], SAVED
    if getattr(answer, "repaired", False):
        return [f"Repaired skill: {title}"], REPAIRED
    if getattr(answer, "decision", "") in ("run", "retry") and getattr(answer, "skill", None):
        return [], USED
    return [], None

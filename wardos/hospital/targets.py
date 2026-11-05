"""Find one element on the page from a target description."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import time

STRATEGIES = ("role", "label", "placeholder", "text", "css")


class TargetNotFound(Exception):
    pass


@dataclass
class Match:
    locator: object
    strategy: str
    drift: bool


def as_pattern(value: str, exact: bool = False):
    if isinstance(value, str) and len(value) > 1 and value.startswith("/") and value.rfind("/") > 0:
        end = value.rfind("/")
        flags = re.I if "i" in value[end + 1:] else 0
        return re.compile(value[1:end], flags)
    if exact:
        return re.compile(r"^\s*" + re.escape(value) + r"\s*$")
    return value


def _candidates(scope, target: dict) -> list[tuple[str, object]]:
    exact = bool(target.get("exact"))
    found = []
    for strategy in [key for key in target if key in STRATEGIES]:
        value = target[strategy]
        if strategy == "role":
            name = target.get("name")
            kwargs = {}
            if name is not None:
                kwargs["name"] = as_pattern(name, exact)
            found.append((strategy, scope.get_by_role(value, **kwargs)))
        elif strategy == "label":
            found.append((strategy, scope.get_by_label(as_pattern(value, exact))))
        elif strategy == "placeholder":
            found.append((strategy, scope.get_by_placeholder(as_pattern(value, exact))))
        elif strategy == "text":
            found.append((strategy, scope.get_by_text(as_pattern(value, exact))))
        elif strategy == "css":
            locator = scope.locator(value)
            if target.get("has_text"):
                locator = locator.filter(has_text=as_pattern(target["has_text"]))
            found.append((strategy, locator))
    if not found:
        raise ValueError(f"target has no usable way to find an element: {target}")
    return found


async def _visible_matches(locator) -> list:
    matches = []
    for item in await locator.all():
        try:
            if await item.is_visible():
                matches.append(item)
        except Exception:
            continue
    return matches


async def resolve(page, target: dict, timeout_ms: int = 10000, poll_ms: int = 250) -> Match:
    scope = page.locator(target["within"]).first if target.get("within") else page
    candidates = _candidates(scope, target)
    deadline = time.monotonic() + timeout_ms / 1000
    nth = target.get("nth")
    last_counts: dict[str, int] = {}
    while True:
        for index, (strategy, locator) in enumerate(candidates):
            visible = await _visible_matches(locator)
            last_counts[strategy] = len(visible)
            if len(visible) == 1 and nth is None:
                return Match(visible[0], strategy, drift=index > 0)
            if visible and nth is not None and -len(visible) <= nth < len(visible):
                return Match(visible[nth], strategy, drift=index > 0)
        if time.monotonic() >= deadline:
            raise TargetNotFound(f"no unique visible element (matches per way: {last_counts})")
        await asyncio.sleep(poll_ms / 1000)


async def is_visible(page, target: dict) -> bool:
    scope = page.locator(target["within"]).first if target.get("within") else page
    for _, locator in _candidates(scope, target):
        if await _visible_matches(locator):
            return True
    return False

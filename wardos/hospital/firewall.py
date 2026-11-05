"""Write firewall: data-changing requests are blocked unless allowed."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import re
from typing import Callable
from urllib.parse import urlsplit

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

READ_WORDS = re.compile(r"get|list|search|fetch|view|find|lookup|dropdown|count|detail|report|master", re.I)
READ_SHAPE = re.compile(r"[a-z]by[A-Z]|By[A-Z]|Array$")
WRITE_WORDS = re.compile(
    r"save|insert|add|create|update|edit|modify|delete|remove|cancel|submit|verify|approve|generate|allot|"
    r"allocat|admit|discharge|transfer|book|issue|upload|reset|change|block|close|finali[sz]e|sign|assign|"
    r"release|dispatch|receive|accept|reject|post|put|logout", re.I)


def read_like(path: str) -> bool:
    last = [part for part in path.split("/") if part][-1:] or [""]
    tail = last[0]
    return bool((READ_WORDS.search(tail) or READ_SHAPE.search(tail)) and not WRITE_WORDS.search(path))


def mask_path(path: str) -> str:
    path = re.sub(r"[A-Za-z0-9_-]{24,}", "{token}", path)
    return re.sub(r"\d{3,}", "{n}", path)


@dataclass
class FirewallEvent:
    method: str
    host: str
    path: str
    decision: str


@dataclass
class WriteFirewall:
    mode: str = "observe"
    allow: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    on_event: Callable[[FirewallEvent], None] | None = None
    allow_read_like: bool = False
    events: list[FirewallEvent] = field(default_factory=list)
    _once: list[re.Pattern] = field(default_factory=list, repr=False)
    _window: int = field(default=0, repr=False)

    def __post_init__(self):
        if self.mode not in {"observe", "enforce"}:
            raise ValueError("firewall mode must be 'observe' or 'enforce'")
        self._allow = [re.compile(p) for p in self.allow]

    async def attach(self, context) -> None:
        await context.route("**/*", self._handle)

    @contextmanager
    def allow_once(self, path_pattern: str):
        pattern = re.compile(path_pattern)
        self._once.append(pattern)
        try:
            yield
        finally:
            if pattern in self._once:
                self._once.remove(pattern)

    @contextmanager
    def allow_writes(self, max_count: int = 3):
        self._window = max_count
        try:
            yield
        finally:
            self._window = 0

    def decide(self, method: str, url: str) -> str:
        method = method.upper()
        parts = urlsplit(url)
        if method in SAFE_METHODS or parts.scheme not in {"http", "https"}:
            return "pass"
        if self.hosts and parts.hostname not in self.hosts:
            return "pass"
        if any(p.search(parts.path) for p in self._allow):
            return "allowed"
        for pattern in list(self._once):
            if pattern.search(parts.path):
                self._once.remove(pattern)
                return "allowed_once"
        if self._window > 0:
            self._window -= 1
            return "allowed_approved"
        if self.allow_read_like and method == "POST" and read_like(parts.path):
            return "allowed_read"
        return "observed" if self.mode == "observe" else "blocked"

    async def _handle(self, route):
        request = route.request
        decision = self.decide(request.method, request.url)
        if decision != "pass":
            parts = urlsplit(request.url)
            event = FirewallEvent(request.method.upper(), parts.hostname or "", mask_path(parts.path), decision)
            self.events.append(event)
            if self.on_event:
                self.on_event(event)
        if decision == "blocked":
            await route.abort("blockedbyclient")
        else:
            await route.fallback()

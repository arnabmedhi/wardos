"""What every portal profile provides."""
from __future__ import annotations

from enum import Enum
from typing import Protocol


class Health(str, Enum):
    LOGGED_OUT = "LOGGED_OUT"
    HEALTHY = "HEALTHY"
    BROKEN = "BROKEN"


class LoginError(RuntimeError):

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class PortalProfile(Protocol):
    name: str
    home_url: str
    origins: list[str]
    firewall_hosts: list[str]
    firewall_allow: list[str]
    credential_keys: tuple[str, str]
    health_skill: str

    async def login(self, browser, *, username: str, password: str, models, otp, log, deadline: float) -> None: ...

    async def logout(self, browser, *, log) -> None: ...

    async def looks_logged_out(self, page) -> bool: ...

    async def session_suspect(self, page) -> bool: ...

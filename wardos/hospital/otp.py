"""Fresh one-time passwords from a mail inbox (read-only IMAP)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
import imaplib
import json
from pathlib import Path
import re
import ssl
import time
from typing import Protocol

from dotenv import dotenv_values

CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
OTP_WORDS_RE = re.compile(r"otp|one[\s-]*time[\s-]*password|verification\s*code", re.I)


@dataclass(frozen=True)
class OtpRequest:
    uidvalidity: str
    baseline_uid: int
    requested_at: datetime
    seconds: int

    @property
    def ends_at(self) -> datetime:
        return self.requested_at + timedelta(seconds=self.seconds)


class OtpProvider(Protocol):
    async def begin(self) -> OtpRequest: ...
    async def next_code(self, request: OtpRequest, tried: set[str], timeout: float) -> str | None: ...
    async def close(self) -> None: ...


def _addresses(message, header: str) -> set[str]:
    return {addr.lower() for _, addr in getaddresses(message.get_all(header, []))}


def _norm(subject: str) -> str:
    return " ".join(str(subject).split()).casefold()


def parse_otp_email(raw: bytes, *, sender: str, subject: str, recipient: str,
                    request: OtpRequest, received_at: datetime) -> str | None:
    if not request.requested_at <= received_at <= request.ends_at:
        return None
    message = message_from_bytes(raw, policy=default_policy)
    if _addresses(message, "From") != {sender.lower()}:
        return None
    if _norm(message.get("Subject", "")) != _norm(subject):
        return None
    if recipient.lower() not in (_addresses(message, "To") | _addresses(message, "Delivered-To")):
        return None
    try:
        sent = parsedate_to_datetime(message["Date"])
    except (TypeError, ValueError, KeyError):
        return None
    if sent is None or sent.tzinfo is None:
        return None
    if not request.requested_at - timedelta(seconds=15) <= sent <= request.ends_at:
        return None
    body = message.get_body(preferencelist=("plain", "html")) if message.is_multipart() else message
    if body is None:
        return None
    text = unescape(re.sub(r"<[^>]+>", " ", body.get_content()))
    if not OTP_WORDS_RE.search(text):
        return None
    codes = set(CODE_RE.findall(text))
    return codes.pop() if len(codes) == 1 else None


@dataclass
class ImapOtpProvider:
    config_file: Path
    source_file: Path
    window_seconds: int = 150
    host: str = "imap.gmail.com"
    poll_seconds: float = 2.0
    _client: imaplib.IMAP4_SSL | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        values = dotenv_values(self.config_file) if self.config_file.is_file() else {}
        self._address = (values.get("WARDOS_OTP_EMAIL") or "").strip()
        self._password = "".join((values.get("WARDOS_OTP_APP_PASSWORD") or "").split())
        if not self._address or not self._password:
            raise RuntimeError(f"OTP inbox config incomplete in {self.config_file.name}")
        source = json.loads(self.source_file.read_text(encoding="utf-8"))
        self._sender, self._subject = source["sender"], source["subject"]

    def _connect(self):
        if self._client is None:
            client = imaplib.IMAP4_SSL(self.host, 993, ssl_context=ssl.create_default_context(), timeout=20)
            client.login(self._address, self._password)
            self._client = client
        status, _ = self._client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("OTP inbox unavailable")
        return self._client

    def _validity(self, client) -> str:
        value = client.response("UIDVALIDITY")[1]
        if not value or not value[0]:
            raise RuntimeError("Mailbox UIDVALIDITY unavailable")
        return value[0].decode("ascii")

    def _snapshot(self) -> OtpRequest:
        client = self._connect()
        validity = self._validity(client)
        status, data = client.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("Cannot read inbox position")
        baseline = max((int(x) for x in data[0].split()), default=0)
        return OtpRequest(validity, baseline, datetime.now(timezone.utc).replace(microsecond=0), self.window_seconds)

    def _scan(self, request: OtpRequest, tried: set[str]) -> str | None:
        client = self._connect()
        if self._validity(client) != request.uidvalidity:
            raise RuntimeError("Mailbox changed identity; request a new OTP")
        status, data = client.uid("search", None, "UID", f"{request.baseline_uid + 1}:*")
        if status != "OK":
            return None
        for uid in sorted({int(x) for x in data[0].split()}, reverse=True):
            if uid <= request.baseline_uid:
                continue
            status, parts = client.uid("fetch", str(uid), "(INTERNALDATE BODY.PEEK[])")
            if status != "OK":
                continue
            chunks = [p for p in parts if isinstance(p, tuple)]
            if not chunks:
                continue
            meta = b" ".join(c[0] for c in chunks)
            match = re.search(rb'INTERNALDATE "([^"]+)"', meta)
            if not match:
                continue
            received = datetime.strptime(match.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
            code = parse_otp_email(b"".join(c[1] for c in chunks), sender=self._sender, subject=self._subject,
                                   recipient=self._address, request=request, received_at=received)
            if code and code not in tried:
                return code
        return None

    async def begin(self) -> OtpRequest:
        return await asyncio.to_thread(self._snapshot)

    async def next_code(self, request: OtpRequest, tried: set[str], timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and datetime.now(timezone.utc) < request.ends_at:
            code = await asyncio.to_thread(self._scan, request, tried)
            if code:
                return code
            await asyncio.sleep(self.poll_seconds)
        return None

    async def close(self) -> None:
        def _logout():
            if self._client is not None:
                try:
                    self._client.logout()
                except Exception:
                    pass
                self._client = None
        await asyncio.to_thread(_logout)

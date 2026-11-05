"""New dangerous values and new reports: durable duplicate claims and web push messages that carry no patient detail."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from urllib.parse import quote, urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ..config import HOSPITAL_TZ
from . import model as m
from .agent_tasks import AGENT_AUTHOR
from .auth import DEV_EMAIL
from .settings import load_push_settings
from .today import build_today

SUBS, SENT = "push_subs", "push_sent"
ICONS = ("icon-192.png", "icon-512.png", "maskable-192.png", "maskable-512.png")
PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
PUSH_HOST_SUFFIXES = (".notify.windows.com",)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def quiet_for_agent(after: dict, agent_sources: dict, *, now: datetime) -> dict:
    def old(alert):
        if alert.get("source_id") not in agent_sources:
            return False
        try:
            then = datetime.fromisoformat(alert.get("time") or "")
        except (TypeError, ValueError):
            return False
        then = then if then.tzinfo else then.replace(tzinfo=HOSPITAL_TZ)
        return now - then > timedelta(hours=24)
    return {**after, "alerts": [a for a in after.get("alerts", []) if not old(a)],
            "notices": [n for n in after.get("notices", []) if n.get("source_id") not in agent_sources]}


def new_notifications(before: dict, after: dict, sent=(), *, report_ids=()) -> list[dict]:
    def danger_key(alert):
        return "danger:" + _hash([alert.get(k) for k in ("key", "value", "unit", "time")])

    seen = set(sent) | {danger_key(a) for a in before.get("alerts", [])}
    events = []
    for alert in after.get("alerts", []):
        key = danger_key(alert)
        if key not in seen:
            events.append({"key": key, "kind": "danger", "silent": False})
            seen.add(key)
    old_reports = {n.get("source_id") for n in before.get("notices", []) if n.get("kind") == "info"}
    reports = list(report_ids) + [n.get("source_id") for n in after.get("notices", [])
                                  if n.get("kind") == "info" and n.get("source_id") not in old_reports
                                  and str(n.get("title", "")).startswith("New report:")]
    for sid in reports:
        key = "report:" + _hash(sid)
        if sid and key not in seen:
            events.append({"key": key, "kind": "report", "silent": True})
            seen.add(key)
    return events


class SubscribeIn(BaseModel):
    subscription: dict | None = None
    device_label: str = Field(default="This phone", max_length=80)


class UnsubscribeIn(BaseModel):
    endpoint: str = Field(max_length=4096)


def validate_subscription(subscription: dict) -> dict:
    try:
        endpoint = subscription["endpoint"]
        if not isinstance(endpoint, str):
            raise ValueError()
        url = urlsplit(endpoint)
        if (len(endpoint) > 4096 or url.scheme != "https" or (url.hostname or "") not in PUSH_HOSTS
                and not (url.hostname or "").endswith(PUSH_HOST_SUFFIXES)
                or url.port not in (None, 443) or url.username or url.password or url.fragment):
            raise ValueError()
        keys = subscription["keys"]
        decoded = {}
        for key, size in (("p256dh", 65), ("auth", 16)):
            value = keys[key]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
                raise ValueError()
            decoded[key] = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            if len(decoded[key]) != size:
                raise ValueError()
        from cryptography.hazmat.primitives.asymmetric import ec
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded["p256dh"])
        return {"endpoint": endpoint, "keys": {k: keys[k] for k in ("p256dh", "auth")}}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Invalid browser push subscription.") from None


class Alerts:
    def __init__(self, record, auth, *, settings=None, sender=None):
        self.record, self.store, self.auth = record, record.store, auth
        self.settings = settings or load_push_settings()
        self.sender = sender or self._webpush

    def subscribe(self, doctor, body: SubscribeIn) -> dict:
        if not self.settings.configured:
            raise HTTPException(503, "Alerts are not configured on this server yet.")
        if body.subscription is not None:
            sub = validate_subscription(body.subscription)
            self.store.put(SUBS, {"id": _hash(sub["endpoint"]), "doctor_email": doctor.email,
                                  "subscription": sub, "device_label": body.device_label,
                                  "created_at": m.now_iso(), "active": True})
        return {"public_key": self.settings.public}

    def unsubscribe(self, doctor, endpoint: str) -> dict:
        def remove(tx):
            row = tx.get(SUBS, _hash(endpoint))
            if row and row.get("doctor_email") == doctor.email:
                tx.put(SUBS, {**row, "active": False, "subscription": {}, "removed_at": m.now_iso()})
        self.store.transact(remove)
        return {"ok": True}

    def snapshot(self, admission_id: str) -> dict:
        admission = self.record.admission(admission_id)
        return build_today(admission, self.record.patient_of(admission_id), self.record.facts(admission_id),
                           self.record.questions(admission_id),
                           sources=self.store.query(m.SOURCES, admission_id=admission_id))

    def _webpush(self, subscription: dict, payload: dict):
        from pywebpush import webpush
        return webpush(subscription_info=subscription, data=json.dumps(payload),
                       vapid_private_key=self.settings.private, vapid_claims={"sub": self.settings.subject},
                       ttl=3600, timeout=10, headers={"Urgency": "low" if payload["silent"] else "high"})

    def _devices(self, email=None) -> list[dict]:
        rows = self.store.query(SUBS, active=True)
        return [r for r in rows if (email is None or r["doctor_email"] == email)
                and ((self.auth.settings.dev_doctor and r["doctor_email"] == DEV_EMAIL)
                     or self.auth.doctor_for(r["doctor_email"]) is not None)]

    def _send(self, devices: list[dict], payload: dict) -> dict:
        def send(row):
            try:
                self.sender(row["subscription"], payload)
                return True
            except Exception as exc:
                response = getattr(exc, "response", None)
                if getattr(response, "status_code", None) in (404, 410):
                    def remove(tx):
                        current = tx.get(SUBS, row["id"])
                        if current and current.get("subscription") == row["subscription"]:
                            tx.put(SUBS, {**current, "active": False, "subscription": {}, "removed_at": m.now_iso()})
                    self.store.transact(remove)
                return False
        with ThreadPoolExecutor(max_workers=min(8, len(devices) or 1)) as pool:
            results = list(pool.map(send, devices))
        return {"sent": sum(results), "failed": len(results) - sum(results)}

    def after_merge(self, admission_id: str, before: dict, *, report_ids=()) -> dict:
        admission = self.record.admission(admission_id)
        if admission.get("archived") or not self.settings.configured:
            return {"sent": 0, "failed": 0}
        devices = self._devices()
        if not devices:
            return {"sent": 0, "failed": 0}
        after = self.snapshot(admission_id)
        admission = self.record.admission(admission_id)
        total = {"sent": 0, "failed": 0}
        agent_sources = {s["id"]: s for s in self.store.query(m.SOURCES, admission_id=admission_id)
                         if s.get("author") == AGENT_AUTHOR}
        after = quiet_for_agent(after, agent_sources, now=datetime.now(timezone.utc))
        for event in new_notifications(before, after, report_ids=report_ids):
            marker = _hash([admission_id, event["key"]])
            def claim(tx):
                if tx.get(SENT, marker):
                    return False
                tx.put(SENT, {"id": marker, "admission_id": admission_id, "event": event["key"],
                              "created_at": m.now_iso()})
                return True
            if not self.store.transact(claim):
                continue
            bed = str(admission.get("bed") or "?")
            bed = bed if re.fullmatch(r"[A-Za-z0-9 -]{1,16}", bed) else "?"
            title = (f"WardOS: Bed {bed} - dangerous value. Tap to open." if event["kind"] == "danger"
                     else f"WardOS: Bed {bed} - new report.")
            payload = {"title": title, "silent": event["silent"], "tag": marker,
                       "url": "/#/a/" + quote(admission_id, safe="")}
            result = self._send(devices, payload)
            for k in total:
                total[k] += result[k]
        return total

    def test(self, doctor) -> dict:
        if not self.settings.configured:
            raise HTTPException(503, "Alerts are not configured on this server yet.")
        return self._send(self._devices(doctor.email),
                          {"title": "WardOS test alert", "silent": False, "tag": "wardos-test", "url": "/"})

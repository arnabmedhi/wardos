"""Doctor sign-in: who may open WardOS, and who did what."""
from __future__ import annotations

import html
import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel

from .settings import AuthSettings

DOCTORS = "doctors"
SESSION_COOKIE, STATE_COOKIE = "wardos_session", "wardos_oauth"
SESSION_SECONDS = 30 * 24 * 3600
STATE_SECONDS = 600
LIST_CACHE_SECONDS = 60
DEV_EMAIL = "dev-doctor@local.test"
ROLES = ("owner", "doctor")

PUBLIC_PATHS = frozenset({"/health", "/auth/login", "/auth/callback", "/auth/logout", "/demo/start", "/demo/status",
                          "/manifest.webmanifest", "/sw.js", "/icons/icon-192.png", "/icons/icon-512.png",
                          "/icons/maskable-192.png", "/icons/maskable-512.png"})
PAGE_PATHS = frozenset({"/"})
MACHINE_ONLY = ((("POST",), re.compile(r"^/internal/(?:process|demo-alert|demo-deliver|demo-process)$")),)
DOCTOR_OR_MACHINE = ((("POST",), re.compile(r"^/admissions/[^/]+/(?:reports|report-values)$")),
                     (("POST",), re.compile(r"^/agent/worker/[a-z/_A-Z0-9-]+$")))
EMAIL_RE = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")
STATIC = Path(__file__).parent / "static"


@dataclass(frozen=True)
class Doctor:
    email: str
    name: str
    role: str


@dataclass(frozen=True)
class Machine:
    email: str


def _google_verify(token: str, audience: str) -> dict:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
    return id_token.verify_oauth2_token(token, google_requests.Request(), audience)


def _safe_next(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(c in value for c in "\r\n"):
        return "/"
    return value


class Auth:
    guest = False
    demo = None

    def __init__(self, settings: AuthSettings, store, *, verify_token=None, exchange_code=None, clock=time.time):
        if settings.dev_doctor and settings.on_cloud_run:
            raise RuntimeError("WARDOS_DEV_DOCTOR is set on Cloud Run (K_SERVICE is set): refusing to start. "
                               "Remove WARDOS_DEV_DOCTOR from this service.")
        self.settings, self.store, self.clock = settings, store, clock
        self._verify = verify_token or _google_verify
        self._exchange = exchange_code or self._google_exchange
        self._session = URLSafeSerializer(settings.cookie_secret, salt="wardos-session") if settings.cookie_secret else None
        self._state = URLSafeSerializer(settings.cookie_secret, salt="wardos-oauth-state") if settings.cookie_secret else None
        self._cache: tuple[float, dict[str, dict]] | None = None
        self._page = (STATIC / "signin.html").read_text(encoding="utf-8")

    def _listed(self) -> dict[str, dict]:
        now = self.clock()
        if self._cache and now - self._cache[0] < LIST_CACHE_SECONDS:
            return self._cache[1]
        rows = {str(d.get("email", "")).lower(): d for d in self.store.query(DOCTORS) if d.get("active")}
        self._cache = (now, rows)
        return rows

    def forget_list(self) -> None:
        self._cache = None

    def doctor_for(self, email: str, fallback_name: str = "") -> Doctor | None:
        email = (email or "").lower()
        if not email:
            return None
        listed = None
        if email not in self.settings.owner_emails:
            listed = self._listed().get(email)
            if listed is None:
                return None
        role = "owner" if email in self.settings.owner_emails else (listed.get("role") if listed.get("role") in ROLES else "doctor")
        name = (listed or {}).get("name") or fallback_name or email.split("@")[0]
        return Doctor(email, name, role)

    def make_cookie(self, doctor: Doctor) -> str:
        return self._session.dumps({"e": doctor.email, "n": doctor.name, "iat": self.clock()})

    def _doctor_from_cookie(self, value: str | None) -> Doctor | None:
        if not value or self._session is None:
            return None
        try:
            data = self._session.loads(value)
            if not isinstance(data, dict) or self.clock() - float(data["iat"]) > SESSION_SECONDS:
                return None
            return self.doctor_for(data["e"], data.get("n") or "")
        except (BadSignature, KeyError, TypeError, ValueError):
            return None

    def _set_cookie(self, response: Response, name: str, value: str, max_age: int, path: str = "/") -> None:
        response.set_cookie(name, value, max_age=max_age, httponly=True, secure=self.settings.cookie_secure,
                            samesite="lax", path=path)

    def _machine(self, token: str) -> Machine | None:
        s = self.settings
        if not (s.public_url and s.machine_accounts and token):
            return None
        try:
            claims = self._verify(token, s.public_url)
        except Exception:
            return None
        email = str(claims.get("email") or "").lower()
        if claims.get("email_verified") is not True or email not in s.machine_accounts:
            return None
        return Machine(email)

    def _kind(self, method: str, path: str) -> str:
        if any(method in m and r.match(path) for m, r in MACHINE_ONLY):
            return "machine"
        if any(method in m and r.match(path) for m, r in DOCTOR_OR_MACHINE):
            return "either"
        return "doctor"

    def render_page(self, message: str = "", status: int = 200, next_path: str = "/",
                    button: str = "Sign in with Google") -> HTMLResponse:
        text = (self._page.replace("{{MESSAGE}}", f'<p class="msg" role="alert">{html.escape(message)}</p>' if message else "")
                .replace("{{NEXT}}", urllib.parse.quote(_safe_next(next_path), safe="")).replace("{{BUTTON}}", html.escape(button))
                .replace("{{DEMO}}", '<a class="btn" href="/demo/start">Try the demo (no sign-in)</a>' if self.demo else "")
                .replace("{{SIGNCLASS}}", "alt" if self.demo else "")
                .replace("{{LEAD}}", "Try the demo, or sign in to open your ward." if self.demo else "Sign in to open your ward."))
        return HTMLResponse(text, status_code=status, headers={"Cache-Control": "no-store"})

    def _refuse(self, request: Request) -> Response:
        if request.method == "GET" and request.url.path in PAGE_PATHS:
            return self.render_page(status=401, next_path=request.url.path)
        return JSONResponse({"detail": "Not signed in."}, status_code=401)

    def gate(self, request: Request) -> tuple[Doctor | Machine | None, Response | None]:
        path = request.url.path
        if path in PUBLIC_PATHS:
            return None, None
        kind = self._kind(request.method, path)
        if kind != "doctor":
            header = request.headers.get("authorization", "")
            if header[:7].lower() == "bearer ":
                machine = self._machine(header[7:].strip())
                if machine is None:
                    return None, JSONResponse({"detail": "This token is not accepted here."}, status_code=403)
                return machine, None
            if kind == "machine":
                return None, self._refuse(request)
        if self.settings.dev_doctor:
            return Doctor(DEV_EMAIL, self.settings.dev_doctor, "owner"), None
        doctor = self._doctor_from_cookie(request.cookies.get(SESSION_COOKIE))
        if doctor is None:
            return None, self._refuse(request)
        return doctor, None

    def _google_exchange(self, code: str, redirect_uri: str) -> str:
        import requests
        response = requests.post("https://oauth2.googleapis.com/token", timeout=15, data={
            "code": code, "client_id": self.settings.client_id, "client_secret": self.settings.client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        response.raise_for_status()
        return response.json()["id_token"]

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_url}/auth/callback"

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.client_id and s.client_secret and s.cookie_secret and s.public_url)


class AuthGate:

    def __init__(self, app, auth: Auth):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await send({"type": "websocket.close", "code": 1008}) if scope["type"] == "websocket" else None
        if self.auth.demo:
            guest = self.auth.demo.wants(Request(scope, receive))
            if guest:
                return await self.auth.demo.serve(guest, scope, receive, send)
        identity, refusal = self.auth.gate(Request(scope, receive))
        if refusal is not None:
            return await refusal(scope, receive, send)
        scope.setdefault("state", {})["identity"] = identity
        await self.app(scope, receive, send)


def _identity(request: Request):
    return getattr(request.state, "identity", None)


def signed_in(request: Request) -> Doctor:
    who = _identity(request)
    if not isinstance(who, Doctor):
        raise HTTPException(401, "Not signed in.")
    return who


def any_caller(request: Request) -> Doctor | Machine:
    who = _identity(request)
    if who is None:
        raise HTTPException(401, "Not signed in.")
    return who


def owner_only(doctor: Doctor = Depends(signed_in)) -> Doctor:
    if doctor.role != "owner":
        raise HTTPException(403, "Only the owner can do this.")
    return doctor


class DoctorIn(BaseModel):
    email: str
    name: str
    role: str = "doctor"


def install(app: FastAPI, auth: Auth) -> None:
    app.add_middleware(AuthGate, auth=auth)
    app.state.auth = auth
    s = auth.settings

    @app.get("/auth/login", include_in_schema=False)
    def login(next: str = "/"):
        nxt = _safe_next(next)
        if s.dev_doctor:
            return RedirectResponse(nxt, status_code=302)
        if not auth.configured:
            return JSONResponse({"detail": "Google sign-in is not configured on this deployment."}, status_code=500)
        nonce = secrets.token_urlsafe(16)
        state = auth._state.dumps({"next": nxt, "n": nonce, "iat": auth.clock()})
        query = urllib.parse.urlencode({"client_id": s.client_id, "redirect_uri": auth.redirect_uri, "response_type": "code",
                                        "scope": "openid email profile", "state": state, "prompt": "select_account"})
        response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + query, status_code=302)
        auth._set_cookie(response, STATE_COOKIE, nonce, STATE_SECONDS, path="/auth")
        return response

    @app.get("/auth/callback", include_in_schema=False)
    def callback(request: Request, code: str = "", state: str = "", error: str = ""):
        def fail(message: str, status: int = 400, button: str = "Try again"):
            response = auth.render_page(message, status, button=button)
            response.delete_cookie(STATE_COOKIE, path="/auth")
            return response
        if error:
            return fail("Sign-in was cancelled.")
        if not auth.configured:
            return fail("Google sign-in is not configured on this deployment.", 500)
        try:
            data = auth._state.loads(state)
            fresh = auth.clock() - float(data["iat"]) <= STATE_SECONDS
            same = secrets.compare_digest(str(data["n"]), request.cookies.get(STATE_COOKIE) or "")
        except (BadSignature, KeyError, TypeError, ValueError):
            fresh = same = False
        if not (fresh and same):
            return fail("This sign-in link expired or was already used. Please try again.")
        try:
            claims = auth._verify(auth._exchange(code, auth.redirect_uri), s.client_id)
        except Exception:
            return fail("Google sign-in did not work. Please try again.", 502)
        email = str(claims.get("email") or "").lower()
        if claims.get("email_verified") is not True or not email:
            return fail("Google did not confirm this email address.")
        doctor = auth.doctor_for(email, str(claims.get("name") or ""))
        if doctor is None:
            response = fail(f"You are signed in as {email}, but you are not on this ward's list. Ask the owner to add you.",
                            403, button="Use another account")
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response
        response = RedirectResponse(_safe_next(data["next"]), status_code=302)
        auth._set_cookie(response, SESSION_COOKIE, auth.make_cookie(doctor), SESSION_SECONDS)
        response.delete_cookie(STATE_COOKIE, path="/auth")
        return response


    @app.get("/auth/logout", include_in_schema=False)
    def logout():
        response = RedirectResponse("/", status_code=302)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie("wardos_guest", path="/")
        return response

    @app.get("/auth/me")
    def me(doctor: Doctor = Depends(signed_in)):
        return {"email": doctor.email, "name": doctor.name, "role": doctor.role, **({"demo": True} if auth.guest else {})}

    def now_iso() -> str:
        return datetime.fromtimestamp(auth.clock(), timezone.utc).isoformat()

    @app.get("/doctors")
    def list_doctors(owner: Doctor = Depends(owner_only)):
        fixed = [{"email": e, "name": "", "role": "owner", "active": True, "fixed": True} for e in s.owner_emails]
        listed = [d for d in sorted(auth.store.query(DOCTORS), key=lambda d: str(d.get("added_at") or ""))
                  if str(d.get("email", "")).lower() not in s.owner_emails]
        return fixed + listed

    @app.post("/doctors")
    def add_doctor(body: DoctorIn, owner: Doctor = Depends(owner_only)):
        email, name = body.email.strip().lower(), body.name.strip()
        if not EMAIL_RE.match(email):
            raise HTTPException(400, "That does not look like an email address.")
        if not name or len(name) > 80:
            raise HTTPException(400, "Give the doctor's name (up to 80 letters).")
        if body.role not in ROLES:
            raise HTTPException(400, "role must be owner or doctor")
        if email in s.owner_emails:
            raise HTTPException(400, "This address is already an owner.")
        doc = {"id": email, "email": email, "name": name, "role": body.role, "added_by": owner.email,
               "added_at": now_iso(), "active": True}
        auth.store.put(DOCTORS, doc)
        auth.forget_list()
        return doc

    @app.post("/doctors/{email}/remove")
    def remove_doctor(email: str, owner: Doctor = Depends(owner_only)):
        email = email.strip().lower()
        if email in s.owner_emails:
            raise HTTPException(400, "This owner is fixed in the settings and cannot be removed here.")
        if email == owner.email:
            raise HTTPException(400, "You cannot remove yourself.")
        doc = auth.store.get(DOCTORS, email)
        if not doc:
            raise HTTPException(404, "no such doctor")
        doc = {**doc, "active": False, "removed_by": owner.email, "removed_at": now_iso()}
        auth.store.put(DOCTORS, doc)
        auth.forget_list()
        return doc

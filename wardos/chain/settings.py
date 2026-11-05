"""Ward record settings, from environment variables; nothing secret here."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from ..config import REPO_DIR, load_settings


@dataclass(frozen=True)
class ChainSettings:
    data_dir: Path
    model: str
    pages_per_read: int
    store: str
    files: str
    project: str = "your-gcp-project"
    prefix: str = "wardos_"
    drive_root: str = ""
    drive_token: Path = REPO_DIR / "token.json"


def load_chain_settings() -> ChainSettings:
    base = load_settings()
    env = os.environ.get
    return ChainSettings(
        data_dir=Path(env("WARDOS_CHAIN_DIR") or base.private_dir / "chain"),
        model=env("WARDOS_CHAIN_MODEL") or base.muse_model,
        pages_per_read=int(env("WARDOS_CHAIN_PAGES_PER_READ") or 1),
        store=(env("WARDOS_CHAIN_STORE") or "sqlite").lower(),
        files=(env("WARDOS_CHAIN_FILES") or "local").lower(),
        project=env("WARDOS_CHAIN_PROJECT") or "your-gcp-project",
        prefix=env("WARDOS_CHAIN_PREFIX") or "wardos_",
        drive_root=env("WARDOS_CHAIN_DRIVE_ROOT") or "",
        drive_token=Path(env("WARDOS_CHAIN_DRIVE_TOKEN") or REPO_DIR / "token.json"),
    )


@dataclass(frozen=True)
class AuthSettings:
    client_id: str = ""
    client_secret: str = ""
    cookie_secret: str = ""
    public_url: str = ""
    owner_emails: tuple[str, ...] = ()
    machine_accounts: tuple[str, ...] = ()
    dev_doctor: str = ""
    cookie_secure: bool = True
    on_cloud_run: bool = False


def _emails(raw: str | None) -> tuple[str, ...]:
    return tuple(sorted({e.strip().lower() for e in (raw or "").split(",") if e.strip()}))


def load_auth_settings(environ=None) -> AuthSettings:
    env = (environ if environ is not None else os.environ).get
    return AuthSettings(
        client_id=env("WARDOS_GOOGLE_CLIENT_ID") or "", client_secret=env("WARDOS_GOOGLE_CLIENT_SECRET") or "",
        cookie_secret=env("WARDOS_COOKIE_SECRET") or "", public_url=(env("WARDOS_PUBLIC_URL") or "").rstrip("/"),
        owner_emails=_emails(env("WARDOS_OWNER_EMAILS")), machine_accounts=_emails(env("WARDOS_MACHINE_ACCOUNTS")),
        dev_doctor=(env("WARDOS_DEV_DOCTOR") or "").strip(),
        cookie_secure=(env("WARDOS_COOKIE_SECURE") or "true").strip().lower() != "false",
        on_cloud_run=bool(env("K_SERVICE")))


@dataclass(frozen=True)
class PushSettings:
    public: str = ""
    private: str = ""
    subject: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.public and self.private and self.subject.startswith("mailto:"))


def load_push_settings() -> PushSettings:
    return PushSettings(public=os.environ.get("WARDOS_VAPID_PUBLIC", ""),
                        private=os.environ.get("WARDOS_VAPID_PRIVATE", ""),
                        subject=os.environ.get("WARDOS_VAPID_SUBJECT", ""))


def open_record(settings: ChainSettings | None = None, *, local_dir: Path | None = None):
    from .docstore import CachedDocStore, FirestoreDocStore, SQLiteDocStore
    from .files import DriveFileStore, LocalFileStore
    from .record import Record
    settings = settings or load_chain_settings()
    local = Path(local_dir or settings.data_dir)
    if settings.store == "firestore":
        from google.oauth2 import service_account
        base = load_settings()
        creds = (service_account.Credentials.from_service_account_file(str(base.google_credentials))
                 if base.google_credentials else None)
        store = FirestoreDocStore(project=settings.project, prefix=settings.prefix, credentials=creds)
        ttl = float(os.environ.get("WARDOS_CHAIN_CACHE_SECONDS") or 10)
        if ttl > 0:
            store = CachedDocStore(store, ttl=ttl)
    else:
        store = SQLiteDocStore(local / "record.sqlite3")
    if settings.files == "drive":
        if not settings.drive_root:
            raise RuntimeError("WARDOS_CHAIN_DRIVE_ROOT is not set")
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(settings.drive_token),
                                                      ["https://www.googleapis.com/auth/drive"])
        files = DriveFileStore(settings.drive_root, creds)
    else:
        files = LocalFileStore(local / "files")
    return Record(store, files)

"""Settings and secret-file locations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil

from dotenv import dotenv_values, load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
WARDOS_DIR = PACKAGE_DIR.parent
REPO_DIR = WARDOS_DIR      # secret files (.env, service-account.json, token.json) live here, git-ignored
SKILLS_DIR = WARDOS_DIR / "skills"

HOSPITAL_TZ = timezone(timedelta(hours=5, minutes=30), "IST")


def hospital_now() -> datetime:
    return datetime.now(HOSPITAL_TZ).replace(tzinfo=None)

DEFAULT_OPENMRS_URL = "https://your-openmrs-host.example"


def openmrs_url() -> str:
    return (os.environ.get("WARDOS_OPENMRS_URL") or DEFAULT_OPENMRS_URL).strip().rstrip("/")


def openmrs_host() -> str:
    return openmrs_url().split("://", 1)[-1].split("/", 1)[0]


def openmrs_rest_base() -> str:
    return openmrs_url() + "/openmrs/ws/rest/v1"


def openmrs_login() -> tuple[str, str]:
    user, password = os.environ.get("OPENMRS_USERNAME") or "admin", os.environ.get("OPENMRS_PASSWORD") or ""
    if not password:
        raise RuntimeError("OPENMRS_PASSWORD is not set (the private OpenMRS login comes from Secret Manager)")
    return user, password


CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
)


def _path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else (WARDOS_DIR / path).resolve()


@dataclass(frozen=True)
class Settings:
    private_dir: Path
    google_credentials: Path | None
    muse_base_url: str
    muse_model: str
    muse_effort: str
    muse_cua_model: str
    portal_secrets_file: Path
    otp_config_file: Path
    otp_source_file: Path
    otp_window_seconds: int
    chrome_path: Path | None

    @property
    def runs_dir(self) -> Path:
        return self.private_dir / "runs"

    @property
    def profiles_dir(self) -> Path:
        return self.private_dir / "profiles"

    @property
    def ledger_dir(self) -> Path:
        return self.private_dir / "ledger"


def find_chrome() -> Path | None:
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    return Path(found) if found else None


def load_settings() -> Settings:
    load_dotenv(WARDOS_DIR / ".env", override=False)
    env = os.environ.get
    credentials = env("WARDOS_GOOGLE_CREDENTIALS") or env("GOOGLE_APPLICATION_CREDENTIALS")
    default_credentials = REPO_DIR / "service-account.json"
    return Settings(
        private_dir=_path(env("WARDOS_PRIVATE_DIR"), WARDOS_DIR / "private"),
        google_credentials=_path(credentials, default_credentials) if (credentials or default_credentials.is_file()) else None,
        muse_base_url=env("WARDOS_MUSE_BASE_URL") or "https://api.meta.ai/v1",
        muse_model=env("WARDOS_MUSE_MODEL") or "muse-spark-1.3-contributor",
        muse_effort=env("WARDOS_MUSE_EFFORT") or "high",
        muse_cua_model=env("WARDOS_MUSE_CUA_MODEL") or "muse-wonder-1.0",
        portal_secrets_file=_path(env("WARDOS_PORTAL_SECRETS"), REPO_DIR / ".env"),
        otp_config_file=_path(env("WARDOS_OTP_CONFIG"), WARDOS_DIR / "private" / "otp.env"),
        otp_source_file=_path(env("WARDOS_OTP_SOURCE"), WARDOS_DIR / "private" / "otp-source.json"),
        otp_window_seconds=int(env("WARDOS_OTP_WINDOW_SECONDS") or 150),
        chrome_path=_path(env("WARDOS_CHROME"), find_chrome()) if (env("WARDOS_CHROME") or find_chrome()) else None,
    )


def read_secret_pair(file: Path, user_key: str, password_key: str) -> tuple[str, str]:
    values = dotenv_values(file) if file.is_file() else {}
    username = (values.get(user_key) or "").strip()
    password = values.get(password_key) or ""
    if not username or not password:
        raise RuntimeError(f"Portal credentials missing: expected {user_key} and {password_key} in {file.name}")
    return username, password


def secret_keys_present(file: Path, keys: list[str]) -> dict[str, bool]:
    values = dotenv_values(file) if file.is_file() else {}
    return {key: bool((values.get(key) or "").strip()) for key in keys}

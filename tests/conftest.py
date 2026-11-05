"""Shared test fixtures: fake model, fake OTP inbox, a session keeper wired to the fake portal."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_portal import CAPTCHA, ORIGIN, OTP, PASSWORD, USER, FakeHospital  # noqa: E402
from wardos.config import load_settings  # noqa: E402
from wardos.hospital.otp import OtpRequest  # noqa: E402
from wardos.hospital.portals.ehospital import EHospitalPortal  # noqa: E402
from wardos.hospital.session import SessionKeeper  # noqa: E402


class FakeModels:
    name = "fake"

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = 0

    async def read_captcha(self, image, mime, length=None):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else CAPTCHA
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def json(self, prompt, **kwargs):
        raise NotImplementedError


class FakeOtp:
    def __init__(self):
        self.begins = 0

    async def begin(self):
        self.begins += 1
        return OtpRequest("1", 0, datetime.now(timezone.utc), 120)

    async def next_code(self, request, tried, timeout):
        return None if OTP in tried else OTP

    async def close(self):
        pass


def fake_portal_profile(**overrides) -> EHospitalPortal:
    options = dict(home_url=ORIGIN + "/", origins=[ORIGIN], firewall_hosts=["ehospital.test"],
                   firewall_allow=[r"^/api/authentication/", r"^/api/user/hpr_skip$", r"^/api/lis/report/search$"])
    options.update(overrides)
    return EHospitalPortal(**options)


@pytest.fixture
def settings(tmp_path):
    base = load_settings()
    if not base.chrome_path:
        pytest.skip("Google Chrome is not installed")
    return replace(base, private_dir=tmp_path / "private")


@pytest_asyncio.fixture
async def make_keeper(settings, tmp_path):
    keepers = []

    async def factory(hospital: FakeHospital, *, answers=(), firewall_mode="observe", keep_login=False, **portal_overrides):
        keeper = SessionKeeper(settings, fake_portal_profile(**portal_overrides), FakeModels(answers), FakeOtp(),
                               headless=True, firewall_mode=firewall_mode, profile_dir=tmp_path / f"profile{len(keepers)}",
                               credentials=(USER, PASSWORD), setup=hospital.install, console=False,
                               login_timeout_s=60, health_timeout_s=25, keep_login=keep_login)
        await keeper.start()
        keepers.append(keeper)
        return keeper

    yield factory
    for keeper in keepers:
        await keeper.stop()

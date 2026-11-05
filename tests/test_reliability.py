"""Hedged model calls, the approved write window, fresh-browser health and hang detection."""
import asyncio
import time

import pytest

from test_brain import FakeExplorer, rig  # noqa: F401  (fixture reused)
from wardos.config import load_settings
from wardos.hospital.approve import Proposal, target_for
from wardos.hospital.explore import ExploreResult
from wardos.hospital.firewall import WriteFirewall
from wardos.hospital.portals.base import Health
from wardos.hospital.portals.ehospital import EHospitalPortal
from wardos.hospital.runlog import NullLog
from wardos.hospital.session import HANG, SessionKeeper
from wardos.models import hedged


@pytest.mark.asyncio
async def test_hedged_second_copy_wins_when_first_stalls():
    calls = []

    async def make():
        calls.append(time.monotonic())
        if len(calls) == 1:
            await asyncio.sleep(5)
            return "slow"
        return "fast"
    started = time.monotonic()
    assert await hedged(make, timeout=3, hedge_after=0.05) == "fast"
    assert time.monotonic() - started < 1 and len(calls) == 2


@pytest.mark.asyncio
async def test_hedged_replaces_a_failed_copy_and_raises_when_all_fail():
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("blip")
        return "ok"
    assert await hedged(flaky, timeout=5, hedge_after=2) == "ok"

    async def broken():
        raise ConnectionError("down")
    with pytest.raises(ConnectionError):
        await hedged(broken, timeout=5, hedge_after=2, max_calls=2)


@pytest.mark.asyncio
async def test_hedged_times_out():
    async def stuck():
        await asyncio.sleep(10)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await hedged(stuck, timeout=0.3, hedge_after=0.1)
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_captcha_used_only_when_both_views_agree():
    from wardos.cli import _fake_captcha
    from wardos.models import _two_views
    image = _fake_captcha("aB3xQ9")

    def reader(as_is, enlarged):
        async def read_one(data, kind):
            if isinstance(as_is if data is image else enlarged, BaseException):
                raise as_is if data is image else enlarged
            return as_is if data is image else enlarged
        return read_one
    assert await _two_views(reader("aB3xQ9", "aB3xQ9"), image, "image/png") == "aB3xQ9"
    assert await _two_views(reader("vI4u18", "vT4u18"), image, "image/png") == ""
    assert await _two_views(reader(TimeoutError(), "K7mP2w"), image, "image/png") == "K7mP2w"
    with pytest.raises(TimeoutError):
        await _two_views(reader(TimeoutError(), TimeoutError()), image, "image/png")


def test_approved_window_lets_exactly_n_writes_through():
    firewall = WriteFirewall(mode="enforce", hosts=["portal.test"])
    url = "https://portal.test/api/visit/save"
    assert firewall.decide("POST", url) == "blocked"
    with firewall.allow_writes(2):
        assert [firewall.decide("POST", url) for _ in range(3)] == ["allowed_approved", "allowed_approved", "blocked"]
    assert firewall.decide("POST", url) == "blocked"


@pytest.mark.asyncio
async def test_fresh_browser_counts_as_logged_out_without_loading_anything():
    keeper = SessionKeeper(load_settings(), EHospitalPortal(), models=None, otp=None)
    assert keeper.browser is None
    assert await keeper.health(NullLog()) is Health.LOGGED_OUT


def test_hang_is_told_apart_from_a_changed_page():
    assert HANG.search("TimeoutError: Locator.click: Timeout 8000ms exceeded")
    assert HANG.search("run deadline reached") and HANG.search("loading indicator did not finish")
    assert not HANG.search("element not found (no unique visible element (matches per way: {'css': 0}))")


def test_approval_target_and_summary():
    target = target_for({"css": "div.form > button.save", "text": "Save and close"}, "Save and close")
    assert target["css"] == "div.form > button.save" and target["has_text"] == "Save and close"
    assert target["role"] == "button" and "Save and close" in target["name"].replace("\\", "")
    lines = Proposal("Save", "/vitals", [{"label": "Temperature", "value": "37.2"}], "shot.png").lines()
    assert lines[0] == "Ready to press 'Save' on /vitals" and "  Temperature: 37.2" in lines


class LostOnceExplorer(FakeExplorer):
    runs = 0

    async def run(self, *args, **kwargs):
        LostOnceExplorer.runs += 1
        if LostOnceExplorer.runs == 1:
            return ExploreResult(goal="beds", status="SESSION_LOST", answer="", verified="UNCHECKED")
        return await super().run(*args, **kwargs)


@pytest.mark.asyncio
async def test_session_lost_during_exploration_logs_in_again_and_retries(rig):  # noqa: F811
    brain, keeper, settings, library = rig
    LostOnceExplorer.runs = 0
    keeper.logouts = 0

    async def logout(log):
        keeper.logouts += 1
    keeper.logout = logout
    brain.explorer_factory = LostOnceExplorer
    result = await brain.run("How many beds are vacant?")
    assert result.status == "ANSWERED", result.note
    assert LostOnceExplorer.runs == 2 and keeper.logouts == 1 and keeper.health_calls >= 2

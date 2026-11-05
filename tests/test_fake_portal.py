"""End-to-end tests against the fake eHospital (real Chrome, headless, no network, no model)."""
from __future__ import annotations

from fake_portal import FakeHospital
from wardos.hospital.runlog import RunLog
from wardos.hospital.targets import resolve

UHID = "20260000001"


async def test_login_handles_wrong_captcha_already_logged_in_otp_and_hpr(make_keeper):
    hospital = FakeHospital(force_already_once=True)
    keeper = await make_keeper(hospital, answers=["WRONG1"])
    log = RunLog.create(keeper.settings.runs_dir, "test-login", console=False)
    recoveries = await keeper.ensure_healthy(log)
    assert recoveries == 1
    assert hospital.logins == 1
    assert keeper.models.calls >= 3
    page = await keeper.browser.active()
    assert await page.locator("input[name='patient_uhid']").is_visible()


async def test_stalled_captcha_reader_does_not_kill_the_login(make_keeper):
    keeper = await make_keeper(FakeHospital(), answers=[TimeoutError("model stalled")])
    log = RunLog.create(keeper.settings.runs_dir, "test-stall", console=False)
    assert await keeper.ensure_healthy(log) == 1
    assert keeper.models.calls >= 2


async def test_broken_session_is_cured_by_logout_and_login(make_keeper):
    hospital = FakeHospital(broken_logins=1)
    keeper = await make_keeper(hospital)
    log = RunLog.create(keeper.settings.runs_dir, "test-broken", console=False)
    recoveries = await keeper.ensure_healthy(log)
    assert recoveries == 2
    assert hospital.logins == 2
    assert hospital.logouts >= 1


async def test_fetch_latest_reports_then_nothing_new_then_all_and_not_found(make_keeper):
    hospital = FakeHospital()
    keeper = await make_keeper(hospital)

    first = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": UHID})
    assert first.status == "FOUND", (first.failed_step, first.reason)
    assert len(first.artifacts) == 1 and first.artifacts[0]["check"]["ok"]
    assert first.outputs["selected_count"] == 3
    assert first.drift_warnings == []

    again = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": UHID})
    assert again.status == "NOTHING_NEW"

    everything = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": UHID, "select": "all"})
    assert everything.status == "FOUND"

    unknown = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": "20269999999"})
    assert unknown.status == "NOT_FOUND"
    assert hospital.logins == 1


async def test_session_expiring_mid_task_is_recovered_and_task_rerun(make_keeper):
    hospital = FakeHospital(expire_on_first_search=True)
    keeper = await make_keeper(hospital)
    result = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": UHID})
    assert result.status == "FOUND", (result.failed_step, result.reason)
    assert result.session_recoveries >= 2
    assert hospital.logins == 2


async def test_firewall_blocks_unapproved_writes_and_allows_approved_once(make_keeper):
    hospital = FakeHospital()
    keeper = await make_keeper(hospital, firewall_mode="enforce")
    result = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": UHID})
    assert result.status == "FOUND", (result.failed_step, result.reason)
    page = await keeper.browser.active()
    await page.click("#save")
    await page.wait_for_function("window.__saveStatus !== undefined || true")
    await page.wait_for_timeout(500)
    assert hospital.writes_received == []
    assert any(e["decision"] == "blocked" and e["path"] == "/api/lis/remarks/save" for e in keeper.firewall_log)
    with keeper.firewall.allow_once(r"^/api/lis/remarks/save$"):
        await page.click("#save")
        await page.wait_for_timeout(800)
    assert hospital.writes_received == ["/api/lis/remarks/save"]


async def test_target_fallback_reports_drift(make_keeper):
    keeper = await make_keeper(FakeHospital())
    page = await keeper.browser.active()
    await page.set_content("<button id='go'>Proceed</button>")
    match = await resolve(page, {"role": "button", "name": "/^Go$/", "css": "#go"}, timeout_ms=1500)
    assert match.strategy == "css" and match.drift is True

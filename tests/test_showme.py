"""Show-me mode: a visible cursor and thought line that change nothing about the work."""
from __future__ import annotations

import asyncio
import time

import pytest

from fake_portal import FakeHospital
from wardos.hospital import showme
from wardos.hospital.explore import DESCRIBE_JS, Explorer, OBSERVE_JS, TABLE_JS
from wardos.hospital.runlog import RunLog
from wardos.hospital.targets import is_visible

UHID = "20260000001"
STATE_JS = "() => { const s = window[Symbol.for('wardos.showme')]; return s ? s.state() : null; }"
BUTTONS = """<!doctype html><html><body style='margin:0;height:100vh'>
  <button id='top' style='position:fixed;left:40px;top:40px'>Top one</button>
  <button id='low' onclick="window.__clicked=true"
    style='position:fixed;left:0;right:0;bottom:0;height:80px;width:100%'>Bottom one</button>
  <input id='box' placeholder='Number' style='position:fixed;left:40px;top:100px'>
  <table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>
</body></html>"""


@pytest.fixture
def show_on(monkeypatch):
    monkeypatch.setenv("WARDOS_SHOW_ME", "1")


@pytest.fixture
def show_off(monkeypatch):
    monkeypatch.delenv("WARDOS_SHOW_ME", raising=False)


async def _paint(keeper) -> bytes:
    import base64
    page = await keeper.browser.active()
    cdp = await keeper.context.new_cdp_session(page)
    try:
        return base64.b64decode((await cdp.send("Page.captureScreenshot", {"format": "png"}))["data"])
    finally:
        await cdp.detach()


@pytest.mark.parametrize("intent, action, values, wanted", [
    ("navigate to the Wards page", "click", (), "Going to the Wards page"),
    ("open the Lab Information System module (may open a new tab)", "click", (), "Opening the Lab Information System module"),
    ("type the patient's UHID", "fill", (), "Typing the patient's UHID"),
    ("click 'Rahul Sharma'", "click", (), "Clicking"),
    ("typed 'endDate'", "fill", (), "Typing"),
    ("choose '--All--'", "select", (), "Choosing"),
    ("click ''", "click", (), "Clicking"),
    ("type {uhid} here", "fill", (), "Typing here"),
    (f"search {UHID} now", "click", (), "Searching now"),
    ("find Rahul Sharma today", "click", ("Rahul Sharma",), "Find today"),
    ("select 100 items per page option", "click", (), "Selecting items per page option"),
    ("", "fill", (), "Typing in a box"),
    (None, "click", (), "Clicking"),
    (None, "end_if", (), ""),
])
def test_clean_line(intent, action, values, wanted):
    assert showme.clean_line(intent, action, values) == wanted


async def test_a_whole_job_never_shows_a_value_or_page_text(make_keeper, show_on):
    hospital = FakeHospital()
    keeper = await make_keeper(hospital)
    result = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": UHID})
    assert result.status == "FOUND", (result.failed_step, result.reason)
    show = showme.for_browser(keeper.browser)
    assert len(show.lines) >= 3
    said = " | ".join(show.lines)
    for secret in (UHID, "S1001", "Complete Blood Count", "Liver Function", "20/10/2026", "Released"):
        assert secret not in said
    assert not any(ch.isdigit() for ch in said)
    state = await (await keeper.browser.active()).evaluate(STATE_JS)
    assert state["line"] in show.lines and UHID not in state["line"]


async def _job(make_keeper):
    hospital = FakeHospital()
    keeper = await make_keeper(hospital)
    requests = []
    keeper.context.on("request", lambda r: r.url.startswith("http") and requests.append((r.method, r.url.split("?")[0])))
    result = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": UHID, "select": "all"})
    decisions = {(e["method"], e["path"], e["decision"]) for e in keeper.firewall_log}
    return result, hospital, set(requests), decisions


async def test_same_job_on_and_off_gives_the_same_result(make_keeper, monkeypatch):
    monkeypatch.delenv("WARDOS_SHOW_ME", raising=False)
    off, hospital_off, requests_off, firewall_off = await _job(make_keeper)
    monkeypatch.setenv("WARDOS_SHOW_ME", "1")
    on, hospital_on, requests_on, firewall_on = await _job(make_keeper)
    for result in (off, on):
        assert result.status == "FOUND", (result.failed_step, result.reason)
    assert on.outputs["selected_count"] == off.outputs["selected_count"] == 3
    assert [(s.id, s.ok, s.strategy, s.drift) for s in on.steps] == [(s.id, s.ok, s.strategy, s.drift) for s in off.steps]
    assert [a["check"]["ok"] for a in on.artifacts] == [a["check"]["ok"] for a in off.artifacts]
    assert on.drift_warnings == off.drift_warnings == []
    assert requests_on == requests_off
    assert firewall_on == firewall_off
    assert hospital_on.logins == hospital_off.logins


async def test_a_failed_job_saves_a_screenshot_without_the_overlay(make_keeper, show_on):
    keeper = await make_keeper(FakeHospital())
    page = await keeper.browser.active()
    await page.set_content(BUTTONS)
    show = showme.for_browser(keeper.browser)
    before = await page.screenshot()
    await show.point(page.locator("#top"))
    await show.say("Opening the wards")
    await asyncio.sleep(0.8)
    assert await page.screenshot() == before
    assert await page.locator("#top").screenshot() == await page.locator("#top").screenshot()
    assert await _paint(keeper) != before
    assert (await page.evaluate(STATE_JS))["line"] == "Opening the wards"


async def test_overlay_is_invisible_to_the_robot_and_never_takes_a_click(make_keeper, show_on):
    keeper = await make_keeper(FakeHospital())
    page = await keeper.browser.active()
    await page.set_content(BUTTONS)
    observed = await page.evaluate(OBSERVE_JS, 180)
    table = await page.evaluate(TABLE_JS, 300)
    described = await page.evaluate(DESCRIBE_JS, 1)
    body = await page.locator("body").inner_text()
    show = showme.for_browser(keeper.browser)
    await show.say("Opening the wards")
    await show.point(page.locator("#low"))

    assert await page.evaluate(OBSERVE_JS, 180) == observed
    assert await page.evaluate(TABLE_JS, 300) == table
    assert await page.evaluate(DESCRIBE_JS, 1) == described
    assert await page.locator("body").inner_text() == body
    assert "Opening the wards" not in await page.content()
    assert await page.get_by_text("Opening the wards").count() == 0
    assert not await is_visible(page, {"text": "Opening the wards"})
    assert await page.get_by_role("button").count() == 2

    await page.locator("#low").click(timeout=3000)
    assert await page.evaluate("window.__clicked") is True
    bottom = await page.evaluate("[document.elementFromPoint(innerWidth / 2, innerHeight - 30).id]")
    assert bottom == ["low"]


async def test_cursor_glides_rings_and_shows_for_typing(make_keeper, show_on):
    keeper = await make_keeper(FakeHospital())
    page = await keeper.browser.active()
    await page.set_content(BUTTONS)
    show = showme.for_browser(keeper.browser)

    started = time.monotonic()
    await show.point(page.locator("#top"))
    assert 0.3 <= time.monotonic() - started <= 1.0
    state = await page.evaluate(STATE_JS)
    box = await page.locator("#top").bounding_box()
    assert abs(state["x"] - (box["x"] + box["width"] / 2)) < 1 and state["rings"] == 1

    await show.point(page.locator("#box"), ring=False)
    state = await page.evaluate(STATE_JS)
    box = await page.locator("#box").bounding_box()
    assert abs(state["y"] - (box["y"] + box["height"] / 2)) < 1 and state["rings"] == 1


async def test_survives_moving_to_a_new_page(make_keeper, show_on):
    hospital = FakeHospital()
    keeper = await make_keeper(hospital)
    log = RunLog.create(keeper.settings.runs_dir, "test-nav", console=False)
    await keeper.ensure_healthy(log)
    page = await keeper.browser.active()
    show = showme.for_browser(keeper.browser)
    await show.say("Opening the wards")
    await show.point(page.locator("input[name='patient_uhid']"), ring=False)
    where = await page.evaluate(STATE_JS)
    await page.reload(wait_until="domcontentloaded")
    for _ in range(40):
        state = await page.evaluate(STATE_JS)
        if state and state["line"]:
            break
        await asyncio.sleep(0.05)
    assert state["line"] == "Opening the wards"
    assert (state["x"], state["y"]) == (where["x"], where["y"])


async def test_strict_page_csp_does_not_stop_it(make_keeper, show_on):
    keeper = await make_keeper(FakeHospital())
    page = await keeper.browser.active()
    await page.set_content("<!doctype html><html><head><meta http-equiv='Content-Security-Policy' content=\"default-src 'none'\">"
                           "</head><body><button id='top'>Top</button></body></html>")
    show = showme.for_browser(keeper.browser)
    before = await page.screenshot()
    await show.point(page.locator("#top"))
    await show.say("Opening the wards")
    await asyncio.sleep(0.8)
    assert await page.screenshot() == before
    assert await _paint(keeper) != before


async def test_overlay_failure_or_hang_does_not_stop_the_job(make_keeper, show_on, monkeypatch):
    async def broken(self, *a):
        raise RuntimeError("overlay blew up")

    async def stuck(self, *a):
        await asyncio.sleep(60)

    keeper = await make_keeper(FakeHospital())
    monkeypatch.setattr(showme.ShowMe, "_point", broken)
    monkeypatch.setattr(showme.ShowMe, "_say", broken)
    result = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": UHID})
    assert result.status == "FOUND", (result.failed_step, result.reason)

    page = await keeper.browser.active()
    monkeypatch.setattr(showme.ShowMe, "_point", stuck)
    show = showme.for_browser(keeper.browser)
    started = time.monotonic()
    await show.point(page.locator("body"))
    assert time.monotonic() - started < showme.POINT_LIMIT_S + 1


async def test_gone_element_does_not_raise(make_keeper, show_on):
    keeper = await make_keeper(FakeHospital())
    page = await keeper.browser.active()
    await page.set_content(BUTTONS)
    show = showme.for_browser(keeper.browser)
    await show.point(page.locator("#not-there"))
    assert show.x is None


async def test_off_touches_nothing(make_keeper, show_off, monkeypatch):
    keeper = await make_keeper(FakeHospital())
    assert showme.for_browser(keeper.browser) is showme.OFF
    result = await keeper.run_skill("lis.fetch_lab_reports", {"uhid": UHID})
    assert result.status == "FOUND", (result.failed_step, result.reason)
    page = await keeper.browser.active()
    assert await page.evaluate(STATE_JS) is None
    assert await page.locator(showme.TAG).count() == 0
    assert getattr(keeper.browser, "_showme", None) is None
    started = time.monotonic()
    for _ in range(100):
        await showme.OFF.point(page.locator("body"))
        await showme.OFF.say("anything")
        await showme.OFF.step({"action": "click", "intent": "anything"})
    assert time.monotonic() - started < 0.05


class ScriptedModels:
    name = "scripted"

    def __init__(self):
        self.images = []
        self.lines_at_call = []

    async def json(self, prompt, images=(), timeout=0):
        self.images.append(images[0][0])
        if len(self.images) == 1:
            box = next(line for line in prompt.splitlines() if line.startswith("[") and " textbox " in line)
            return {"action": "type", "element": int(box[1:box.index("]")]), "text": UHID, "thought": "type it"}
        if len(self.images) == 2:
            return {"action": "scroll_down", "thought": f"patient {UHID} looks fine"}
        return {"action": "done", "answer": "ok", "thought": f"finished with {UHID}"}


async def test_explorer_shows_fixed_words_only(make_keeper, show_on):
    keeper = await make_keeper(FakeHospital())
    log = RunLog.create(keeper.settings.runs_dir, "test-explore", console=False)
    await keeper.ensure_healthy(log)
    models = ScriptedModels()
    explorer = Explorer(keeper, models, log, max_steps=5, minutes=2)
    explorer.show.lines.clear()
    result = await explorer.run("type the number", start_here=True)
    assert result.status == "DONE"
    show = showme.for_browser(keeper.browser)
    assert set(show.lines) >= {"Typing in a box", "Scrolling down", "Looking at the page"}
    assert set(show.lines) <= set(showme.EXPLORER_WORDS.values())
    page = await keeper.browser.active()
    assert (await page.evaluate(STATE_JS))["x"] is not None
    assert await page.locator("[data-wardos-overlay]").count() == 0
    assert len(models.images) == 3

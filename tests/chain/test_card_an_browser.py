"""The patient screen in real Chrome at phone size: voice, no internet, one page at a time, the pill."""
import re

from .test_beds import stay
from .test_fix_up_browser import browser_world, chrome_once, end_instead_of_hanging, say, screen_page  # noqa: F401
from wardos.models import Usage

STUB = "export default async function(f) {return f}"


def phone(browser, workers=False):
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                  service_workers="allow" if workers else "block")
    context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(content_type="text/css", body=""))
    context.route("https://fonts.gstatic.com/**", lambda route: route.abort())
    context.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(content_type="application/javascript", body=STUB))
    page = context.new_page()
    page.add_init_script("delete window.SpeechRecognition; delete window.webkitSpeechRecognition;")
    return context, page


def test_voice_uses_the_phones_own_speech_first_and_shows_words_live(browser_world, monkeypatch):
    from playwright.sync_api import expect
    from wardos.chain import voice
    api, r, aid, oid, url, browser = browser_world
    heard = []

    async def words(model, audio, mime, **kwargs):
        heard.append(len(audio))
        return "never used", Usage("fake", cost_inr=0)
    monkeypatch.setattr(voice, "transcribe", words)
    context, page = phone(browser)
    page.add_init_script("""window.__started = 0;
      window.SpeechRecognition = window.webkitSpeechRecognition = class {
        start() { window.__started++; setTimeout(() => { this.onresult?.({ results: [[{ transcript: 'Lilavati Five This' }]] }); }, 10); }
        stop() { this.onend?.(); } abort() {} };""")
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#micBtn").click()
    expect(page.locator("#micBtn")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#pillText")).to_have_text("Lilavati Five This")
    expect(page.locator("#camBtn")).to_be_visible()
    page.locator("#micBtn").click()
    expect(page.locator("#askIn")).to_have_value("Lilavati Five This")
    assert not heard and page.evaluate("window.__started") == 1
    context.close()


def test_voice_recording_goes_to_muse_when_the_browser_has_no_speech(browser_world, monkeypatch):
    from playwright.sync_api import expect
    from wardos.chain import voice
    api, r, aid, oid, url, browser = browser_world
    heard = []

    async def words(model, audio, mime, **kwargs):
        heard.append(len(audio))
        return "new patient bed 17 test Ram 40 male", Usage("fake", cost_inr=0)
    monkeypatch.setattr(voice, "transcribe", words)
    context, page = phone(browser)
    asks = []
    page.on("request", lambda q: asks.append(q.url) if q.url.endswith("/ask") else None)
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#micBtn").click()
    expect(page.locator("#micBtn")).to_have_class(re.compile(r"\bon\b"))
    page.wait_for_timeout(300)
    page.locator("#micBtn").click()
    expect(page.locator("#askIn")).to_have_value("new patient bed 17 test Ram 40 male")
    expect(page.locator("#voiceHint")).to_have_text("Check, then Send")
    assert heard and heard[0] > 100 and not asks
    page.locator("#pill").evaluate("f => f.requestSubmit()")
    expect(page.locator("#nGo")).to_be_visible()
    expect(page.locator("#n-bed")).to_have_value("17")
    expect(page.locator("#n-name")).to_have_value("Test Ram")
    expect(page.locator("#n-age")).to_have_value("40")
    expect(page.locator("#n-sex")).to_have_value("M")
    expect(page.locator("#voiceHint")).to_have_count(0)
    assert len(asks) == 1
    context.close()


def test_no_internet_is_plain_words_and_it_goes_on_by_itself(browser_world, tmp_path):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    context, page = phone(browser, workers=True)
    page.goto(url, wait_until="domcontentloaded")
    page.evaluate("() => navigator.serviceWorker.ready")
    page.goto(url + "/?w=1#/a/" + aid, wait_until="domcontentloaded")
    expect(page.locator("#bedBtn")).to_contain_text("Bed 5")
    page.wait_for_function("() => !!navigator.serviceWorker.controller")
    page.evaluate("""() => { window.__t = []; new MutationObserver(() => window.__t.push(document.querySelector('#toast').textContent))
                      .observe(document.querySelector('#toast'), { childList: true, characterData: true, subtree: true }); }""")
    context.set_offline(True)
    page.evaluate("dispatchEvent(new Event('offline'))")
    expect(page.locator("#offline")).to_have_text("No internet. Will continue when you are back online.", timeout=15000)
    page.screenshot(path=str(tmp_path / "offline.png"))
    say(page, "reports bed 5")
    expect(page.locator("#askIn")).to_have_value("reports bed 5")
    page.locator('[data-act="pill-close"]').click()
    before = len(r.store.query("sources", admission_id=aid))
    page.locator("#files").set_input_files([{"name": "a.jpg", "mimeType": "image/jpeg", "buffer": b"\xff\xd8\xff" + b"1" * 400}])
    expect(page.locator("#pillText")).to_have_text("Waiting for internet")
    all_text = page.locator("body").inner_text()
    assert "TypeError" not in all_text and "FetchEvent" not in all_text and "Load failed" not in all_text
    assert len(r.store.query("sources", admission_id=aid)) == before
    context.set_offline(False)
    expect(page.locator("#offline")).to_be_hidden(timeout=15000)
    for _ in range(100):
        if len(r.store.query("sources", admission_id=aid)) == before + 1:
            break
        page.wait_for_timeout(100)
    assert len(r.store.query("sources", admission_id=aid)) == before + 1
    seen = " ".join(page.evaluate("() => window.__t"))
    assert "TypeError" not in seen and "FetchEvent" not in seen and "Could not load" not in seen
    context.close()


def test_a_reload_with_no_internet_still_opens_the_page(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    context, page = phone(browser, workers=True)
    page.goto(url, wait_until="domcontentloaded")
    page.evaluate("() => navigator.serviceWorker.ready")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => !!navigator.serviceWorker.controller")
    expect(page.locator("#askOpen")).to_be_visible()
    context.set_offline(True)
    context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#offline")).to_be_visible(timeout=15000)
    expect(page.locator("#askOpen")).to_be_visible()
    context.set_offline(False)
    expect(page.locator("#offline")).to_be_hidden(timeout=15000)
    expect(page.locator(".ward-top")).to_contain_text("1 patient")
    context.close()


def test_a_new_answer_is_a_page_and_never_piles_up(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    context, page = phone(browser)
    page.goto(url, wait_until="domcontentloaded")
    expect(page.locator("#wardList")).to_be_visible()

    def ask(words):
        with page.expect_response("**/ask"):
            say(page, words)
        expect(page.locator("#askOpen")).to_be_visible()
    for _ in range(3):
        ask("plan bed 5")
        expect(page.locator(".col:not(.hide) .page")).to_have_count(1, timeout=2000)
    expect(page.locator(".title")).to_have_count(1)
    assert page.locator(".acard").count() == 0
    context.close()


def test_a_dangerous_value_shows_the_whole_unit_on_the_first_page(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    context, page = phone(browser)
    alerts = [{"kind": "danger", "title": "Chloride past danger line", "name": "Chloride", "value": "82", "unit": "mmol/L",
               "was": "91", "go": "tests", "key": "chloride"}]

    def today(route):
        body = route.fetch().json()
        body["alerts"] = alerts
        route.fulfill(json=body)
    page.route(f"**/admissions/{aid}/today", today)
    page.goto(url + f"/#/a/{aid}/alert", wait_until="domcontentloaded")
    expect(page.locator("#alertBox")).to_contain_text("Chloride 82")
    expect(page.locator("#alertBox")).to_contain_text("was 91")
    page.unroute_all(behavior="ignoreErrors")
    context.close()


def test_a_page_not_read_stops_the_line_and_offers_try_again(browser_world, monkeypatch):
    from playwright.sync_api import expect
    from wardos.chain import worker
    api, r, aid, oid, url, browser = browser_world

    async def bad_reply(*args, **kwargs):
        raise RuntimeError("extraction reply has no facts list (object with ['note'])")

    async def read_ok(model, photos, **kwargs):
        return ["Date 21/10\nPR 96/min"] * len(photos), Usage("fake", cost_inr=0)
    monkeypatch.setattr(worker, "extract_facts", bad_reply)
    monkeypatch.setattr(worker, "read_photos", read_ok)
    context, page = phone(browser)
    page.goto(url + "/#/a/" + aid, wait_until="domcontentloaded")
    expect(page.locator("#bedBtn")).to_contain_text("Bed 5")
    page.locator("#files").set_input_files([{"name": "a.jpg", "mimeType": "image/jpeg", "buffer": b"\xff\xd8\xff" + b"2" * 400}])
    expect(page.locator("#toast")).to_contain_text("1 photo not read - Try again", timeout=20000)
    row = page.locator("#stuckBtn")
    expect(row).to_contain_text("Try again")
    monkeypatch.undo()
    row.click()
    expect(page.locator("#stuckBtn")).to_have_count(0, timeout=15000)
    context.close()

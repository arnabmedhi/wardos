"""Offline real-Chrome regressions on the patient screen: made-up patients, SQLite, loopback, no paid model or cloud."""
import faulthandler
import os
import socket
import sys
import threading
import time

import pytest

from .test_api import _client
from .test_beds import stay
from .test_fix_up import old_stay

TEST_LIMIT_S = 240


@pytest.fixture(autouse=True)
def end_instead_of_hanging():
    def give_up():
        print(f"\nSTOPPED: this real-Chrome test passed its {TEST_LIMIT_S} s limit, so Chrome or the local test site "
              "stopped answering. Close other Chrome test runs and run this file alone.", file=sys.stderr, flush=True)
        faulthandler.dump_traceback(file=sys.stderr)
        os._exit(3)
    timer = threading.Timer(TEST_LIMIT_S, give_up)
    timer.daemon = True
    timer.start()
    yield
    timer.cancel()


def screen_page(browser, width, height):
    from playwright.sync_api import expect
    expect.set_options(timeout=15000)
    page = browser.new_page(viewport={"width": width, "height": height}, service_workers="block")
    page.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(content_type="text/css", body=""))
    page.route("https://fonts.gstatic.com/**", lambda route: route.abort())
    page.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(
        content_type="application/javascript", body="export default async function(f) {return f}"))
    page.add_init_script("delete window.SpeechRecognition; delete window.webkitSpeechRecognition;")  # tests of the Muse fallback
    return page


def say(page, words):
    page.locator("#askOpen").click()
    page.locator("#askIn").fill(words)
    page.locator("#pill").evaluate("f => f.requestSubmit()")


@pytest.fixture(scope="module")
def chrome_once():
    from playwright.sync_api import sync_playwright
    from wardos.config import find_chrome
    if not find_chrome():
        pytest.skip("Installed Chrome is required")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True, timeout=20000,
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        yield browser
        browser.close()


@pytest.fixture
def browser_world(tmp_path, chrome_once):
    import uvicorn
    browser = chrome_once
    api = _client(tmp_path)
    r = api.app.state.record
    aid = stay(api, "Test Rina", "5")["id"]
    oid = old_stay(api)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(.01)
    yield api, r, aid, oid, f"http://127.0.0.1:{port}", browser
    for context in list(browser.contexts):
        context.close()
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "the test server did not stop within 10 s"


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_old_stay_is_read_only_and_saves_paint_first_and_roll_back(browser_world, monkeypatch, width, height):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    page = screen_page(browser, width, height)
    page.goto(url + "/#/a/" + oid, wait_until="domcontentloaded")
    expect(page.locator("#askOpen")).to_have_text("Ask about Test's old stay...")
    who = page.locator(".who").first
    expect(who).to_contain_text("2026-10-20 to 2026-10-23")
    assert page.locator("#camBtn").count() == 0 and page.locator("#bedBtn").count() == 0
    assert page.locator(".sev").count() == 0 and page.locator("#getReports").count() == 0
    expect(page.locator("body")).not_to_contain_text("Nothing pending")
    page.goto(url + "/?current=1#/a/" + aid, wait_until="domcontentloaded")
    expect(page.locator("#bedBtn")).to_contain_text("Bed 5")
    page.locator(".sev").click()
    button = page.locator('[data-s="watch"]')
    real = r.set_severity
    gate = threading.Event()

    def slow(*args, **kwargs):
        assert gate.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(r, "set_severity", slow)
    shown = button.evaluate("b => {const t=performance.now(); b.click(); return performance.now()-t}")
    assert shown < 300
    expect(page.locator(".sev")).to_have_text("Watch")
    assert r.admission(aid).get("severity") is None
    gate.set()
    for _ in range(100):
        if r.admission(aid).get("severity") == "watch":
            break
        time.sleep(.02)
    assert r.admission(aid)["severity"] == "watch"
    page.route("**/admissions/*/severity", lambda route: route.fulfill(status=503, json={"detail": "unavailable"}))
    page.locator(".sev").click()
    page.locator('[data-s="unstable"]').click()
    expect(page.locator("#toast")).to_have_text("Not saved - try again")
    expect(page.locator(".sev")).to_have_text("Watch")
    assert r.admission(aid)["severity"] == "watch"
    page.route("**/admissions/*", lambda route: route.fulfill(status=503, json={"detail": "unavailable"})
               if route.request.method == "PATCH" else route.continue_())
    page.locator("#bedBtn").click()
    page.locator("#bedIn").fill("9")
    page.locator("#bedGo").click()
    expect(page.locator("#toast")).to_have_text("Not saved - try again")
    assert r.admission(aid)["bed"] == "5"
    page.close()


def test_a_question_opens_before_the_ward_list_is_ready(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    api.post(f"/admissions/{aid}/notes", json={"text": "Date 20/10\nPR 88/min"})
    api.post(f"/admissions/{aid}/notes", json={"text": "Date 20/10\nPR 120/min"})
    page = screen_page(browser, 1440, 900)
    page.route("**/admissions", lambda route: route.fulfill(json=[]))
    page.goto(url + "/#/a/" + aid, wait_until="domcontentloaded")
    page.locator(".flagline.amber").first.click()
    expect(page.locator(".opt")).to_have_count(2)
    page.close()


def test_a_failed_recording_can_be_tried_again_and_nothing_is_sent_alone(browser_world, monkeypatch):
    from playwright.sync_api import expect
    from wardos.chain import voice, model as m
    from wardos.models import Usage
    api, r, aid, oid, url, browser = browser_world
    called = []

    async def words(model, audio, mime, **kwargs):
        assert len(audio) > 100 and mime == "audio/webm"
        called.append(audio)
        return "नया मरीज बेड १९ नाम आशा", Usage("fake", cost_inr=0)
    monkeypatch.setattr(voice, "transcribe", words)
    page = screen_page(browser, 390, 844)
    page.goto(url, wait_until="domcontentloaded")
    expect(page.locator("#micBtn")).to_be_visible()
    before = len(r.store.query(m.SOURCES))
    page.locator("#micBtn").click()
    expect(page.locator("#micBtn")).to_have_class(__import__("re").compile(r"\bon\b"))
    page.wait_for_timeout(300)
    page.route("**/voice/transcribe", lambda route: route.fulfill(status=503, json={"detail": "try again"}), times=1)
    page.locator("#micBtn").click()
    page.locator("#voiceRetry").click()
    expect(page.locator("#askIn")).to_have_value("नया मरीज बेड १९ नाम आशा")
    expect(page.locator("#voiceHint")).to_have_text("Check, then Send")
    assert len(called) == 1 and len(r.store.query(m.SOURCES)) == before
    assert page.locator("#nGo").count() == 0
    page.locator("#pill").evaluate("f=>f.requestSubmit()")
    expect(page.locator("#nGo")).to_be_visible()
    page.close()


def test_answering_on_one_patient_leaves_the_other_alone(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    bid = stay(api, "Test Other", "9")["id"]
    for target in (aid, bid):
        for value in (88, 120):
            api.post(f"/admissions/{target}/notes", json={"text": f"Date 20/10\nPR {value}/min"})
    page = screen_page(browser, 390, 844)
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.url.endswith("/answer") else None)
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#tileNeeds").click()                                  # the Needs you tile
    page.locator('[data-act="open-q"]').filter(has_text="Bed 9").first.click()
    page.locator(".opt").first.click()
    expect(page.locator("#toast")).to_contain_text("Saved")
    assert len(r.questions(aid)) == 1 and not r.questions(bid) and len(writes) == 1
    page.close()


def test_many_questions_are_one_line_that_opens_them(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    for day, values in (("20/10", (88, 120)), ("21/10", (90, 130))):
        for value in values:
            api.post(f"/admissions/{aid}/notes", json={"text": f"Date {day}\nPR {value}/min"})
    assert len(r.questions(aid)) == 2
    page = screen_page(browser, 390, 844)
    page.goto(url + "/#/a/" + aid, wait_until="domcontentloaded")
    expect(page.locator("#questionsLine")).to_have_text("2 questions for you")
    expect(page.locator(".flagline.amber")).to_have_count(1)
    page.locator("#questionsLine").click()
    expect(page.locator("#questionList .ln")).to_have_count(2)
    page.locator("#questionList .ln").first.click()
    page.locator(".opt").first.click()
    expect(page.locator("#questionList .ln")).to_have_count(1)
    page.locator("#questionList .ln").first.click()
    page.locator(".opt").first.click()
    expect(page.locator("#questionList")).to_contain_text("Nothing is waiting")
    assert not r.questions(aid)
    page.close()


def test_owner_test_alert_button_says_plainly_what_happened(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    page = screen_page(browser, 390, 844)
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#menuBtn").click()
    for reply, words in [(dict(status=200, json={"sent": 2, "failed": 0}), "sent to 2 devices"),
                         (dict(status=200, json={"sent": 0, "failed": 0}), "No device has alerts on"),
                         (dict(status=503, json={"detail": "not configured"}), "not set up")]:
        page.route("**/push/test", lambda route, request=None, reply=reply: route.fulfill(**reply))
        page.locator("#testAlertBtn").click()
        expect(page.locator("#toast")).to_contain_text(words)
        page.unroute("**/push/test")
    page.route("**/auth/me", lambda route, request=None: route.fulfill(json={"email": "d@x.test", "name": "Dr Other", "role": "doctor"}))
    page.goto(url + "/?other=1", wait_until="domcontentloaded")
    expect(page.locator("#menuBtn")).to_have_text("O")
    page.locator("#menuBtn").click()
    expect(page.locator("text=Sign out")).to_be_visible()
    assert page.locator("#testAlertBtn").count() == 0 and page.locator("#docsBtn").count() == 0
    page.close()


@pytest.fixture
def held_world(tmp_path, chrome_once):
    import uvicorn
    from .test_card_ak import world
    api, r, aid = world(tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(api.app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(.01)
    yield api, r, aid, f"http://127.0.0.1:{sock.getsockname()[1]}", chrome_once
    for context in list(chrome_once.contexts):
        context.close()
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "the test server did not stop within 10 s"


def open_hold_page(page, url, aid):
    from playwright.sync_api import expect
    page.goto(url + "/#/a/" + aid, wait_until="domcontentloaded")
    page.locator(".flagline.amber").filter(has_text="another patient").click()
    expect(page.locator("#qText")).to_contain_text("This page shows a different name. Hold it?")
    expect(page.locator(".opt-b")).to_have_count(2)


def test_it_is_this_patient_adds_the_page_in_real_chrome_phone(held_world):
    from playwright.sync_api import expect
    api, r, aid, url, browser = held_world
    page = screen_page(browser, 390, 844)
    answers = []
    page.on("request", lambda request: answers.append(request.url) if request.url.endswith("/answer") else None)
    open_hold_page(page, url, aid)
    assert page.locator(".opt-b").all_inner_texts() == ["Hold it", "It is this patient"]
    expect(page.locator("#qNote")).to_have_count(0)
    expect(page.locator("#qphoto, .notebox")).to_have_count(1)
    before = len(r.facts(aid))
    page.get_by_role("button", name="It is this patient", exact=True).click()
    expect(page.locator("#toast")).to_contain_text("2 values from this page added to the record")
    assert len(answers) == 1 and len(r.facts(aid)) == before + 2
    assert r.questions(aid) == [] or all(q["kind"] != "wrong_patient" for q in r.questions(aid))
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(500)
    assert len(r.facts(aid)) == before + 2
    page.close()


def test_hold_it_adds_nothing_in_real_chrome_phone(held_world):
    from playwright.sync_api import expect
    api, r, aid, url, browser = held_world
    page = screen_page(browser, 390, 844)
    open_hold_page(page, url, aid)
    before = len(r.facts(aid))
    page.get_by_role("button", name="Hold it", exact=True).click()
    expect(page.locator("#toast")).to_contain_text("Saved")
    assert len(r.facts(aid)) == before
    page.close()


def test_a_page_held_before_the_fix_says_its_values_cannot_be_added(held_world):
    from playwright.sync_api import expect
    api, r, aid, url, browser = held_world
    q = next(q for q in r.questions(aid) if q["kind"] == "wrong_patient")
    r.store.put("questions", {k: v for k, v in q.items() if k != "values_kept"})
    import sqlite3
    from wardos.chain import model as m
    with sqlite3.connect(str(r.store.path)) as conn:
        conn.execute("DELETE FROM docs WHERE coll = ? AND id = ?", (m.HELD, m.stable_id("held", q["page_id"])))
    page = screen_page(browser, 390, 844)
    open_hold_page(page, url, aid)
    expect(page.locator("#qNote")).to_contain_text("cannot be added to the record")
    before = len(r.facts(aid))
    page.get_by_role("button", name="It is this patient", exact=True).click()
    expect(page.locator("#toast")).to_contain_text("could not be added")
    assert len(r.facts(aid)) == before
    page.close()


def test_urea_shows_without_the_flag_letter_in_real_chrome_phone(held_world):
    from playwright.sync_api import expect
    api, r, aid, url, browser = held_world
    q = next(q for q in r.questions(aid) if q["kind"] == "wrong_patient")
    api.post(f"/questions/{q['id']}/answer", json={"answer": "It is this patient"})
    urea = next(f for f in r.facts(aid) if f["key"] == "urea")
    r.store.put("facts", dict(urea, value=dict(urea["value"], text="H 426")))
    page = screen_page(browser, 390, 844)
    page.goto(url + "/#/a/" + aid, wait_until="domcontentloaded")
    page.locator('[data-go="tests"]').first.click()
    expect(page.locator("body")).to_contain_text("426")
    assert "H 426" not in page.locator("body").inner_text()
    page.close()

""" ward view, patient overview, sections on the right, Tasks + work panel, photos "+", discharge, alerts.
Real taps in real Chrome on made-up patients (local site). Screenshots go outside git, to C:\\tmp\\bg\\."""
import tempfile
import os
import re

from .test_fix_up_browser import browser_world, chrome_once, end_instead_of_hanging, screen_page  # noqa: F401
from .test_beds import stay

SHOTS = tempfile.gettempdir()
TASK = {"id": "t1", "kind": "get_reports", "status": "working", "active": True, "bed": "5", "job": "Fetch lab reports",
        "steps": ["23:15 · Signing in to eHospital", "23:15 · Signed in to eHospital", "23:16 · Opening lab reports"],
        "health": "", "created_at": "2026-11-02T17:45:00+00:00", "time": "23:15"}
WAITING = {"id": "t2", "kind": "get_reports", "status": "waiting", "active": True, "bed": "9", "steps": [],
           "health": "The hospital robot is starting. This begins in about a minute.", "created_at": "2026-11-02T17:40:00+00:00", "time": "23:10"}


def shot(page, name):
    os.makedirs(SHOTS, exist_ok=True)
    page.screenshot(path=os.path.join(SHOTS, "test_" + name + ".png"))


def seed(api, aid):
    api.post(f"/admissions/{aid}/notes", json={"text": "Date 20/10\nPR 88/min\nHb 9.0 g/dL\nPotassium 4.1 mmol/L"})
    api.post(f"/admissions/{aid}/notes", json={"text": "Date 21/10\nPR 120/min\nHb 7.1 g/dL\nPotassium 2.6 mmol/L"})


def tasks_route(page, aid, tasks):
    for t in tasks:
        t["admission_id"] = aid
    page.route(re.compile(r".*/agent/tasks(\?.*)?$"), lambda route: route.fulfill(json={"tasks": tasks}))
    page.route(re.compile(r".*/agent/tasks/t\d$"), lambda route: route.fulfill(json=next(t for t in tasks if route.request.url.endswith(t["id"]))))


def test_laptop_ward_view_overview_and_sections_replace_the_right_side(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    seed(api, aid)
    page = screen_page(browser, 1366, 860)
    page.goto(url, wait_until="domcontentloaded")
    expect(page.locator("#wardToday")).to_contain_text("Your ward today")
    assert page.locator("#wardTiles .tile").count() == 4
    for box in ("#summaryNeeds", "#summaryNew"):
        expect(page.locator(box)).to_be_visible()
    expect(page.locator("#summaryHints")).to_contain_text("Try asking:")
    expect(page.locator("#summaryTasks")).to_have_count(0)                         # Tasks: tile only, not a block
    assert "Keep watching" not in page.locator("body").inner_text()
    expect(page.locator("#summaryHints")).to_contain_text("Who needs me now?")
    shot(page, "laptop_ward_view")
    # opening a patient shows the overview (not the Tests page)
    page.locator("#wardList .pt", has_text="Test Rina").click()
    expect(page.locator("#overview")).to_be_visible()
    expect(page.locator("#tableBtn")).to_have_count(0)
    for box in ("#ovVitals", "#ovTests", "#ovMeds", "#ovPlan", "#ptTasks", "#ptDischarge", "#ovRecord", "#ovFiles"):
        expect(page.locator(box)).to_be_visible()
    v, t = page.locator("#ovVitals").bounding_box(), page.locator("#ovTests").bounding_box()
    assert t["x"] > v["x"] + 200 and abs(t["y"] - v["y"]) < 5                     # previews in 2 columns
    expect(page.locator("#ovMeds")).to_contain_text("No medicines in the record yet")  # empty = one small line
    assert page.locator("#ovMeds").bounding_box()["height"] < 60
    shot(page, "laptop_overview")
    # a section replaces the right side; "<- name" goes back; never 3 columns
    page.locator("#ovTests .sech").click()
    expect(page.locator(".back")).to_contain_text("Test Rina")
    expect(page.locator(".who")).to_have_count(0)                                  # the old page slides out
    assert page.locator(".col:not(.hide)").count() == 2
    expect(page.locator(".title", has_text="Tests")).to_be_visible()
    shot(page, "laptop_tests_section")
    page.locator(".back").click()
    page.wait_for_timeout(400)                                                    # the slide ends
    expect(page.locator("#overview")).to_be_visible()
    # discharge from the patient page opens the discharge screen (no jump back), no entry counts
    page.locator("#ptDischarge").click()
    expect(page.locator(".title", has_text="Discharge summary")).to_be_visible()
    expect(page.locator("#finalGo")).to_be_visible()
    assert "Entries used" not in page.locator("body").inner_text()
    shot(page, "laptop_discharge")
    page.locator(".back").click()
    page.wait_for_timeout(400)                                                    # the slide ends
    page.locator("#ovRecord").click()
    page.locator("#recordSheet ~ div [data-go='discharge']").click()
    expect(page.locator(".title", has_text="Discharge summary")).to_be_visible()
    assert page.locator(".col:not(.hide)").count() == 2
    page.close()


def test_tasks_list_work_panel_and_plain_words(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    seed(api, aid)
    page = screen_page(browser, 1366, 860)
    tasks_route(page, aid, [dict(TASK), dict(WAITING)])
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#tileTasks").click()
    expect(page.locator("#taskList .task")).to_have_count(2)
    body = page.locator("#taskList").inner_text()
    assert "Fetch lab reports" in body and "hospital agent is starting" in body
    assert "robot" not in body.lower() and "demo" not in body.lower()
    page.locator("#taskList .task").first.click()
    expect(page.locator("#taskSteps .ok")).to_have_count(2)                       # done steps ticked
    expect(page.locator("#taskSteps .cur")).to_contain_text("Opening lab reports")  # the live step
    expect(page.locator("#taskStop")).to_be_visible()
    shot(page, "laptop_work_panel")
    # tapping something else shows that: the panel never sticks
    page.locator("#wardList .pt", has_text="Test Rina").click()
    expect(page.locator("#overview")).to_be_visible()
    expect(page.locator("#taskSteps")).to_have_count(0)
    expect(page.locator("#ptTasks .cnt")).to_have_text("2")                       # this patient's tasks
    page.close()


def test_phone_ward_tiles_photos_plus_and_reading_stays_with_its_patient(browser_world, tmp_path):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    seed(api, aid)
    stay(api, "Test Other", "9")
    page = screen_page(browser, 390, 844)
    page.goto(url, wait_until="domcontentloaded")
    tiles = page.locator("#wardTiles .tile")
    expect(tiles).to_have_count(4)
    for i in range(4):
        expect(tiles.nth(i)).to_be_visible()
    page.wait_for_timeout(600)                                                    # the first renders settle
    a, b, c = (tiles.nth(i).bounding_box() for i in range(3))
    assert abs(a["y"] - b["y"]) < 3 and c["y"] > a["y"] + 40                      # 2 x 2
    shot(page, "phone_ward_view")
    page.locator("#wardList .pt", has_text="Test Rina").click()
    expect(page.locator("#overview")).to_be_visible()
    page.locator("#ovFiles").click()
    expect(page.locator("#filesAdd")).to_be_visible()
    photo = tmp_path / "p.png"
    photo.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                                    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"))
    page.route("**/admissions/*/uploads", lambda route: route.fulfill(json={"queued": 1, "files": [{"source_id": "src_fake"}]}))
    with page.expect_file_chooser() as chooser:
        page.locator("#filesAdd").click()
    chooser.value.set_files(str(photo))
    expect(page.locator("#pillText")).to_contain_text("Reading")
    expect(page.locator("#camBtn")).to_be_visible()                               # "+" stays while reading
    shot(page, "phone_reading_plus")
    page.locator(".page:last-child .back").click()
    expect(page.locator("#overview")).to_have_count(1)
    page.locator(".page:last-child .back").click()
    page.locator("#wardList .pt", has_text="Test Other").click()
    expect(page.locator(".who h2")).to_contain_text("Test Other")
    expect(page.locator("#askOpen")).to_be_visible()                              # not the other patient's reading
    assert "Reading" not in page.locator("#pill").inner_text()
    page.close()


def test_iphone_tab_explains_alerts_and_sex_has_other(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, service_workers="block",
                              user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
    ctx.add_init_script("delete window.PushManager;")                              # a Safari tab has no push
    page = ctx.new_page()
    page.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(content_type="text/css", body=""))
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#menuBtn").click()
    page.locator("#phoneAlerts").click()
    expect(page.locator("#alertHelp")).to_contain_text("Add to Home Screen")
    assert not page.locator("#phoneAlerts").is_checked()
    shot(page, "iphone_alerts_explained")
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#wardAdd").click()
    page.locator("#nType").click()
    assert page.locator("#n-sex option").all_inner_texts()[1:] == ["Male", "Female", "Other"]
    page.close()


def test_a_watch_with_a_missing_detail_asks_one_short_question_with_tap_answers(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    page = screen_page(browser, 390, 844)
    seen = []
    ASK = {"text": "", "intent": {}, "card": {"kind": "ask_detail", "title": "WardOS agent", "patient": None, "text": "Check how often?",
           "options": [{"label": "15 min", "add": "every 15 minutes"}, {"label": "1 hour", "add": "every 60 minutes"}]}}
    DONE = {"text": "", "intent": {}, "card": {"kind": "message", "title": "x", "patient": None, "text": "ok"}}

    def answer(route):
        seen.append(route.request.post_data_json["text"])
        route.fulfill(json=ASK if len(seen) == 1 else DONE)
    page.route("**/ask", answer)
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#askOpen").click()
    page.locator("#askIn").fill("watch bed 5 potassium till 8 am")
    page.locator("#pill").evaluate("f => f.requestSubmit()")
    expect(page.locator("#detailQ")).to_have_text("Check how often?")
    expect(page.locator("#detailOpts button")).to_have_count(2)
    page.locator("#detailOpts button", has_text="15 min").click()
    expect(page.locator("#detailQ")).to_have_count(0)
    assert seen[1] == "watch bed 5 potassium till 8 am every 15 minutes"
    page.close()


CABLE_MIC = """(() => {
  const devices = [{ deviceId: 'default', kind: 'audioinput', label: 'Default - CABLE Output (VB-Audio Virtual Cable)' },
                   { deviceId: 'cable', kind: 'audioinput', label: 'CABLE Output (VB-Audio Virtual Cable)' },
                   { deviceId: 'real', kind: 'audioinput', label: 'Microphone Array (Realtek)' }];
  const make = (loud) => { const ctx = new AudioContext(); const dest = ctx.createMediaStreamDestination(); const o = ctx.createOscillator(), g = ctx.createGain();
    g.gain.value = loud ? 0.5 : 0; o.frequency.value = 220; o.connect(g); g.connect(dest); o.start(); return dest.stream; };
  window.__asked = [];
  navigator.mediaDevices.enumerateDevices = async () => devices;
  navigator.mediaDevices.getUserMedia = async (c) => { const id = (c.audio && c.audio.deviceId && (c.audio.deviceId.exact || c.audio.deviceId)) || 'default'; window.__asked.push(id); return make(id === 'real'); };
  window.SpeechRecognition = window.webkitSpeechRecognition = class { start() {} stop() {} abort() {} };   // browser speech that never produces a word
})();"""


def test_a_virtual_cable_microphone_is_heard_as_silence_and_the_doctor_can_pick_another(browser_world):
    """A laptop with Edge set to the virtual 'CABLE Output' device, which gives silence."""
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    page = screen_page(browser, 1366, 860)
    page.add_init_script(CABLE_MIC)
    sent = []
    page.route("**/voice/transcribe", lambda route: (sent.append(route.request.headers.get("content-type", "")), route.fulfill(json={"text": "potassium of bed 5"}))[1])
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#micBtn").click()
    expect(page.locator("#meter")).to_be_visible()                                # a live sound meter while listening
    expect(page.locator("#pillText")).to_contain_text("Listening")
    expect(page.locator("#micMsg")).to_be_visible(timeout=8000)                   # no sound for 3 s: said in words
    assert page.locator("#micMsg").inner_text().startswith("No sound from your microphone. Your browser may be using the wrong one. Pick another below, or type.")
    names = page.locator("#micPick option").all_inner_texts()
    assert names[0].startswith("Pick a microphone") and "Microphone Array (Realtek)" in names[1] and names[-1].endswith("(virtual)")   # real first, cables last
    shot(page, "voice_cable_silence")
    page.locator("#micPick").select_option("real")                                 # pick another: records from it
    page.wait_for_timeout(1800)
    assert page.evaluate("window.__asked")[-1] == "real"
    expect(page.locator("#micMsg")).to_have_count(0)                               # sound is heard now
    page.locator("#micBtn").click()
    expect(page.locator("#askIn")).to_have_value("potassium of bed 5", timeout=8000)   # recorded, sent to Muse, words in the ask bar
    assert sent and "multipart" in sent[0]
    page.close()


def test_a_question_waits_behind_an_answer_bar_and_is_listed_as_a_question(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    seed(api, aid)
    page = screen_page(browser, 1366, 860)
    page.goto(url + "/#/a/" + aid, wait_until="domcontentloaded")
    expect(page.locator("#overview")).to_be_visible()
    say_it = page.locator("#askOpen"); say_it.click()
    page.locator("#askIn").fill("show the vitals")
    page.locator("#pill").evaluate("f => f.requestSubmit()")
    expect(page.locator("#pillText")).to_contain_text("Answer ready", timeout=15000)
    expect(page.locator("#overview")).to_be_visible()                              # it did not jump to the answer
    expect(page.locator(".title", has_text="Vitals")).to_have_count(0)
    page.locator("#agentBtn").click()
    row = page.locator("#taskList .task").first
    expect(row).to_contain_text("Question: show the vitals")                       # listed as a question, not a task
    expect(row).to_contain_text("Answered")
    row.click()
    expect(page.locator(".title", has_text="Vitals")).to_be_visible()              # View shows the answer
    page.close()

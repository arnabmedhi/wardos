"""UI fixes, checked by real taps in real Chrome (made-up patients, local site).
Screenshots go outside git, to C:\\tmp\\bd\\."""
import tempfile
import os
import re
import time

from .test_fix_up_browser import browser_world, chrome_once, end_instead_of_hanging, screen_page  # noqa: F401
from .test_beds import stay

SHOTS = tempfile.gettempdir()


def shot(page, name):
    os.makedirs(SHOTS, exist_ok=True)
    page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def seed(api, aid):
    api.post(f"/admissions/{aid}/notes", json={"text": "Date 20/10\nPR 88/min"})
    api.post(f"/admissions/{aid}/notes", json={"text": "Date 20/10\nPR 120/min"})
    api.post(f"/admissions/{aid}/notes", json={"text": "Date 21/10\nHb 7.1 g/dL\nPotassium 2.6 mmol/L"})


def test_laptop_ward_summary_row_labels_close_and_ward_actions(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    seed(api, aid)
    patient = api.post("/patients", json={"name": "Test Dayman"}).json()
    api.post("/admissions", json={"patient_id": patient["id"], "unit": "Surgery Unit 2", "bed": "7", "admitted_at": "2026-10-20"})
    page = screen_page(browser, 1366, 860)
    page.goto(url, wait_until="domcontentloaded")
    expect(page.locator("#wardList .pt").first).to_be_visible()
    # 5, 6: labelled numbers; nothing opens by itself; the ward summary shows "Needs you" first
    expect(page.locator("#wardList .pt .bed").first).to_contain_text("Bed")
    assert page.locator(".dayline").count() >= 1 and "in hospital" in page.locator(".dayline").first.inner_text()
    assert page.locator(".who").count() == 0
    expect(page.locator("#summaryNeeds")).to_be_visible()
    assert page.locator(".page .label").first.inner_text().startswith("Needs you")
    expect(page.locator("#wardAdd")).to_have_text("+ Add patient")
    shot(page, "laptop_ward_summary")
    # open a patient: header, today's plan, bold only the name
    page.locator("#wardList .pt").first.click()
    expect(page.locator(".who h2").first).to_contain_text("Test Rina")
    expect(page.locator("#closePatient")).to_be_visible()
    assert page.locator("#ovPlan .sech", has_text="Today's plan").count() == 1     # plan preview on the overview
    weights = page.evaluate("""() => { const w = e => getComputedStyle(e).fontWeight;
      return { meta: w(document.querySelector('.who .meta')), bed: w(document.querySelector('#bedBtn')), sev: w(document.querySelector('.sev')), name: w(document.querySelector('.who h2')) }; }""")
    assert int(weights["meta"]) <= 400 and int(weights["bed"]) <= 400 and int(weights["sev"]) <= 500 and int(weights["name"]) >= 700
    assert "Male" in page.locator(".who .meta").inner_text() or "Bed 5" in page.locator(".who .meta").inner_text()
    shot(page, "laptop_patient")
    # 7: tap the same row again closes; x closes
    page.locator("#wardList .pt").first.click()
    expect(page.locator(".who")).to_have_count(0)
    expect(page.locator("#summaryNeeds")).to_be_visible()
    page.locator("#wardList .pt").first.click()
    page.locator("#closePatient").click()
    expect(page.locator(".who")).to_have_count(0)
    # 8: a ward action while a patient is open unselects the patient
    page.locator("#wardList .pt").first.click()
    expect(page.locator(".who").first).to_be_visible()
    page.locator("#wardAdd").click()
    expect(page.locator(".who")).to_have_count(0)
    expect(page.locator("#nPhotos")).to_be_visible()
    expect(page.locator("#nType")).to_be_visible()
    assert page.locator("#n-bed").count() == 0                       # 19: fields hidden until "Type the details"
    assert page.locator("#n-unit option").count() == 10              # 20: every department and unit
    shot(page, "laptop_new_patient_choices")
    page.locator("#nType").click()
    expect(page.locator("#n-bed")).to_be_visible()
    assert "from the photos" not in page.content()
    page.close()


def test_laptop_record_two_columns_collapse_questions_and_table(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    seed(api, aid)
    page = screen_page(browser, 1366, 860)
    page.goto(url + "/#/a/" + aid, wait_until="domcontentloaded")
    expect(page.locator(".who").first).to_be_visible()
    page.locator("#ovRecord").click()
    expect(page.locator("#recordSheet")).to_be_visible()
    cols = page.locator("#recordSheet .recgrid > .recc")
    assert cols.count() == 2
    a, b = cols.nth(0).bounding_box(), cols.nth(1).bounding_box()
    assert b["x"] > a["x"] + 200 and abs(a["y"] - b["y"]) < 5          # side by side
    first = page.locator("#recordSheet .recsec").first
    assert "Final diagnosis" in first.inner_text()                       # 12: final diagnosis at the very top
    assert page.locator("#recQuestions").count() == 1                    # 14: open questions tappable
    shot(page, "laptop_record")
    head = page.locator("#recordSheet .recsec .rh").nth(1)
    head.click()
    expect(page.locator("#recordSheet .recsec .rh[aria-expanded='false']")).to_have_count(1)   # 13: collapse
    page.locator("#recQuestions").click()
    expect(page.locator("#questionList .ln").first).to_be_visible()
    page.locator("#questionList .ln").first.click()
    expect(page.locator("#qOtherBtn")).to_have_text("Other: type it")     # 15
    page.locator("#qOtherBtn").click()
    expect(page.locator("#qOtherIn")).to_be_visible()
    shot(page, "laptop_question_other")
    page.close()


def test_phone_pill_plus_menu_cancel_time_text_and_account(browser_world, monkeypatch):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    seed(api, aid)
    page = screen_page(browser, 390, 844)
    page.goto(url, wait_until="domcontentloaded")
    expect(page.locator("#wardList .pt").first).to_be_visible()
    assert page.locator(".who").count() == 0
    shot(page, "phone_ward")
    # 5 / 17: words on the ward button, labelled numbers
    expect(page.locator("#wardAdd")).to_have_text("+ Add patient")
    page.locator("#wardList .pt").first.click()
    expect(page.locator(".who").first).to_be_visible()
    shot(page, "phone_patient")
    # 18 / 21: one "+" that asks which
    page.locator("#camBtn").click()
    expect(page.locator("#addPhotos")).to_have_text("Photos for this patient")
    expect(page.locator("#addPatient")).to_have_text("New patient")
    shot(page, "phone_add_menu")
    page.locator("#addPatient").click()
    expect(page.locator("#nType")).to_be_visible()
    # 2: while the recording runs, the + stays and Cancel is there; Cancel ends it
    page.add_init_script("window.__x = 1")
    page.evaluate("""() => { window.SpeechRecognition = window.webkitSpeechRecognition = class { start() {} stop() { this.onend?.(); } abort() {} }; }""")
    page.locator("#micBtn").click()
    expect(page.locator("#pillCancel")).to_be_visible()
    expect(page.locator("#camBtn")).to_be_visible()
    shot(page, "phone_recording")
    page.locator("#pillCancel").click()
    expect(page.locator("#pillCancel")).to_have_count(0)
    expect(page.locator("#askOpen")).to_be_visible()
    # 22 and 23: account menu texts, nothing about the old engine or money
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#menuBtn").click()
    expect(page.locator("body")).to_contain_text("Get danger alerts on this phone")
    body = page.locator("body").inner_text()
    assert "Summary service" not in body and "Rs " not in body and "1,762" not in body
    shot(page, "phone_menu")
    page.close()


def test_a_waiting_robot_job_does_not_spin_and_can_be_cancelled(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    page = screen_page(browser, 390, 844)
    page.route("**/agent/tasks/*", lambda route: route.fulfill(json={
        "id": "t1", "kind": "get_reports", "status": "waiting", "active": True, "health": "The laptop worker is off. This starts when it is on.",
        "steps": [], "worker_online": False, "admission_id": aid}))
    page.goto(url + "/#/a/" + aid, wait_until="domcontentloaded")
    page.evaluate("""() => { }""")
    page.locator("#askOpen").click()
    page.locator("#askIn").fill("get bed 5's reports")
    page.route("**/ask", lambda route: route.fulfill(json={"text": "", "intent": {}, "card": {"kind": "agent", "title": "x", "patient": {"id": aid},
        "task": {"id": "t1", "kind": "get_reports", "status": "waiting", "active": True, "health": "The laptop worker is off. This starts when it is on.", "steps": [], "admission_id": aid}}}))
    page.locator("#pill").evaluate("f => f.requestSubmit()")
    expect(page.locator("#pillText")).to_contain_text("hospital agent is off")   # no "laptop worker"/"robot" on screen
    assert page.locator("#pill .spin").count() == 0
    expect(page.locator("#pillCancel")).to_be_visible()
    page.close()


def test_open_patient_speed(browser_world):
    api, r, aid, oid, url, browser = browser_world
    seed(api, aid)
    page = screen_page(browser, 390, 844)
    t0 = time.time()
    page.goto(url + "/#/a/" + aid, wait_until="domcontentloaded")
    page.locator("#ovPlan").wait_for()
    print(f"\nSPEED open straight on a patient: {time.time() - t0:.2f}s")
    page.close()

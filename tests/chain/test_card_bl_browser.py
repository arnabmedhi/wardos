r""" a `jobs` card gives one row per job and an honest count; an `ask_ai` card shows options + Other and replies
with `reply_to`; the bottom bar names the job, never a step. /ask is stubbed. Screenshots go outside git, to the temp folder."""
import tempfile
import os
import json
import re

from .test_fix_up_browser import browser_world, chrome_once, end_instead_of_hanging, screen_page  # noqa: F401


def job(i, aid, bed):
    return {"id": f"t{i}", "kind": "get_reports", "status": "working", "active": True, "bed": bed, "admission_id": aid,
            "steps": ["23:15 · Signing in to eHospital", "23:15 · Signed in to eHospital"], "health": "",
            "created_at": "2026-11-03T17:45:00+00:00", "time": "23:15"}


def test_jobs_card_rows_count_bar_and_ai_question_with_reply_to(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    tasks = [job(1, aid, "5"), job(2, aid, "6"), job(3, aid, "7")]
    posted, served = [], []                                  # no task is running until the jobs card arrives
    page = screen_page(browser, 1366, 860)
    page.route(re.compile(r".*/agent/tasks(\?.*)?$"), lambda route: route.fulfill(json={"tasks": served}))
    page.route(re.compile(r".*/agent/tasks/t\d$"), lambda route: route.fulfill(json=next(t for t in tasks if route.request.url.endswith(t["id"]))))

    def ask(route):
        body = json.loads(route.request.post_data or "{}")
        posted.append(body)
        if body.get("reply_to"):
            return route.fulfill(json={"text": "", "intent": {}, "card": {"kind": "jobs", "title": "none", "jobs": [], "skipped": []}})
        if "jobs" in body["text"]:
            served[:] = tasks
            card = {"kind": "jobs", "title": "3 things", "skipped": [], "jobs": [{"id": f"j{t['id']}", "kind": "get_reports", "label": "Fetch", "task": t, "answer": None, "error": None} for t in tasks]}
        else:
            card = {"kind": "ask_ai", "text": "Which potassium?", "options": [{"label": "Bed 5", "add": "bed 5"}, {"label": "Bed 6", "add": "bed 6"}], "allow_other": True, "context_id": "ctx1"}
        route.fulfill(json={"text": "", "intent": {}, "card": card})
    page.route("**/ask", ask)
    page.goto(url, wait_until="domcontentloaded")
    # an AI question first: options + Other; a tap posts the reply with reply_to
    page.locator("#askOpen").click()
    page.locator("#askIn").fill("potassium please")
    page.locator("#askGo").click()
    expect(page.locator("#detailQ")).to_contain_text("Which potassium?")
    expect(page.locator("#detailOpts button")).to_have_count(2)
    expect(page.locator("#aiOtherIn")).to_be_visible()
    page.screenshot(path=os.path.join(tempfile.gettempdir(), "test_bl_ask_ai.png"))
    page.locator("#detailOpts button", has_text="Bed 6").click()
    page.wait_for_timeout(500)
    assert any(p.get("reply_to") == "ctx1" and p["text"] == "Bed 6" for p in posted)
    page.wait_for_timeout(300)
    page.locator("#askOpen").click()
    page.locator("#askIn").fill("do three jobs")
    page.locator("#askGo").click()
    expect(page.locator("#taskList .task")).to_have_count(3)
    expect(page.locator("#agentBtn .cnt")).to_have_text("3")
    expect(page.locator("#pillText")).to_have_text("3 running")
    assert "Signed in" not in page.locator("#pill").inner_text()
    page.screenshot(path=os.path.join(tempfile.gettempdir(), "test_bl_jobs.png"))
    page.close()

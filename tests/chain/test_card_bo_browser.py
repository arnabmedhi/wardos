r""" a task with the planner's `ask` shows option buttons + Other on its task page; a tap posts reply_to = context_id;
the row says "Needs your answer"; the bar never says "Signed in". Endpoints stubbed. Screenshots go outside git, to the temp folder."""
import tempfile
import os
import json
import re

from .test_fix_up_browser import browser_world, chrome_once, end_instead_of_hanging, screen_page  # noqa: F401


def test_planner_question_shows_taps_and_replies_with_context_id(browser_world):
    from playwright.sync_api import expect
    api, r, aid, oid, url, browser = browser_world
    task = {"id": "t1", "kind": "get_reports", "status": "failed", "active": False, "bed": "5", "admission_id": aid,
            "steps": ["23:15 · Signed in to eHospital", "23:16 · Planner: asking the doctor: Which potassium?"], "health": "",
            "summary": "Did not finish", "created_at": "2026-11-03T17:45:00+00:00", "time": "23:15",
            "ask": {"text": "Which potassium?", "options": [{"label": "Bed 5", "add": "bed 5"}, {"label": "Bed 6", "add": "bed 6"}],
                    "allow_other": True, "context_id": "ctx9"}}
    posted = []
    page = screen_page(browser, 1366, 860)
    page.route(re.compile(r".*/agent/tasks(\?.*)?$"), lambda route: route.fulfill(json={"tasks": [task]}))
    page.route(re.compile(r".*/agent/tasks/t1$"), lambda route: route.fulfill(json=task))

    def ask(route):
        posted.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(json={"text": "", "intent": {}, "card": {"kind": "jobs", "title": "none", "jobs": [], "skipped": []}})
    page.route("**/ask", ask)
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#agentBtn").click()
    expect(page.locator("#taskList .task")).to_contain_text("Needs your answer")
    assert "Signed in" not in page.locator("#pill").inner_text()
    page.locator("#taskList .task").click()
    expect(page.locator("#taskAskQ")).to_contain_text("Which potassium?")
    expect(page.locator("#taskAskOpts button")).to_have_count(2)
    expect(page.locator("#taskAskIn")).to_be_visible()
    page.screenshot(path=os.path.join(tempfile.gettempdir(), "test_bo_ask.png"))
    page.locator("#taskAskOpts button", has_text="Bed 6").click()
    page.wait_for_timeout(600)
    assert any(p.get("reply_to") == "ctx9" for p in posted)
    expect(page.locator("#taskAsk")).to_have_count(0)
    page.close()

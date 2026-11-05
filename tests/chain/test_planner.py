""" after a hospital job ends, the planner decides next / retry / other skill / ask / stop, inside hard limits. Offline."""
from __future__ import annotations

import asyncio
import json

from wardos.chain import planner as planner_module

from .test_agent import FakeHands, NAME, UHID, command, jobs_of, make_worker, world  # noqa: F401

ASK = "how many vacant beds in eHospital"


class FailingHands(FakeHands):
    async def ask(self, question, say=None):
        self.questions = getattr(self, "questions", []) + [question]
        raise RuntimeError("site down")


def start(world, decision):
    task = command(world, ASK)["card"]["task"]
    world.model.reply = decision
    return task


def run_robot(world, hands):
    worker, _ = make_worker(world, [], worker="one")
    worker.hands = hands
    asyncio.run(worker.run_once())


def tasks_of(world, kind="hospital_question"):
    return sorted((t for t in world.client.get("/agent/tasks").json()["tasks"] if t["kind"] == kind), key=lambda t: t["created_at"])


def test_failure_retries_once_then_asks_the_doctor(world):
    first = start(world, {"action": "retry", "why": "the site did not answer"})
    hands = FailingHands()
    run_robot(world, hands)
    old = world.desk.get(first["id"])
    assert old["status"] == "failed" and any(s.startswith("Planner: the site did not answer, retrying") for s in old["steps"])
    retry = [t for t in world.desk.store.query("agent_tasks", parent=first["id"])]
    assert len(retry) == 1 and retry[0]["attempt"] == 1 and retry[0]["status"] == "waiting"
    assert retry[0]["steps"][0].startswith("Planner: retrying with")
    # the model asks for a second retry: code refuses it and goes back to the doctor
    run_robot(world, hands)
    second = world.desk.get(retry[0]["id"])
    assert second["status"] == "failed" and second["ask"]["text"] and second["ask"]["context_id"]
    assert world.desk.store.query("agent_tasks", parent=second["id"]) == []
    assert any("asking the doctor" in s for s in second["steps"])


def test_without_a_model_a_failure_is_retried_once_then_asked(world):
    first = command(world, ASK)["card"]["task"]
    world.model.fail = True
    hands = FailingHands()
    run_robot(world, hands)
    retry = world.desk.store.query("agent_tasks", parent=first["id"])
    assert len(retry) == 1
    run_robot(world, hands)
    assert world.desk.get(retry[0]["id"])["ask"]["context_id"]


def test_a_limit_stops_the_planner_and_says_why(world, monkeypatch):
    monkeypatch.setenv("WARDOS_PLANNER_MAX_DECISIONS", "1")
    first = start(world, {"action": "retry", "why": "temporary"})
    hands = FailingHands()
    run_robot(world, hands)
    retry = world.desk.store.query("agent_tasks", parent=first["id"])[0]
    run_robot(world, hands)
    last = world.desk.get(retry["id"])
    assert any(s.startswith("Stopped: the planner already made 1 decisions") for s in last["steps"])
    assert world.desk.store.query("agent_tasks", parent=last["id"]) == []


def test_cost_and_time_limits(world, monkeypatch):
    monkeypatch.setenv("WARDOS_PLANNER_MAX_COST_INR", "0.005")
    first = start(world, {"action": "retry", "why": "temporary"})
    run_robot(world, FailingHands())
    retry = world.desk.store.query("agent_tasks", parent=first["id"])[0]
    run_robot(world, FailingHands())
    assert any(s.startswith("Stopped: the planner reached its cost limit") for s in world.desk.get(retry["id"])["steps"])


def test_a_write_skill_is_refused(world, tmp_path, monkeypatch):
    folder = tmp_path / "ehospital"
    folder.mkdir()
    (folder / "bad.save_order.json").write_text(json.dumps({"skill": "bad.save_order", "portal": "ehospital", "kind": "read", "steps": [
        {"id": "s", "action": "click", "target": {"css": "button.save", "has_text": "Save order"}}]}), encoding="utf-8")
    monkeypatch.setattr(planner_module, "SKILLS_DIR", tmp_path)
    assert "change data" in planner_module.read_only_skill("ehospital", "bad.save_order")
    assert planner_module.read_only_skill("ehospital", "not.saved")
    first = start(world, {"action": "other_skill", "skill": "bad.save_order", "why": "try it"})
    run_robot(world, FailingHands())
    old = world.desk.get(first["id"])
    assert any(s.startswith("Stopped: the planner named a skill that was refused") for s in old["steps"])
    assert world.desk.store.query("agent_tasks", parent=first["id"]) == []


def test_a_saved_read_skill_is_tried_and_the_hint_reaches_the_robot(world):
    first = start(world, {"action": "other_skill", "skill": "bed_occupancy.count_vacant_beds", "why": "no answer"})
    run_robot(world, FailingHands())
    child = world.desk.store.query("agent_tasks", parent=first["id"])[0]
    assert child["skill_hint"] == "bed_occupancy.count_vacant_beds" and "trying" in child["steps"][0]
    claimed = world.client.post("/agent/worker/next", json={"worker": "one"}).json()["task"]
    assert "Use the saved skill bed_occupancy.count_vacant_beds" in claimed["question"]


def test_order_is_respected_inside_a_batch(world):
    world.model.reply = {"jobs": [
        {"kind": "get_reports", "bed": "5", "test": "potassium", "text": "fetch potassium of bed 5"},
        {"kind": "get_reports", "bed": "12", "test": "potassium", "text": "fetch potassium of bed 12"}], "order": [2, 1]}
    card = jobs_of(world, "fetch potassium of bed 5 and bed 12 now")
    assert [j["task"]["bed"] for j in card["jobs"]] == ["12", "5"]
    rows = tasks_of(world, "get_reports")
    assert {t["bed"]: t["position"] for t in rows} == {"12": 1, "5": 2} and len({t["batch_id"] for t in rows}) == 1
    claimed = world.client.post("/agent/worker/next", json={"worker": "one"}).json()["task"]
    assert claimed["bed"] == "12"

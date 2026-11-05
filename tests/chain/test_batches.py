""" a batch uses one login; robots in parallel only on different batches; shared login counters; forced logout. Offline."""
from __future__ import annotations

import asyncio

from wardos.agent.client import ServerClient
from wardos.agent.hands import Fetched

from .test_agent import AppSession, FakeHands, _no_wait, clock, command, make_worker, world  # noqa: F401
from wardos.agent.worker import Worker

GET = "Get bed five's reports"


class SessionHands(FakeHands):
    """A fake hospital: the first job of a batch logs in; later jobs reuse the session. Counts real logins."""

    def __init__(self, answers=None):
        super().__init__(answers)
        self.batching = False
        self.session_lost = False
        self.logged_in = 0
        self.open = False
        self.drop_next = False

    async def fetch_new(self, who, say):
        made = 0
        if not self.open or self.drop_next:
            self.logged_in += 1
            made, self.open = 1, self.batching
            if self.drop_next:
                self.drop_next, self.session_lost = False, True
        if not self.batching:
            self.open = False
        out = await super().fetch_new(who, say)
        out.logins = made
        return out

    async def end_batch(self):
        self.open = False


def tasks(world):
    return world.client.get("/agent/tasks").json()["tasks"]


def two_in_one_request(world):
    world.model.reply = {"jobs": [{"kind": "get_reports", "bed": "5", "text": "a"}, {"kind": "get_reports", "bed": "12", "text": "b"}]}
    world.model.replies = []
    world.client.post("/ask", json={"text": "get bed 5 and bed 12 reports"})
    batches = {}
    for t in world.desk.store.query("agent_tasks"):
        batches.setdefault(t.get("batch_id"), []).append(t)
    return batches


def robot(world, name, hands=None):
    hands = hands or SessionHands([Fetched("NOTHING_NEW", found=1) for _ in range(10)])
    client = ServerClient("http://test", name, session=AppSession(world.client), token=None)
    return Worker(client, hands, sleep=_no_wait, alive_s=3600), hands


def test_a_batch_of_three_uses_one_login(world):
    world.model.reply = {"jobs": [{"kind": "get_reports", "bed": "5", "text": "a"}, {"kind": "get_reports", "bed": "12", "text": "b"},
                                  {"kind": "hospital_question", "text": "How many vacant beds?"}]}
    world.client.post("/ask", json={"text": "get reports of bed 5, bed 12 and count vacant beds"})
    rows = world.desk.store.query("agent_tasks")
    assert len(rows) == 3 and len({t["batch_id"] for t in rows}) == 1
    worker, hands = robot(world, "cloud-a")
    assert asyncio.run(worker.run_resident(idle_exit_s=0, max_run_s=60)) == "nothing to do"
    assert all(t["status"] == "done" for t in world.desk.store.query("agent_tasks"))
    assert hands.logged_in == 1 and len(hands.seen) == 2 and hands.logged_in == 1


def test_two_batches_one_after_the_other_with_max_one_and_together_with_max_two(world, monkeypatch):
    command(world, GET)
    command(world, "get bed 12 reports")
    one = ServerClient("http://test", "r1", session=AppSession(world.client), token=None)
    two = ServerClient("http://test", "r2", session=AppSession(world.client), token=None)
    first = one.next()
    assert first["task"] and two.next()["refused"]            # max 1: the second waits
    one.finish(first["task"]["id"], "done", "ok")
    assert one.next(first["batch_id"])["batch_done"]          # the first batch is empty
    monkeypatch.setenv("WARDOS_MAX_ROBOTS", "2")
    world.desk.heartbeat("r1", batch_id=first["batch_id"])
    command(world, "get bed 5 reports")
    again = two.next()                                         # max 2: a different batch than the one r1 holds
    assert again["task"] and again["batch_id"] != first["batch_id"]
    third = one.next()
    assert third["task"] and third["batch_id"] not in (again["batch_id"],) and third["task"]["id"] != again["task"]["id"]


def test_a_robot_does_not_take_the_batch_another_robot_holds(world, monkeypatch):
    monkeypatch.setenv("WARDOS_MAX_ROBOTS", "2")
    batches = two_in_one_request(world)
    assert len(batches) == 1 and len(next(iter(batches.values()))) == 2
    one = ServerClient("http://test", "r1", session=AppSession(world.client), token=None)
    two = ServerClient("http://test", "r2", session=AppSession(world.client), token=None)
    got = one.next()
    assert got["task"]
    assert two.next()["task"] is None                          # the rest of the batch belongs to r1
    assert one.next(got["batch_id"])["task"]                   # r1 continues it in the same session


def test_a_dead_robots_task_is_returned(world, clock):
    batches = two_in_one_request(world)
    ids = {t["id"] for t in next(iter(batches.values()))}
    one = ServerClient("http://test", "r1", session=AppSession(world.client), token=None)
    taken = one.next()["task"]["id"]
    clock.go(minutes=13)
    world.desk.sweep()
    back = world.desk.get(taken)
    assert taken in ids and back["status"] == "waiting" and back.get("requeued")
    two = ServerClient("http://test", "r2", session=AppSession(world.client), token=None)
    assert two.next()["task"]["id"] in ids


def test_login_counters_are_shared_between_robots(world):
    one = ServerClient("http://test", "r1", session=AppSession(world.client), token=None)
    two = ServerClient("http://test", "r2", session=AppSession(world.client), token=None)
    one.note_logins(1, True)
    shared = two.note_logins(1, True)
    assert shared["failed"] == 2 and len(shared["times"]) == 2
    worker, _ = robot(world, "r3")
    worker._count_logins(1, True)
    assert worker.failed_logins == 3 and len(worker.login_times) == 3


def test_a_forced_logout_lowers_the_robots_to_one(world, monkeypatch):
    monkeypatch.setenv("WARDOS_MAX_ROBOTS", "2")
    command(world, GET)
    command(world, "get bed 12 reports")
    other = ServerClient("http://test", "r2", session=AppSession(world.client), token=None)
    assert other.next()["task"]                                 # a second robot is working
    hands = SessionHands([Fetched("NOTHING_NEW", found=1), Fetched("NOTHING_NEW", found=1)])
    hands.drop_next = True                                      # the site drops the session at the next login
    worker, _ = robot(world, "r1", hands)
    assert asyncio.run(worker.run_resident(idle_exit_s=0, max_run_s=60)) == "logged out by the site"
    assert world.desk.max_robots() == 1
    assert world.desk.get("not-there") is None

"""Watch only when told, saved login, no extra one-time codes."""
from datetime import datetime, timedelta

import pytest

from fake_portal import FakeHospital
from wardos.hospital.runlog import RunLog
from wardos.hospital.watch import Watch, is_dangerous, parse_every, parse_until
from wardos.record.labs import LabRecord, LabValue


def value(number, sample, collected, reference="3.5 - 5.1"):
    return LabValue(analyte="Potassium", value=str(number), number=number, flag=None, unit="mmol/L", reference=reference,
                    panel=None, lab=None, sample_no=sample, collected_at=collected, verified_at=None, page=1,
                    source_sha256=sample)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 30, 22, 0)

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


def make_watch(tmp_path, statuses, *, minutes=45, logins=lambda: 0, **kw):
    clock = Clock()
    record = LabRecord(tmp_path / "records", "p", "x")
    fed = {}
    calls = {"fetch": 0, "visit": 0}

    async def fetch():
        calls["fetch"] += 1
        step = statuses.pop(0) if statuses else ("NOTHING_NEW", [])
        if isinstance(step, Exception):
            raise step
        return step

    async def visit():
        calls["visit"] += 1

    def ingest(paths):
        for path in paths:
            record.add(fed[path], tmp_path / path)

    watch = Watch(test="potassium", every_s=900, until=clock.t + timedelta(minutes=minutes), record=record,
                  log_path=tmp_path / "w.log", stop_path=tmp_path / "STOP", fetch=fetch, visit=visit, ingest=ingest,
                  now=clock, sleep=clock.sleep, logins=logins, **kw)
    return watch, clock, calls, fed, record


def lines(tmp_path):
    return (tmp_path / "w.log").read_text(encoding="utf-8").splitlines()


def test_times_are_read():
    assert parse_every("15m") == 900 and parse_every("1m") == 60 and parse_every("90s") == 90
    with pytest.raises(ValueError):
        parse_every("soon")
    now = datetime(2026, 10, 30, 22, 0)
    assert parse_until("08:00", now) == datetime(2026, 10, 31, 8, 0)
    assert parse_until("23:00", now) == datetime(2026, 10, 30, 23, 0)


def test_danger_uses_the_screen_rule():
    row = {"analyte": "Potassium", "unit": "mmol/L", "reference": "3.5 - 5.1"}
    assert is_dangerous({**row, "number": 6.0, "value": "6.0"})
    assert is_dangerous({**row, "number": 2.6, "value": "2.6"})
    assert is_dangerous({**row, "number": 5.5, "value": "5.5"})           # rule: outside the normal range is an alert
    assert not is_dangerous({**row, "number": 4.5, "value": "4.5"})


async def test_quiet_watch_writes_one_line_a_round_and_ends_by_itself(tmp_path):
    watch, _, calls, _, _ = make_watch(tmp_path, [])
    assert await watch.run() == 0
    text = lines(tmp_path)
    assert calls["fetch"] == 3 and sum("checked · no new potassium" in l for l in text) == 3
    assert "end time reached" in text[-1] and "danger lines 0" in text[-1]
    assert calls["visit"] == 0


async def test_danger_line_has_the_new_and_earlier_values(tmp_path):
    watch, _, _, fed, record = make_watch(tmp_path, [("FOUND", ["b.pdf"])], minutes=10)
    record.add([value(5.0, "S1", "2026-10-30T10:00:00")], tmp_path / "a.pdf")
    fed["b.pdf"] = [value(6.8, "S2", "2026-10-30T21:30:00")]
    await watch.run()
    line = next(l for l in lines(tmp_path) if "DANGER" in l)
    assert "6.8 mmol/L" in line and "normal 3.5 - 5.1" in line and "5.0 (30/10 10:00)" in line
    assert watch.danger_lines == 1


async def test_normal_new_value_and_old_catch_up_are_not_danger(tmp_path):
    watch, _, _, fed, _ = make_watch(tmp_path, [("FOUND", ["a.pdf"]), ("FOUND", ["b.pdf"])], minutes=20)
    fed["a.pdf"] = [value(7.5, "S0", "2026-10-20T10:00:00")]
    fed["b.pdf"] = [value(4.1, "S1", "2026-10-30T22:05:00")]
    await watch.run()
    text = "\n".join(lines(tmp_path))
    assert "older report, not new tonight" in text and "new Potassium 4.1 mmol/L · not dangerous" in text
    assert "DANGER" not in text


async def test_portal_down_is_written_and_the_watch_goes_on(tmp_path):
    watch, _, calls, _, _ = make_watch(tmp_path, [ConnectionError("x"), ("STEP_FAILED", [])])
    await watch.run()
    text = "\n".join(lines(tmp_path))
    assert "eHospital not answering (ConnectionError)" in text and "did not finish (STEP_FAILED)" in text
    assert calls["fetch"] == 3 and watch.problems == 2


async def test_stop_file_ends_it_and_old_stop_does_not(tmp_path):
    (tmp_path / "STOP").write_text("old")
    watch, _, calls, _, _ = make_watch(tmp_path, [], minutes=600)
    real_fetch = watch.fetch

    async def fetch():
        if calls["fetch"] == 1:
            (tmp_path / "STOP").write_text("stop")
        return await real_fetch()
    watch.fetch = fetch
    await watch.run()
    assert calls["fetch"] == 2 and "told to stop" in lines(tmp_path)[-1]
    assert not (tmp_path / "STOP").exists()


async def test_keep_alive_visits_only_between_far_apart_rounds(tmp_path):
    watch, _, calls, _, _ = make_watch(tmp_path, [], minutes=15, keep_alive_s=240)
    await watch.run()
    assert calls["fetch"] == 1 and calls["visit"] == 3


async def test_too_many_logins_stops_the_watch(tmp_path):
    watch, _, calls, _, _ = make_watch(tmp_path, [], minutes=600, logins=lambda: 7)
    await watch.run()
    assert calls["fetch"] == 1 and "stopped after 6 logins" in lines(tmp_path)[-1]


async def test_second_job_reuses_the_login_with_no_otp(make_keeper):
    hospital = FakeHospital()
    first = await make_keeper(hospital, keep_login=True)
    assert await first.ensure_healthy(RunLog.create(first.settings.runs_dir, "job1", console=False)) == 1
    assert (first.logins, first.otps) == (1, 1)
    assert first.login_file.is_file()
    await first.stop()
    assert hospital.logouts == 0

    second = await make_keeper(hospital, keep_login=True)
    assert await second.ensure_healthy(RunLog.create(second.settings.runs_dir, "job2", console=False)) == 0
    assert (second.logins, second.otps) == (0, 0)
    assert hospital.logins == 1 and hospital.logouts == 0


async def test_dead_saved_login_logs_in_again_once(make_keeper):
    hospital = FakeHospital()
    first = await make_keeper(hospital, keep_login=True)
    await first.ensure_healthy(RunLog.create(first.settings.runs_dir, "job1", console=False))
    await first.stop()
    second = await make_keeper(hospital, keep_login=True)
    hospital.tokens.clear()
    await second.ensure_healthy(RunLog.create(second.settings.runs_dir, "job2", console=False))
    assert (second.logins, second.otps) == (1, 1) and hospital.logins == 2


async def test_two_jobs_cannot_use_one_login_at_once(make_keeper):
    from wardos.hospital.session import SessionError
    hospital = FakeHospital()
    await make_keeper(hospital, keep_login=True)
    with pytest.raises(SessionError, match="another WardOS job"):
        await make_keeper(hospital, keep_login=True)


async def _keeper_with_health(settings, states):
    from wardos.hospital.portals.base import Health
    from wardos.hospital.session import SessionKeeper
    from conftest import FakeModels, FakeOtp, fake_portal_profile
    keeper = SessionKeeper(settings, fake_portal_profile(), FakeModels(), FakeOtp(), headless=True, keep_login=True,
                           max_recoveries=2)
    seen = {"logins": 0, "logouts": 0}

    async def health(log):
        return Health[states.pop(0)]

    async def login(log, deadline=None):
        seen["logins"] += 1
        keeper.logins += 1

    async def logout(log):
        seen["logouts"] += 1
    keeper.health, keeper._login, keeper.logout = health, login, logout
    keeper.save_login = lambda: _done()
    return keeper, seen


async def _done():
    return None


async def test_broken_kept_login_gets_a_second_look_before_any_login(settings, tmp_path):
    keeper, seen = await _keeper_with_health(settings, ["BROKEN", "HEALTHY"])
    log = RunLog.create(settings.runs_dir, "second-look", console=False)
    assert await keeper.ensure_healthy(log) == 0
    assert seen == {"logins": 0, "logouts": 0}


async def test_still_broken_after_the_second_look_logs_in_once_and_then_checks_it(settings, tmp_path):
    keeper, seen = await _keeper_with_health(settings, ["BROKEN", "BROKEN", "HEALTHY"])
    log = RunLog.create(settings.runs_dir, "still-broken", console=False)
    assert await keeper.ensure_healthy(log) == 1
    assert seen == {"logins": 1, "logouts": 1}

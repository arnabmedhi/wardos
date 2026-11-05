"""Coordinator tests: synthetic data, fake models/keeper, no browser or network."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wardos.hospital.brain import (
    BrainError, HospitalBrain, Plan, _complete, check_read_skill, choose, table_answer,
)
from wardos.hospital.explore import ExploreResult, TABLE_JS
from wardos.hospital.runner import RunResult
from wardos.hospital.skills import SkillLibrary


def capability(portal="ehospital", target="#beds", version=1):
    return {"skill": "beds.status", "portal": portal, "version": version, "kind": "read",
            "description": "Read ward bed occupancy", "inputs": {}, "steps": [
                {"id": "open", "action": "click", "target": {"css": target},
                 "expect": {"url_contains": "/beds"}},
                {"id": "rows", "action": "read_table", "only_selectable": False, "save_as": "rows"}]}


class FakePage:
    url = "https://hospital.test/beds"

    def __init__(self):
        self.table = {"headers": ["Ward", "Vacant Beds"], "rows": [["A", "12"], ["B", "7"]],
                      "total_rows": 2, "pager": ""}
        self.body = "Bed occupancy"
        self.shots = 0

    async def evaluate(self, script, *args):
        return deepcopy(self.table) if script == TABLE_JS else ["button 'Beds' id=beds"]

    def locator(self, selector):
        assert selector == "body"
        return self

    async def inner_text(self, **kwargs):
        return self.body

    async def screenshot(self, **kwargs):
        self.shots += 1
        return b"synthetic screenshot bytes"


class FakeModels:
    def __init__(self, portal):
        self.portal = portal
        self.prompts = []
        self.route_override = {}
        self.repair_change = None
        self.compiled_change = None

    async def json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if prompt.startswith("Choose a READ"):
            data = json.loads(prompt.splitlines()[-1])
            exists = any(s["skill"] == "beds.status" for s in data["skills"])
            return {"mode": "run" if exists else "explore", "skill": "beds.status", "name": "beds.status",
                    "description": "Read ward bed occupancy", "inputs": {}, "input_spec": {},
                    "answer": {"kind": "table", "operation": "sum", "column": "Vacant Beds", "subject": "beds"},
                    **self.route_override}
        if prompt.startswith("You help turn a recording"):
            return {"wandering": [], **(self.compiled_change or {})}
        if prompt.startswith("A skill"):
            candidate = json.JSONDecoder().raw_decode(prompt.split("CURRENT SKILL:\n")[1])[0]
            for step in candidate["steps"]:
                if (step.get("target") or {}).get("css") == "#broken":
                    step["target"] = {"css": "#beds"}
            if self.repair_change:
                self.repair_change(candidate)
            return candidate
        if prompt.startswith("Match requested"):
            data = json.loads(prompt.splitlines()[-1])
            return {"columns": {w: ("Vacant Beds" if "vacant" in w.lower() else "Ward") for w in data["wanted"]}}
        raise AssertionError("Unexpected model call")


PRINT_TRACE = [{"kind": "click", "tag": "button", "text": "Print", "css": "#print", "css_count": 1,
                "path": "/lab", "after_path": "/lab"}]


def bed_trace(css="#beds"):
    return [{"kind": "click", "tag": "button", "text": "Beds", "css": css, "css_count": 1, "path": "/home",
             "after_path": "/beds"},
            {"kind": "read_table", "path": "/beds", "after_path": "/beds", "columns": ["Ward", "Vacant Beds"]}]


class FakeExplorer:
    def __init__(self, keeper, models, log, **kwargs):
        self.keeper = keeper
        assert kwargs["private_console"] is True

    async def run(self, *args, **kwargs):
        self.keeper.explorations += 1
        taking_over = bool(kwargs.get("start_here"))
        self.keeper.take_overs += taking_over
        status = self.keeper.explore_statuses.pop(0) if self.keeper.explore_statuses else "DONE"
        trace = (self.keeper.take_over_trace if taking_over else self.keeper.explore_trace) or bed_trace()
        return ExploreResult(goal="beds", status=status, answer="Made-up answer: 99999", verified="UNCHECKED",
                             trace=deepcopy(trace))


class FakeKeeper:
    firewall_mode = "enforce"
    firewall_read_like = True
    firewall_log = []

    def __init__(self, library, portal):
        self.library = library
        self.portal = SimpleNamespace(name=portal, origins=["https://hospital.test"])
        self.models = FakeModels(portal)
        self.page = FakePage()
        self.browser = SimpleNamespace(active=self.active)
        self.explorations = self.health_calls = self.take_overs = 0
        self.runs = []
        self.statuses = []
        self.before_run = None
        self.explore_trace = self.take_over_trace = None
        self.explore_statuses = []

    async def active(self):
        return self.page

    async def ensure_healthy(self, log):
        self.health_calls += 1

    async def run_skill(self, name, inputs, *, log=None):
        skill = self.library.load(self.portal.name, name)
        self.runs.append(deepcopy(skill))
        if self.before_run:
            self.before_run(skill)
        if self.statuses:
            return self.statuses.pop(0)
        broken = next((s["id"] for s in skill["steps"] if (s.get("target") or {}).get("css") == "#broken"), None)
        if broken:
            return RunResult(name, "STEP_FAILED", 0.2, failed_step=broken, reason="element not found")
        outputs = {}
        for step in skill["steps"]:
            if step["action"] == "read_table":
                table = self.page.table or {"headers": [], "rows": []}
                outputs[step.get("save_as", "rows")] = [
                    {"cells": dict(zip(table["headers"], row))} for row in table["rows"]]
                if not table["rows"] and step.get("on_empty"):
                    return RunResult(name, step["on_empty"], 0.1, outputs=outputs)
        return RunResult(name, "OK", 0.1, outputs=outputs)


@pytest.fixture(params=["ehospital", "openmrs"])
def rig(tmp_path, request):
    portal = request.param
    root = tmp_path / "skills"
    (root / portal).mkdir(parents=True)
    settings = SimpleNamespace(private_dir=tmp_path / "private", runs_dir=tmp_path / "private/runs",
                               ledger_dir=tmp_path / "private/ledger")
    library = SkillLibrary(root)
    keeper = FakeKeeper(library, portal)
    brain = HospitalBrain(keeper, settings, library=library, explorer_factory=FakeExplorer)
    return brain, keeper, settings, library


def install(rig, *, target="#beds", version=1):
    brain, keeper, settings, library = rig
    skill = capability(keeper.portal.name, target, version)
    library.path(keeper.portal.name, skill["skill"]).write_text(json.dumps(skill), encoding="utf-8")
    return skill


@pytest.mark.asyncio
async def test_explore_verify_save_then_reuse(rig):
    brain, keeper, settings, library = rig
    first = await brain.run("How many beds are vacant?")
    assert first.status == "ANSWERED", first.note
    assert first.learned and first.decision == "explore"
    assert first.answer == "Vacant Beds: 19 (calculated from 2 rows)."
    assert "99999" not in first.answer
    saved = library.load(keeper.portal.name, "beds.status")
    assert saved["verified"]["status"] == "OK"
    assert saved["answer_contract"]["headers"] == ["Ward", "Vacant Beds"]
    assert (settings.private_dir / "skills-staging" / keeper.portal.name / "beds.status.json").exists()
    assert len(list(settings.runs_dir.iterdir())) == 2
    keeper.page.table["rows"][0][1] = "2"
    second = await brain.run("Tell me the current vacant bed total")
    assert second.status == "ANSWERED", second.note
    assert second.decision == "run" and not second.learned
    assert second.answer == "Vacant Beds: 9 (calculated from 2 rows)."
    assert keeper.explorations == 1 and len(keeper.runs) == 2
    assert keeper.library is library


@pytest.mark.asyncio
async def test_failed_step_repaired_and_old_version_archived(rig):
    brain, keeper, settings, library = rig
    old = install(rig, target="#broken", version=4)
    old["produces"] = "table"
    library.path(keeper.portal.name, old["skill"]).write_text(json.dumps(old))
    result = await brain.run("Vacant beds?")
    assert result.status == "ANSWERED", result.note
    assert result.repaired and result.decision == "repair"
    saved = library.load(keeper.portal.name, "beds.status")
    assert saved["version"] == 5 and saved["produces"] == "table"
    archive = list((settings.private_dir / "skills-archive" / keeper.portal.name).glob("*.json"))
    assert len(archive) == 1 and json.loads(archive[0].read_text()) == old
    assert keeper.page.shots == 1 and keeper.library is library


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    lambda skill: skill["steps"][0].pop("expect"),
    lambda skill: skill["steps"][1].update(save_as="different"),
    lambda skill: skill["steps"][0].update(target={"text": "Save"}),
])
async def test_unsafe_repair_keeps_original(rig, change):
    brain, keeper, settings, library = rig
    old = install(rig, target="#broken", version=3)
    keeper.models.repair_change = change
    result = await brain.run("Vacant beds?")
    assert result.status == "NEEDS_ATTENTION"
    assert not result.repaired
    assert library.load(keeper.portal.name, "beds.status") == old
    assert not (settings.private_dir / "skills-archive").exists()
    assert keeper.library is library


@pytest.mark.asyncio
async def test_session_failure_is_not_selector_repair(rig):
    brain, keeper, settings, library = rig
    install(rig)
    keeper.statuses = [RunResult("beds.status", "STEP_FAILED", 1, failed_step="open", session_suspect=True)]
    result = await brain.run("Vacant beds?")
    assert result.status == "STEP_FAILED" and not result.repaired
    assert len(keeper.models.prompts) == 1 and len(keeper.runs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["NOT_FOUND", "PDF_NOT_VERIFIED", "NEEDS_LOGIN"])
async def test_inconclusive_replay_never_activates(rig, status):
    brain, keeper, settings, library = rig
    keeper.statuses = [RunResult("beds.status", status, 1)]
    result = await brain.run("Vacant beds?")
    assert result.status == "ANSWERED" and not result.learned and "not saved" in result.note
    assert not library.path(keeper.portal.name, "beds.status").exists()
    assert keeper.explorations == 1 and len(keeper.runs) == 1


def keep_broken(skill):
    for step in skill["steps"]:
        if step["action"] == "click":
            step["target"] = {"css": "#broken"}


@pytest.mark.asyncio
async def test_stuck_learning_replay_is_finished_live_and_replaces_the_stuck_part(rig):
    brain, keeper, settings, library = rig
    keeper.explore_trace = bed_trace("#broken")
    keeper.models.repair_change = keep_broken
    result = await brain.run("Vacant beds?")
    assert result.status == "ANSWERED" and result.learned, result.note
    assert result.answer == "Vacant Beds: 19 (calculated from 2 rows)." and keeper.take_overs == 1
    saved = library.load(keeper.portal.name, "beds.status")
    assert "#broken" not in json.dumps(saved) and saved["verified"]["status"] == "OK"


@pytest.mark.asyncio
async def test_stuck_learning_replay_stays_staged_when_the_take_over_fails(rig):
    brain, keeper, settings, library = rig
    keeper.explore_trace = bed_trace("#broken")
    keeper.models.repair_change = keep_broken
    keeper.explore_statuses = ["DONE", "GAVE_UP"]
    result = await brain.run("Vacant beds?")
    assert result.status == "ANSWERED" and not result.learned and "not saved" in result.note
    assert result.answer == "Vacant Beds: 19 (calculated from 2 rows)."
    assert not library.path(keeper.portal.name, "beds.status").exists()


@pytest.mark.asyncio
async def test_stuck_saved_route_is_finished_live_and_saved_as_a_new_version(rig):
    brain, keeper, settings, library = rig
    old = install(rig, target="#broken", version=2)
    keeper.models.repair_change = keep_broken
    result = await brain.run("Vacant beds?")
    assert result.status == "ANSWERED" and result.decision == "take_over", result.note
    assert result.answer == "Vacant Beds: 19 (calculated from 2 rows)."
    assert keeper.take_overs == 1 and result.repaired
    saved = library.load(keeper.portal.name, "beds.status")
    assert saved["version"] == 3 and "#broken" not in json.dumps(saved)
    archive = list((settings.private_dir / "skills-archive" / keeper.portal.name).glob("*.json"))
    assert [json.loads(a.read_text()) for a in archive] == [old]


@pytest.mark.asyncio
async def test_learned_script_is_built_exactly_from_the_recording(rig):
    brain, keeper, settings, library = rig
    keeper.models.compiled_change = {"wandering": ["s1_click_beds"]}
    result = await brain.run("Vacant beds?")
    assert result.status == "ANSWERED" and result.learned, result.note
    saved = library.load(keeper.portal.name, "beds.status")
    click = next(s for s in saved["steps"] if s["action"] == "click")
    assert click["target"]["css"] == "#beds" and click["expect"] == {"url_contains": "/beds"}


@pytest.mark.asyncio
async def test_replay_must_check_evidence_not_just_ok_status(rig):
    brain, keeper, settings, library = rig
    keeper.before_run = lambda _: setattr(keeper.page, "url", "https://hospital.test/wrong-screen")
    result = await brain.run("Vacant beds?")
    assert result.status == "ANSWERED" and not result.learned and "not saved" in result.note
    assert not library.path(keeper.portal.name, "beds.status").exists()


@pytest.mark.asyncio
async def test_explicit_empty_screen_can_be_learned_and_replayed(rig):
    brain, keeper, settings, library = rig
    keeper.page.table = None
    keeper.page.body = "Bed management\nNo beds configured\nAdd bed"
    first = await brain.run("Vacant beds?")
    assert first.status == "ANSWERED", first.note
    assert first.learned and "count: 0" in first.answer
    second = await brain.run("How many beds are there?")
    assert second.status == "ANSWERED" and second.decision == "run"
    keeper.page.body = "Loading..."
    third = await brain.run("How many beds are there?")
    assert third.status == "NEEDS_ATTENTION" and not third.answer


@pytest.mark.asyncio
async def test_absent_table_is_not_zero(rig):
    brain, keeper, settings, library = rig
    keeper.page.table = None
    keeper.page.body = "Loading..."
    result = await brain.run("Vacant beds?")
    assert result.status == "NEEDS_ATTENTION" and not result.answer
    assert not keeper.runs


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["clarify", "blocked"])
async def test_non_executable_decision_does_not_touch_portal(rig, mode):
    brain, keeper, settings, library = rig
    keeper.models.route_override = {"mode": mode, "message": "untrusted text 123456789"}
    result = await brain.run("Some request")
    assert result.status == mode.upper() and "123456789" not in result.note
    assert keeper.health_calls == 0 and not keeper.runs and keeper.explorations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"mode": "run", "skill": "not.installed"},
    {"mode": "explore", "name": "../escape"},
    {"inputs": {"uhid": "20260000001"}},
])
async def test_invalid_model_route_fails_closed(rig, override):
    brain, keeper, settings, library = rig
    keeper.models.route_override = override
    result = await brain.run("Vacant beds?")
    assert result.status == "NEEDS_ATTENTION"
    assert not keeper.runs and not keeper.explorations


@pytest.mark.asyncio
async def test_missing_required_input_does_not_touch_portal(rig):
    brain, keeper, settings, library = rig
    skill = install(rig)
    skill["inputs"] = {"uhid": {"type": "string", "required": True}}
    library.path(keeper.portal.name, skill["skill"]).write_text(json.dumps(skill))
    result = await brain.run("Read this patient's result")
    assert result.status == "CLARIFY"
    assert not keeper.runs and not keeper.explorations


@pytest.mark.asyncio
async def test_unrelated_staging_does_not_override_dependency(rig):
    brain, keeper, settings, library = rig
    from wardos.hospital.learn import save_staged
    save_staged(settings, {**capability(keeper.portal.name), "skill": "other.task", "kind": "write"})
    result = await brain.run("Vacant beds?")
    assert result.status == "ANSWERED", result.note
    assert keeper.library is library
    assert not library.path(keeper.portal.name, "other.task").exists()


@pytest.mark.asyncio
async def test_firewall_cannot_be_downgraded(rig):
    brain, keeper, settings, library = rig
    keeper.firewall_mode = "observe"
    result = await brain.run("Vacant beds?")
    assert result.status == "NEEDS_ATTENTION" and not keeper.models.prompts


@pytest.mark.asyncio
async def test_unknown_patient_workflow_needs_different_patient_replay(rig):
    brain, keeper, settings, library = rig
    result = await brain.run("Vacant beds for {uhid}?", {"uhid": "synthetic-patient-a"})
    assert result.status == "NEEDS_ATTENTION" and "verify-uhid-file" in result.note
    assert not library.path(keeper.portal.name, "beds.status").exists()


@pytest.mark.asyncio
async def test_lab_answer_uses_verified_pdf_and_existing_record_code(rig, monkeypatch, tmp_path):
    from wardos.hospital import brain as module
    from wardos.record.labs import LabRecord, LabValue
    brain, keeper, settings, library = rig
    skill = install(rig)
    skill.update(produces="lab_pdf", inputs={"uhid": {"type": "string", "required": True}})
    library.path(keeper.portal.name, "beds.status").write_text(json.dumps(skill))
    keeper.models.route_override = {"answer": {"kind": "lab_latest", "test": "creatinine"}}
    patient = "synthetic-patient-a"
    parsed = []
    value = LabValue("Serum Creatinine", "1.2", 1.2, None, "mg/dl", None, "KFT", None,
                     "fake-sample", "2026-10-23T09:00:00", None, 1, "synthetic-sha")
    def parse(path, expected):
        parsed.append((path.name, expected))
        return [value], []
    monkeypatch.setattr(module, "parse_lis_pdf", parse)
    keeper.statuses = [RunResult("beds.status", "FOUND", .1, artifacts=[
        {"path": str(tmp_path / "verified.pdf"), "check": {"ok": True}},
        {"path": str(tmp_path / "wrong-patient.pdf"), "check": {"ok": False}}])]
    result = await brain.run("Latest creatinine?", {"uhid": patient})
    assert result.status == "ANSWERED", result.note
    assert result.answer.startswith("Serum Creatinine: 1.2 mg/dl")
    assert parsed == [("verified.pdf", patient)]
    assert LabRecord(settings.private_dir / "records", keeper.portal.name, patient).find("creatinine")
    assert all(patient not in prompt for prompt in keeper.models.prompts)


@pytest.mark.asyncio
async def test_new_lab_workflow_does_not_mix_verification_patient(rig, monkeypatch, tmp_path):
    from wardos.hospital import brain as module
    brain, keeper, settings, library = rig
    keeper.models.route_override = {"answer": {"kind": "files"}}
    keeper.explore_trace = PRINT_TRACE
    first, second = "synthetic-patient-a", "synthetic-patient-b"
    keeper.statuses = [RunResult("beds.status", "FOUND", .1, artifacts=[
        {"path": str(tmp_path / "second.pdf"), "check": {"ok": True}}]),
        RunResult("beds.status", "FOUND", .1, artifacts=[
        {"path": str(tmp_path / "first.pdf"), "check": {"ok": True}}])]
    parsed = []
    def parse(path, expected):
        parsed.append((path.name, expected))
        return [], []
    monkeypatch.setattr(module, "parse_lis_pdf", parse)
    result = await brain.run("Download reports for {uhid}", {"uhid": first}, verification_uhid=second)
    assert result.status == "ANSWERED", result.note
    assert result.learned and parsed == [("second.pdf", second), ("first.pdf", first)]
    saved = library.path(keeper.portal.name, "beds.status").read_text()
    assert first not in saved and second not in saved


@pytest.mark.asyncio
async def test_new_lab_workflow_cannot_promote_nothing_new(rig):
    brain, keeper, settings, library = rig
    keeper.models.route_override = {"answer": {"kind": "files"}}
    keeper.explore_trace = PRINT_TRACE
    keeper.statuses = [RunResult("beds.status", "NOTHING_NEW", .1)]
    result = await brain.run("Download reports", {"uhid": "synthetic-patient-a"},
                             verification_uhid="synthetic-patient-b")
    assert result.status in {"NEEDS_ATTENTION", "NOT_LEARNED"} and not result.answer
    assert not library.path(keeper.portal.name, "beds.status").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [
    {"tag": "button", "label": "Save", "zone": "main"},
    {"tag": "a", "label": "Transfer", "zone": "main"},
    {"tag": "input", "type": "password", "label": "", "zone": "main"},
])
async def test_runtime_guard_checks_actual_element_before_action(monkeypatch, control):
    from wardos.hospital import brain as module
    from wardos.hospital.runner import StepFailed
    runner = object.__new__(module.ReadRunner)
    async def active():
        return object()
    async def evaluate(script):
        return control
    async def resolve(*args):
        return SimpleNamespace(locator=SimpleNamespace(evaluate=evaluate))
    async def never(*args):
        pytest.fail("Unsafe browser action executed")
    runner.state = SimpleNamespace(active=active)
    runner._timeout = lambda _: 5
    monkeypatch.setattr(module, "resolve", resolve)
    monkeypatch.setattr(module.SkillRunner, "_do", never)
    with pytest.raises(StepFailed, match="Read-only guard"):
        await runner._do({"id": "open", "action": "click", "target": {"css": "#innocent-selector"}}, {}, None, None, 0)


def test_read_skill_rejects_write_dependencies_and_cross_portal(rig):
    brain, keeper, settings, library = rig
    child = install(rig)
    child["kind"] = "write"
    library.path(keeper.portal.name, child["skill"]).write_text(json.dumps(child))
    parent = {**capability(keeper.portal.name), "skill": "parent.task",
              "steps": [{"id": "child", "action": "call_skill", "skill": "beds.status"}]}
    with pytest.raises(BrainError):
        check_read_skill(parent, keeper.portal, library)
    wrong_portal = capability("another_portal")
    with pytest.raises(BrainError):
        check_read_skill(wrong_portal, keeper.portal, library)


@pytest.mark.asyncio
async def test_column_binding_gets_headers_not_values():
    models = FakeModels("ehospital")
    rows = [{"cells": {"Ward": "sensitive cell", "Vacant Beds": "1,200"}},
            {"cells": {"Ward": "B", "Vacant Beds": "3"}},
            {"cells": {"Ward": "Total", "Vacant Beds": "1,203"}}]
    answer = await table_answer(models, rows, {"operation": "sum", "column": "vacant"})
    assert "1203" in answer and "2 rows" in answer
    assert "sensitive cell" not in models.prompts[0] and "1,200" not in models.prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", ["", "unknown", "NaN", "inf"])
async def test_total_rejects_missing_or_nonfinite_values(cell):
    rows = [{"cells": {"Vacant Beds": "2"}}, {"cells": {"Vacant Beds": cell}}]
    with pytest.raises(BrainError):
        await table_answer(FakeModels("ehospital"), rows, {"operation": "sum", "column": "Vacant Beds"})


@pytest.mark.asyncio
async def test_filters_and_counts_use_code():
    rows = [{"cells": {"Ward": "A", "Vacant Beds": "5"}}, {"cells": {"Ward": "B", "Vacant Beds": "8"}}]
    models = FakeModels("ehospital")
    result = await table_answer(models, rows, {"operation": "sum", "column": "Vacant Beds",
                                             "where": {"column": "Ward", "value": "A"}})
    assert result == "Vacant Beds: 5 (calculated from 1 rows)."
    with pytest.raises(BrainError):
        await table_answer(models, rows, {"operation": "count", "where": {"column": "Ward", "value": "missing"}})
    assert await table_answer(models, rows, {"operation": "count"}) == "Matching rows: 2."


@pytest.mark.parametrize("pager, count, expected", [("1 - 10 of 99", 10, False), ("1 - 99 of 99", 99, True),
                                                    ("Showing 1 to 10 of 99 entries", 10, False),
                                                    ("Page 1 of 5", 10, False), ("1 2 3", 10, False), ("", 99, True)])
def test_pagination_is_not_silently_summed(pager, count, expected):
    assert _complete({"rows": [[]] * count, "total_rows": count, "pager": pager}) == expected


def test_cli_rejects_identity_in_question_or_input_before_live(monkeypatch, capsys):
    from wardos import cli
    monkeypatch.setattr(cli, "_live", lambda *a: pytest.fail("Live work was attempted"))
    assert cli.main(["ask", "Read 20260000001"]) == 2
    assert cli.main(["ask", "Read results", "--input", "uhid=20260000001"]) == 2
    assert "20260000001" not in capsys.readouterr().out


def test_cli_portal_and_enforce_defaults(monkeypatch):
    from wardos import cli
    seen = []
    monkeypatch.setattr(cli, "cmd_ask", lambda args: seen.append(args) or 0)
    assert cli.main(["--portal", "openmrs", "ask", "How many beds?"]) == 0
    args = seen[0]
    assert args.portal == "openmrs" and args.firewall == "enforce" and args.read_like
    with pytest.raises(SystemExit):
        cli.main(["ask", "beds?", "--firewall", "observe"])


@pytest.mark.parametrize("text", ['20260000001', '{"uhid": "20260000001"}', '"20260000001"'])
def test_uhid_file_accepts_plain_number_and_json(tmp_path, text):
    from wardos.cli import _uhid_from_args
    path = tmp_path / "patient.txt"
    path.write_text(text)
    assert _uhid_from_args(SimpleNamespace(uhid=None, uhid_file=str(path))) == "20260000001"


def _ward_skill(portal="openmrs", key="Enter", text="Surgery Ward"):
    steps = [{"id": "pick_ward", "action": "click", "target": {"text": text}},
             {"id": "enter_ward", "action": "press", "key": key}]
    return {"skill": "wards.surgery.patients", "portal": portal, "version": 1, "kind": "read", "steps": steps}


def test_enter_is_allowed_only_right_after_the_openmrs_ward_picker():
    check_read_skill(_ward_skill(), SimpleNamespace(name="openmrs"), None)
    for bad in (dict(portal="ehospital"), dict(text="Submit order"), dict(key="Space"), dict(text="Surgery Ward and more")):
        skill = _ward_skill(**bad)
        with pytest.raises(BrainError):
            check_read_skill(skill, SimpleNamespace(name=skill["portal"]), None)
    lone = _ward_skill()
    lone["steps"] = [{"id": "a", "action": "press", "key": "Enter"}, lone["steps"][0]]
    with pytest.raises(BrainError):
        check_read_skill(lone, SimpleNamespace(name="openmrs"), None)

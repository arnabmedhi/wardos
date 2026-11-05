"""WardOS command line."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

from .config import load_settings, secret_keys_present
from .hospital.portals import get_portal
from .hospital.runlog import RunLog
from .hospital.skills import SkillLibrary


def _ok(flag: bool) -> str:
    return "ok" if flag else "MISSING"


def cmd_check(args) -> int:
    settings = load_settings()
    portal = get_portal(args.portal)
    import os
    print(f"MODEL_API_KEY ........ {_ok(bool(os.environ.get('MODEL_API_KEY')))}")
    print(f"models ............... {settings.muse_model} ({settings.muse_effort}), computer use: {settings.muse_cua_model}")
    print(f"chrome ............... {settings.chrome_path or 'MISSING (will try channel=chrome)'}")
    keys = secret_keys_present(settings.portal_secrets_file, list(portal.credential_keys))
    print(f"portal secrets file .. {settings.portal_secrets_file.name}: "
          + ", ".join(f"{k} {_ok(v)}" for k, v in keys.items()))
    otp = secret_keys_present(settings.otp_config_file, ["WARDOS_OTP_EMAIL", "WARDOS_OTP_APP_PASSWORD"])
    print(f"otp inbox config ..... {settings.otp_config_file.name}: " + ", ".join(f"{k} {_ok(v)}" for k, v in otp.items()))
    print(f"otp source file ...... {_ok(settings.otp_source_file.is_file())}")
    library = SkillLibrary()
    skills = library.list(portal.name)
    print(f"skills ({portal.name}) ... {len(skills)} valid: " + ", ".join(f"{s['skill']} v{s['version']}" for s in skills))
    print(f"private folder ....... {settings.private_dir}")
    if getattr(args, "live_model", False):
        return asyncio.run(_model_check(settings))
    return 0


def _fake_captcha(text: str) -> bytes:
    import io
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (160, 50), (235, 235, 235))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    draw.text((12, 8), text, fill=(20, 20, 90), font=font)
    for x in range(0, 160, 9):
        draw.line((x, 0, x + 20, 50), fill=(180, 180, 200))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


async def _model_check(settings) -> int:
    import time
    from .models import get_model_client
    client = get_model_client(settings)
    good = 0
    try:
        for number, text in enumerate(["aB3xQ9", "K7mP2w", "9Tz4Hd"], 1):
            started = time.monotonic()
            try:
                read = await client.read_captcha(_fake_captcha(text), "image/png", 6)
                ok = read == text
                print(f"captcha {number}: {'right' if ok else 'WRONG'} in {time.monotonic() - started:.1f}s")
            except Exception as exc:
                ok = False
                print(f"captcha {number}: {type(exc).__name__} after {time.monotonic() - started:.1f}s")
            started = time.monotonic()
            try:
                reply = await client.json('Reply with JSON {"sum": 2 + 3 as a number}.', timeout=45)
                ok = ok and reply.get("sum") == 5
                print(f"json {number}: {'right' if reply.get('sum') == 5 else 'WRONG'} in {time.monotonic() - started:.1f}s")
            except Exception as exc:
                ok = False
                print(f"json {number}: {type(exc).__name__} after {time.monotonic() - started:.1f}s")
            good += ok
    finally:
        if hasattr(client, "aclose"):
            await client.aclose()
    print(f"model {client.name}: {good}/3 rounds fully right")
    return 0 if good == 3 else 2


def cmd_skills(args) -> int:
    library = SkillLibrary()
    for skill in library.list(args.portal):
        print(f"{skill['skill']:28} v{skill['version']}  {skill['kind']:5}  {skill.get('description', '')}")
    return 0


async def _keeper(args):
    from .hospital.otp import ImapOtpProvider
    from .hospital.session import SessionKeeper
    from .models import get_model_client
    settings = load_settings()
    portal = get_portal(args.portal)
    models = get_model_client(settings)
    otp = ImapOtpProvider(settings.otp_config_file, settings.otp_source_file, settings.otp_window_seconds)
    keeper_class = SessionKeeper
    if getattr(args, "command", None) in {"ask", "chat"}:
        from .hospital.brain import ReadSessionKeeper
        keeper_class = ReadSessionKeeper
    keeper = keeper_class(settings, portal, models, otp, headless=args.headless, firewall_mode=args.firewall,
                           firewall_read_like=getattr(args, "read_like", False),
                           credentials=getattr(portal, "demo_credentials", None), keep_login=True)
    return settings, keeper, models, otp


async def _live(args, work) -> int:
    settings, keeper, models, otp = await _keeper(args)
    try:
        await keeper.start()
        return await work(settings, keeper)
    finally:
        await keeper.stop()
        await otp.close()
        if hasattr(models, "aclose"):
            await models.aclose()


def cmd_login(args) -> int:
    async def work(settings, keeper):
        log = RunLog.create(settings.runs_dir, "login")
        recoveries = await keeper.ensure_healthy(log)
        log.write_json("result.json", {"status": "HEALTHY", "recoveries": recoveries, "seconds": log.elapsed})
        print(f"Session healthy after {log.elapsed}s (logout/login cycles: {recoveries}). Run folder: {log.directory}")
        if args.hold:
            input("Browser stays open. Press Enter to close... ")
        return 0
    return asyncio.run(_live(args, work))


INSPECT_JS = r"""
() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const short = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const out = [];
  for (const e of document.querySelectorAll('button, a, input, select, textarea, [role=button], mat-select, label, h1, h2, h3, h4, h5, th')) {
    if (!vis(e)) continue;
    const tag = e.tagName.toLowerCase();
    const item = {tag, id: e.id || '', name: e.getAttribute('name') || '', fcn: e.getAttribute('formcontrolname') || '',
                  type: e.getAttribute('type') || '', cls: short(e.className && e.className.baseVal === undefined ? e.className : ''),
                  href: tag === 'a' ? (e.getAttribute('href') || '') : '', aria: e.getAttribute('aria-label') || ''};
    if (tag === 'input' || tag === 'textarea') item.placeholder = e.getAttribute('placeholder') || '';   // never the value
    else if (tag === 'select') item.options = Array.from(e.options).map(o => short(o.label || o.textContent));
    else item.text = short(e.innerText);
    out.push(item);
  }
  return {url: location.pathname, title: document.title, elements: out};
}
"""


def cmd_inspect(args) -> int:
    async def work(settings, keeper):
        log = RunLog.create(settings.runs_dir, "inspect")
        await keeper.ensure_healthy(log)
        page = await keeper.browser.active()
        report = await page.evaluate(INSPECT_JS)
        log.write_json("inspect.json", report)
        print(f"page: {report['url']}  ({len(report['elements'])} visible controls)")
        for e in report["elements"]:
            ident = " ".join(f"{k}={e[k]}" for k in ("id", "name", "fcn", "type", "href", "aria") if e.get(k))
            label = e.get("text") or e.get("placeholder") or ""
            extra = f" options={e['options']}" if e.get("options") else ""
            print(f"  {e['tag']:8} {ident[:70]:70} | {label}{extra}")
        return 0
    return asyncio.run(_live(args, work))


def cmd_record(args) -> int:
    from .hospital.recorder import RECORDER_JS, summarize

    async def work(settings, keeper):
        log = RunLog.create(settings.runs_dir, "record")
        await keeper.ensure_healthy(log)
        events: list[dict] = []

        async def on_event(source, event):
            if isinstance(event, dict):
                event.pop("value", None)
                events.append(event)
                log.event("recorded", **event)
                print("  " + summarize(event), flush=True)

        await keeper.context.expose_binding("__wardosRecord", on_event)
        script = f"({RECORDER_JS})({json.dumps(keeper.portal.origins)})"
        await keeper.context.add_init_script(script=script)
        for page in keeper.context.pages:
            try:
                await page.evaluate(RECORDER_JS, keeper.portal.origins)
            except Exception:
                pass
        print("\nRecording. Do the task in the Chrome window (search, choose rows, Print...).")
        print("Only WHICH controls you use are recorded - never what you type.")
        await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter here when you have finished... ")
        log.write_json("trace.json", events)
        print(f"\nSaved {len(events)} steps to {log.directory / 'trace.json'}")
        return 0
    return asyncio.run(_live(args, work))


def cmd_survey(args) -> int:
    from .hospital.survey import catalog_lines, save_catalog, survey

    async def work(settings, keeper):
        log = RunLog.create(settings.runs_dir, "survey")
        await keeper.ensure_healthy(log)
        catalog = await survey(keeper, log, max_tiles=args.max_tiles)
        log.write_json("catalog.json", catalog)
        if keeper.firewall_log:
            log.write_json("firewall.json", keeper.firewall_log)
        print()
        print("\n".join(catalog_lines(catalog)))
        print(f"\nMap saved: {save_catalog(settings, catalog)}  (run folder: {log.directory})")
        return 0
    return asyncio.run(_live(args, work))


def _inputs_from_args(args) -> dict:
    inputs = {}
    for pair in args.input or []:
        name, _, value = pair.partition("=")
        inputs[name.strip()] = value.strip()
    if getattr(args, "uhid_file", None):
        inputs["uhid"] = _uhid_from_args(args)
    return inputs


def cmd_explore(args) -> int:
    from .hospital.explore import Explorer, map_text

    inputs = _inputs_from_args(args)
    goal = args.goal.format(**inputs) if inputs else args.goal
    site_map = None
    if args.map:
        settings = load_settings()
        path = settings.private_dir / "catalog" / f"{args.portal}.json"
        if not path.is_file():
            print(f"No map yet ({path.name}); run 'survey' first.")
            return 2
        site_map = map_text(json.loads(path.read_text(encoding="utf-8")))

    async def work(settings, keeper):
        await keeper.ensure_healthy(RunLog.create(settings.runs_dir, "explore-login"))
        rows = []
        for number in range(1, args.runs + 1):
            log = RunLog.create(settings.runs_dir, "explore")
            explorer = Explorer(keeper, keeper.models, log, max_steps=args.max_steps, minutes=args.minutes,
                                private_console=args.private)
            result = await explorer.run(goal, hint=args.hint, site_map=site_map, inputs=inputs,
                                        expect_text=args.expect_text, expect_url=args.expect_url, shown_goal=args.goal)
            log.write_json("result.json", result.to_dict())
            log.write_json("trace.json", result.trace)
            if keeper.firewall_log:
                log.write_json("firewall.json", keeper.firewall_log)
            rows.append((number, result, log.directory))
            print(f"\nRun {number}: {result.status}, check {result.verified}, {len(result.steps)} steps, "
                  f"{result.seconds}s (model {result.model_seconds}s), refused clicks {result.blocked}, "
                  f"ended on {result.final_path}")
            if args.show_answer and result.answer:
                print(f"Answer: {result.answer}")
            elif result.answer:
                print(f"Answer saved in {log.directory / 'result.json'} (use --show-answer to print it)")
        if len(rows) > 1:
            done = sum(1 for _, r, _ in rows if r.status == "DONE")
            passed = sum(1 for _, r, _ in rows if r.verified == "PASS")
            print(f"\nSummary: {done}/{len(rows)} finished, {passed}/{len(rows)} passed the check, "
                  f"median {sorted(r.seconds for _, r, _ in rows)[len(rows) // 2]}s")
        return 0
    return asyncio.run(_live(args, work))


def cmd_learn(args) -> int:
    from pathlib import Path
    from .config import SKILLS_DIR
    from .hospital import learn as L
    from .hospital.skills import LayeredSkillLibrary
    from .models import get_model_client

    settings = load_settings()
    run_dir = Path(args.from_run)
    events = json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))
    inputs = L.parse_input_spec(args.input_spec)
    columns = [c.strip() for c in args.table_columns.split(",")] if args.table_columns else None
    library = LayeredSkillLibrary([L.staging_dir(settings), SKILLS_DIR])
    replay_inputs = _inputs_from_args(args)
    replay_values = {k: v for k, v in replay_inputs.items() if k != "uhid"}

    async def compile_it():
        models = get_model_client(settings)
        try:
            return await L.compile_skill(models, events=events, name=args.name, portal=args.portal,
                                         description=args.description, inputs=inputs, library=library,
                                         table_columns=columns, source=f"learned from recording {run_dir.name} ({datetime_now()})",
                                         exclude=tuple(x for x in [args.compare] if x), values=replay_values,
                                         keeps_pdf=any(re.search(r"print|download", str(e.get("text") or ""), re.I)
                                                       for e in events if e.get("kind") == "click"))
        finally:
            if hasattr(models, "aclose"):
                await models.aclose()

    skill, lines = asyncio.run(compile_it())
    if any(s["action"] == "capture_pdf" for s in skill["steps"]):
        skill.setdefault("produces", "lab_pdf")
    print(f"Recording: {len(events)} events -> {len(lines)} condensed steps")
    for line in lines:
        print("   " + line[:160])
    staged = L.save_staged(settings, skill)
    print(f"\nLearned skill '{skill['skill']}' ({len(skill['steps'])} steps), staged at {staged}")
    print("\n".join(L.skill_outline(skill)))
    if args.compare:
        print(f"\nCompared with the hand-written '{args.compare}':")
        print("\n".join(L.compare(skill, SkillLibrary().load(args.portal, args.compare))))
    if not args.verify:
        print("\nNot replayed yet (add --verify to replay it live; it is saved to the skill library only if it passes).")
        return 0

    async def work(settings_, keeper):
        passed, message, _, _ = await L.verify_skill(keeper, skill, replay_inputs, settings=settings, lines=lines,
                                                     inputs=inputs, library_root=SKILLS_DIR)
        print(("PASSED: " if passed else "NOT SAVED: ") + message)
        return 0 if passed else 2
    return asyncio.run(_live(args, work))


def cmd_ask(args) -> int:
    import re
    from .hospital.brain import HospitalBrain

    if re.search(r"\d{6,}", args.question):
        print("Keep identifiers out of the question. Use {uhid} and --uhid-file.")
        return 2
    for pair in args.input or []:
        key, separator, value = pair.partition("=")
        if (not separator or not key.strip() or not value.strip()
                or re.search(r"uhid|patient|registration|password|secret|token|otp|credential", key, re.I)):
            print("Use --input name=value for non-private inputs; use --uhid-file for patient identity.")
            return 2
    try:
        inputs = _inputs_from_args(args)
        if args.uhid_file and not inputs.get("uhid"):
            print("The UHID file does not contain an identifier.")
            return 2
        verification_uhid = None
        if args.verify_uhid_file:
            verification_uhid = _uhid_from_args(argparse.Namespace(uhid=None, uhid_file=args.verify_uhid_file))
            if not verification_uhid or verification_uhid == inputs.get("uhid"):
                print("The verification file must contain a different patient identifier.")
                return 2
    except (OSError, ValueError, TypeError, AttributeError):
        print("An identifier file could not be read. Use plain text or a JSON object containing uhid.")
        return 2
    args.firewall, args.read_like = "enforce", True

    async def work(settings, keeper):
        result = await HospitalBrain(keeper, settings).run(
            args.question, inputs, max_steps=args.max_steps, minutes=args.minutes,
            verification_uhid=verification_uhid)
        print(f"Decision: {result.decision}; status: {result.status}; time: {result.seconds}s.")
        if result.skill:
            print(f"Skill: {result.skill}; learned: {result.learned}; repaired: {result.repaired}.")
        if result.note:
            print(result.note)
        print(f"Answer and run details saved privately: {result.run_folder}")
        return 0 if result.status == "ANSWERED" else 2

    return asyncio.run(_live(args, work))


def _question_problem(question: str) -> str | None:
    import re
    if re.search(r"\d{6,}", question):
        return "Keep identifiers out of the question. Use {uhid} and --uhid-file."
    return None


def cmd_chat(args) -> int:
    from pathlib import Path
    from .hospital.brain import HospitalBrain

    questions = None
    if args.questions_file:
        questions = [q.strip() for q in Path(args.questions_file).read_text(encoding="utf-8").splitlines()
                     if q.strip() and not q.lstrip().startswith("#")]
        bad = next((p for p in map(_question_problem, questions) if p), None)
        if bad:
            print(bad)
            return 2
    try:
        inputs = _inputs_from_args(args)
    except (OSError, ValueError, TypeError, AttributeError):
        print("The UHID file could not be read. Use plain text or a JSON object containing uhid.")
        return 2
    args.firewall, args.read_like = "enforce", True

    async def work(settings, keeper):
        brain = HospitalBrain(keeper, settings)
        await keeper.ensure_healthy(RunLog.create(settings.runs_dir, "chat-login"))
        loop = asyncio.get_running_loop()
        pending = iter(questions) if questions is not None else None
        answered = failed = 0
        while True:
            if pending is not None:
                question = next(pending, None)
            else:
                ask_task = loop.run_in_executor(None, input, "\nQuestion (empty line to stop): ")
                while True:
                    done, _ = await asyncio.wait({ask_task}, timeout=args.interval * 60)
                    if done:
                        break
                    async with keeper._lock:
                        await keeper.ensure_healthy(RunLog.create(settings.runs_dir, "keep-alive", console=False))
                question = (ask_task.result() or "").strip() or None
            if question is None:
                break
            problem = _question_problem(question)
            if problem:
                print(problem)
                continue
            result = await brain.run(question, inputs, max_steps=args.max_steps, minutes=args.minutes)
            answered += result.status == "ANSWERED"
            failed += result.status != "ANSWERED"
            print(f"\nQ: {question}\nDecision: {result.decision}; status: {result.status}; time: {result.seconds}s."
                  + (f" Skill: {result.skill}; learned: {result.learned}; repaired: {result.repaired}." if result.skill else ""))
            if result.note:
                print(result.note)
            if args.show_answer and result.answer:
                print(f"Answer: {result.answer}")
            print(f"Run folder: {result.run_folder}")
        print(f"\nDone: {answered} answered, {failed} not answered.")
        return 0 if failed == 0 else 2

    return asyncio.run(_live(args, work))


def cmd_do(args) -> int:
    from .hospital.approve import find_final_button, press_approved, prepare
    from .hospital.explore import Explorer

    problem = _question_problem(args.task)
    if problem:
        print(problem)
        return 2
    if args.yes_demo and args.portal != "openmrs":
        print("--yes-demo only works on the OpenMRS demo (fake patients). Real portals need a typed YES.")
        return 2
    inputs = _inputs_from_args(args)
    goal = args.task.format(**inputs) if inputs else args.task
    args.firewall, args.read_like = "enforce", True

    async def work(settings, keeper):
        log = RunLog.create(settings.runs_dir, "do")
        await keeper.ensure_healthy(log)
        explorer = Explorer(keeper, keeper.models, log, max_steps=args.max_steps, minutes=args.minutes,
                            private_console=True)
        result = await explorer.run(goal, inputs=inputs, shown_goal=args.task)
        log.write_json("exploration.json", result.to_dict())
        log.write_json("trace.json", result.trace)
        print(f"\nAgent: {result.status} in {result.seconds}s ({len(result.steps)} steps).")
        pending = result.pending
        if not pending and result.status == "DONE":
            pending = await find_final_button(await keeper.browser.active()) or {}
        if not pending:
            print("It did not stop at one clear data-changing button on the final screen, so there is nothing to approve.")
            return 2
        proposal = await prepare(keeper, pending, log)
        print("\n" + "\n".join(proposal.lines()))
        if args.yes_demo:
            approved = True
            print("(demo site: approved automatically by --yes-demo)")
        else:
            answer = await asyncio.get_running_loop().run_in_executor(
                None, input, f"\nType YES to press '{proposal.button}' now (anything else cancels): ")
            approved = answer.strip() == "YES"
        log.event("approval", approved=approved, button=proposal.button)
        if not approved:
            print("Cancelled. Nothing was pressed; the write firewall stayed closed.")
            return 0
        outcome = await press_approved(keeper, proposal, log)
        print(f"\nResult: {outcome['status']}. Write requests sent: {len(outcome['writes'])} "
              + (f"({', '.join(outcome['writes'])})" if outcome["writes"] else ""))
        if outcome["errors"]:
            print(f"Message on screen: {outcome['errors']}")
        if keeper.firewall_log:
            log.write_json("firewall.json", keeper.firewall_log)
        print(f"Run folder: {log.directory}")
        return 0 if outcome["status"] == "SUBMITTED" else 2

    return asyncio.run(_live(args, work))


def datetime_now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _uhid_from_args(args) -> str:
    if args.uhid:
        return args.uhid
    from pathlib import Path
    text = Path(args.uhid_file).read_text(encoding="utf-8").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict):
        return str(data.get("uhid") or data.get("patient") or "").strip()
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, int) and not isinstance(data, bool):
        return text
    raise ValueError("Use plain text or a JSON object containing uhid.")


def _ingest(settings, portal_name: str, uhid: str, pdf_paths: list[str]) -> list[str]:
    from pathlib import Path
    from .record.labs import LabRecord, parse_lis_pdf
    record = LabRecord(settings.private_dir / "records", portal_name, uhid)
    messages = []
    for path in pdf_paths:
        values, warnings = parse_lis_pdf(Path(path), uhid)
        stats = record.add(values, Path(path))
        count = sum(1 for v in values if v.kind == "value")
        messages.append(f"read {count} values from {Path(path).name}: {stats['added']} new, {stats['corrected']} corrected"
                        + (f"; {len(warnings)} warning(s): {warnings[:3]}" if warnings else ""))
    return messages


def cmd_import_pdf(args) -> int:
    settings = load_settings()
    for line in _ingest(settings, args.portal, _uhid_from_args(args), args.pdf):
        print(line)
    return 0


def cmd_latest(args) -> int:
    from datetime import datetime, timezone
    from .hospital.ledger import Ledger
    from .record.labs import LabRecord, answer_latest
    settings = load_settings()
    portal = get_portal(args.portal)
    uhid = _uhid_from_args(args)
    ledger = Ledger(settings.ledger_dir, portal.name, uhid)
    record = LabRecord(settings.private_dir / "records", portal.name, uhid)
    had_values = bool(record.load()["values"])
    last = ledger.last_checked()
    age_min = ((datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60) if last else None
    note, new_since = None, None
    if args.offline or (age_min is not None and age_min <= args.max_age):
        print(f"Using stored results (hospital system checked {age_min:.0f} min ago)." if age_min is not None
              else "Using stored results (offline).")
    else:
        print(f"Hospital system last checked: {'never' if last is None else f'{age_min:.0f} min ago'} -> checking for new reports")
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")

        async def work(settings_, keeper):
            return await keeper.run_skill("lis.fetch_lab_reports", {"uhid": uhid, "select": "new"})
        try:
            result = asyncio.run(_live(args, work))
        except Exception as exc:
            result, note = None, f"check failed: {type(exc).__name__}: {str(exc)[:120]}; answering from stored data"
        if result is not None:
            if result.status == "FOUND":
                for line in _ingest(settings, portal.name, uhid, [a["path"] for a in result.artifacts if a["check"]["ok"]]):
                    print("  " + line)
                new_since = started if had_values else None
            elif result.status == "NOTHING_NEW":
                print("  no new reports since the last check")
            elif result.status == "NOT_FOUND":
                print("  the hospital system shows no results for this patient")
            else:
                note = f"check failed at step '{result.failed_step}': {result.reason}; answering from stored data"
    print()
    print(answer_latest(record, args.test, checked_at=ledger.last_checked(), new_since=new_since, portal_note=note))
    return 0


def cmd_fetch_reports(args) -> int:
    async def work(settings, keeper):
        inputs = {"uhid": _uhid_from_args(args), "select": args.select}
        if args.days or args.hours:
            inputs["select"] += f",days:{args.days}" if args.days else f",hours:{args.hours}"
        if args.from_date:
            inputs["from_date"] = args.from_date
        if args.to_date:
            inputs["to_date"] = args.to_date
        result = await keeper.run_skill(args.skill, inputs)
        summary = {"status": result.status, "seconds": result.seconds,
                   "session_recoveries": result.session_recoveries,
                   "logins": keeper.logins, "otps": keeper.otps,
                   "pdfs": [{"file": a["path"], "ok": a["check"]["ok"], "reasons": a["check"]["reasons"]}
                            for a in result.artifacts],
                   "failed_step": result.failed_step, "reason": result.reason,
                   "drift_warnings": result.drift_warnings}
        print(json.dumps(summary, indent=2))
        if result.status == "FOUND":
            for line in _ingest(settings, keeper.portal.name, inputs["uhid"],
                                [a["path"] for a in result.artifacts if a["check"]["ok"]]):
                print(line)
        return 0 if result.status in {"FOUND", "NOTHING_NEW", "NOT_FOUND"} else 2
    return asyncio.run(_live(args, work))


def cmd_keep_alive(args) -> int:
    async def work(settings, keeper):
        log = RunLog.create(settings.runs_dir, "keep-alive")
        stop = asyncio.Event()
        task = asyncio.create_task(keeper.keep_alive(args.interval * 60, stop, log))
        try:
            await asyncio.sleep(args.minutes * 60)
        finally:
            stop.set()
            await task
        return 0
    return asyncio.run(_live(args, work))


def cmd_logout(args) -> int:
    from .hospital.portals.base import Health

    async def work(settings, keeper):
        log = RunLog.create(settings.runs_dir, "logout")
        if keeper.login_file.is_file() and await keeper.health(log) is not Health.LOGGED_OUT:
            await keeper.logout(log)
        keeper.forget_login()
        print("Logged out; no saved login is kept.")
        return 0
    return asyncio.run(_live(args, work))


def cmd_watch(args) -> int:
    from datetime import timedelta
    import re
    from .config import hospital_now
    from .hospital.watch import Watch, parse_every, parse_until
    from .record.labs import LabRecord
    every = parse_every(args.every)
    keep_alive = parse_every(args.keep_alive) if args.keep_alive else None
    now = hospital_now()
    until = parse_until(args.until, now) if args.until else now + timedelta(seconds=parse_every(args.duration))

    async def work(settings, keeper):
        uhid = _uhid_from_args(args)
        name = keeper.portal.name
        keeper.max_recoveries = 2

        async def fetch():
            result = await keeper.run_skill(args.skill, {"uhid": uhid, "select": "new"})
            return result.status, [a["path"] for a in result.artifacts if a["check"]["ok"]]

        async def visit():
            log = RunLog.create(settings.runs_dir, "watch-visit", console=False)
            async with keeper._lock:
                await keeper.ensure_healthy(log)

        slug = re.sub(r"[^a-z0-9]+", "-", args.test.lower())
        watch = Watch(test=args.test, every_s=every, until=until, keep_alive_s=keep_alive,
                      record=LabRecord(settings.private_dir / "records", name, uhid),
                      log_path=settings.private_dir / "watch" / f"{now:%Y%m%d-%H%M}-{slug}.log",
                      stop_path=settings.private_dir / "watch" / "STOP", fetch=fetch, visit=visit,
                      ingest=lambda paths: _ingest(settings, name, uhid, paths),
                      logins=lambda: keeper.logins, otps=lambda: keeper.otps)
        print(f"Log: {watch.log_path}")
        return await watch.run()
    try:
        return asyncio.run(_live(args, work))
    except KeyboardInterrupt:
        print("Watch stopped.")
        return 0


def cmd_watch_stop(args) -> int:
    path = load_settings().private_dir / "watch" / "STOP"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop", encoding="utf-8")
    print("The watch stops within a few seconds.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="wardos")
    parser.add_argument("--portal", default="ehospital")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--live-model", action="store_true",
                       help="also call the model: 3 fake CAPTCHAs + 3 JSON replies (no portal)")
    check.set_defaults(func=cmd_check)
    sub.add_parser("skills").set_defaults(func=cmd_skills)
    imp = sub.add_parser("import-pdf", help="read already-downloaded result PDFs into the patient record")
    imp_who = imp.add_mutually_exclusive_group(required=True)
    imp_who.add_argument("--uhid")
    imp_who.add_argument("--uhid-file")
    imp.add_argument("pdf", nargs="+")
    imp.set_defaults(func=cmd_import_pdf)
    for name, func in (("login", cmd_login), ("fetch-reports", cmd_fetch_reports), ("keep-alive", cmd_keep_alive),
                       ("inspect", cmd_inspect), ("record", cmd_record), ("latest", cmd_latest)):
        p = sub.add_parser(name)
        p.add_argument("--headless", action="store_true")
        p.add_argument("--firewall", choices=["observe", "enforce"], default="observe")
        p.set_defaults(func=func)
        if name == "login":
            p.add_argument("--hold", action="store_true", help="keep the browser open afterwards")
        if name == "fetch-reports":
            who = p.add_mutually_exclusive_group(required=True)
            who.add_argument("--uhid")
            who.add_argument("--uhid-file", help="file holding the UHID (keeps it off the command line)")
            p.add_argument("--select", choices=["new", "all"], default="new")
            p.add_argument("--skill", default="lis.fetch_lab_reports", help="lab skill to run (e.g. a learned one)")
            p.set_defaults(firewall="enforce", read_like=True)
            span = p.add_mutually_exclusive_group()
            span.add_argument("--days", type=int, help="only rows collected in the last N calendar days, today "
                                                           "included, from midnight (date column)")
            span.add_argument("--hours", type=float, help="only rows collected in the last N hours (date column)")
            p.add_argument("--from", dest="from_date")
            p.add_argument("--to", dest="to_date")
        if name == "latest":
            who = p.add_mutually_exclusive_group(required=True)
            who.add_argument("--uhid")
            who.add_argument("--uhid-file")
            p.add_argument("--test", required=True, help="e.g. creatinine, sodium, hb, bilirubin")
            p.add_argument("--max-age", type=float, default=15, help="minutes; older than this -> check the portal first")
            p.add_argument("--offline", action="store_true", help="never contact the portal")
        if name == "keep-alive":
            p.add_argument("--minutes", type=float, default=30)
            p.add_argument("--interval", type=float, default=5, help="minutes between health checks")
    logout_p = sub.add_parser("logout", help="end the saved hospital login now")
    logout_p.add_argument("--headless", action="store_true")
    logout_p.set_defaults(func=cmd_logout, firewall="enforce", read_like=True)
    watch_p = sub.add_parser("watch", help="keep checking one test for one patient, only while this runs")
    watch_who = watch_p.add_mutually_exclusive_group(required=True)
    watch_who.add_argument("--uhid", help=argparse.SUPPRESS)
    watch_who.add_argument("--uhid-file", help="file holding the patient identifier")
    watch_p.add_argument("--test", required=True, help="e.g. potassium")
    watch_p.add_argument("--every", default="15m", help="between checks: 15m, 1m, 90s")
    watch_end = watch_p.add_mutually_exclusive_group(required=True)
    watch_end.add_argument("--until", help="hospital clock time, e.g. 08:00")
    watch_end.add_argument("--duration", help="e.g. 1h, 90m")
    watch_p.add_argument("--keep-alive", help="a small page visit this often between checks, e.g. 10m (only useful "
                                              "when --every is longer than the portal's idle limit)")
    watch_p.add_argument("--skill", default="lis.fetch_lab_reports_learned")
    watch_p.add_argument("--headless", action="store_true")
    watch_p.set_defaults(func=cmd_watch, firewall="enforce", read_like=True)
    sub.add_parser("watch-stop", help="tell a running watch to stop").set_defaults(func=cmd_watch_stop)
    survey_p = sub.add_parser("survey", help="map every module and screen this account can open (read-only)")
    survey_p.add_argument("--headless", action="store_true")
    survey_p.add_argument("--max-tiles", type=int, default=30)
    survey_p.set_defaults(func=cmd_survey, firewall="enforce", read_like=True)
    ex = sub.add_parser("explore", help="figure-it-out test: the agent gets only a goal and finds the way (read-only)")
    ex.add_argument("--headless", action="store_true")
    ex.add_argument("--goal", required=True, help="e.g. \"How many beds are vacant in ward X?\"; may use {uhid}")
    ex.add_argument("--hint", help="one line, e.g. \"it is under IPD\"")
    ex.add_argument("--map", action="store_true", help="give the agent the survey map")
    ex.add_argument("--input", action="append", help="name=value used in the goal, e.g. ward=Surgery")
    ex.add_argument("--uhid", help=argparse.SUPPRESS)
    ex.add_argument("--uhid-file", help="file holding a UHID (input 'uhid'), kept off the command line")
    ex.add_argument("--expect-text", help="regex that must be on the final page for PASS")
    ex.add_argument("--expect-url", help="text that must be in the final URL for PASS")
    ex.add_argument("--max-steps", type=int, default=40)
    ex.add_argument("--minutes", type=float, default=10)
    ex.add_argument("--runs", type=int, default=1)
    ex.add_argument("--private", action="store_true", help="hide the agent's reasoning text in the console")
    ex.add_argument("--show-answer", action="store_true", help="print the agent's final answer")
    ex.set_defaults(func=cmd_explore, firewall="enforce", read_like=True)
    ln = sub.add_parser("learn", help="turn a recording (record/explore run folder) into a skill; --verify replays it live")
    ln.add_argument("--from-run", required=True, help="run folder holding trace.json")
    ln.add_argument("--name", required=True)
    ln.add_argument("--description", required=True)
    ln.add_argument("--input-spec", action="append", help="e.g. uhid:string:required or select:string:default=new:choices=new|all")
    ln.add_argument("--table-columns", help="comma-separated result-table columns (older recordings lack them)")
    ln.add_argument("--compare", help="existing skill to compare against")
    ln.add_argument("--verify", action="store_true", help="replay live; save to the library only if it passes")
    ln.add_argument("--input", action="append", help="replay input name=value")
    ln.add_argument("--uhid", help=argparse.SUPPRESS)
    ln.add_argument("--uhid-file", help="replay input 'uhid' from a file")
    ln.add_argument("--headless", action="store_true")
    ln.set_defaults(func=cmd_learn, firewall="enforce", read_like=True)
    ask = sub.add_parser("ask", help="ask a plain question; choose, learn and repair a read skill automatically")
    ask.add_argument("question", help="plain question; use {uhid} with --uhid-file for patient identity")
    ask.add_argument("--input", action="append", help="non-private input, e.g. ward=Surgery")
    ask.add_argument("--uhid-file", help="file holding the patient identifier")
    ask.add_argument("--verify-uhid-file", help="different patient's identifier for learning a new patient workflow")
    ask.add_argument("--headless", action="store_true")
    ask.add_argument("--max-steps", type=int, default=40)
    ask.add_argument("--minutes", type=float, default=10)
    ask.set_defaults(func=cmd_ask, firewall="enforce", read_like=True, uhid=None)
    chat = sub.add_parser("chat", help="log in once, answer many questions; the session stays warm between them")
    chat.add_argument("--questions-file", help="one question per line (# = comment); otherwise type them")
    chat.add_argument("--input", action="append", help="non-private input, e.g. ward=Surgery")
    chat.add_argument("--uhid-file", help="file holding the patient identifier")
    chat.add_argument("--show-answer", action="store_true", help="print answers (only for non-patient questions)")
    chat.add_argument("--interval", type=float, default=4, help="minutes between health checks while idle")
    chat.add_argument("--headless", action="store_true")
    chat.add_argument("--max-steps", type=int, default=40)
    chat.add_argument("--minutes", type=float, default=10)
    chat.set_defaults(func=cmd_chat, firewall="enforce", read_like=True, uhid=None)
    do = sub.add_parser("do", help="a task that changes something: fill, stop at the final button, press after YES")
    do.add_argument("task", help="e.g. \"Record vitals for patient {patient}: temperature 37.2\"")
    do.add_argument("--input", action="append", help="name=value used in the task")
    do.add_argument("--uhid-file", help="file holding the patient identifier (input 'uhid')")
    do.add_argument("--yes-demo", action="store_true", help="approve automatically - OpenMRS demo only")
    do.add_argument("--headless", action="store_true")
    do.add_argument("--max-steps", type=int, default=30)
    do.add_argument("--minutes", type=float, default=8)
    do.set_defaults(func=cmd_do, firewall="enforce", read_like=True, uhid=None)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

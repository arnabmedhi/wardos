<p align="center"><img src="wardos/chain/static/icons/icon-512.png" alt="WardOS logo" width="96"></p>

<h1 align="center">WardOS</h1>

<p align="center"><b>An agentic OS that handles a patient's paperwork from admission to discharge, for hospitals that still run on paper.</b></p>

<p align="center"><a href="https://wardos-chain-yoo4mmceqq-el.a.run.app/demo/start?fresh=1"><b>Try the live demo →</b></a> (no login, names changed) · <a href="VIDEO_LINK_PLACEHOLDER"><b>Watch the demo video</b></a></p>

<p align="center"><img src="docs/images/demo.gif" alt="The agent working on the Demo hospital" width="100%"></p>

## The agent works for the doctor

1. Photograph the paper file, or speak or type what you need.
2. WardOS reads it into one living record for the patient.
3. The agent does the hospital-website work, and watches a test overnight.
4. One click fills the discharge summary form. You check and decide.

<table>
<tr>
<td width="33%"><img src="docs/images/dashboard.jpg" alt="Ward dashboard"><br><sub><b>Ward today.</b> Demo, names changed.</sub></td>
<td width="33%"><img src="docs/images/alert.jpg" alt="Potassium 2.6 alert with graph"><br><sub><b>The agent watches.</b> It alerts only on danger.</sub></td>
<td width="33%"><img src="docs/images/skill.jpg" alt="Saved skill steps"><br><sub><b>The agent learns.</b> Its steps are saved as a skill.</sub></td>
</tr>
</table>

## By hand vs with WardOS

| | By hand | With WardOS |
|---|---|---|
| One discharge summary | 40-60 min | 4-6 min |

---

Built by a doctor at AIIMS Rishikesh, one of India's flagship government hospitals. Powered by Meta Muse Spark.

## The problem

Most hospital AI runs on electronic records. Most hospitals in developing countries don't have them. Even at AIIMS, every note and ward round is written by hand.

So the doctor becomes the API:

- Lab reports sit behind a government website with a CAPTCHA and an OTP, one patient at a time.
- Facts are scattered across a thick paper file.
- One discharge summary takes 40-60 minutes to type by hand.

## How WardOS helps

The doctor photographs the file and speaks or types what they need. WardOS keeps one living record for each patient, does the website work, and writes the summary. A doctor always checks and decides.

| | |
|---|---|
| **Reads** | A photo of a paper file becomes a structured record: labs by date, medicines and plan. Every number links to its source photo. |
| **Asks** | Ask a plain-language question about a patient and get an answer with the entries it used. |
| **Works** | One sentence starts hospital-website jobs. A robot browser signs in, finds the reports and reads the values with code. |
| **Learns** | A new job is worked out once by Meta Muse Spark and saved as a reusable skill. Later runs replay it without a model. |
| **Writes** | One click fills the discharge summary form from the record, using Muse Spark and a separate summary service. |
| **Watches** | Overnight watches keep checking a test and alert only on danger. |

## How Meta AI is used

All model calls in WardOS are Muse Spark. They go through one class, `MuseClient` in `wardos/models.py`: HTTPS requests to the Meta Model API (`/responses`). The default model is `muse-spark-1.3-contributor` at effort `high`. Set it with `WARDOS_MUSE_MODEL` and `WARDOS_MUSE_EFFORT`.

| Job | Input to Muse Spark | File and function |
|---|---|---|
| Read a paper page word for word | Photos | `wardos/chain/read.py` `read_photos` |
| Turn page text into checked facts | Text | `wardos/chain/extract.py` `extract_facts` |
| Answer a question about a patient | Record entries | `wardos/chain/askai.py` `answer_anything` |
| Turn a voice note or command into text | Audio | `wardos/chain/voice.py` `transcribe` |
| Turn a sentence into a list of jobs, or one question back | Text | `wardos/chain/agent_jobs.py` `plan` |
| Decide next, retry, other skill, ask or stop after a job | Job result | `wardos/chain/planner.py` `review` |
| Choose a saved skill, explore, clarify or refuse | Question and skill list | `wardos/hospital/brain.py` `choose` |
| Find the way through a site it has not seen | Screenshot and numbered controls | `wardos/hospital/explore.py` `Explorer.run` |
| Turn a recorded exploration into a skill | Recording | `wardos/hospital/learn.py` `compile_skill` |
| Repair a saved skill after a failed replay | Failure log and screen controls | `wardos/hospital/learn.py` `verify_skill` |
| Read the portal CAPTCHA | Image | `wardos/hospital/portals/ehospital.py` `login` |
| Write the summary fields from facts | Facts and unit rules | `wardos/chain/render.py` `write_fields` |

Plain code, with no model, does the rest: replaying skills, reading answers off the screen, checking quotes and dates, merging facts, and the danger rule.

## How it works

<p align="center"><img src="docs/images/architecture.svg" alt="WardOS architecture" width="760"></p>

### How the agent works

Blue steps use Meta Muse Spark. Green steps are plain code. Red is an alert.

- **Photos.** `read_photos` in `wardos/chain/read.py` reads each page word for word. `extract_facts` in `wardos/chain/extract.py` turns the text into candidate facts, each with its exact quote. Code checks that the quote is on the page before the fact enters the record.
- **Commands.** `plan` in `wardos/chain/agent_jobs.py` has Muse read every command and return a list of jobs, or one question with options. If Muse gives nothing usable, nothing starts and the command is not guessed at.
- **Queue and worker.** The worker in `wardos/agent/worker.py` asks WardOS for the next job, does it, and reports every step.
- **The brain.** `choose` in `wardos/hospital/brain.py` picks a saved skill, explores, asks the doctor, or refuses.
- **Saved skills.** A skill is a JSON step list, per site, in `skills/`. Plain code replays it in Chrome, with no model.
- **The firewall.** `WriteFirewall` in `wardos/hospital/firewall.py` blocks data-changing requests. `read_only_skill` in `wardos/chain/planner.py` refuses a saved skill that has a write-like control.
- **Values.** The answer is read off the screen by code, not taken from the model's words. It goes into the record.
- **Review.** `review` in `wardos/chain/planner.py` reads each result and decides: done, retry once, another saved skill or explore, ask the doctor, or give up. It never changes an answer, and it works inside hard limits.
- **Explore and learn.** `Explorer.run` in `wardos/hospital/explore.py` finds the way through a site it has not seen. `compile_skill` in `wardos/hospital/learn.py` turns the recording into a skill. `verify_skill` replays it, and the skill is saved only if the replay gives the same answer. After a failed replay, a skill can be repaired.
- **Watches.** `Watch` in `wardos/hospital/watch.py` checks one test for one patient, only when told. `is_dangerous` decides if a new value is dangerous.
- **Alerts.** `new_notifications` in `wardos/chain/alerts.py` finds new dangerous values and new reports. Web push messages carry no patient detail.

- **Doctor:** phone or laptop web app. Photos, voice and one sentence go in.
- **Server and record:** a FastAPI chain server (`wardos/chain/api.py`). Reading turns photo to text to facts, and every value is linked to its source. SQLite locally or Firestore. Alerts go out by web push, danger only.
- **Agents and planner:** a task queue and job list; the planner reviews each job result.
- **Meta Muse Spark:** all model calls, through `wardos/models.py`.
- **Hospital robot:** `wardos/agent` and `wardos/hospital`. Saved skills are replayed by plain code in Chrome, behind a write firewall.
- **Hospital website:** read-only, no API. Values are read by code into the record.

The discharge summary document is made by a separate service, started as a Cloud Run Job by `wardos/chain/finals.py`.

## Engineering highlights

- **One door to the model:** every call is Muse Spark, through `MuseClient`.
- **Learns once, replays without a model:** a new job becomes a saved skill, kept only if a replay gives the same answer.
- **Read-only robot:** a network firewall blocks data-changing requests.
- **Every value links to its source:** the code checks that the quote is really on the page.
- **Answers read by code:** from the screen, not from the model's words.

## Safety

- **The hospital robot is read-only.** Save, submit and discharge buttons are refused. A network firewall (`wardos/hospital/firewall.py`) blocks data-changing requests. It lets through login and requests whose names clearly read, such as ward lists fetched by POST, and it logs them.
- **No patient data is in this repo.** The guest demo uses changed names.
- **Every value links to its source.** A fact keeps its page and quote, and the code checks that the quote is really on the page.
- **Answers from a site are read by code from the screen,** not taken from the model's words. A new skill is saved only if a replay gives the same answer.
- **Sign-in:** doctors use Google sign-in and must be on the ward's list. The local test sign-in refuses to start on Cloud Run.

---

## Run it yourself

You need Python 3.12. Google Chrome is needed only for the hospital robot and browser tests.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-chain.txt
copy .env.example .env
```

Put your own Meta API key in `.env` as `MODEL_API_KEY=...`. On macOS or Linux, use `.venv/bin/python` and `cp`.

Start the app:

```powershell
.\.venv\Scripts\python.exe -m uvicorn wardos.chain.api:app --port 8080
```

Open <http://127.0.0.1:8080>. The `.env` from the example signs you in as a test doctor (`WARDOS_DEV_DOCTOR`), so do not use that setting on a public server. Your record starts empty, so add a patient and upload a photo of a note.

Run the tests (no keys and no network needed):

```powershell
.\.venv\Scripts\python.exe -m pytest tests tools/tests --ignore-glob="*_browser.py" --ignore=tests/test_fake_portal.py
```

To include the tests that open Chrome, leave out the two `--ignore` options.

### What runs where

| Part | Local | Needs |
|---|---|---|
| Web app, record, reading, asks, voice, draft summary fields | Yes (SQLite and a local folder) | `MODEL_API_KEY` |
| Hospital robot | Yes, with Chrome | `MODEL_API_KEY` and a site login. For OpenMRS: `WARDOS_OPENMRS_URL`, `OPENMRS_USERNAME`, `OPENMRS_PASSWORD` |
| Discharge summary document | No | The summary service, configured separately through `WARDOS_FINAL_JOB` and the `WARDOS_FINAL_*` Drive folders. Without it, the button says "summary service not configured" |
| Google sign-in, Firestore, Cloud Tasks, cloud robots | No | A Google Cloud project; see `.env.example`, `Dockerfile.chain`, `cloud/` |
| Web push alerts | Needs HTTPS or localhost | `WARDOS_VAPID_*` keys |

## Repo map

| Folder | What it holds |
|---|---|
| `wardos/chain/` | The server and web app: reading, record, asks, agents, planner, alerts, summary fields |
| `wardos/hospital/` | The robot's engine: brain, explorer, learning, replay, firewall, watch, site profiles |
| `wardos/agent/` | The worker that picks up doctors' tasks and runs them on the hospital site |
| `wardos/record/` | Lab PDF parsing |
| `wardos/models.py` | The one door to Meta Muse Spark |
| `skills/` | Saved skills as JSON step lists, per site, with no patient data |
| `tests/`, `tools/tests/` | Offline tests and browser tests on local pages |
| `tools/` | Demo-site seeding and helper scripts |
| `cloud/` | Container files for the cloud robots |

---

## Limits

- The demo data and real unit rules are not in this repo. The files in `wardos/chain/units/rules/` hold sample rules, so draft summaries follow those samples. `/demo/start` works only on the hosted demo.
- The final summary document needs the separate summary service.
- The real hospital site needs a hospital login with CAPTCHA and email OTP, so judges can try the robot only on the hosted demo or on their own OpenMRS.
- Photos, voice and record entries are sent to the Meta Model API to be read. Handwriting reading can be wrong, so a doctor must check every summary.
- WardOS is a working prototype. It is not a certified medical device.

## Licence

[MIT](LICENSE)

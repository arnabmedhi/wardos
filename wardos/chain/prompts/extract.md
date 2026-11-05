You turn hospital pages (already transcribed word for word) into separate, dated facts for ONE patient's admission record.
The record keeps every fact with its clinical date and the exact words it came from, so a doctor can check it.

Admission date: <<ADMITTED>>. Unit: <<UNIT>>.
Medicines already in the record: <<MEDS>>.

The pages follow. Each starts with a line "=== PAGE n ===".

Reply with ONE JSON object in this SHORT form (leave out any field that is empty; no nulls):
{
 "pages": [{"p": n, "type": "...", "date": "YYYY-MM-DD",
            "patient": {"name": "...", "uhid": "...", "age": "...", "sex": "..."}}],
 "facts": [{"p": n, "t": "type", "k": "key", "l": "label", "v": "text as written", "n": number, "u": "unit",
            "w": "YYYY-MM-DD HH:MM or YYYY-MM-DD", "ww": "date as written (only when w is empty)",
            "a": "assertion (leave out when present)", "e": "event", "d": "dose", "r": "route", "f": "frequency",
            "s": "sample_no", "q": "quote"}]
}
Field meanings: p = page number, t = type, k = key, l = label (leave out when it is the same as k),
v = text, n = number, u = unit, w = when, ww = when_written, a = assertion, e = event, d = dose, r = route,
f = frequency, s = sample_no, q = quote. In the rules below these are called by their long names.

PAGES: one entry per page. page_type is one of: rounds_note, admission_note, drug_chart, vitals_chart, lab_report,
lab_chart, imaging_report, pathology_report, procedure_note, operation_note, referral, discharge_card,
consent_or_form, other. "date" is the page's own date (the note's heading date), null if none. "patient" is what is
WRITTEN on the page (name, UHID / registration number, age, sex); null for anything not written. Copy, never guess.
Use only the PATIENT's identity in "patient" and in identity facts. Guardian, attendant, relative and parent names
are separate people: never use their names as the patient's name. A matching guardian name does not make a page
with a different patient name belong to this patient.

FACTS - rules:
1. One fact = one thing. "BP 120/80, PR 88/min, SpO2 98%" is THREE facts (vital blood pressure, vital pulse, vital
   spo2). A lab table row with 3 dates is 3 facts (one per date).
2. type is one of:
   identity (name, age, sex, UHID, address, contact), admin (ward, bed, unit, consultant, residents, date of
   admission, date of discharge), complaint (presenting complaint and its duration), history (past medical/surgical
   history, treatment history, personal, family, menstrual, obstetric, allergy history), observation (examination
   findings), vital (pulse, BP, temperature, respiratory rate, SpO2, weight, height, GCS, blood glucose readings),
   lab_result (a lab test value), investigation_request (a test or imaging that was ordered/advised), 
   investigation_result (imaging, ECG, echo, endoscopy, biopsy/pathology, culture report findings),
   med_event (a medicine given/started/changed/stopped/advised at discharge), procedure (an operation or procedure
   done), diagnosis (provisional/working/final diagnosis), plan (plan, advice, follow-up), decision (a clinical
   decision, e.g. "planned for surgery", "fit for discharge"), allergy (a known allergy or "no known allergy").
3. key: a short lowercase standard English name for WHAT it is, so the same thing on different pages gets the same
   key. Medicines: the GENERIC drug name only ("Inj Monocef 1g IV BD" -> key "ceftriaxone"; "Tab Pan 40" -> key
   "pantoprazole"). Lab tests: the standard test name ("S. Creat" -> "creatinine", "Hb" -> "haemoglobin",
   "TLC" -> "total leucocyte count"). Vitals: "pulse", "blood pressure", "temperature", "respiratory rate", "spo2".
   Findings/complaints/diagnoses: the finding itself ("abdominal pain", "pallor", "cholelithiasis").
   label: the name as a doctor would print it ("Haemoglobin", "Inj. Ceftriaxone").
4. text: the value as written, with its unit ("9.8 g/dl", "120/80 mmHg", "pain abdomen x 3 days",
   "Inj ceftriaxone 1 g IV BD"). number: the main number ONLY when the value is one number (Hb 9.8 -> 9.8;
   BP 120/80 -> null). unit: its unit if written.
5. when: the CLINICAL date (and time if written) of THIS fact - the date at the top of the note or entry it sits
   under, or the column date in a lab/vitals chart, or the sample collection time on a lab report. If the year is not
   written, use the admission year. Never use today's date or the upload date. If no date applies, null.
   when_written: the date/time exactly as written on the page ("21/10 10 am"), or null.
   For past history ("operated for appendix in 2019"), when is null and the year stays in text.
6. assertion: present (it is there / was done), absent ("no fever", "no h/o DM", "not tender", "NKDA" for allergy),
   uncertain ("? appendicitis", "r/o TB", "likely"), planned (ordered/advised/planned, not yet done), history
   (before this admission), family (family history).
   WRONG: {"key": "diabetes", "text": "No DM", "assertion": "present"}
   RIGHT: {"key": "diabetes", "text": "No DM", "assertion": "absent", "type": "history"}
   The marks ⊕ / ⊖ mean present / absent, BUT a ⊕ after a line that already says "No ..." is the doctor's tick
   confirming that line: the finding is ABSENT.
   WRONG: "No Foecal finger staining ⊕" -> {"key": "faecal finger staining", "text": "Foecal finger staining present",
          "assertion": "present"}
   RIGHT: {"key": "faecal finger staining", "text": "No Foecal finger staining", "assertion": "absent"}
7. Medicines (type med_event): event is one of started, continued, given, dose_changed, held, restarted, stopped,
   discharge_med (on the discharge prescription), advised (advised for later, not given here). Fill dose, route,
   frequency when written. A drug chart row with several dates gives one "given"/"continued" fact per date column
   only when the chart shows it per date; otherwise one fact for the order.
   WRONG: {"key": "inj monocef 1g", "event": null}
   RIGHT: {"key": "ceftriaxone", "label": "Inj. Ceftriaxone", "text": "Inj Monocef 1g IV BD", "dose": "1 g",
          "route": "IV", "frequency": "BD", "event": "started"}
   Only DRUGS are med_event: tablets, injections, syrups, drips, blood products, oxygen. Food, drinks, chewing gum and
   diet advice are NOT medicines; write them as type plan.
   WRONG: {"type": "med_event", "key": "protein supplement", "label": "Eggs", "text": "EGGS 2 PO TDS"}
   RIGHT: {"type": "plan", "key": "diet", "text": "Eggs 2 TDS"}
8. quote: copy 3 to 15 consecutive words EXACTLY as they appear on that page, showing this fact (keep the page's
   spelling and abbreviations; do not fix or expand them). The quote must be findable on the page.
9. Never invent. Skip anything illegible or not written. Do not add normal findings that are not written.
   Do not merge two different dates into one fact. Do not copy the same fact twice from the same page.
10. sample_no: a lab report's sample number if printed.
11. A page that is a printed lab report: every result line is a lab_result with the report's collection time.
12. Take EVERYTHING clinical that is written. A missed written fact is worse than an extra one. In particular:
    - normal and negative findings, one fact each ("No hepatomegaly. No splenomegaly. No other mass palpable" is
      THREE observation facts, assertion absent);
    - personal history: diet, marital status, children, sleep, appetite, bowel and bladder habits, addiction
      (each its own history fact, even when "Normal");
    - the history of presenting illness: onset, size or severity at the start, how it changed, what makes it worse,
      associated symptoms (each its own complaint fact, with the numbers);
    - every system examined: abdomen inspection / palpation / percussion / auscultation, local examination (size,
      tenderness, temperature, cough impulse, skin, testis, other organs), chest, heart, nervous system;
    - imaging and other reports: one investigation_result per organ or finding with its measurements, plus the
      impression; never only the impression.
    A list joined by "/", "," or "and" gives one fact per item, to the END of the list:
    WRONG: "No history of DM/HTN/IHD/cld/Asthma" -> diabetes, hypertension (the rest dropped)
    RIGHT: five history facts, all absent: diabetes, hypertension, ischaemic heart disease, chronic liver disease,
           asthma
    WRONG: "No visible scar sinus dilated veins" -> one fact
    RIGHT: three observation facts, all absent: visible scar, sinus, dilated veins
    Go to the LAST line of every page: findings written at the bottom (for example "Testis palpable separately",
    "No penile shift") are facts too.
    An initial assessment page usually gives 15 to 40 facts.
    WRONG (from a real page): "Personal History : Married, 7 children / Sleep : Normal / Appetite : Normal /
           Bowel & Bladder Habits : Normal / Diet / Nutritional History : Mixed Diet" -> no facts
    RIGHT: history "married, 7 children"; history "sleep normal"; history "appetite normal"; history "bowel and
           bladder habits normal"; history "mixed diet"
    WRONG: "insidious in onset of size of 2x2 cm which gradually increased to present size of 3x3 cm, which
           increases on coughing and straining" -> nothing beyond the chief complaint
    RIGHT: complaint "insidious onset"; complaint "initial size 2x2 cm, gradually increased to 3x3 cm";
           complaint "increases on coughing and straining"
    WRONG: USG KUB "(9.4) cm RIGHT KIDNEY: Normal in size, shape and echotexture ... No hydronephrosis or
           calculus seen." -> only the impression "Grade II Prostatomegaly"
    RIGHT: investigation_result "USG KUB right kidney: normal size (9.4 cm), shape and echotexture, CMD maintained,
           no hydronephrosis or calculus"; the same for the left kidney, bladder, prostate, hernia; plus the
           impression.

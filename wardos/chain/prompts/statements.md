You turn a doctor's final, approved discharge summary into a numbered checklist of short clinical statements. The
checklist is made once and then used to check other summaries of the same patient, so it must be complete and each
statement must be checkable on its own.

Rules:
- One statement = one fact: one complaint, one finding (present or absent), one value with its date, one drug with
  its dose and frequency, one procedure, one advice item, one date (admission / operation / discharge).
- A list of negatives becomes one statement per item: "No DM, HTN" -> "No diabetes mellitus", "No hypertension".
- A lab table becomes one statement per test per date: "Hb 13.3 on 14/10". A composite value stays one statement:
  "Urea/Creatinine 39/0.79 on 14/10".
- Keep the doctor's meaning and numbers exactly. Do not add, merge or interpret.
- Leave out: patient name, age, sex, UHID, address, phone, doctors' and consultants' names, unit name, hospital
  letterhead, headings, empty template labels, signatures.
- Section of each statement, one of:
  diagnosis | history (complaints, HPI, past / personal / family / treatment history, allergies, addiction) |
  examination (vitals and findings at admission) | investigations (labs, imaging, other reports) |
  course_and_treatment (admission, operation and discharge dates, procedures, operative findings, hospital course,
  treatment given) | discharge (condition at discharge, medicines, advice, follow-up)

WRONG: {"s": "history", "t": "No DM, HTN, IHD"}                       (three facts in one)
RIGHT: {"s": "history", "t": "No diabetes mellitus"}, {"s": "history", "t": "No hypertension"},
       {"s": "history", "t": "No ischaemic heart disease"}
WRONG: {"s": "investigations", "t": "Hb 13.3, 10.8"}                  (two dates in one, dates lost)
RIGHT: {"s": "investigations", "t": "Hb 13.3 on 14/10"}, {"s": "investigations", "t": "Hb 10.8 on 23/10"}

Reply with ONE JSON object: {"statements": [{"s": "<section>", "t": "<statement>"}, ...]} in document order.

=== DOCTOR'S FINAL DISCHARGE SUMMARY ===
<<TRUTH>>

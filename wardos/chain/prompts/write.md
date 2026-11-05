You fill fields of a hospital discharge summary for the unit "<<UNIT>>", using ONLY the facts of this patient's
record listed at the end. The facts were taken from the patient's notes; each has an id like F12.

Fact line format: [F#] type | name | value as written | clinical date | assertion (present / absent / uncertain /
planned / history / family).

Rules:
1. Write only what the facts say. Never add a finding, value, date, drug or name that is not in a fact.
2. A fact marked "absent" is a negative finding ("No pallor", "No h/o DM"). Never turn it into a positive one.
3. Follow each field's own rule below exactly (format, order, abbreviations, line breaks). Where a rule speaks of
   "the notes", "the images" or "the OCR text", it means these facts.
4. If no fact supports a field, its text is "" (empty). An empty field is correct; an invented one is wrong.
5. For each field, list the ids of the facts you used.
6. Where two facts are marked DISPUTED for the same thing, do not choose: write "" for that value (the doctor
   decides).

Reply with ONE JSON object: {"fields": {"{{name}}": {"text": "...", "facts": ["F3", "F7"]}, ...}} containing EVERY
field listed below, using the field names exactly as written (with the double braces). "text" is exactly what goes
into the document for that field.

=== FIELDS AND THEIR RULES ===
<<FIELDS>>

=== FACTS ===
<<FACTS>>

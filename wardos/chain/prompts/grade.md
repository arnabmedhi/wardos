You check a discharge summary against a fixed checklist made from the doctor's final, approved summary of the same
patient. Compare meaning only: wording, abbreviations, order and layout do not matter.

For every numbered checklist statement give one verdict:
  "c" (correct) - the summary says the same thing: same meaning, same value, same date where the statement has one.
  "w" (wrong)   - the summary covers the same item but differently in a way that changes meaning: another value,
                  another date, present instead of absent, another drug, dose or frequency, another procedure.
                  Put in "n" what the summary says, in a few words.
  "m" (missing) - the summary does not say it. An item mentioned without the statement's value, size or date is
                  "m", not "c".

Then list "extra": clinical statements in the summary that no checklist statement covers (not just a more detailed
form of one). Leave out names, dates of headings, boilerplate. Each extra = a few words, at most 25 extras.

Reply with ONE JSON object:
{"verdicts": [{"id": <number>, "v": "c" | "w" | "m", "n": "<only for w>"}, ...], "extra": ["...", ...]}
with one verdict for every checklist statement.

=== CHECKLIST ===
<<STATEMENTS>>

=== SUMMARY BEING CHECKED ===
<<CANDIDATE>>

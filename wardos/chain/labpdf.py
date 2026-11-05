"""Hospital lab-report PDFs, fetched or uploaded, become lab_result facts by code only."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

from . import model as m
from .normalize import fact_key, quote_found

LAB_MARKERS = re.compile(r"Lab\s*Cent(?:er|re)\s*Name|Sample\s*Collection", re.I)


def is_lab_report(page_texts: list[str]) -> bool:
    return bool(page_texts) and sum(1 for t in page_texts if LAB_MARKERS.search(t)) >= max(1, len(page_texts) // 2)


def lab_candidates(data: bytes, pages: list[dict], *, admission: dict, source_id: str,
                   uhid: str | None = None) -> tuple[list[dict], list[str]]:
    from ..record.labs import parse_lis_pdf
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        handle.write(data)
        path = Path(handle.name)
    try:
        values, warnings = parse_lis_pdf(path, expected_registration=uhid)
    finally:
        os.unlink(path)
    by_number = {p["number"]: p for p in pages}
    out = []
    for index, v in enumerate(values):
        page = by_number.get(v.page)
        if not page:
            continue
        if v.kind == "note":
            if not re.search(r"\d", v.value) and len(v.value.split()) <= 4:
                continue
            type_, text, number, quote = "investigation_result", v.value, None, v.value
        else:
            type_, text, number = "lab_result", " ".join(x for x in (v.value, v.flag, v.unit) if x), v.number
            quote = f"{v.analyte} {v.value}"
        value = {"text": text, "number": number, "unit": v.unit, "flag": v.flag, "reference": v.reference,
                 "sample_no": v.sample_no, "lab": v.lab, "panel": v.panel}
        doc = m.fact_doc(admission_id=admission["id"], source_id=source_id, page_id=page["id"], index=index,
                         type=type_, key=fact_key(type_, v.analyte), label=v.analyte, value=value,
                         clinical_time=v.collected_at, time_precision="exact" if v.collected_at else "unknown",
                         assertion="present", quote=quote, quote_found=quote_found(quote, page["text"]))
        doc["when_written"] = v.collected_at
        out.append(doc)
    return out, warnings

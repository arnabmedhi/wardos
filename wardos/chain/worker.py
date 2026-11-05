"""One upload batch becomes facts in the record: read, extract, identity check, merge."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import asyncio
import os
import time

from ..models import ModelClient, Usage
from . import model as m
from .extract import _year_misread, extract_facts, to_candidates
from .normalize import check_iso_time, same_patient
from .read import read_photos
from .labpdf import is_lab_report, lab_candidates
from .record import Record
from .settings import ChainSettings
from .voice import transcribe

EXTRACT_PAGES_PER_CALL = int(os.environ.get("WARDOS_EXTRACT_PAGES") or 3)
EXTRACT_PARALLEL = int(os.environ.get("WARDOS_EXTRACT_PARALLEL") or 4)
READ_PARALLEL = 12


@dataclass
class BatchReport:
    sources: int = 0
    duplicates: int = 0
    pages_read: int = 0
    facts_proposed: int = 0
    quotes_found: int = 0
    added: int = 0
    corroborated: int = 0
    disputed: int = 0
    superseded: int = 0
    questions: int = 0
    held_pages: int = 0
    read_cost_inr: float = 0.0
    extract_cost_inr: float = 0.0
    seconds: float = 0.0
    read_seconds: float = 0.0
    extract_seconds: float = 0.0
    model_seconds: float = 0.0
    merge_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


def pdf_text_pages(data: bytes) -> list[str]:
    import io
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            texts = [(page.extract_text() or "").strip() for page in pdf.pages]
    except Exception:
        return []
    return texts if texts and all(len(t) >= 40 for t in texts) else []


IST = timezone(timedelta(hours=5, minutes=30))


def _voice_header(src: dict) -> str:
    try:
        when = datetime.fromisoformat(src["received_at"]).astimezone(IST).strftime("%d %b %Y, %H:%M")
    except (KeyError, TypeError, ValueError):
        when = "time not known"
    return f"Voice note (recorded {when}):\n"


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _cost(usage: Usage | None) -> float:
    return float(usage.cost_inr or 0.0) if usage else 0.0


def _adopt_identity(record: Record, patient: dict, pages: list[dict]) -> dict:
    if patient.get("name") or patient.get("uhid"):
        return patient
    names = Counter((p.get("patient_seen") or {}).get("name") for p in pages if (p.get("patient_seen") or {}).get("name"))
    uhids = Counter((p.get("patient_seen") or {}).get("uhid") for p in pages if (p.get("patient_seen") or {}).get("uhid"))
    fields = {}
    if names:
        fields["name"] = names.most_common(1)[0][0]
    if uhids:
        fields["uhid"] = uhids.most_common(1)[0][0]
    if not fields:
        return patient
    fields["identity_status"] = "from pages, not confirmed"
    return record.update_patient(patient["id"], **fields)


UNDATED_BY_NATURE = {"identity", "history", "allergy"}


def _date_from_page(candidates: list[dict], page_info: dict[str, dict], admission: dict,
                    texts: dict[str, str] | None = None) -> None:
    texts = texts or {}
    anchor = admission.get("admitted_at") or admission.get("reference_date")
    try:
        around = date.fromisoformat(anchor[:10]) if anchor else None
    except ValueError:
        around = None
    for cand in candidates:
        if cand.get("clinical_time") or cand["type"] in UNDATED_BY_NATURE or cand["assertion"] in ("history", "family"):
            continue
        info = page_info.get(cand["page_id"]) or {}
        page_date = info.get("page_date")
        if not page_date and info.get("page_type") == "admission_note":
            page_date = admission.get("admitted_at")
        when, precision = check_iso_time(page_date)
        if when and around and abs((date.fromisoformat(when[:10]) - around).days) > 400:
            when, precision = _year_misread(None, when, texts.get(cand["page_id"], ""), around)
        if not when:
            continue
        cand.update(clinical_time=when[:10], time_precision="day", time_from="page")


STUCK_AFTER_SECONDS = 300


def _stuck_sources(record: Record, admission_id: str, skip: set[str]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STUCK_AFTER_SECONDS)
    out = []
    for s in record.store.query(m.SOURCES, admission_id=admission_id):
        if s["id"] in skip or s["status"] != m.SRC_READ:
            continue
        try:
            if datetime.fromisoformat(s["received_at"]) > cutoff:
                continue
        except (TypeError, ValueError):
            continue
        out.append(s)
    return out


async def process_batch(record: Record, model: ModelClient, settings: ChainSettings, admission_id: str,
                        source_ids: list[str], *, saved_text: dict[str, str] | None = None) -> BatchReport:
    started = time.monotonic()
    report = BatchReport(sources=len(source_ids))
    admission = await asyncio.to_thread(record.admission, admission_id)
    sources = [await asyncio.to_thread(record.source, s) for s in source_ids]
    sources += await asyncio.to_thread(_stuck_sources, record, admission_id, set(source_ids))
    todo = [s for s in sources if s["status"] not in (m.SRC_MERGED, m.SRC_DUPLICATE)]

    unread = [s for s in todo if s["status"] == m.SRC_RECEIVED]
    photos = []
    for src in unread:
        if saved_text and src["id"] in saved_text:
            await asyncio.to_thread(record.save_pages, src["id"], [{"number": 1, "text": saved_text[src["id"]]}])
        elif src["kind"] == "text":
            raw_text = (await asyncio.to_thread(record.read_file, src)).decode("utf-8", "replace")
            await asyncio.to_thread(record.save_pages, src["id"], [{"number": 1, "text": raw_text}])
        elif src["kind"] == "photo":
            photos.append(src)
        elif src["kind"] == "voice":
            try:
                text, usage = await transcribe(model, await asyncio.to_thread(record.read_file, src), src.get("mime") or "audio/webm",
                                               model_id=settings.model)
            except Exception as error:
                report.errors.append(f"{src['id']}: voice note not written down: {type(error).__name__}: "
                                     f"{str(error)[:120]}")
                await asyncio.to_thread(record.update_source, src["id"], status=m.SRC_FAILED, error="voice note not written down")
                continue
            report.read_cost_inr += _cost(usage)
            await asyncio.to_thread(record.save_pages, src["id"], [{"number": 1, "text": f"{_voice_header(src)}{text}"}])
            await asyncio.to_thread(record.update_source, src["id"], model=settings.model, cost_inr=_cost(usage), seconds=usage.seconds)
        elif src["kind"] in ("pdf", "portal_report"):
            data = await asyncio.to_thread(record.read_file, src)
            texts = pdf_text_pages(data)
            if texts:
                saved = await asyncio.to_thread(record.save_pages, src["id"], [{"number": i + 1, "text": t} for i, t in enumerate(texts)])
                if is_lab_report(texts):
                    patient = await asyncio.to_thread(record.patient_of, admission_id)
                    cands, warnings = lab_candidates(data, saved, admission=admission, source_id=src["id"],
                                                     uhid=patient.get("portal_uhid") or patient.get("uhid"))
                    report.errors += [f"{src['id']}: {w}" for w in warnings[:5]]
                    summary = await asyncio.to_thread(record.apply, src["id"], cands)
                    report.facts_proposed += len(cands)
                    report.quotes_found += sum(1 for c in cands if c["quote_found"])
                    report.added += summary.get("added", 0)
                    report.corroborated += summary.get("corroborated", 0)
                    report.superseded += summary.get("superseded", 0)
                    report.disputed += summary.get("disputed", 0)
                    report.questions += summary.get("questions", 0)
            else:
                report.errors.append(f"{src['id']}: PDF has no text layer (reading scanned PDFs is not built yet)")
                await asyncio.to_thread(record.update_source, src["id"], status=m.SRC_FAILED, error="no text layer")
        else:
            report.errors.append(f"{src['id']}: reading {src['kind']} sources is not built yet")
    read_gate = asyncio.Semaphore(READ_PARALLEL)

    async def read_group(group):
        async with read_gate:
            try:
                data = await asyncio.to_thread(lambda: [(record.read_file(s), s.get("mime") or "image/jpeg") for s in group])
                return group, await read_photos(model, data, model_id=settings.model)
            except Exception as error:
                return group, error

    read_started = time.monotonic()
    for waiting in asyncio.as_completed([read_group(g) for g in _chunks(photos, settings.pages_per_read)]):
        group, done = await waiting
        if isinstance(done, Exception):
            error = done
            report.errors.append(f"reading {len(group)} photos failed: {type(error).__name__}: {str(error)[:120]}")
            for s in group:
                await asyncio.to_thread(record.update_source, s["id"], status=m.SRC_FAILED, error="reading failed")
            continue
        texts, usage = done
        report.read_cost_inr += _cost(usage)
        for s, text in zip(group, texts):
            await asyncio.to_thread(record.save_pages, s["id"], [{"number": 1, "text": text}])
            await asyncio.to_thread(record.update_source, s["id"], model=settings.model, cost_inr=_cost(usage) / len(group),
                                    seconds=usage.seconds / len(group))
    report.read_seconds = round(time.monotonic() - read_started, 1)
    extract_started = time.monotonic()
    report.pages_read = len(unread)

    ready = [await asyncio.to_thread(record.source, s["id"]) for s in todo]
    ready = [s for s in ready if s["status"] == m.SRC_READ]
    pages = [p for s in ready for p in await asyncio.to_thread(record.pages, admission_id, s["id"])]
    meds = sorted(k for k, v in (await asyncio.to_thread(record.medicines, admission_id)).items() if v["state"] == "on")
    admitted = admission.get("admitted_at") or (
        f"not recorded (the notes are from around {admission['reference_date']})" if admission.get("reference_date") else None)
    candidates: list[dict] = []
    page_info: dict[str, dict] = {}
    extracted: set[str] = set()
    gate = asyncio.Semaphore(EXTRACT_PARALLEL)

    async def extract_once(group):
        numbered = [{"number": i + 1, "text": p["text"]} for i, p in enumerate(group)]
        async with gate:
            for attempt in range(2):
                try:
                    raw, usage = await extract_facts(model, numbered, admitted=admitted, unit=admission["unit"],
                                                     meds=meds, model_id=settings.model)
                    report.extract_cost_inr += _cost(usage)
                    return raw
                except Exception as error:
                    report.errors.append(f"extracting {len(group)} pages failed (try {attempt + 1}): "
                                         f"{type(error).__name__}: {str(error)[:200]}")
        return None

    async def extract_group(group):
        raw = await extract_once(group)
        if raw is not None or len(group) == 1:
            return [(group, raw)]
        return [([p], await extract_once([p])) for p in group]
    groups = list(_chunks(pages, EXTRACT_PAGES_PER_CALL))
    parts = [part for done in await asyncio.gather(*(extract_group(g) for g in groups)) for part in done]
    report.model_seconds = round(time.monotonic() - extract_started, 1)
    merge_started = time.monotonic()
    for group, raw in parts:
        if raw is None:
            continue
        extracted.update(p["id"] for p in group)
        candidates += to_candidates(raw, group, admission=admission)
        for info in raw.get("pages") or []:
            try:
                page = group[int(info.get("p", info.get("page"))) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            page_info[page["id"]] = {"page_type": info.get("type", info.get("page_type")), "page_date": info.get("date"),
                                     "patient_seen": info.get("patient") or {}}
    if page_info:
        await asyncio.to_thread(record.update_pages, page_info)
    _date_from_page(candidates, page_info, admission, {p["id"]: p["text"] for p in pages})
    report.facts_proposed += len(candidates)
    report.quotes_found += sum(1 for c in candidates if c["quote_found"])

    patient = await asyncio.to_thread(lambda: _adopt_identity(record, record.patient_of(admission_id), [dict(p, **page_info.get(p["id"], {})) for p in pages]))
    held: dict[str, list[dict]] = {}
    for p in pages:
        seen = page_info.get(p["id"], {}).get("patient_seen") or {}
        if seen and same_patient({**patient, **({"uhid": patient["portal_uhid"]} if patient.get("portal_uhid") else {}),
                                  **({"name": patient["portal_name"]} if patient.get("portal_name") else {})}, seen) is False:
            why = "UHID differs" if seen.get("uhid") and patient.get("uhid") else "name differs"
            held.setdefault(p["source_id"], []).append({"page_id": p["id"], "number": p["number"], "why": why})
    held_ids = {h["page_id"] for hs in held.values() for h in hs}

    for src in ready:
        src_pages = [p["id"] for p in pages if p["source_id"] == src["id"]]
        if not src_pages or not all(pid in extracted for pid in src_pages):
            report.errors.append(f"{src['id']}: not merged (its pages were not extracted); will retry")
            await asyncio.to_thread(record.update_source, src["id"], not_added=True)
            continue
        mine = [c for c in candidates if c["source_id"] == src["id"] and c["page_id"] not in held_ids]
        aside: dict[str, list[dict]] = {}
        for c in candidates:
            if c["source_id"] == src["id"] and c["page_id"] in held_ids:
                aside.setdefault(c["page_id"], []).append(c)
        out = await asyncio.to_thread(record.apply, src["id"], mine, held_pages=held.get(src["id"]), set_aside=aside)
        for name in ("added", "corroborated", "disputed", "superseded", "questions", "held_pages"):
            setattr(report, name, getattr(report, name) + int(out.get(name) or 0))
    report.extract_seconds = round(time.monotonic() - extract_started, 1)
    report.merge_seconds = round(time.monotonic() - merge_started, 1)
    report.duplicates = sum(1 for s in sources if s["status"] == m.SRC_DUPLICATE)
    report.seconds = round(time.monotonic() - started, 1)
    return report

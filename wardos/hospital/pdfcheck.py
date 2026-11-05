"""Check a captured PDF before trusting it."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

VALUE_LINE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:mg/dl|g/dl|mmol/l|meq/l|u/l|iu/l|iu/ml|%|cells/|/cumm|/ul|x10|lakh|ng/ml|pg/ml|"
    r"µg|ug/|mg/l|fl\b|pg\b|sec\b|seconds|ratio)", re.I)
RANGE_RE = re.compile(r"\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?")
QUALITATIVE_RE = re.compile(r"\b(?:no growth|growth of|positive|negative|reactive|non[- ]reactive|isolated|sensitive|resistant)\b", re.I)


@dataclass
class PdfCheck:
    path: Path
    pages: int = 0
    text_chars: int = 0
    identifier_found: bool = False
    value_lines: int = 0
    qualitative_hits: int = 0
    ok: bool = False
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"file": self.path.name, "pages": self.pages, "identifier_found": self.identifier_found,
                "value_lines": self.value_lines, "qualitative_hits": self.qualitative_hits,
                "ok": self.ok, "reasons": self.reasons}


def extract_text(path: Path) -> tuple[int, str]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return len(reader.pages), "\n".join(page.extract_text() or "" for page in reader.pages)


def check_pdf(path: Path, identifier: str | None) -> PdfCheck:
    result = PdfCheck(path=path)
    try:
        result.pages, text = extract_text(path)
    except Exception as exc:
        result.reasons.append(f"unreadable PDF ({type(exc).__name__})")
        return result
    result.text_chars = len(text.strip())
    if not result.pages:
        result.reasons.append("no pages")
    if result.text_chars < 50:
        result.reasons.append("no extractable text")
    if identifier:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(identifier) + r"(?![A-Za-z0-9])"
        result.identifier_found = bool(re.search(pattern, text))
        if not result.identifier_found:
            result.reasons.append("patient identifier not found")
    lines = text.splitlines()
    result.value_lines = sum(1 for line in lines if VALUE_LINE_RE.search(line) or RANGE_RE.search(line))
    result.qualitative_hits = len(QUALITATIVE_RE.findall(text))
    if result.value_lines < 3 and result.qualitative_hits == 0:
        result.reasons.append("does not look like a results report (few measured values)")
    result.ok = not result.reasons
    return result

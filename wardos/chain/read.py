"""Step 1 of an upload: read each photo word for word."""
from __future__ import annotations

from pathlib import Path
import re

from ..models import ModelClient, Usage

PROMPT = (Path(__file__).parent / "prompts" / "read.md").read_text(encoding="utf-8")
IMAGE_BLOCK = re.compile(r"^\s*\*\*Image\s+(\d+):\*\*", re.M)
READ_MAX_TOKENS = 32768
READ_HEDGE_AFTER_S = 45.0  # a photo that has not come back by now gets a second try running beside it; the first still counts


def read_prompt(count: int) -> str:
    label = (f"- Label each image's content separately, starting at \"**Image 1:**\" for the first image below and "
             f"counting up in order to \"**Image {count}:**\" for the last one.\n")
    return PROMPT.replace("{label_line}", label)


def split_images(text: str, count: int) -> list[str] | None:
    marks = list(IMAGE_BLOCK.finditer(text or ""))
    numbers = [int(mk.group(1)) for mk in marks]
    if numbers != list(range(1, count + 1)):
        return None
    return [text[mk.end(): (marks[i + 1].start() if i + 1 < len(marks) else len(text))].strip()
            for i, mk in enumerate(marks)]


def looks_like_loop(text: str) -> bool:
    if len(text) < 500:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for size in range(1, 5):
        for start in range(0, max(0, len(lines) - size * 8)):
            block = lines[start:start + size]
            if all(lines[start + k * size: start + (k + 1) * size] == block for k in range(8)):
                return True
    return False


async def read_photos(model: ModelClient, photos: list[tuple[bytes, str]], *, model_id: str, timeout: float = 7200) -> tuple[list[str], Usage]:
    total = None
    for attempt in range(2):
        text, usage = await model.generate(read_prompt(len(photos)), images=photos, model=model_id, max_tokens=READ_MAX_TOKENS, as_json=False,
                                           timeout=timeout, hedge_after=READ_HEDGE_AFTER_S)
        total = usage if total is None else total.add(usage)
        texts = None if looks_like_loop(text) else split_images(text, len(photos))
        if texts is not None:
            return texts, total
    raise RuntimeError("the reading did not come back as one block per photo (twice)")

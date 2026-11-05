"""A doctor's voice note becomes the words, written down as spoken."""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..models import ModelClient, Usage
from .read import looks_like_loop

PROMPT = (Path(__file__).parent / "prompts" / "voice.md").read_text(encoding="utf-8")


# Muse now and then answers a perfectly good recording with "I don't see any audio" or "(unclear)": ask again (up to 3 times)
NO_AUDIO = re.compile(r"^\(unclear\)$|no audio|don.t (?:have|see|hear)|can.t hear|cannot hear|not attached|upload (?:or|the)|attach the", re.I)


async def transcribe(model: ModelClient, audio: bytes, mime: str, *, model_id: str) -> tuple[str, Usage]:
    total = None
    for attempt in range(3):
        text, usage = await model.generate(PROMPT, audio=[(audio, mime)], model=model_id,
                                           as_json=False, timeout=7200, hedge_after=25, effort=os.environ.get("WARDOS_VOICE_EFFORT") or "high")
        total = usage if total is None else total.add(usage)
        text = (text or "").strip()
        if text and not looks_like_loop(text) and not NO_AUDIO.search(text):
            return text, total
    if not text:
        raise RuntimeError("the voice note came back empty")
    if looks_like_loop(text):
        raise RuntimeError("the voice note came back as a repeating loop")
    return "", total

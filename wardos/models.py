"""One door for every model call: the Meta Model API, reached with plain HTTPS requests."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import io
import json
import os
import random
import re
import time
from typing import Protocol
import wave

import httpx

from .config import Settings

CAPTCHA_PROMPT = (
    "This image is a website CAPTCHA. Transcribe exactly the characters shown, preserving upper/lower case. "
    "Do not guess extra characters and do not pad. Reply with JSON only: {\"captcha\": \"...\"}. "
    "If you cannot read it, reply {\"captcha\": \"\"}."
)

HEDGE_AFTER_S = 10.0
BUSY_WAITS_S = (5, 15, 30, 60)
MAX_PARALLEL_CALLS = 3


async def hedged(make, *, timeout: float, hedge_after: float = HEDGE_AFTER_S, max_calls: int = MAX_PARALLEL_CALLS):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    pending: set[asyncio.Task] = set()
    launched, error = 0, None

    def launch():
        nonlocal launched
        launched += 1
        pending.add(asyncio.ensure_future(make()))

    launch()
    try:
        while pending:
            left = end - loop.time()
            if left <= 0:
                raise TimeoutError("model did not answer in time")
            wait = min(left, hedge_after) if launched < max_calls else left
            done, _ = await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                if launched < max_calls:
                    launch()
                continue
            for task in done:
                pending.discard(task)
                if task.exception() is None:
                    return task.result()
                error = task.exception()
            if not pending and launched < max_calls:
                await asyncio.sleep(min(1.0, max(0.0, end - loop.time())))
                launch()
        raise error
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2)


class ModelClient(Protocol):
    name: str

    async def read_captcha(self, image: bytes, mime: str, length: int | None = None) -> str: ...

    async def json(self, prompt: str, *, images: list[tuple[bytes, str]] | None = None,
                   pdfs: list[bytes] | None = None, timeout: float = 90) -> dict: ...

    async def generate(self, prompt: str, *, images: list[tuple[bytes, str]] | None = None,
                       pdfs: list[bytes] | None = None, audio: list[tuple[bytes, str]] | None = None,
                       model: str | None = None, max_tokens: int = 8192, as_json: bool = True, timeout: float = 240,
                       hedge_after: float | None = None, grounding: bool = False,
                       schema: dict | None = None, effort: str | None = None) -> tuple[dict | str, "Usage"]: ...


PRICE_USD_PER_1M = {
    "muse-spark-1.3": {"input": 1.25, "cached": 0.15, "output": 4.25},
    "muse-spark-1.3-contributor": {"input": 0.10, "cached": 0.002, "output": 0.20},
    "muse-wonder-1.0": {"input": 4.00, "cached": 0.20, "output": 20.00},
}
MUSE_HEDGE_AFTER_S = 45.0
MUSE_CAPTCHA_HEDGE_AFTER_S = 20.0
MUSE_REASONING_ROOM = 16000
USD_TO_INR = float(os.environ.get("WARDOS_USD_TO_INR") or 95.545)
MUSE_NO_RETRY = {400, 401, 402, 403, 404, 413, 504}
DIRECT_AUDIO = {"audio/wav": "wav", "audio/mpeg": "mp3"}


class ModelHTTPError(RuntimeError):
    def __init__(self, status_code: int, headers=None, message: str = ""):
        super().__init__(f"model API answered HTTP {status_code}{': ' + message if message else ''}")
        self.status_code = status_code
        self.headers = headers or {}


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    cost_inr: float | None = None
    grounding: dict | None = None
    cost_usd: float | None = None

    def add(self, other: "Usage") -> "Usage":
        cost = None if self.cost_inr is None or other.cost_inr is None else self.cost_inr + other.cost_inr
        usd = None if self.cost_usd is None or other.cost_usd is None else self.cost_usd + other.cost_usd
        return Usage(self.model, self.input_tokens + other.input_tokens, self.cached_tokens + other.cached_tokens,
                     self.output_tokens + other.output_tokens, self.seconds + other.seconds, cost, cost_usd=usd)


def _enlarge(image: bytes, mime: str) -> tuple[bytes, str]:
    try:
        import io
        from PIL import Image
        picture = Image.open(io.BytesIO(image))
        if picture.width >= 400:
            return image, mime
        picture = picture.convert("RGB").resize((picture.width * 3, picture.height * 3), Image.LANCZOS)
        buffer = io.BytesIO()
        picture.save(buffer, "PNG")
        return buffer.getvalue(), "image/png"
    except Exception:
        return image, mime


async def _two_views(read_one, image: bytes, mime: str) -> str:
    big, big_mime = _enlarge(image, mime)
    if big is image:
        return await read_one(image, mime)
    first, second = await asyncio.gather(read_one(image, mime), read_one(big, big_mime), return_exceptions=True)
    reads = [r for r in (first, second) if isinstance(r, str)]
    if not reads:
        raise first
    if len(reads) == 1:
        return reads[0]
    return reads[0] if reads[0] == reads[1] else ""


def _clean_captcha(value: str, length: int | None) -> str:
    value = re.sub(r"[^A-Za-z0-9]", "", value or "")
    if length and len(value) != length:
        return ""
    return value


def _first_json_object(text: str) -> dict:
    text = (text or "").strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"value": value}
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("model reply contained no JSON object")
    return json.loads(match.group(0))


def _audio_part(data: bytes, mime: str) -> dict:
    kind = DIRECT_AUDIO.get(mime)
    if kind is None:
        data, kind = _as_wav(data), "wav"
    return {"type": "input_audio", "input_audio": {"data": base64.b64encode(data).decode(), "format": kind}}


def _as_wav(data: bytes) -> bytes:
    import av
    pcm = bytearray()
    with av.open(io.BytesIO(data)) as box:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in box.decode(audio=0):
            for out in resampler.resample(frame):
                pcm += bytes(out.planes[0])[: out.samples * 2]
        for out in resampler.resample(None):
            pcm += bytes(out.planes[0])[: out.samples * 2]
    if not pcm:
        raise ValueError("the recording holds no sound")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(16000)
        sink.writeframes(bytes(pcm))
    return buffer.getvalue()


def _output_text(body: dict) -> str:
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    return "".join(part.get("text") or "" for item in body.get("output") or [] if item.get("type") == "message"
                   for part in item.get("content") or [] if part.get("type") == "output_text")


class MuseClient:
    name = "muse"

    def __init__(self, settings: Settings):
        key = os.environ.get("MODEL_API_KEY")
        if not key:
            raise RuntimeError("MODEL_API_KEY is not set (Meta Model API key)")
        self._url = settings.muse_base_url.rstrip("/") + "/responses"
        self._client = httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"})
        self._model = settings.muse_model
        self._effort = settings.muse_effort
        self.calls: list[dict] = []

    @staticmethod
    def _data_url(data: bytes, mime: str) -> str:
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"

    def _note(self, model: str, started: float, body: dict | None = None, status: str = "ok") -> Usage:
        usage = (body or {}).get("usage") or {}
        reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
        total = usage.get("input_tokens") or 0
        cached = min((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0, total)
        out = usage.get("output_tokens") or 0
        price = PRICE_USD_PER_1M.get(model)
        cost = round(((total - cached) * price["input"] + cached * price["cached"] + out * price["output"]) / 1e6, 8) \
            if price else None
        seconds = round(time.monotonic() - started, 2)
        self.calls.append({"model": model, "effort": self._effort, "status": status, "input": total, "cached": cached,
                           "output": out, "reasoning": reasoning, "seconds": seconds, "usd": cost})
        inr = round(cost * USD_TO_INR, 6) if cost is not None else None
        return Usage(model, total - cached, cached, out, seconds, inr, cost_usd=cost)

    async def _post(self, payload: dict, timeout: float) -> dict:
        response = await self._client.post(self._url, json=payload, timeout=timeout)
        if response.status_code >= 400:
            try:
                message = str((response.json().get("error") or {}).get("message") or "")[:200]
            except ValueError:
                message = ""
            raise ModelHTTPError(response.status_code, response.headers, message)
        return response.json()

    async def _respond(self, content: list, timeout: float, *, hedge_after: float = MUSE_HEDGE_AFTER_S,
                       model: str | None = None, max_tokens: int = 8192, as_json: bool = True,
                       max_calls: int = MAX_PARALLEL_CALLS, effort: str | None = None) -> tuple[str, Usage]:
        model = model or self._model
        total = Usage(model, cost_inr=0.0, cost_usd=0.0)
        fatal: list[BaseException] = []
        payload = {"model": model, "input": [{"role": "user", "content": content}],
                   "reasoning": {"effort": effort or self._effort}, "max_output_tokens": max_tokens + MUSE_REASONING_ROOM,
                   **({"text": {"format": {"type": "json_object"}}} if as_json else {})}

        async def once():
            nonlocal total
            if fatal:
                raise fatal[0]
            for wait in BUSY_WAITS_S + (None,):
                started = time.monotonic()
                try:
                    body = await self._post(payload, timeout)
                    break
                except asyncio.CancelledError:
                    self._note(model, started, status="cancelled")
                    raise
                except ModelHTTPError as error:
                    self._note(model, started, status=f"http {error.status_code}")
                    if error.status_code in MUSE_NO_RETRY:
                        fatal.append(error)
                        raise
                    if wait is None or error.status_code not in {429, 503}:
                        raise
                    after = error.headers.get("retry-after") or ""
                    await asyncio.sleep(float(after) if after.replace(".", "", 1).isdigit() else wait + random.random())
            total = total.add(self._note(model, started, body))
            text = _output_text(body)
            if not text.strip():
                raise RuntimeError(f"model returned no text (status {body.get('status')})")
            return text

        text = await hedged(once, timeout=timeout, hedge_after=hedge_after, max_calls=max_calls)
        return text, total

    async def read_captcha(self, image: bytes, mime: str, length: int | None = None) -> str:
        async def read_one(data, kind):
            text, _ = await self._respond([{"type": "input_text", "text": CAPTCHA_PROMPT},
                                           {"type": "input_image", "image_url": self._data_url(data, kind)}], 45,
                                          hedge_after=MUSE_CAPTCHA_HEDGE_AFTER_S)
            return _clean_captcha(str(_first_json_object(text).get("captcha", "")), length)
        return await _two_views(read_one, image, mime)

    def _content(self, prompt: str, images, pdfs, as_json: bool, audio=None) -> list:
        content: list = [{"type": "input_text", "text": prompt + ("\nReply with one JSON object only." if as_json else "")}]
        content += [{"type": "input_image", "image_url": self._data_url(d, m)} for d, m in images or []]
        content += [{"type": "input_file", "filename": f"document-{i}.pdf",
                     "file_data": self._data_url(d, "application/pdf")} for i, d in enumerate(pdfs or [])]
        content += [_audio_part(d, m) for d, m in audio or []]
        return content

    async def json(self, prompt, *, images=None, pdfs=None, timeout=90) -> dict:
        text, _ = await self._respond(self._content(prompt, images, pdfs, True), timeout)
        return _first_json_object(text)

    async def generate(self, prompt, *, images=None, pdfs=None, audio=None, model=None, max_tokens=8192,
                       as_json=True, timeout=240, hedge_after=None, grounding=False, schema=None, effort=None):
        if grounding:
            raise NotImplementedError("web search grounding is not available on this model")
        started = time.monotonic()
        if audio:
            content = await asyncio.to_thread(self._content, prompt, images, pdfs, as_json, audio)
        else:
            content = self._content(prompt, images, pdfs, as_json)
        text, total = await self._respond(content, timeout, model=model, max_tokens=max_tokens, as_json=as_json,
                                          max_calls=2, hedge_after=hedge_after or max(timeout / 2, MUSE_HEDGE_AFTER_S), effort=effort)
        total.seconds = round(time.monotonic() - started, 2)
        return (_first_json_object(text) if as_json else text), total

    def spent(self) -> dict:
        done = [c for c in self.calls if c["status"] == "ok"]
        return {"calls": len(self.calls), "answered": len(done),
                "cancelled": sum(c["status"] == "cancelled" for c in self.calls),
                "input": sum(c["input"] for c in done), "cached": sum(c["cached"] for c in done),
                "output": sum(c["output"] for c in done), "reasoning": sum(c["reasoning"] for c in done),
                "usd": round(sum(c["usd"] or 0 for c in done), 6)}

    async def aclose(self):
        await self._client.aclose()


def get_model_client(settings: Settings) -> ModelClient:
    return MuseClient(settings)

"""The model door against a fake API: request shape, retries, costs, audio and the plain-HTTP rule."""
from __future__ import annotations

import ast
import asyncio
import base64
import io
import json
from pathlib import Path
import wave

import httpx
import pytest

from wardos import models as door
from wardos.config import load_settings
from wardos.hospital.brain import choose
from wardos.hospital.portals import get_portal
from wardos.hospital.skills import SkillLibrary
from wardos.models import ModelHTTPError, MuseClient, get_model_client


def reply(text='{"ok": 1}', *, status="completed", total=100, cached=10, out=50, reasoning=40):
    return {"status": status, "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": total, "output_tokens": out, "input_tokens_details": {"cached_tokens": cached},
                      "output_tokens_details": {"reasoning_tokens": reasoning}}}


def http_error(code, retry_after=None):
    return ("error", code, {"retry-after": retry_after} if retry_after else {})


class FakeApi:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.headers = []
        self.urls = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        self.urls.append(str(request.url))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, tuple):
            _, code, headers = answer
            return httpx.Response(code, headers=headers, json={"error": {"message": f"http {code}"}})
        return httpx.Response(200, json=answer)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-not-a-key")
    monkeypatch.delenv("WARDOS_MUSE_MODEL", raising=False)
    monkeypatch.delenv("WARDOS_MUSE_EFFORT", raising=False)
    monkeypatch.delenv("WARDOS_MUSE_BASE_URL", raising=False)
    made = MuseClient(load_settings())
    made.api = FakeApi(reply())
    made._client = httpx.AsyncClient(transport=httpx.MockTransport(made.api), headers=made._client.headers)
    return made


def run(coroutine):
    return asyncio.run(coroutine)


def tone_wav(seconds=0.5, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        high, low = (8000).to_bytes(2, "little", signed=True), (-8000).to_bytes(2, "little", signed=True)
        sink.writeframes(b"".join(high if (i // 20) % 2 else low for i in range(int(seconds * rate))))
    return buffer.getvalue()


def tone_webm(seconds=0.5):
    import av
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="webm") as box:
        stream = box.add_stream("libopus", rate=48000)
        stream.layout = "mono"
        frame_size = 960
        for start in range(0, int(seconds * 48000), frame_size):
            frame = av.AudioFrame(format="s16", layout="mono", samples=frame_size)
            frame.planes[0].update(bytes(frame_size * 2))
            frame.sample_rate = 48000
            frame.pts = start
            for packet in stream.encode(frame):
                box.mux(packet)
        for packet in stream.encode(None):
            box.mux(packet)
    return buffer.getvalue()


def test_owner_defaults_are_settings(monkeypatch):
    for name in ("WARDOS_MUSE_MODEL", "WARDOS_MUSE_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert (settings.muse_model, settings.muse_effort) == ("muse-spark-1.3-contributor", "high")
    monkeypatch.setenv("WARDOS_MUSE_MODEL", "muse-spark-1.3")
    monkeypatch.setenv("WARDOS_MUSE_EFFORT", "xhigh")
    settings = load_settings()
    assert (settings.muse_model, settings.muse_effort) == ("muse-spark-1.3", "xhigh")


def test_the_one_door_is_muse(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-not-a-key")
    assert isinstance(get_model_client(load_settings()), MuseClient)
    clients = {name for name in vars(door) if name.endswith("Client")}
    assert clients == {"ModelClient", "MuseClient"}


def test_the_door_speaks_plain_http_only():
    tree = ast.parse(Path(door.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module.split(".")[0])
    third_party = imported - {"__future__", "asyncio", "base64", "dataclasses", "io", "json", "os", "random", "re",
                              "time", "typing", "wave"}
    assert third_party == {"httpx", "av", "PIL"}


def test_muse_without_key_says_so(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MODEL_API_KEY"):
        get_model_client(load_settings())


def test_json_request_shape_and_token_count(client):
    assert run(client.json("question", images=[(b"x", "image/jpeg")])) == {"ok": 1}
    sent = client.api.requests[0]
    assert client.api.urls[0] == "https://api.meta.ai/v1/responses"
    assert client.api.headers[0]["authorization"] == "Bearer test-not-a-key"
    assert sent["model"] == "muse-spark-1.3-contributor"
    assert sent["reasoning"] == {"effort": "high"}
    assert sent["text"] == {"format": {"type": "json_object"}}
    assert sent["input"][0]["content"][1]["type"] == "input_image"
    assert sent["max_output_tokens"] == 8192 + door.MUSE_REASONING_ROOM
    call = client.calls[0]
    assert (call["input"], call["cached"], call["output"], call["reasoning"]) == (100, 10, 50, 40)
    assert call["usd"] == pytest.approx((90 * 0.10 + 10 * 0.002 + 50 * 0.20) / 1e6)
    assert client.spent()["answered"] == 1


def test_output_text_field_is_used_when_present(client):
    client.api.answers = [{**reply(), "output_text": '{"ok": 2}'}]
    assert run(client.json("question")) == {"ok": 2}


def test_generate_plain_text_returns_usage(client):
    client.api.answers = [reply("hello")]
    text, usage = run(client.generate("say hello", as_json=False))
    assert text == "hello"
    assert "text" not in client.api.requests[0]
    assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (90, 10, 50)
    assert usage.cost_usd > 0 and usage.cost_inr == pytest.approx(usage.cost_usd * door.USD_TO_INR, rel=1e-3)


def test_generate_sends_pdfs_as_files(client):
    run(client.generate("read", pdfs=[b"%PDF-1.4 x"]))
    part = client.api.requests[0]["input"][0]["content"][1]
    assert part["type"] == "input_file" and part["file_data"].startswith("data:application/pdf;base64,")


def test_grounding_is_refused_before_any_call(client):
    with pytest.raises(NotImplementedError):
        run(client.generate("search", as_json=False, grounding=True))
    assert client.api.requests == []


def test_captcha_reads_both_views(client):
    client.api.answers = [reply('{"captcha": "aB3xQ9"}')]
    from wardos.cli import _fake_captcha
    assert run(client.read_captcha(_fake_captcha("aB3xQ9"), "image/png", 6)) == "aB3xQ9"
    assert len(client.api.requests) == 2


def test_busy_reply_waits_and_tries_again(client, monkeypatch):
    waits = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        waits.append(seconds)
        await real_sleep(0)
    monkeypatch.setattr(door.asyncio, "sleep", sleep)
    client.api.answers = [http_error(429, "7"), http_error(503), reply()]
    assert run(client.json("question")) == {"ok": 1}
    assert waits[0] == 7.0
    assert 15 <= waits[1] < 16
    assert [c["status"] for c in client.calls] == ["http 429", "http 503", "ok"]


@pytest.mark.parametrize("code", [400, 401, 402, 404, 504])
def test_errors_that_cannot_help_are_not_retried(client, code):
    client.api.answers = [http_error(code)]
    with pytest.raises(ModelHTTPError) as caught:
        run(client.json("question"))
    assert caught.value.status_code == code
    assert len(client.api.requests) == 1


def test_empty_reply_is_an_error_not_an_answer(client):
    client.api.answers = [reply("", status="incomplete")]
    with pytest.raises(RuntimeError, match="no text"):
        run(client.json("question", timeout=5))
    assert client.spent()["output"] == 150


def test_stalled_call_times_out_and_is_noted(client):
    async def stuck(request):
        await asyncio.sleep(30)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(stuck))
    with pytest.raises(TimeoutError):
        run(client.json("question", timeout=0.05))
    assert client.spent()["cancelled"] == 1


def test_wav_and_mp3_go_as_they_are(client):
    wav = tone_wav()
    run(client.generate("words", audio=[(wav, "audio/wav"), (b"ID3 fake mp3", "audio/mpeg")], as_json=False))
    parts = client.api.requests[0]["input"][0]["content"][1:]
    assert parts[0] == {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}}
    assert parts[1]["input_audio"]["format"] == "mp3"
    assert base64.b64decode(parts[1]["input_audio"]["data"]) == b"ID3 fake mp3"


def test_phone_recording_is_turned_into_wav(client):
    run(client.generate("words", audio=[(tone_webm(), "audio/webm")], as_json=False))
    part = client.api.requests[0]["input"][0]["content"][1]
    assert part["input_audio"]["format"] == "wav"
    with wave.open(io.BytesIO(base64.b64decode(part["input_audio"]["data"]))) as sound:
        assert (sound.getnchannels(), sound.getsampwidth(), sound.getframerate()) == (1, 2, 16000)
        assert 0.4 < sound.getnframes() / 16000 < 0.7


def test_broken_recording_is_an_error_before_any_call(client):
    with pytest.raises(Exception):
        run(client.generate("words", audio=[(b"not audio at all", "audio/webm")], as_json=False))
    assert client.api.requests == []


def test_any_portal_page_may_now_go(client):
    get_portal("ehospital")
    assert run(client.json("page text", images=[(b"shot", "image/jpeg")])) == {"ok": 1}
    assert run(client.generate("a patient's record"))[0] == {"ok": 1}
    assert len(client.api.requests) == 2


def test_choose_sends_only_the_doctors_words(client, tmp_path):
    client.api.answers = [reply('{"mode": "clarify", "clarify_about": "dates"}')]
    plan = run(choose(client, SkillLibrary(tmp_path), "ehospital", "how many beds?", {"uhid": "SECRET-ID"}))
    assert plan.mode == "clarify"
    sent = client.api.requests[0]["input"][0]["content"]
    assert [part["type"] for part in sent] == ["input_text"]
    assert "SECRET-ID" not in sent[0]["text"]

"""Agora provider + REST control-plane — credit-safe, offline.

No network, no SDK native libs, no credentials, no credit. Proves:
  * the REST client builds the verified endpoint/auth/body and retries only
    transient failures;
  * the provider refuses (no REST call) when unconfigured, live-disabled, over
    the daily ceiling, or when the media bridge is unavailable;
  * with a real bridge it drives join -> start -> publish -> stop -> close in
    order, and cleanup never masks the original error.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from persephone_worker.providers.agora import (
    AgoraConfig,
    AgoraProvider,
    AgoraRestClient,
    MediaBridgeUnavailable,
    MockAgoraProvider,
    _TransientRestError,
    basic_auth_header,
)
from persephone_worker.providers.base import EmptyTranscript, ProviderError, ProviderUnavailable

CFG = AgoraConfig(
    app_id="APPID",
    app_certificate="CERT",
    customer_id="cid",
    customer_secret="csecret",
    live_enabled=True,
)
DUMMY = Path("dummy.wav")


def _live_cfg(**kw):
    base = {
        "app_id": "APPID",
        "app_certificate": "CERT",
        "customer_id": "cid",
        "customer_secret": "csecret",
        "live_enabled": True,
    }
    base.update(kw)
    return AgoraConfig(**base)


# ---------------------------------------------------------------------------
# REST control-plane
# ---------------------------------------------------------------------------
def test_basic_auth_header():
    assert basic_auth_header("cust", "secret") == "Basic Y3VzdDpzZWNyZXQ="


async def test_start_task_verified_endpoint_auth_and_body():
    seen = {}

    async def fake_post(url, headers, body):
        seen.update(url=url, headers=headers, body=body)
        return {"agent_id": "agent-123"}

    client = AgoraRestClient(CFG, http_post=fake_post)
    agent_id = await client.start_task(
        channel="chan",
        name="chan",
        sub_bot_uid="10001",
        pub_bot_uid="10002",
        languages=["en-US"],
        sub_bot_token="subtok",
        pub_bot_token="pubtok",
    )
    assert agent_id == "agent-123"
    assert seen["url"] == "https://api.agora.io/api/speech-to-text/v1/projects/APPID/join"
    assert seen["headers"]["Authorization"].startswith("Basic ")
    rtc = seen["body"]["rtcConfig"]
    assert rtc["channelName"] == "chan"
    assert rtc["subBotUid"] == "10001"
    assert rtc["pubBotUid"] == "10002"
    assert rtc["subBotToken"] == "subtok" and rtc["pubBotToken"] == "pubtok"
    assert seen["body"]["maxIdleTime"] == CFG.idle_seconds


async def test_start_task_no_agent_id_raises():
    async def fake_post(url, headers, body):
        return {}

    with pytest.raises(ProviderError) as ei:
        await AgoraRestClient(CFG, http_post=fake_post).start_task(
            channel="c", name="c", sub_bot_uid="1", pub_bot_uid="2", languages=["en"]
        )
    assert ei.value.code == "agora_stt_start_failed"


async def test_stop_task_leave_endpoint():
    seen = {}

    async def fake_post(url, headers, body):
        seen["url"] = url
        return {}

    await AgoraRestClient(CFG, http_post=fake_post).stop_task("agent-9")
    assert seen["url"].endswith("/agents/agent-9/leave")


async def test_stop_task_idempotent_on_404():
    async def fake_post(url, headers, body):
        err = ProviderError("gone", code="agora_stt_start_failed")
        err.http_status = 404  # type: ignore[attr-defined]
        raise err

    # A 404 (already gone) must be swallowed.
    await AgoraRestClient(CFG, http_post=fake_post).stop_task("agent-9")


async def test_rest_retries_transient_then_succeeds():
    calls = {"n": 0}

    async def flaky(url, headers, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _TransientRestError("network blip")
        return {"agent_id": "a"}

    client = AgoraRestClient(CFG, http_post=flaky, max_attempts=2)
    agent_id = await client.start_task(
        channel="c", name="c", sub_bot_uid="1", pub_bot_uid="2", languages=["en"]
    )
    assert agent_id == "a"
    assert calls["n"] == 2


async def test_rest_does_not_retry_non_transient():
    calls = {"n": 0}

    async def hard(url, headers, body):
        calls["n"] += 1
        raise ProviderError("rejected", code="agora_stt_start_failed")

    with pytest.raises(ProviderError):
        await AgoraRestClient(CFG, http_post=hard, max_attempts=3).start_task(
            channel="c", name="c", sub_bot_uid="1", pub_bot_uid="2", languages=["en"]
        )
    assert calls["n"] == 1  # no retry


# ---------------------------------------------------------------------------
# Provider — refusals (no credit)
# ---------------------------------------------------------------------------
def _exploding_factory(_channel):
    raise AssertionError("media bridge must not be constructed")


async def _exploding_post(url, headers, body):
    raise AssertionError("REST must not be called")


async def test_provider_unconfigured_refuses():
    provider = AgoraProvider(AgoraConfig())
    with pytest.raises(ProviderUnavailable) as ei:
        await provider.transcribe(DUMMY, "q1")
    assert ei.value.code == "agora_not_configured"


async def test_provider_live_disabled_refuses_without_bridge_or_rest():
    cfg = _live_cfg(live_enabled=False)
    provider = AgoraProvider(
        cfg,
        bridge_factory=_exploding_factory,
        rest_client=AgoraRestClient(cfg, http_post=_exploding_post),
    )
    with pytest.raises(ProviderUnavailable) as ei:
        await provider.transcribe(DUMMY, "q1")
    assert ei.value.code == "agora_live_disabled"


async def test_provider_credit_limit_refuses_without_bridge_or_rest():
    cfg = _live_cfg(daily_max_jobs=5)
    provider = AgoraProvider(
        cfg,
        bridge_factory=_exploding_factory,
        rest_client=AgoraRestClient(cfg, http_post=_exploding_post),
        usage_counter=lambda: 5,
    )
    with pytest.raises(ProviderError) as ei:
        await provider.transcribe(DUMMY, "q1")
    assert ei.value.code == "agora_credit_limit_reached"


async def test_provider_default_bridge_unavailable_consumes_no_credit(wav_file):
    called = {"rest": 0}

    async def exploding_post(url, headers, body):
        called["rest"] += 1
        raise AssertionError("REST must not be called when the bridge is unavailable")

    cfg = _live_cfg()
    # Default bridge is UnavailableMediaBridge -> open() raises BEFORE any REST call.
    provider = AgoraProvider(cfg, rest_client=AgoraRestClient(cfg, http_post=exploding_post))
    with pytest.raises(MediaBridgeUnavailable):
        await provider.transcribe(wav_file, "q1")
    assert called["rest"] == 0


async def test_provider_rejects_unsupported_audio_before_rest(tmp_path):
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00\x00\x00" * 100)
    cfg = _live_cfg()
    provider = AgoraProvider(
        cfg,
        bridge_factory=_exploding_factory,
        rest_client=AgoraRestClient(cfg, http_post=_exploding_post),
    )
    with pytest.raises(ProviderError) as ei:
        await provider.transcribe(path, "q1")
    assert ei.value.code == "agora_unsupported_audio"


async def test_provider_empty_audio_never_starts_task(tmp_path):
    path = tmp_path / "empty.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"")
    cfg = _live_cfg()
    provider = AgoraProvider(
        cfg,
        bridge_factory=_exploding_factory,
        rest_client=AgoraRestClient(cfg, http_post=_exploding_post),
    )
    with pytest.raises(EmptyTranscript) as ei:
        await provider.transcribe(path, "q1")
    assert ei.value.code == "agora_empty_transcript"


# ---------------------------------------------------------------------------
# Provider — full sequence with a real (fake) bridge
# ---------------------------------------------------------------------------
class _OkBridge:
    def __init__(self, events, transcript="bridged transcript"):
        self.events = events
        self._t = transcript

    async def open(self, channel, uid, token, *, timeout):
        self.events.append("open")

    async def publish_and_collect(self, frames, *, max_seconds, timeout):
        self.events.append("publish")
        for _ in frames:
            pass
        return self._t

    async def close(self):
        self.events.append("close")


async def test_provider_full_sequence_order(wav_file):
    events: list[str] = []

    async def fake_post(url, headers, body):
        if url.endswith("/join"):
            events.append("start")
            return {"agent_id": "agent-1"}
        events.append("stop")
        return {}

    cfg = _live_cfg()
    provider = AgoraProvider(
        cfg,
        bridge_factory=lambda ch: _OkBridge(events),
        rest_client=AgoraRestClient(cfg, http_post=fake_post),
        usage_counter=lambda: 0,
    )
    result = await provider.transcribe(wav_file, "q1")
    assert result.transcript == "bridged transcript"
    assert result.provider == "agora"
    assert events == ["open", "start", "publish", "stop", "close"]


async def test_provider_empty_transcript_stops_task(wav_file):
    events: list[str] = []

    async def fake_post(url, headers, body):
        events.append("start" if url.endswith("/join") else "stop")
        return {"agent_id": "agent-1"} if url.endswith("/join") else {}

    cfg = _live_cfg()
    provider = AgoraProvider(
        cfg,
        bridge_factory=lambda ch: _OkBridge(events, transcript="   "),
        rest_client=AgoraRestClient(cfg, http_post=fake_post),
    )
    with pytest.raises(EmptyTranscript) as ei:
        await provider.transcribe(wav_file, "q1")
    assert ei.value.code == "agora_empty_transcript"
    assert "stop" in events  # task was stopped even on empty


async def test_cleanup_does_not_mask_original_error(wav_file):
    events: list[str] = []

    async def fake_post(url, headers, body):
        events.append("start" if url.endswith("/join") else "stop")
        return {"agent_id": "agent-1"} if url.endswith("/join") else {}

    class _RaisingBridge:
        async def open(self, channel, uid, token, *, timeout):
            events.append("open")

        async def publish_and_collect(self, frames, *, max_seconds, timeout):
            raise ProviderError("boom", code="agora_publish_failed", safe_message="publish failed")

        async def close(self):
            raise RuntimeError("cleanup also fails")

    cfg = _live_cfg()
    provider = AgoraProvider(
        cfg,
        bridge_factory=lambda ch: _RaisingBridge(),
        rest_client=AgoraRestClient(cfg, http_post=fake_post),
    )
    with pytest.raises(ProviderError) as ei:
        await provider.transcribe(wav_file, "q1")
    assert ei.value.code == "agora_publish_failed"  # original error, not the cleanup error
    assert "stop" in events  # stop attempted despite the failure


# ---------------------------------------------------------------------------
# Mock provider (offline demos)
# ---------------------------------------------------------------------------
async def test_mock_agora_provider_returns_transcript():
    result = await MockAgoraProvider(transcript="mock says hi").transcribe(DUMMY, "q1")
    assert result.transcript == "mock says hi"
    assert result.provider == "agora"


async def test_mock_agora_provider_can_fail():
    with pytest.raises(ProviderUnavailable):
        await MockAgoraProvider(fail=True).transcribe(DUMMY, "q1")


async def test_mock_agora_provider_can_be_empty():
    with pytest.raises(EmptyTranscript):
        await MockAgoraProvider(empty=True).transcribe(DUMMY, "q1")

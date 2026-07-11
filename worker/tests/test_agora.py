"""Agora provider: credit-safe. No network, no SDK, no credentials.

Proves:
  * the REST control-plane builds the verified endpoint + Basic auth header;
  * AgoraProvider raises (media bridge unavailable) WITHOUT any REST call, so no
    credit is consumed;
  * the deterministic mock returns a transcript offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whisp_worker.providers.agora import (
    AgoraConfig,
    AgoraProvider,
    AgoraRestClient,
    MediaBridgeUnavailable,
    MockAgoraProvider,
    basic_auth_header,
)
from whisp_worker.providers.base import EmptyTranscript, ProviderUnavailable

DUMMY = Path("dummy.wav")


def test_basic_auth_header():
    # base64("cust:secret") == "Y3VzdDpzZWNyZXQ="
    assert basic_auth_header("cust", "secret") == "Basic Y3VzdDpzZWNyZXQ="


async def test_rest_client_start_task_uses_verified_endpoint_and_auth():
    cfg = AgoraConfig(app_id="APPID", customer_id="cid", customer_secret="csecret")
    seen = {}

    async def fake_post(url, headers, body):
        seen["url"] = url
        seen["headers"] = headers
        seen["body"] = body
        return {"agent_id": "agent-123"}

    client = AgoraRestClient(cfg, http_post=fake_post)
    agent_id = await client.start_task(
        channel="chan",
        name="chan",
        sub_bot_uid="1",
        pub_bot_uid="2",
        languages=["en"],
    )
    assert agent_id == "agent-123"
    assert seen["url"] == "https://api.agora.io/api/speech-to-text/v1/projects/APPID/join"
    assert seen["headers"]["Authorization"].startswith("Basic ")
    assert seen["body"]["rtcConfig"]["channelName"] == "chan"


async def test_rest_client_stop_task():
    cfg = AgoraConfig(app_id="APPID", customer_id="cid", customer_secret="csecret")
    seen = {}

    async def fake_post(url, headers, body):
        seen["url"] = url
        return {}

    client = AgoraRestClient(cfg, http_post=fake_post)
    await client.stop_task("agent-9")
    assert seen["url"].endswith("/agents/agent-9/leave")


async def test_agora_provider_unconfigured_raises_without_network():
    provider = AgoraProvider(AgoraConfig())  # no creds
    with pytest.raises(ProviderUnavailable):
        await provider.transcribe(DUMMY, "q1")


async def test_agora_provider_media_bridge_unavailable_consumes_no_credit():
    # Configured, but the default media bridge is unavailable. Inject a REST
    # client whose calls would explode — proving they are never reached.
    called = {"rest": 0}

    async def exploding_post(url, headers, body):
        called["rest"] += 1
        raise AssertionError("REST must not be called when the bridge is unavailable")

    cfg = AgoraConfig(app_id="A", customer_id="c", customer_secret="s")
    rest = AgoraRestClient(cfg, http_post=exploding_post)
    provider = AgoraProvider(cfg, rest_client=rest)

    with pytest.raises(MediaBridgeUnavailable):
        await provider.transcribe(DUMMY, "q1")
    assert called["rest"] == 0


async def test_agora_provider_with_real_bridge_runs_full_sequence(wav_file):
    """If a real MediaBridge is supplied, the provider drives start→publish→stop."""
    events = []

    class OkBridge:
        async def open(self):
            events.append("open")

        async def publish_and_collect(self, channel, frames, sample_rate, timeout):
            events.append("publish")
            # exhaust frames to mimic real push
            for _ in frames:
                pass
            return "bridged transcript"

        async def close(self):
            events.append("close")

    async def fake_post(url, headers, body):
        if url.endswith("/join"):
            events.append("start")
            return {"agent_id": "agent-1"}
        events.append("stop")
        return {}

    cfg = AgoraConfig(app_id="A", customer_id="c", customer_secret="s")
    rest = AgoraRestClient(cfg, http_post=fake_post)
    provider = AgoraProvider(cfg, bridge_factory=lambda: OkBridge(), rest_client=rest)
    result = await provider.transcribe(wav_file, "q1")
    assert result.transcript == "bridged transcript"
    assert result.provider == "agora"
    assert events == ["open", "start", "publish", "stop", "close"]


async def test_mock_agora_provider_returns_transcript():
    provider = MockAgoraProvider(transcript="mock says hi")
    result = await provider.transcribe(DUMMY, "q1")
    assert result.transcript == "mock says hi"
    assert result.provider == "agora"


async def test_mock_agora_provider_can_fail():
    with pytest.raises(ProviderUnavailable):
        await MockAgoraProvider(fail=True).transcribe(DUMMY, "q1")


async def test_mock_agora_provider_can_be_empty():
    with pytest.raises(EmptyTranscript):
        await MockAgoraProvider(empty=True).transcribe(DUMMY, "q1")

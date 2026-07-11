"""Agora Real-Time Speech-to-Text provider.

Reality (verified against Agora docs, July 2026): RT-STT is an RTC-*channel*
product, not a "POST a WAV" endpoint. You start a task via REST (v7.x) and
Agora's bots join your channel and subscribe to audio. To transcribe our
pre-recorded WAV we must PUSH PCM frames into the channel using the Linux-only
``agora-python-server-sdk`` (the "media bridge"); Agora's bot then transcribes
and the result is retrieved from the channel data-stream or a WebVTT file.

What is implemented here:
  * ``AgoraRestClient`` — the VERIFIED control-plane (v7.x join/query/leave,
    HTTP Basic auth with Customer ID/Secret). Unit-tested with a mocked
    transport; it makes NO network calls at import or construction time.
  * ``MediaBridge`` — the boundary for pushing PCM into the channel. The real
    implementation needs the Linux SDK + real credentials + credit, so the
    default is ``UnavailableMediaBridge`` which raises with the documented
    blocker. See docs/AGORA_SETUP.md.
  * ``AgoraProvider`` — orchestrates the full sequence with cleanup in a
    ``finally`` block. Because the media bridge opens FIRST and the default is
    unavailable, ``transcribe`` raises BEFORE any paid REST task starts, so no
    credit is ever consumed until a real bridge is supplied.
  * ``MockAgoraProvider`` — deterministic, offline; used by tests and for a
    credential-free "agora" demo.

Nothing here is invented as "working": endpoints + auth are verified; the media
bridge is an explicit, documented boundary, not a fabrication.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from whisp_worker.audio import iter_pcm_frames, read_wav_pcm16_mono
from whisp_worker.providers.base import (
    EmptyTranscript,
    ProviderError,
    ProviderUnavailable,
    TranscriptionResult,
)

log = logging.getLogger("whisp.provider.agora")

AGORA_HOST = "https://api.agora.io"
STT_BASE = "/api/speech-to-text/v1/projects"

# The single, documented external blocker (see docs/AGORA_SETUP.md).
MEDIA_BRIDGE_BLOCKER = (
    "Agora RT-STT media bridge is not available. Pushing our recorded WAV into "
    "an RTC channel requires the Linux-only 'agora-python-server-sdk' plus real "
    "Agora credentials and transcription credit. Install the SDK in WSL2/Linux "
    "and provide a real MediaBridge implementation to enable Agora. See "
    "docs/AGORA_SETUP.md."
)


@dataclass
class AgoraConfig:
    app_id: str = ""
    app_certificate: str = ""
    customer_id: str = ""
    customer_secret: str = ""
    max_duration_seconds: int = 30
    idle_seconds: int = 10

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.customer_id and self.customer_secret)


def basic_auth_header(customer_id: str, customer_secret: str) -> str:
    raw = f"{customer_id}:{customer_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


class AgoraRestClient:
    """Verified v7.x RT-STT control-plane. HTTP layer is injectable for tests."""

    def __init__(self, config: AgoraConfig, http_post: Any = None, http_get: Any = None) -> None:
        self._cfg = config
        # Optional injected callables (used in tests). Signature mirrors httpx.
        self._post = http_post
        self._get = http_get

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": basic_auth_header(self._cfg.customer_id, self._cfg.customer_secret),
            "Content-Type": "application/json",
        }

    def _join_url(self) -> str:
        return f"{AGORA_HOST}{STT_BASE}/{self._cfg.app_id}/join"

    def _agent_url(self, agent_id: str) -> str:
        return f"{AGORA_HOST}{STT_BASE}/{self._cfg.app_id}/agents/{agent_id}"

    async def start_task(
        self,
        *,
        channel: str,
        name: str,
        sub_bot_uid: str,
        pub_bot_uid: str,
        languages: list[str],
        sub_bot_token: str | None = None,
        pub_bot_token: str | None = None,
    ) -> str:
        """POST .../join → returns agent_id. Body field names should be checked
        against the current v7.x docs before enabling live (see AGORA_SETUP)."""
        body: dict[str, Any] = {
            "name": name,
            "languages": languages[:2],
            "maxIdleTime": self._cfg.idle_seconds,
            "rtcConfig": {
                "channelName": channel,
                "subBotUid": sub_bot_uid,
                "pubBotUid": pub_bot_uid,
            },
        }
        if sub_bot_token:
            body["rtcConfig"]["subBotToken"] = sub_bot_token
        if pub_bot_token:
            body["rtcConfig"]["pubBotToken"] = pub_bot_token

        data = await self._do("POST", self._join_url(), body)
        agent_id = data.get("agent_id") or data.get("agentId") or data.get("taskId")
        if not agent_id:
            raise ProviderError(
                "Agora start returned no agent id",
                code="agora_start_failed",
                safe_message="Agora task did not start",
            )
        log.info("agora task started agent_id=%s channel=%s", agent_id, channel)
        return str(agent_id)

    async def query_task(self, agent_id: str) -> dict[str, Any]:
        return await self._do("GET", self._agent_url(agent_id), None)

    async def stop_task(self, agent_id: str) -> None:
        await self._do("POST", f"{self._agent_url(agent_id)}/leave", {})
        log.info("agora task stopped agent_id=%s", agent_id)

    async def _do(self, method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        # Prefer injected callables (tests); otherwise use httpx lazily.
        if method == "POST" and self._post is not None:
            return await self._post(url, self._headers(), body)
        if method == "GET" and self._get is not None:
            return await self._get(url, self._headers())

        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            if method == "POST":
                resp = await client.post(url, headers=self._headers(), json=body)
            else:
                resp = await client.get(url, headers=self._headers())
            if resp.status_code >= 400:
                # Never log the response verbatim (may contain tokens).
                raise ProviderError(
                    f"Agora REST {method} {resp.status_code}",
                    code="agora_http_error",
                    safe_message="Agora request failed",
                )
            try:
                return resp.json()
            except Exception:
                return {}


# ---------------------------------------------------------------------------
# Media bridge boundary
# ---------------------------------------------------------------------------
@runtime_checkable
class MediaBridge(Protocol):
    async def open(self) -> None: ...
    async def publish_and_collect(
        self, channel: str, frames: Iterator[bytes], sample_rate: int, timeout: float
    ) -> str: ...
    async def close(self) -> None: ...


class MediaBridgeUnavailable(ProviderUnavailable):
    """Raised when no real Agora media bridge is installed/available."""


class UnavailableMediaBridge:
    """Default bridge: refuses to run and explains the exact blocker."""

    def __init__(self, reason: str = MEDIA_BRIDGE_BLOCKER) -> None:
        self.reason = reason

    async def open(self) -> None:
        raise MediaBridgeUnavailable(self.reason, safe_message="Agora media bridge unavailable")

    async def publish_and_collect(
        self, channel: str, frames: Iterator[bytes], sample_rate: int, timeout: float
    ) -> str:  # pragma: no cover - never reached (open() raises first)
        raise MediaBridgeUnavailable(self.reason, safe_message="Agora media bridge unavailable")

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class AgoraProvider:
    name = "agora"

    def __init__(
        self,
        config: AgoraConfig,
        *,
        language: str = "en",
        bridge_factory: Any = None,
        rest_client: AgoraRestClient | None = None,
    ) -> None:
        self.config = config
        self.language = language
        self._bridge_factory = bridge_factory or (lambda: UnavailableMediaBridge())
        self._rest = rest_client

    async def transcribe(self, audio_path: Path, question_id: str) -> TranscriptionResult:
        if not self.config.configured:
            raise ProviderUnavailable(
                "Agora is not configured", safe_message="Agora not configured"
            )

        t0 = time.monotonic()
        channel = f"whisp-{question_id.replace('-', '')[:16]}"
        bridge = self._bridge_factory()

        # Open the media bridge FIRST. With the default (unavailable) bridge this
        # raises here — BEFORE any paid REST task starts, so no credit is spent.
        await bridge.open()

        rest = self._rest or AgoraRestClient(self.config)
        agent_id: str | None = None
        try:
            pcm, rate = read_wav_pcm16_mono(audio_path)
            frames = iter_pcm_frames(pcm, rate)
            agent_id = await rest.start_task(
                channel=channel,
                name=channel,
                sub_bot_uid="10001",
                pub_bot_uid="10002",
                languages=[self.language],
            )
            transcript = await bridge.publish_and_collect(
                channel, frames, rate, timeout=float(self.config.max_duration_seconds)
            )
            processing_ms = int((time.monotonic() - t0) * 1000)
            if not transcript or not transcript.strip():
                raise EmptyTranscript()
            return TranscriptionResult(
                transcript=transcript.strip(),
                provider=self.name,
                processing_ms=processing_ms,
                language=self.language,
                raw_metadata={"channel": channel, "agent_id": agent_id},
            )
        finally:
            if agent_id is not None:
                try:
                    await rest.stop_task(agent_id)
                except Exception:
                    log.warning("agora cleanup stop_task failed for agent_id=%s", agent_id)
            await bridge.close()


class MockAgoraProvider:
    """Deterministic, offline Agora stand-in for tests and credential-free demos."""

    name = "agora"

    def __init__(
        self,
        transcript: str = "(mock agora transcript)",
        *,
        language: str = "en",
        fail: bool = False,
        empty: bool = False,
    ) -> None:
        self._transcript = transcript
        self._language = language
        self._fail = fail
        self._empty = empty

    async def transcribe(self, audio_path: Path, question_id: str) -> TranscriptionResult:
        if self._fail:
            raise ProviderUnavailable("mock agora failure", safe_message="Agora unavailable")
        if self._empty:
            raise EmptyTranscript()
        return TranscriptionResult(
            transcript=self._transcript,
            provider=self.name,
            processing_ms=1,
            language=self._language,
            raw_metadata={"mock": True},
        )

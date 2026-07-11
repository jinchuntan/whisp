"""Agora Real-Time Speech-to-Text provider.

Agora RT-STT is an RTC-*channel* product, not a "POST a WAV" endpoint. You start
a task via REST; Agora's bots join your channel and subscribe to the audio
published there. To transcribe our pre-recorded badge WAV we must PUSH PCM frames
into the channel using the Linux-only ``agora-python-server-sdk`` (the "media
bridge", ``providers/agora_bridge.py``); the bot transcribes and captions come
back on the channel data stream.

This module owns the CREDIT-SAFE orchestration and the verified REST control
plane. Live RTC/STT calls never run unless ``AGORA_LIVE_ENABLED`` is true AND a
real media bridge is supplied; the default bridge refuses to run and explains the
blocker, so no credit is ever spent by accident.

Correct order (prevents a paid STT agent waiting in an empty channel):
  config → live gate → credit guard → read/validate WAV → channel + tokens →
  bridge.open (join + await connected) → REST start → publish PCM + collect →
  REST stop → bridge.close.

Nothing here logs secrets, tokens, or raw provider bodies.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from persephone_worker.audio import AudioError, iter_pcm_frames, read_wav_pcm16_mono
from persephone_worker.providers.agora_token import build_rtc_publisher_token
from persephone_worker.providers.base import (
    AGORA_CLEANUP_FAILED,
    AGORA_CREDIT_LIMIT_REACHED,
    AGORA_EMPTY_TRANSCRIPT,
    AGORA_LIVE_DISABLED,
    AGORA_NOT_CONFIGURED,
    AGORA_STT_START_FAILED,
    AGORA_TOKEN_ERROR,
    AGORA_UNSUPPORTED_AUDIO,
    EmptyTranscript,
    ProviderError,
    ProviderUnavailable,
    TranscriptionResult,
)

log = logging.getLogger("persephone.provider.agora")

AGORA_HOST = "https://api.agora.io"
STT_BASE = "/api/speech-to-text/v1/projects"
SAMPLE_RATE = 16000

# Map our short language codes to Agora's BCP-47-ish codes. Verify the exact
# accepted codes against the live "join" reference (Level B).
_LANGUAGE_MAP = {"en": "en-US", "zh": "zh-CN", "es": "es-ES", "fr": "fr-FR", "de": "de-DE"}

MEDIA_BRIDGE_BLOCKER = (
    "Agora RT-STT media bridge is not available. Publishing our recorded WAV into "
    "an RTC channel requires the Linux-only 'agora-python-server-sdk' plus real "
    "Agora credentials, AGORA_LIVE_ENABLED=true, and transcription credit. See "
    "docs/AGORA_SETUP.md."
)


@dataclass
class AgoraConfig:
    app_id: str = ""
    app_certificate: str = ""
    customer_id: str = ""
    customer_secret: str = ""
    live_enabled: bool = False
    worker_uid: int = 20001
    sub_bot_uid: int = 10001
    pub_bot_uid: int = 10002
    token_ttl_seconds: int = 300
    max_duration_seconds: int = 12
    idle_seconds: int = 10
    daily_max_jobs: int = 20
    connect_timeout: float = 8.0
    rest_timeout: float = 10.0

    @property
    def configured(self) -> bool:
        return bool(
            self.app_id and self.app_certificate and self.customer_id and self.customer_secret
        )


def basic_auth_header(customer_id: str, customer_secret: str) -> str:
    import base64

    raw = f"{customer_id}:{customer_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def agora_language(code: str) -> str:
    return _LANGUAGE_MAP.get(code.lower(), code)


# ---------------------------------------------------------------------------
# REST control-plane (hardened, injectable transport)
# ---------------------------------------------------------------------------
class _TransientRestError(Exception):
    """Internal: a retryable REST failure (network / 5xx / 429)."""


class AgoraRestClient:
    """v7.x RT-STT control-plane. HTTP layer injectable for tests.

    Retries ONLY transient failures (network, 5xx, 429); never retries 4xx
    (credential/schema) errors. Never logs tokens or raw response bodies.
    """

    def __init__(
        self,
        config: AgoraConfig,
        *,
        http_post: Any = None,
        http_get: Any = None,
        timeout: float | None = None,
        max_attempts: int = 2,
    ) -> None:
        self._cfg = config
        self._post = http_post
        self._get = http_get
        self._timeout = timeout if timeout is not None else config.rest_timeout
        self._max_attempts = max(1, max_attempts)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": basic_auth_header(self._cfg.customer_id, self._cfg.customer_secret),
            "Content-Type": "application/json",
        }

    def _join_url(self) -> str:
        return f"{AGORA_HOST}{STT_BASE}/{self._cfg.app_id}/join"

    def _agent_url(self, agent_id: str) -> str:
        return f"{AGORA_HOST}{STT_BASE}/{self._cfg.app_id}/agents/{agent_id}"

    @staticmethod
    def build_join_body(
        *,
        channel: str,
        name: str,
        sub_bot_uid: str,
        pub_bot_uid: str,
        languages: list[str],
        idle_seconds: int,
        sub_bot_token: str | None,
        pub_bot_token: str | None,
    ) -> dict[str, Any]:
        """Typed builder for the v7.x 'join' request body.

        Field spellings (channelName / subBotUid / pubBotUid / maxIdleTime) are
        from Agora's documented schema and must be confirmed live (Level B).
        """
        rtc: dict[str, Any] = {
            "channelName": channel,
            "subBotUid": sub_bot_uid,
            "pubBotUid": pub_bot_uid,
        }
        if sub_bot_token:
            rtc["subBotToken"] = sub_bot_token
        if pub_bot_token:
            rtc["pubBotToken"] = pub_bot_token
        return {
            "name": name,
            "languages": languages[:2],
            "maxIdleTime": idle_seconds,
            "rtcConfig": rtc,
        }

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
        body = self.build_join_body(
            channel=channel,
            name=name,
            sub_bot_uid=sub_bot_uid,
            pub_bot_uid=pub_bot_uid,
            languages=languages,
            idle_seconds=self._cfg.idle_seconds,
            sub_bot_token=sub_bot_token,
            pub_bot_token=pub_bot_token,
        )
        data = await self._request("POST", self._join_url(), body)
        agent_id = data.get("agent_id") or data.get("agentId") or data.get("taskId")
        if not agent_id:
            raise ProviderError(
                "Agora join returned no agent id",
                code=AGORA_STT_START_FAILED,
                safe_message="Agora task did not start",
            )
        log.info("agora task started agent_id=%s channel=%s", agent_id, channel)
        return str(agent_id)

    async def query_task(self, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", self._agent_url(agent_id), None)

    async def stop_task(self, agent_id: str) -> None:
        # Idempotent: a 404 (already gone) is treated as success.
        try:
            await self._request("POST", f"{self._agent_url(agent_id)}/leave", {})
        except ProviderError as exc:
            if getattr(exc, "http_status", None) == 404:
                return
            raise
        log.info("agora task stopped agent_id=%s", agent_id)

    async def _request(self, method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return await self._send(method, url, body)
            except _TransientRestError as exc:
                last_exc = exc
                if attempt + 1 < self._max_attempts:
                    await asyncio.sleep(0.2 * (attempt + 1))
        raise ProviderError(
            "Agora REST transient failure",
            code=AGORA_STT_START_FAILED,
            safe_message="Agora request failed",
        ) from last_exc

    async def _send(self, method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        # Injected transport (tests) — no retry/instrumentation.
        if method == "POST" and self._post is not None:
            return await self._post(url, self._headers(), body)
        if method == "GET" and self._get is not None:
            return await self._get(url, self._headers())

        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                if method == "POST":
                    resp = await client.post(url, headers=self._headers(), json=body)
                else:
                    resp = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise _TransientRestError(str(exc)) from exc

        if resp.status_code in (429,) or resp.status_code >= 500:
            raise _TransientRestError(f"status {resp.status_code}")
        if resp.status_code >= 400:
            # Never include the response body (may echo tokens). Only the status.
            err = ProviderError(
                f"Agora REST {method} {resp.status_code}",
                code=AGORA_STT_START_FAILED,
                safe_message="Agora request rejected",
            )
            err.http_status = resp.status_code  # type: ignore[attr-defined]
            raise err
        try:
            return resp.json()
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Media bridge boundary
# ---------------------------------------------------------------------------
@runtime_checkable
class MediaBridge(Protocol):
    async def open(self, channel: str, uid: int, token: str, *, timeout: float) -> None: ...
    async def publish_and_collect(
        self, frames: Iterator[bytes], *, max_seconds: float, timeout: float
    ) -> str: ...
    async def close(self) -> None: ...


class MediaBridgeUnavailable(ProviderUnavailable):
    """Raised when no real Agora media bridge is installed/available."""


class UnavailableMediaBridge:
    """Default bridge: refuses to run and explains the exact blocker. No credit."""

    def __init__(self, reason: str = MEDIA_BRIDGE_BLOCKER) -> None:
        self.reason = reason

    async def open(self, channel: str, uid: int, token: str, *, timeout: float) -> None:
        raise MediaBridgeUnavailable(self.reason, safe_message="Agora media bridge unavailable")

    async def publish_and_collect(
        self, frames: Iterator[bytes], *, max_seconds: float, timeout: float
    ) -> str:  # pragma: no cover - never reached (open raises first)
        raise MediaBridgeUnavailable(self.reason, safe_message="Agora media bridge unavailable")

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
BridgeFactory = Callable[[str], MediaBridge]
UsageCounter = Callable[[], int]


class AgoraProvider:
    name = "agora"

    def __init__(
        self,
        config: AgoraConfig,
        *,
        language: str = "en",
        bridge_factory: BridgeFactory | None = None,
        rest_client: AgoraRestClient | None = None,
        token_builder: Callable[..., str] | None = None,
        usage_counter: UsageCounter | None = None,
        channel_suffix: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.language = language
        self._bridge_factory: BridgeFactory = bridge_factory or (
            lambda _c: UnavailableMediaBridge()
        )
        self._rest = rest_client
        self._build_token = token_builder or build_rtc_publisher_token
        self._usage_counter = usage_counter
        self._channel_suffix = channel_suffix or (lambda: secrets.token_hex(3))

    def _channel_name(self, question_id: str) -> str:
        short = "".join(ch for ch in question_id if ch.isalnum())[:8] or "q"
        return f"persephone-{short}-{self._channel_suffix()}"

    def _tokens(self, channel: str) -> tuple[str, str, str]:
        try:
            ttl = self.config.token_ttl_seconds
            worker = self._build_token(
                self.config.app_id,
                self.config.app_certificate,
                channel,
                self.config.worker_uid,
                ttl,
            )
            sub = self._build_token(
                self.config.app_id,
                self.config.app_certificate,
                channel,
                self.config.sub_bot_uid,
                ttl,
            )
            pub = self._build_token(
                self.config.app_id,
                self.config.app_certificate,
                channel,
                self.config.pub_bot_uid,
                ttl,
            )
            return worker, sub, pub
        except Exception as exc:
            raise ProviderError(
                "Agora token generation failed",
                code=AGORA_TOKEN_ERROR,
                safe_message="Agora token error",
            ) from exc

    async def transcribe(self, audio_path: Path, question_id: str) -> TranscriptionResult:
        # 1) configuration + 2) live gate — refuse before any bridge/REST.
        if not self.config.configured:
            raise ProviderUnavailable(
                "Agora is not configured",
                code=AGORA_NOT_CONFIGURED,
                safe_message="Agora not configured",
            )
        if not self.config.live_enabled:
            raise ProviderUnavailable(
                "Agora live calls are disabled (AGORA_LIVE_ENABLED=false)",
                code=AGORA_LIVE_DISABLED,
                safe_message="Agora live disabled",
            )

        # 3) credit guard — daily ceiling (no credit spent when it trips).
        if self.config.daily_max_jobs > 0 and self._usage_counter is not None:
            used = self._usage_counter()
            if used >= self.config.daily_max_jobs:
                raise ProviderError(
                    f"Agora daily job ceiling reached ({used}/{self.config.daily_max_jobs})",
                    code=AGORA_CREDIT_LIMIT_REACHED,
                    safe_message="Agora daily limit reached",
                )

        # 4) read + validate audio before spending anything.
        try:
            pcm, rate = read_wav_pcm16_mono(audio_path)
        except AudioError as exc:
            raise ProviderError(
                "Unsupported audio for Agora",
                code=AGORA_UNSUPPORTED_AUDIO,
                safe_message="Unsupported audio format",
            ) from exc
        if rate != SAMPLE_RATE:
            raise ProviderError(
                f"Agora expects {SAMPLE_RATE} Hz mono PCM16, got {rate} Hz",
                code=AGORA_UNSUPPORTED_AUDIO,
                safe_message="Unsupported audio sample rate",
            )
        if not pcm:
            # Empty audio: never start a paid task.
            raise EmptyTranscript("No audio to transcribe", code=AGORA_EMPTY_TRANSCRIPT)

        # 5) channel + short-lived tokens (local crypto; no network).
        channel = self._channel_name(question_id)
        worker_token, sub_token, pub_token = self._tokens(channel)

        # 6) open bridge FIRST (join + await connected). Default bridge raises here,
        # BEFORE any paid REST call — so credit is never spent by accident.
        bridge = self._bridge_factory(channel)
        rest = self._rest or AgoraRestClient(self.config)
        agent_id: str | None = None
        audio_seconds = round(len(pcm) / (rate * 2), 2)
        t0 = time.monotonic()
        try:
            await bridge.open(
                channel, self.config.worker_uid, worker_token, timeout=self.config.connect_timeout
            )
            # 7) start the paid STT task now that our publisher is connected.
            agent_id = await rest.start_task(
                channel=channel,
                name=channel,
                sub_bot_uid=str(self.config.sub_bot_uid),
                pub_bot_uid=str(self.config.pub_bot_uid),
                languages=[agora_language(self.language)],
                sub_bot_token=sub_token,
                pub_bot_token=pub_token,
            )
            # 8) publish PCM in real time + 9) collect the transcript.
            frames = iter_pcm_frames(pcm, rate)
            transcript = await bridge.publish_and_collect(
                frames,
                max_seconds=float(self.config.max_duration_seconds),
                timeout=float(self.config.idle_seconds + self.config.max_duration_seconds),
            )
            processing_ms = int((time.monotonic() - t0) * 1000)
            if not transcript or not transcript.strip():
                raise EmptyTranscript("Agora returned no transcript", code=AGORA_EMPTY_TRANSCRIPT)
            return TranscriptionResult(
                transcript=transcript.strip(),
                provider=self.name,
                processing_ms=processing_ms,
                language=self.language,
                raw_metadata={
                    "channel": channel,
                    "agent_id": agent_id,
                    "audio_seconds": audio_seconds,
                    "language": agora_language(self.language),
                },
            )
        finally:
            # 10)+11) stop task and close bridge. Cleanup never masks the original error.
            if agent_id is not None:
                try:
                    await rest.stop_task(agent_id)
                except Exception:
                    log.warning(
                        "%s: stop_task failed for agent_id=%s", AGORA_CLEANUP_FAILED, agent_id
                    )
            try:
                await bridge.close()
            except Exception:
                log.warning("%s: bridge close failed", AGORA_CLEANUP_FAILED)


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

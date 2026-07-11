"""Real Agora media bridge — publishes badge PCM into an RTC channel and
collects STT captions from the channel data stream.

Built on ``agora-python-server-sdk`` (Linux/glibc only). Every SDK import is
LAZY so the worker imports and runs without the package (e.g. in
``faster_whisper_only`` or on Windows/Vercel). Nothing in this module runs unless
the caller has already checked ``AGORA_LIVE_ENABLED`` and constructed the bridge.

Lifecycle (see AgoraProvider for the full orchestration):
  * ``AgoraServiceManager`` initializes exactly ONE ``AgoraService`` per process
    (the SDK requires this) and releases it at worker shutdown.
  * ``AgoraPcmMediaBridge`` owns ONE RTC connection per question: join → publish
    PCM in real time → collect captions → tear down.

Threading: native SDK callbacks fire on SDK threads. They copy their bytes and
hand off to the asyncio loop via ``call_soon_threadsafe`` — no heavy work runs in
a callback, and all state mutation happens on the loop thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import types
from collections.abc import Iterator

from persephone_worker.providers.agora_captions import CaptionCollector
from persephone_worker.providers.base import (
    AGORA_CAPTION_TIMEOUT,
    AGORA_PUBLISH_FAILED,
    AGORA_RTC_JOIN_FAILED,
    AGORA_RTC_JOIN_TIMEOUT,
    AGORA_SDK_UNAVAILABLE,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)

log = logging.getLogger("persephone.provider.agora.bridge")

# PCM framing for badge audio (16 kHz mono PCM16 → 10 ms / 160 samples / 320 B).
FRAME_MS = 10
SAMPLE_RATE = 16000
CHANNELS = 1
# Small grace after STT start so Agora's bot subscribes before we speak, and a
# quiescence window to let trailing captions settle. Live-tuning knobs.
DEFAULT_PRE_PUBLISH_DELAY = 0.5
DEFAULT_CAPTION_SETTLE = 1.2
DEFAULT_CONNECT_TIMEOUT = 8.0
DEFAULT_CLEANUP_TIMEOUT = 5.0


def _load_sdk() -> types.SimpleNamespace:
    """Import the Agora SDK lazily. Raises ProviderUnavailable if unavailable.

    Import failure (package missing OR native libs not loadable because
    LD_LIBRARY_PATH is unset) is treated as 'Agora unavailable' so the router can
    fall back to Faster-Whisper where the mode allows it.
    """
    try:
        from agora.rtc.agora_base import (
            AudioPublishType,
            AudioScenarioType,
            ChannelProfileType,
            ClientRoleType,
            RTCConnConfig,
            RtcConnectionPublishConfig,
        )
        from agora.rtc.agora_service import AgoraService, AgoraServiceConfig
        from agora.rtc.local_user_observer import IRTCLocalUserObserver
        from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
    except Exception as exc:  # ImportError, OSError (missing .so), etc.
        raise ProviderUnavailable(
            "Agora server SDK is not available",
            code=AGORA_SDK_UNAVAILABLE,
            safe_message="Agora SDK unavailable",
        ) from exc
    return types.SimpleNamespace(
        AgoraService=AgoraService,
        AgoraServiceConfig=AgoraServiceConfig,
        RTCConnConfig=RTCConnConfig,
        RtcConnectionPublishConfig=RtcConnectionPublishConfig,
        AudioScenarioType=AudioScenarioType,
        ClientRoleType=ClientRoleType,
        ChannelProfileType=ChannelProfileType,
        AudioPublishType=AudioPublishType,
        IRTCConnectionObserver=IRTCConnectionObserver,
        IRTCLocalUserObserver=IRTCLocalUserObserver,
    )


def agora_sdk_available() -> bool:
    """True if the Agora SDK (and its native libs) can be imported. No side effects
    beyond the import; never raises."""
    try:
        _load_sdk()
        return True
    except ProviderUnavailable:
        return False


def sdk_version() -> str:
    """Best-effort SDK version string for safe metadata (never raises)."""
    try:
        import os

        import agora  # noqa: PLC0415

        path = os.path.join(os.path.dirname(agora.__file__), "version.txt")
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return "unknown"


class AgoraServiceManager:
    """Process-wide singleton for the one ``AgoraService`` the SDK allows.

    Thread-safe. ``get`` initializes on first use; ``release`` is called once at
    worker shutdown. A failed per-question connection never releases the service.
    """

    _lock = threading.Lock()
    _service: object | None = None
    _app_id: str | None = None

    @classmethod
    def get(cls, app_id: str) -> object:
        with cls._lock:
            if cls._service is not None:
                return cls._service
            sdk = _load_sdk()
            service = sdk.AgoraService()
            config = sdk.AgoraServiceConfig()
            config.appid = app_id
            config.audio_scenario = sdk.AudioScenarioType.AUDIO_SCENARIO_AI_SERVER
            rc = service.initialize(config)
            if rc != 0:
                with contextlib.suppress(Exception):
                    service.release()
                raise ProviderError(
                    f"AgoraService.initialize returned {rc}",
                    code=AGORA_RTC_JOIN_FAILED,
                    safe_message="Agora service initialization failed",
                )
            cls._service = service
            cls._app_id = app_id
            log.info("AgoraService initialized (sdk=%s)", sdk_version())
            return service

    @classmethod
    def is_initialized(cls) -> bool:
        with cls._lock:
            return cls._service is not None

    @classmethod
    def release(cls) -> None:
        with cls._lock:
            if cls._service is None:
                return
            try:
                cls._service.release()  # type: ignore[attr-defined]
            except Exception:
                log.warning("AgoraService.release failed")
            finally:
                cls._service = None
                cls._app_id = None
                log.info("AgoraService released")


def _build_conn_observer(sdk, loop, on_connected, on_failed):
    class _ConnObserver(sdk.IRTCConnectionObserver):
        def on_connected(self, conn, info, reason):
            loop.call_soon_threadsafe(on_connected)

        def on_connection_failure(self, conn, info, reason):
            loop.call_soon_threadsafe(on_failed)

        def on_disconnected(self, conn, info, reason):
            pass

    return _ConnObserver()


def _build_local_user_observer(sdk, loop, sink):
    class _LocalUserObserver(sdk.IRTCLocalUserObserver):
        def on_stream_message(self, local_user, user_id, stream_id, data, length):
            if data:
                # Copy immediately; hand the raw bytes to the loop for decoding.
                loop.call_soon_threadsafe(sink, bytes(data))

        def on_audio_meta_data_received(self, local_user, user_id, data):
            if data:
                loop.call_soon_threadsafe(sink, bytes(data))

    return _LocalUserObserver()


class AgoraPcmMediaBridge:
    """One RTC connection per question: join, publish PCM, collect captions."""

    def __init__(
        self,
        app_id: str,
        *,
        collector: CaptionCollector | None = None,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_ms: int = FRAME_MS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        pre_publish_delay: float = DEFAULT_PRE_PUBLISH_DELAY,
        caption_settle: float = DEFAULT_CAPTION_SETTLE,
        cleanup_timeout: float = DEFAULT_CLEANUP_TIMEOUT,
        sdk: types.SimpleNamespace | None = None,
        service: object | None = None,
    ) -> None:
        self._app_id = app_id
        self.collector = collector or CaptionCollector()
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_ms = frame_ms
        self._connect_timeout = connect_timeout
        self._pre_publish_delay = pre_publish_delay
        self._caption_settle = caption_settle
        self._cleanup_timeout = cleanup_timeout
        # Injectable for offline tests (never load native libs in tests).
        self._sdk = sdk
        self._service = service
        self._conn: object | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def open(
        self, channel: str, uid: int, token: str, *, timeout: float | None = None
    ) -> None:
        """Join the RTC channel as the worker publisher and await connection."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        sdk = self._sdk or _load_sdk()
        service = self._service or AgoraServiceManager.get(self._app_id)

        conn_config = sdk.RTCConnConfig(
            auto_subscribe_audio=0,
            auto_subscribe_video=0,
            client_role_type=sdk.ClientRoleType.CLIENT_ROLE_BROADCASTER,
            channel_profile=sdk.ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
        )
        publish_config = sdk.RtcConnectionPublishConfig(
            is_publish_audio=True,
            is_publish_video=False,
            audio_publish_type=sdk.AudioPublishType.AUDIO_PUBLISH_TYPE_PCM,
            audio_scenario=sdk.AudioScenarioType.AUDIO_SCENARIO_AI_SERVER,
        )
        conn = service.create_rtc_connection(conn_config, publish_config)  # type: ignore[attr-defined]
        if conn is None:
            raise ProviderError(
                "create_rtc_connection returned None",
                code=AGORA_RTC_JOIN_FAILED,
                safe_message="Agora RTC connection failed",
            )
        self._conn = conn

        connected = asyncio.Event()
        failed = {"flag": False}

        def _on_connected() -> None:
            connected.set()

        def _on_failed() -> None:
            failed["flag"] = True
            connected.set()

        conn.register_observer(_build_conn_observer(sdk, loop, _on_connected, _on_failed))
        conn.register_local_user_observer(
            _build_local_user_observer(sdk, loop, self.collector.feed_bytes)
        )

        conn.connect(token, channel, str(uid))
        try:
            await asyncio.wait_for(connected.wait(), timeout=timeout or self._connect_timeout)
        except asyncio.TimeoutError as exc:
            raise ProviderTimeout(
                "Timed out joining Agora RTC channel",
                code=AGORA_RTC_JOIN_TIMEOUT,
                safe_message="Agora RTC join timed out",
            ) from exc
        if failed["flag"]:
            raise ProviderError(
                "Agora RTC connection failed",
                code=AGORA_RTC_JOIN_FAILED,
                safe_message="Agora RTC join failed",
            )
        log.info("agora bridge joined channel (uid=%s)", uid)

    async def publish_and_collect(
        self,
        frames: Iterator[bytes],
        *,
        max_seconds: float,
        timeout: float,
        collect: bool = True,
    ) -> str:
        """Publish PCM frames in real time, then collect the final transcript.

        With ``collect=False`` this is an RTC-only smoke test: audio is published
        but no captions are awaited (returns "").
        """
        conn = self._conn
        if conn is None:
            raise ProviderError(
                "publish_and_collect called before open()",
                code=AGORA_PUBLISH_FAILED,
                safe_message="Agora publish failed",
            )
        loop = self._loop or asyncio.get_running_loop()

        rc = conn.publish_audio()  # type: ignore[attr-defined]
        if rc is not None and rc < 0:
            raise ProviderError(
                f"publish_audio returned {rc}",
                code=AGORA_PUBLISH_FAILED,
                safe_message="Agora publish failed",
            )

        if self._pre_publish_delay > 0:
            await asyncio.sleep(self._pre_publish_delay)

        await self._pace_frames(conn, frames, loop, max_seconds=max_seconds)

        if not collect:
            return ""
        return await self._collect(loop, timeout=timeout)

    async def _pace_frames(
        self,
        conn: object,
        frames: Iterator[bytes],
        loop: asyncio.AbstractEventLoop,
        *,
        max_seconds: float,
    ) -> None:
        frame_dt = self._frame_ms / 1000.0
        max_frames = int(max_seconds * 1000 / self._frame_ms) if max_seconds > 0 else None
        start = loop.time()
        sent = 0
        for frame in frames:
            if max_frames is not None and sent >= max_frames:
                log.info("agora publish hit max duration (%.1fs)", max_seconds)
                break
            # The SDK's sender does ctypes.from_buffer(frame.data), which requires a
            # WRITABLE buffer — immutable bytes raise "underlying buffer is not
            # writable". Hand it a fresh bytearray per frame.
            rc = conn.push_audio_pcm_data(  # type: ignore[attr-defined]
                bytearray(frame), self._sample_rate, self._channels, sent * self._frame_ms
            )
            if rc is not None and rc < 0:
                raise ProviderError(
                    f"push_audio_pcm_data returned {rc}",
                    code=AGORA_PUBLISH_FAILED,
                    safe_message="Agora publish failed",
                )
            sent += 1
            # Pace against the monotonic loop clock — target time is derived from
            # the frame index (start + n*dt), so there is no cumulative drift.
            target = start + sent * frame_dt
            delay = target - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        log.info("agora published %d frames (%.2fs)", sent, sent * frame_dt)

    async def _collect(self, loop: asyncio.AbstractEventLoop, *, timeout: float) -> str:
        """Wait for a final caption, then quiescence, bounded by ``timeout``."""
        overall_deadline = loop.time() + timeout
        last_count = -1
        quiet_deadline = loop.time() + self._caption_settle
        while loop.time() < overall_deadline:
            count = self.collector.final_count
            if count != last_count:
                last_count = count
                quiet_deadline = loop.time() + self._caption_settle
            if self.collector.has_final and loop.time() >= quiet_deadline:
                break
            await asyncio.sleep(0.05)
        transcript = self.collector.transcript()
        if not transcript and not self.collector.has_final:
            # No final and nothing interim within the window.
            raise ProviderTimeout(
                "No Agora captions received",
                code=AGORA_CAPTION_TIMEOUT,
                safe_message="Agora returned no captions",
            )
        return transcript

    async def close(self) -> None:
        """Tear down the per-question connection. Never raises (cleanup is best-effort)."""
        conn = self._conn
        self._conn = None
        if conn is None:
            return

        def _teardown() -> None:
            for name in ("unpublish_audio", "disconnect", "release"):
                try:
                    getattr(conn, name)()
                except Exception:
                    log.warning("agora bridge %s failed during cleanup", name)

        try:
            await asyncio.wait_for(asyncio.to_thread(_teardown), timeout=self._cleanup_timeout)
        except Exception:
            # Bounded cleanup (incl. timeout); the global service stays alive for
            # the next job, and we never re-raise from close().
            log.warning("agora bridge cleanup did not complete cleanly")

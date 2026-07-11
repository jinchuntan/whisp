"""Real media bridge — exercised entirely with FAKE SDK objects.

No native Agora libs are loaded, no network, no credit. The bridge accepts an
injected ``sdk`` + ``service`` so the RTC/PCM/caption/cleanup logic is testable
offline.
"""

from __future__ import annotations

import json
import types

import pytest

from persephone_worker.audio import iter_pcm_frames
from persephone_worker.providers import agora_bridge
from persephone_worker.providers.agora_bridge import (
    AgoraPcmMediaBridge,
    AgoraServiceManager,
    _build_local_user_observer,
)
from persephone_worker.providers.base import ProviderError, ProviderTimeout


# --------------------------------------------------------------------------
# Fake SDK
# --------------------------------------------------------------------------
def _fake_sdk() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        RTCConnConfig=lambda **kw: kw,
        RtcConnectionPublishConfig=lambda **kw: kw,
        ClientRoleType=types.SimpleNamespace(CLIENT_ROLE_BROADCASTER=1),
        ChannelProfileType=types.SimpleNamespace(CHANNEL_PROFILE_LIVE_BROADCASTING=1),
        AudioPublishType=types.SimpleNamespace(AUDIO_PUBLISH_TYPE_PCM=1),
        AudioScenarioType=types.SimpleNamespace(AUDIO_SCENARIO_AI_SERVER=9),
        IRTCConnectionObserver=object,
        IRTCLocalUserObserver=object,
    )


class FakeConnection:
    def __init__(self, *, captions=(), connect_mode="connected", publish_rc=0, push_rc=0):
        self.captions = list(captions)
        self.connect_mode = connect_mode  # "connected" | "failed" | "silent"
        self.publish_rc = publish_rc
        self.push_rc = push_rc
        self.conn_obs = None
        self.lu_obs = None
        self.pushed = 0
        self.teardown: list[str] = []
        self.release_raises = False

    def register_observer(self, obs):
        self.conn_obs = obs

    def register_local_user_observer(self, obs):
        self.lu_obs = obs

    def connect(self, token, channel, uid):
        if self.connect_mode == "connected":
            self.conn_obs.on_connected(self, None, 0)
        elif self.connect_mode == "failed":
            self.conn_obs.on_connection_failure(self, None, 0)
        # "silent" -> never fire (join timeout)

    def publish_audio(self):
        for payload in self.captions:
            self.lu_obs.on_stream_message(None, "10002", 1, payload, len(payload))
        return self.publish_rc

    def push_audio_pcm_data(self, data, sample_rate, channels, pts):
        self.pushed += 1
        return self.push_rc

    def unpublish_audio(self):
        self.teardown.append("unpublish")
        return 0

    def disconnect(self):
        self.teardown.append("disconnect")
        return 0

    def release(self):
        self.teardown.append("release")
        if self.release_raises:
            raise RuntimeError("release blew up")


class FakeService:
    def __init__(self, conn):
        self._conn = conn

    def create_rtc_connection(self, conn_config, publish_config):
        return self._conn


def _bridge(conn, **kw):
    return AgoraPcmMediaBridge(
        "app",
        sdk=_fake_sdk(),
        service=FakeService(conn),
        pre_publish_delay=0.0,
        caption_settle=0.05,
        connect_timeout=0.2,
        cleanup_timeout=1.0,
        **kw,
    )


def _pcm(seconds=0.2, rate=16000):
    return b"\x01\x00" * int(seconds * rate)


# --------------------------------------------------------------------------
# open / join
# --------------------------------------------------------------------------
async def test_open_connects_and_registers_observers():
    conn = FakeConnection()
    bridge = _bridge(conn)
    await bridge.open("chan", 20001, "tok", timeout=0.2)
    assert conn.conn_obs is not None and conn.lu_obs is not None
    await bridge.close()


async def test_open_join_timeout():
    conn = FakeConnection(connect_mode="silent")
    bridge = _bridge(conn)
    with pytest.raises(ProviderTimeout) as ei:
        await bridge.open("chan", 20001, "tok", timeout=0.15)
    assert ei.value.code == "agora_rtc_join_timeout"


async def test_open_join_failure():
    conn = FakeConnection(connect_mode="failed")
    bridge = _bridge(conn)
    with pytest.raises(ProviderError) as ei:
        await bridge.open("chan", 20001, "tok", timeout=0.2)
    assert ei.value.code == "agora_rtc_join_failed"


# --------------------------------------------------------------------------
# publish + collect
# --------------------------------------------------------------------------
async def test_publish_and_collect_returns_final_transcript():
    caption = json.dumps({"text": "hello room", "is_final": True, "seqnum": 1}).encode()
    conn = FakeConnection(captions=[caption])
    bridge = _bridge(conn)
    await bridge.open("chan", 20001, "tok", timeout=0.2)
    text = await bridge.publish_and_collect(
        iter_pcm_frames(_pcm(0.05), 16000), max_seconds=1.0, timeout=1.0
    )
    assert text == "hello room"
    assert conn.pushed == 5  # 50 ms / 10 ms
    await bridge.close()


async def test_publish_paces_and_enforces_max_seconds():
    conn = FakeConnection()
    bridge = _bridge(conn)
    await bridge.open("chan", 20001, "tok", timeout=0.2)
    # 0.5 s of audio (50 frames) but capped to 0.1 s -> 10 frames.
    await bridge.publish_and_collect(
        iter_pcm_frames(_pcm(0.5), 16000), max_seconds=0.1, timeout=0.5, collect=False
    )
    assert conn.pushed == 10
    await bridge.close()


async def test_publish_failure_raises():
    conn = FakeConnection(push_rc=-1)
    bridge = _bridge(conn)
    await bridge.open("chan", 20001, "tok", timeout=0.2)
    with pytest.raises(ProviderError) as ei:
        await bridge.publish_and_collect(
            iter_pcm_frames(_pcm(0.05), 16000), max_seconds=1.0, timeout=0.5, collect=False
        )
    assert ei.value.code == "agora_publish_failed"
    await bridge.close()


async def test_collect_timeout_when_no_captions():
    conn = FakeConnection(captions=[])
    bridge = _bridge(conn)
    await bridge.open("chan", 20001, "tok", timeout=0.2)
    with pytest.raises(ProviderTimeout) as ei:
        await bridge.publish_and_collect(
            iter_pcm_frames(_pcm(0.02), 16000), max_seconds=1.0, timeout=0.2
        )
    assert ei.value.code == "agora_caption_timeout"
    await bridge.close()


async def test_out_of_order_and_duplicate_captions_merge():
    caps = [
        json.dumps({"text": "world", "is_final": True, "seqnum": 2}).encode(),
        json.dumps({"text": "hello", "is_final": True, "seqnum": 1}).encode(),
        json.dumps({"text": "hello", "is_final": True, "seqnum": 1}).encode(),  # dup
    ]
    conn = FakeConnection(captions=caps)
    bridge = _bridge(conn)
    await bridge.open("chan", 20001, "tok", timeout=0.2)
    text = await bridge.publish_and_collect(
        iter_pcm_frames(_pcm(0.02), 16000), max_seconds=1.0, timeout=1.0
    )
    assert text == "hello world"
    await bridge.close()


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------
async def test_close_tears_down_in_order():
    conn = FakeConnection()
    bridge = _bridge(conn)
    await bridge.open("chan", 20001, "tok", timeout=0.2)
    await bridge.close()
    assert conn.teardown == ["unpublish", "disconnect", "release"]


async def test_close_swallows_teardown_errors():
    conn = FakeConnection()
    conn.release_raises = True
    bridge = _bridge(conn)
    await bridge.open("chan", 20001, "tok", timeout=0.2)
    await bridge.close()  # must not raise


async def test_close_is_safe_without_open():
    bridge = _bridge(FakeConnection())
    await bridge.close()  # no-op, no error


# --------------------------------------------------------------------------
# callback -> asyncio handoff copies data
# --------------------------------------------------------------------------
async def test_local_user_observer_copies_callback_bytes():
    import asyncio

    received: list[bytes] = []
    loop = asyncio.get_running_loop()
    obs = _build_local_user_observer(_fake_sdk(), loop, received.append)
    buf = bytearray(b"caption-bytes")
    obs.on_stream_message(None, "u", 1, buf, len(buf))
    buf[:] = b"xxxxxxxxxxxxx"  # mutate the source after the callback returns
    await asyncio.sleep(0)  # let call_soon_threadsafe run
    assert received == [b"caption-bytes"]


# --------------------------------------------------------------------------
# Service manager (one per process)
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_service_manager():
    AgoraServiceManager.release()
    yield
    AgoraServiceManager.release()


def _service_sdk(init_results):
    class Svc:
        created = 0

        def __init__(self):
            type(self).created += 1
            self.released = False

        def initialize(self, config):
            return init_results.pop(0) if init_results else 0

        def release(self):
            self.released = True

    class Cfg:
        def __init__(self):
            self.appid = ""
            self.audio_scenario = None

    sdk = types.SimpleNamespace(
        AgoraService=Svc,
        AgoraServiceConfig=Cfg,
        AudioScenarioType=types.SimpleNamespace(AUDIO_SCENARIO_AI_SERVER=9),
    )
    return sdk, Svc


def test_service_manager_single_instance(monkeypatch):
    sdk, Svc = _service_sdk([0])
    monkeypatch.setattr(agora_bridge, "_load_sdk", lambda: sdk)
    s1 = AgoraServiceManager.get("app")
    s2 = AgoraServiceManager.get("app")
    assert s1 is s2
    assert Svc.created == 1
    assert AgoraServiceManager.is_initialized()


def test_service_manager_release_allows_reinit(monkeypatch):
    sdk, Svc = _service_sdk([0, 0])
    monkeypatch.setattr(agora_bridge, "_load_sdk", lambda: sdk)
    AgoraServiceManager.get("app")
    AgoraServiceManager.release()
    assert not AgoraServiceManager.is_initialized()
    AgoraServiceManager.get("app")
    assert Svc.created == 2


def test_service_manager_failed_init_raises_and_stays_uninitialized(monkeypatch):
    sdk, _ = _service_sdk([-7])  # non-zero init result
    monkeypatch.setattr(agora_bridge, "_load_sdk", lambda: sdk)
    with pytest.raises(ProviderError):
        AgoraServiceManager.get("app")
    assert not AgoraServiceManager.is_initialized()

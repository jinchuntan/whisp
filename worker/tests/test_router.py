"""Provider router: the four modes, ordering, fallback, and the guarantee that
Agora is never invoked in faster_whisper_only."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeProvider
from whisp_worker.providers.base import ProviderError, ProviderUnavailable
from whisp_worker.providers.router import AGORA, FASTER_WHISPER, MODE_ORDER, ProviderRouter

DUMMY = Path("dummy.wav")


def make_router(mode, fw=None, agora=None, timeouts=None, on_agora_construct=None):
    factories = {}
    if fw is not None:
        factories[FASTER_WHISPER] = lambda: fw
    if agora is not None:

        def agora_factory():
            if on_agora_construct:
                on_agora_construct()
            return agora

        factories[AGORA] = agora_factory
    return ProviderRouter(mode, factories, timeouts=timeouts)


# --------------------------- mode ordering --------------------------------
def test_mode_order_map():
    assert MODE_ORDER["agora_first"] == [AGORA, FASTER_WHISPER]
    assert MODE_ORDER["faster_whisper_first"] == [FASTER_WHISPER, AGORA]
    assert MODE_ORDER["agora_only"] == [AGORA]
    assert MODE_ORDER["faster_whisper_only"] == [FASTER_WHISPER]


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        ProviderRouter("nonsense", {})


# --------------------------- agora_first ----------------------------------
async def test_agora_first_uses_agora_when_ok():
    fw = FakeProvider(FASTER_WHISPER, transcript="from-fw")
    agora = FakeProvider(AGORA, transcript="from-agora")
    outcome = await make_router("agora_first", fw=fw, agora=agora).transcribe(DUMMY, "q")
    assert outcome.status == "done"
    assert outcome.provider_used == AGORA
    assert outcome.fallback_used is False
    assert agora.calls == 1
    assert fw.calls == 0  # no fallback needed


async def test_agora_first_falls_back_on_exception():
    fw = FakeProvider(FASTER_WHISPER, transcript="from-fw")
    agora = FakeProvider(AGORA, raises=ProviderUnavailable("boom"))
    outcome = await make_router("agora_first", fw=fw, agora=agora).transcribe(DUMMY, "q")
    assert outcome.status == "done"
    assert outcome.provider_used == FASTER_WHISPER
    assert outcome.fallback_used is True
    assert agora.calls == 1 and fw.calls == 1
    # Both attempts recorded.
    assert [a.provider for a in outcome.attempts] == [AGORA, FASTER_WHISPER]
    assert outcome.attempts[0].status == "error"
    assert outcome.attempts[1].status == "success"


async def test_agora_first_falls_back_on_timeout():
    fw = FakeProvider(FASTER_WHISPER, transcript="from-fw")
    agora = FakeProvider(AGORA, delay=0.2)
    router = make_router("agora_first", fw=fw, agora=agora, timeouts={AGORA: 0.01})
    outcome = await router.transcribe(DUMMY, "q")
    assert outcome.status == "done"
    assert outcome.provider_used == FASTER_WHISPER
    assert outcome.fallback_used is True
    assert outcome.attempts[0].status == "timeout"


async def test_agora_first_falls_back_on_empty():
    fw = FakeProvider(FASTER_WHISPER, transcript="from-fw")
    agora = FakeProvider(AGORA, empty=True)
    outcome = await make_router("agora_first", fw=fw, agora=agora).transcribe(DUMMY, "q")
    assert outcome.status == "done"
    assert outcome.provider_used == FASTER_WHISPER
    assert outcome.fallback_used is True
    assert outcome.attempts[0].status == "empty"


# --------------------------- faster_whisper_first -------------------------
async def test_fw_first_uses_fw_when_ok():
    fw = FakeProvider(FASTER_WHISPER, transcript="from-fw")
    agora = FakeProvider(AGORA, transcript="from-agora")
    outcome = await make_router("faster_whisper_first", fw=fw, agora=agora).transcribe(DUMMY, "q")
    assert outcome.provider_used == FASTER_WHISPER
    assert outcome.fallback_used is False
    assert agora.calls == 0


async def test_fw_first_falls_back_to_agora():
    fw = FakeProvider(FASTER_WHISPER, raises=ProviderError("fw down"))
    agora = FakeProvider(AGORA, transcript="from-agora")
    outcome = await make_router("faster_whisper_first", fw=fw, agora=agora).transcribe(DUMMY, "q")
    assert outcome.provider_used == AGORA
    assert outcome.fallback_used is True


# --------------------------- agora_only -----------------------------------
async def test_agora_only_no_fallback_on_failure():
    agora = FakeProvider(AGORA, raises=ProviderUnavailable("boom"))
    # Provide a fw too, to prove it is NOT used.
    fw = FakeProvider(FASTER_WHISPER, transcript="should-not-be-used")
    outcome = await make_router("agora_only", fw=fw, agora=agora).transcribe(DUMMY, "q")
    assert outcome.status == "error"
    assert outcome.provider_used is None
    assert fw.calls == 0
    assert len(outcome.attempts) == 1


async def test_agora_only_success():
    agora = FakeProvider(AGORA, transcript="only-agora")
    outcome = await make_router("agora_only", agora=agora).transcribe(DUMMY, "q")
    assert outcome.status == "done"
    assert outcome.provider_used == AGORA
    assert outcome.fallback_used is False


# --------------------------- faster_whisper_only --------------------------
async def test_fw_only_success():
    fw = FakeProvider(FASTER_WHISPER, transcript="only-fw")
    outcome = await make_router("faster_whisper_only", fw=fw).transcribe(DUMMY, "q")
    assert outcome.status == "done"
    assert outcome.provider_used == FASTER_WHISPER
    assert outcome.fallback_used is False


async def test_fw_only_empty_stays_empty_no_fallback():
    fw = FakeProvider(FASTER_WHISPER, empty=True)
    outcome = await make_router("faster_whisper_only", fw=fw).transcribe(DUMMY, "q")
    assert outcome.status == "empty"
    assert outcome.provider_used is None


async def test_fw_only_never_constructs_or_calls_agora():
    """Acceptance criterion 10: zero Agora operations in faster_whisper_only."""
    constructed = {"count": 0}
    called = {"count": 0}

    class ExplodingAgora:
        name = AGORA

        async def transcribe(self, audio_path, question_id):
            called["count"] += 1
            raise AssertionError("Agora.transcribe must not be called")

    fw = FakeProvider(FASTER_WHISPER, transcript="only-fw")
    router = make_router(
        "faster_whisper_only",
        fw=fw,
        agora=ExplodingAgora(),
        on_agora_construct=lambda: constructed.__setitem__("count", constructed["count"] + 1),
    )
    outcome = await router.transcribe(DUMMY, "q")
    assert outcome.status == "done"
    assert constructed["count"] == 0  # factory never invoked
    assert called["count"] == 0  # transcribe never invoked


async def test_fw_only_raises_when_empty_and_error_mix_is_error():
    # Two providers can't happen in only-mode; ensure error path for hard failure.
    fw = FakeProvider(FASTER_WHISPER, raises=ProviderError("kaput"))
    outcome = await make_router("faster_whisper_only", fw=fw).transcribe(DUMMY, "q")
    assert outcome.status == "error"
    assert outcome.error_code is not None
    assert outcome.safe_error_message

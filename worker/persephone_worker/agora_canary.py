"""Manual, user-controlled Agora live-test ("canary").

This is the ONLY code path that intentionally spends Agora credit, and it refuses
to run unless ALL of these hold:

  * ``--live`` is passed on the command line,
  * ``AGORA_LIVE_ENABLED=true`` in the environment/.env,
  * Agora credentials are complete,
  * the operator types the exact confirmation phrase ``SPEND AGORA CREDIT``,
  * the WAV duration is within the small canary cap.

It joins ONE unique RTC channel, optionally starts ONE STT task, publishes at most
a few seconds of audio, prints the transcript, and cleans up. Secrets and tokens
are NEVER printed.

Usage (from the ``worker/`` directory inside WSL2, with LD_LIBRARY_PATH set — see
scripts/run_worker_agora.sh):

    python -m persephone_worker.agora_canary --wav sample.wav --live --max-seconds 3
    python -m persephone_worker.agora_canary --wav sample.wav --live --rtc-only   # no STT

Do NOT run this in CI. It is excluded from the automated test suite.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from persephone_worker.audio import iter_pcm_frames, pcm_duration_seconds, read_wav_pcm16_mono
from persephone_worker.config import get_worker_settings
from persephone_worker.providers.agora import AgoraConfig, AgoraProvider, AgoraRestClient

CONFIRM_PHRASE = "SPEND AGORA CREDIT"
HARD_CAP_SECONDS = 5


def _config_from_settings(settings, max_seconds: int) -> AgoraConfig:
    return AgoraConfig(
        app_id=settings.agora_app_id,
        app_certificate=settings.agora_app_certificate,
        customer_id=settings.agora_customer_id,
        customer_secret=settings.agora_customer_secret,
        live_enabled=settings.agora_live_enabled,
        worker_uid=settings.agora_worker_uid,
        sub_bot_uid=settings.agora_sub_bot_uid,
        pub_bot_uid=settings.agora_pub_bot_uid,
        token_ttl_seconds=settings.agora_token_ttl_seconds,
        max_duration_seconds=max_seconds,
        idle_seconds=settings.agora_idle_seconds,
        daily_max_jobs=0,  # the canary bypasses the daily counter; the phrase gates it
    )


def _print_safe_status(settings, max_seconds: int, rtc_only: bool) -> None:
    from persephone_worker.providers.agora_bridge import agora_sdk_available, sdk_version

    print("── Agora canary — SAFE status (no secret values) ──")
    print(f"  app_id present:      {bool(settings.agora_app_id)}")
    print(f"  app_certificate:     {bool(settings.agora_app_certificate)}")
    print(f"  customer_id present: {bool(settings.agora_customer_id)}")
    print(f"  customer_secret:     {bool(settings.agora_customer_secret)}")
    print(f"  live_enabled:        {settings.agora_live_enabled}")
    print(f"  sdk_available:       {agora_sdk_available()}")
    print(f"  sdk_version:         {sdk_version()}")
    print(f"  language:            {settings.transcription_language}")
    print(
        f"  worker/sub/pub uid:  {settings.agora_worker_uid}/"
        f"{settings.agora_sub_bot_uid}/{settings.agora_pub_bot_uid}"
    )
    print(f"  token_ttl_seconds:   {settings.agora_token_ttl_seconds}")
    print(f"  publish max_seconds: {max_seconds}")
    print(
        f"  mode:                {'RTC-only (no STT, no STT credit)' if rtc_only else 'full STT'}"
    )
    print("───────────────────────────────────────────────────")


def _refuse(reason: str) -> int:
    print(f"REFUSING to run: {reason}", file=sys.stderr)
    return 2


async def _run_rtc_only(config: AgoraConfig, pcm: bytes, rate: int, max_seconds: int) -> None:
    """Publish PCM into a channel WITHOUT starting an STT task."""
    from persephone_worker.providers.agora_bridge import AgoraPcmMediaBridge
    from persephone_worker.providers.agora_token import build_rtc_publisher_token

    channel = f"persephone-canary-{int(time.time()) % 100000}"
    token = build_rtc_publisher_token(
        config.app_id, config.app_certificate, channel, config.worker_uid, config.token_ttl_seconds
    )
    bridge = AgoraPcmMediaBridge(config.app_id, connect_timeout=config.connect_timeout)
    t0 = time.monotonic()
    print(f"joining channel '{channel}' as uid {config.worker_uid} …")
    try:
        await bridge.open(channel, config.worker_uid, token, timeout=config.connect_timeout)
        print("connected. publishing PCM (no STT) …")
        await bridge.publish_and_collect(
            iter_pcm_frames(pcm, rate), max_seconds=float(max_seconds), timeout=1.0, collect=False
        )
        print("published PCM with no STT task started.")
    finally:
        await bridge.close()
    print(f"RTC-only canary done in {time.monotonic() - t0:.1f}s")


async def _run_full(config: AgoraConfig, wav_path: str) -> None:
    """Full path: join → start STT → publish → collect → stop → cleanup."""
    from persephone_worker.providers.agora_bridge import AgoraPcmMediaBridge

    provider = AgoraProvider(
        config,
        language=get_worker_settings().transcription_language,
        bridge_factory=lambda _ch: AgoraPcmMediaBridge(
            config.app_id, connect_timeout=config.connect_timeout
        ),
        rest_client=AgoraRestClient(config),
        usage_counter=None,
    )
    t0 = time.monotonic()
    print("starting full Agora STT canary (this WILL spend a small amount of credit) …")
    result = await provider.transcribe(Path(wav_path), "canary")
    print("\n── TRANSCRIPT ──")
    print(result.transcript)
    print("────────────────")
    print(
        f"provider={result.provider} processing_ms={result.processing_ms} "
        f"channel={result.raw_metadata.get('channel')} agent_id={result.raw_metadata.get('agent_id')}"
    )
    print(f"full canary done in {time.monotonic() - t0:.1f}s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agora live canary (spends credit).")
    parser.add_argument("--wav", required=True, help="Path to a short PCM16 mono 16 kHz WAV.")
    parser.add_argument("--live", action="store_true", help="Required. Acknowledge live mode.")
    parser.add_argument(
        "--max-seconds", type=int, default=None, help="Cap seconds of audio published."
    )
    parser.add_argument("--rtc-only", action="store_true", help="Publish PCM without starting STT.")
    parser.add_argument(
        "--yes", action="store_true", help="Skip the interactive phrase (still needs --live + env)."
    )
    args = parser.parse_args(argv)

    settings = get_worker_settings()
    cap = settings.agora_canary_max_seconds or 3
    max_seconds = min(args.max_seconds or cap, cap, HARD_CAP_SECONDS)

    if not args.live:
        return _refuse("pass --live to acknowledge this spends Agora credit.")
    if not settings.agora_live_enabled:
        return _refuse("AGORA_LIVE_ENABLED is not true.")
    config = _config_from_settings(settings, max_seconds)
    if not config.configured:
        return _refuse("Agora credentials are incomplete.")

    try:
        pcm, rate = read_wav_pcm16_mono(Path(args.wav))
    except Exception as exc:  # noqa: BLE001
        return _refuse(f"could not read WAV: {exc}")
    if rate != 16000:
        return _refuse(f"WAV must be 16 kHz mono; got {rate} Hz.")
    duration = pcm_duration_seconds(pcm, rate)
    if duration > max_seconds + 0.25:
        return _refuse(f"WAV duration {duration:.1f}s exceeds the canary cap {max_seconds}s.")

    _print_safe_status(settings, max_seconds, args.rtc_only)

    if not args.yes:
        try:
            typed = input(f'\nType exactly "{CONFIRM_PHRASE}" to proceed: ')
        except EOFError:
            return _refuse("no confirmation provided.")
        if typed.strip() != CONFIRM_PHRASE:
            return _refuse("confirmation phrase did not match.")

    try:
        if args.rtc_only:
            asyncio.run(_run_rtc_only(config, pcm, rate, max_seconds))
        else:
            asyncio.run(_run_full(config, args.wav))
    except Exception as exc:  # noqa: BLE001
        print(f"canary failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        # The native SDK aborts at process exit if the AgoraService is still
        # initialized. Release the process singleton before returning.
        from persephone_worker.providers.agora_bridge import AgoraServiceManager

        AgoraServiceManager.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

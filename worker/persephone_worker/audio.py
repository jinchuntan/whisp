"""Audio helpers for the worker: download private WAVs and convert to the raw
PCM framing Agora's media bridge expects.

Uses only the Python stdlib (``wave``) so no ffmpeg is required for standard
PCM WAV input.
"""

from __future__ import annotations

import logging
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger("persephone.audio")

# Agora server SDK pushes 16-bit mono PCM in 10 ms frames.
AGORA_FRAME_MS = 10


class AudioError(ValueError):
    pass


def download_audio(client: Any, bucket: str, object_path: str, dest: Path) -> Path:
    """Download a private object from Supabase Storage to a local file."""
    data = client.storage.from_(bucket).download(object_path)
    dest.write_bytes(data)
    log.info("downloaded audio %s -> %s (%d bytes)", object_path, dest, len(data))
    return dest


def read_wav_pcm16_mono(path: Path) -> tuple[bytes, int]:
    """Return (raw little-endian PCM16 mono bytes, sample_rate).

    Accepts standard PCM16 WAV. Mono is required; anything else raises so we
    never silently mis-handle audio.
    """
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if width != 2:
        raise AudioError(f"Expected 16-bit PCM, got {width * 8}-bit")
    if channels != 1:
        raise AudioError(f"Expected mono audio, got {channels} channels")
    return frames, rate


def pcm_duration_seconds(pcm: bytes, sample_rate: int, *, bytes_per_sample: int = 2) -> float:
    """Duration of raw mono PCM in seconds (used for credit/duration guards)."""
    if sample_rate <= 0 or bytes_per_sample <= 0:
        return 0.0
    return len(pcm) / (sample_rate * bytes_per_sample)


def iter_pcm_frames(
    pcm: bytes, sample_rate: int, frame_ms: int = AGORA_FRAME_MS
) -> Iterator[bytes]:
    """Yield fixed-size PCM frames (zero-padded last frame) for real-time push.

    e.g. 16 kHz mono => 320 bytes / 10 ms frame.
    """
    bytes_per_frame = int(sample_rate / 1000 * frame_ms) * 2  # *2 = 16-bit
    if bytes_per_frame <= 0:
        raise AudioError("Invalid sample rate for framing")
    for i in range(0, len(pcm), bytes_per_frame):
        chunk = pcm[i : i + bytes_per_frame]
        if len(chunk) < bytes_per_frame:
            chunk = chunk + b"\x00" * (bytes_per_frame - len(chunk))
        yield chunk

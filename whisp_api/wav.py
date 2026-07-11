"""Minimal, dependency-free WAV validation for upload requests.

The badge always sends standard 16 kHz mono PCM16 with a 44-byte header, but we
validate defensively: bad or truncated uploads are rejected with a clear message
before anything is stored or queued. This does NOT decode audio — it only walks
the RIFF chunk headers.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

WAV_FORMAT_PCM = 1


@dataclass(frozen=True)
class WavInfo:
    audio_format: int
    channels: int
    sample_rate: int
    bits_per_sample: int
    data_bytes: int


class WavValidationError(ValueError):
    """Raised when the uploaded bytes are not an acceptable PCM WAV."""


def parse_wav(data: bytes, *, require_mono: bool = True, require_pcm16: bool = True) -> WavInfo:
    """Parse and validate a PCM WAV. Raises WavValidationError on any problem."""
    if len(data) < 44:
        raise WavValidationError("File too small to be a WAV (need at least a 44-byte header)")
    if data[0:4] != b"RIFF":
        raise WavValidationError("Missing RIFF marker")
    if data[8:12] != b"WAVE":
        raise WavValidationError("Missing WAVE marker")

    fmt: WavInfo | None = None
    data_len = 0
    pos = 12
    n = len(data)
    while pos + 8 <= n:
        chunk_id = data[pos : pos + 4]
        (chunk_size,) = struct.unpack_from("<I", data, pos + 4)
        body = pos + 8
        if chunk_id == b"fmt " and body + 16 <= n:
            audio_format, channels, sample_rate, _byte_rate, _block_align, bits = (
                struct.unpack_from("<HHIIHH", data, body)
            )
            fmt = WavInfo(audio_format, channels, sample_rate, bits, 0)
        elif chunk_id == b"data":
            data_len = chunk_size
        pos = body + chunk_size + (chunk_size & 1)  # chunks are word-aligned

    if fmt is None:
        raise WavValidationError("Missing 'fmt ' chunk")
    if require_pcm16 and fmt.audio_format != WAV_FORMAT_PCM:
        raise WavValidationError(f"Unsupported WAV format {fmt.audio_format} (expected PCM)")
    if require_mono and fmt.channels != 1:
        raise WavValidationError(f"Expected mono audio, got {fmt.channels} channels")
    if require_pcm16 and fmt.bits_per_sample != 16:
        raise WavValidationError(f"Expected 16-bit samples, got {fmt.bits_per_sample}-bit")
    if fmt.sample_rate <= 0:
        raise WavValidationError("Invalid sample rate")

    return WavInfo(
        audio_format=fmt.audio_format,
        channels=fmt.channels,
        sample_rate=fmt.sample_rate,
        bits_per_sample=fmt.bits_per_sample,
        data_bytes=data_len,
    )

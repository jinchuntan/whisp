"""Audio helpers: WAV reading and PCM framing."""

from __future__ import annotations

import pytest

from whisp_worker.audio import AudioError, iter_pcm_frames, read_wav_pcm16_mono


def test_read_wav_mono_16k(wav_file):
    pcm, rate = read_wav_pcm16_mono(wav_file)
    assert rate == 16000
    assert isinstance(pcm, bytes)
    assert len(pcm) == 16000 * 2 * 0.2  # 0.2s of 16-bit mono


def test_read_wav_rejects_stereo(tmp_path):
    import wave

    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00\x00\x00" * 100)
    with pytest.raises(AudioError):
        read_wav_pcm16_mono(path)


def test_iter_pcm_frames_10ms_16k():
    # 16 kHz mono, 10 ms -> 320 bytes/frame
    pcm = b"\x01\x00" * (16000 // 100)  # exactly one 10 ms frame of samples
    frames = list(iter_pcm_frames(pcm, 16000, frame_ms=10))
    assert len(frames) == 1
    assert len(frames[0]) == 320


def test_iter_pcm_frames_pads_last_frame():
    pcm = b"\x01\x00" * 10  # 20 bytes, far less than one frame
    frames = list(iter_pcm_frames(pcm, 16000, frame_ms=10))
    assert len(frames) == 1
    assert len(frames[0]) == 320  # zero-padded
    assert frames[0].endswith(b"\x00")

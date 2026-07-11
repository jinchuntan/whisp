"""Agora Real-Time STT caption collection.

Agora delivers transcription results into the RTC channel as **data-stream
messages** (received via ``IRTCLocalUserObserver.on_stream_message``). The payload
is either protobuf (Agora's ``audio2text`` ``Text``/``Word`` schema) or, in some
product variants, JSON.

Design notes / honesty:
  * The **wire decoding** here (protobuf varint/length-delimited scanning and JSON
    parsing) is standard and fully unit-tested against synthetic payloads.
  * The **field numbers** of Agora's live caption message (Word.text = 1,
    Word.is_final = 4, Text.seqnum = 3, repeated Word = 10) come from Agora's
    documented schema and are the ONE piece that must be confirmed against a live
    stream (Level B). They are isolated as module constants so that, if Agora's
    actual schema differs, only these need adjusting — the collector, merge/dedup,
    ordering and final-detection logic are schema-agnostic.
  * Nothing here does any I/O or blocks; callbacks hand raw bytes in and this code
    only parses + accumulates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

log = logging.getLogger("persephone.provider.agora.captions")

# --- Agora audio2text field numbers (documented schema; verify live = Level B) -
_WORD_TEXT = 1
_WORD_IS_FINAL = 4
_TEXT_SEQNUM = 3
_TEXT_WORDS = 10
_TEXT_DATA_TYPE = 2  # some variants carry a "transcribe"/"translate" string here


@dataclass
class CaptionFragment:
    """One decoded caption update."""

    text: str
    is_final: bool
    seqnum: int | None = None
    uid: int | None = None


# ---------------------------------------------------------------------------
# Minimal protobuf wire reader (no schema compiler needed; stdlib only)
# ---------------------------------------------------------------------------
def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            break
    raise ValueError("truncated varint")


def _iter_fields(buf: bytes):
    """Yield (field_number, wire_type, value) for a protobuf message.

    value is an int (varint / 32 / 64-bit) or bytes (length-delimited). Malformed
    tails stop iteration rather than raising, so a partial message still yields
    what parsed.
    """
    pos = 0
    n = len(buf)
    while pos < n:
        try:
            tag, pos = _read_varint(buf, pos)
        except ValueError:
            return
        field_no = tag >> 3
        wire = tag & 0x7
        if wire == 0:  # varint
            try:
                val, pos = _read_varint(buf, pos)
            except ValueError:
                return
            yield field_no, wire, val
        elif wire == 2:  # length-delimited
            try:
                length, pos = _read_varint(buf, pos)
            except ValueError:
                return
            if pos + length > n:
                return
            yield field_no, wire, buf[pos : pos + length]
            pos += length
        elif wire == 5:  # 32-bit
            if pos + 4 > n:
                return
            yield field_no, wire, int.from_bytes(buf[pos : pos + 4], "little")
            pos += 4
        elif wire == 1:  # 64-bit
            if pos + 8 > n:
                return
            yield field_no, wire, int.from_bytes(buf[pos : pos + 8], "little")
            pos += 8
        else:  # unknown/deprecated wire types (3,4) — cannot safely continue
            return


@runtime_checkable
class SttCaptionDecoder(Protocol):
    def decode(self, payload: bytes) -> list[CaptionFragment]: ...


class ProtobufTextDecoder:
    """Decode Agora's ``audio2text`` ``Text`` message into caption fragments."""

    def decode(self, payload: bytes) -> list[CaptionFragment]:
        seqnum: int | None = None
        words: list[tuple[str, bool]] = []
        for field_no, wire, value in _iter_fields(payload):
            if field_no == _TEXT_SEQNUM and wire == 0:
                seqnum = int(value)
            elif field_no == _TEXT_WORDS and wire == 2 and isinstance(value, bytes):
                text, is_final = self._decode_word(value)
                if text:
                    words.append((text, is_final))
        if not words:
            return []
        combined = " ".join(t for t, _ in words).strip()
        if not combined:
            return []
        message_final = all(is_final for _, is_final in words)
        return [CaptionFragment(text=combined, is_final=message_final, seqnum=seqnum)]

    @staticmethod
    def _decode_word(buf: bytes) -> tuple[str, bool]:
        text = ""
        is_final = False
        for field_no, wire, value in _iter_fields(buf):
            if field_no == _WORD_TEXT and wire == 2 and isinstance(value, bytes):
                text = value.decode("utf-8", "replace")
            elif field_no == _WORD_IS_FINAL and wire == 0:
                is_final = bool(value)
        return text, is_final


class JsonCaptionDecoder:
    """Decode JSON caption payloads (used by some Agora STT variants)."""

    def decode(self, payload: bytes) -> list[CaptionFragment]:
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        if not isinstance(obj, dict):
            return []
        seqnum = obj.get("seqnum") if isinstance(obj.get("seqnum"), int) else None
        is_final = bool(obj.get("is_final") or obj.get("final"))
        text = obj.get("text")
        if not text and isinstance(obj.get("words"), list):
            parts = [w.get("text", "") for w in obj["words"] if isinstance(w, dict)]
            text = " ".join(p for p in parts if p)
            if obj["words"]:
                is_final = all(bool(w.get("is_final")) for w in obj["words"] if isinstance(w, dict))
        if not isinstance(text, str) or not text.strip():
            return []
        return [CaptionFragment(text=text.strip(), is_final=is_final, seqnum=seqnum)]


class AutoCaptionDecoder:
    """Try JSON first (cheap, unambiguous), then the protobuf ``Text`` schema."""

    def __init__(self) -> None:
        self._json = JsonCaptionDecoder()
        self._proto = ProtobufTextDecoder()

    def decode(self, payload: bytes) -> list[CaptionFragment]:
        if not payload:
            return []
        frags = self._json.decode(payload)
        if frags:
            return frags
        return self._proto.decode(payload)


# ---------------------------------------------------------------------------
# Collector — accumulates fragments into a final transcript
# ---------------------------------------------------------------------------
def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


@dataclass
class CaptionCollector:
    """Merge caption fragments into a transcript, in sequence order.

    Not thread-safe by design: the media bridge marshals every SDK callback onto
    the asyncio loop (``call_soon_threadsafe``) before calling ``feed``/``feed_bytes``,
    so all mutation happens on one thread.
    """

    decoder: SttCaptionDecoder = field(default_factory=AutoCaptionDecoder)
    _finals: dict[int, str] = field(default_factory=dict, init=False)
    _final_order: list[int] = field(default_factory=list, init=False)
    _interim: str = field(default="", init=False)
    _auto_seq: int = field(default=0, init=False)
    _final_count: int = field(default=0, init=False)

    def feed_bytes(self, payload: bytes) -> None:
        """Decode a raw data-stream payload and merge its fragments."""
        for frag in self.decoder.decode(payload):
            self.feed(frag)

    def feed(self, fragment: CaptionFragment) -> None:
        if not fragment.text:
            return
        if fragment.is_final:
            key = fragment.seqnum
            if key is None:
                key = -(self._auto_seq + 1)  # negative synthetic keys keep arrival order
                self._auto_seq += 1
            if key not in self._finals:
                self._final_order.append(key)
            self._finals[key] = fragment.text  # repeated seqnum overwrites (dedup)
            self._final_count += 1
            self._interim = ""
        else:
            # Interim: replace the rolling partial; never appended to finals.
            self._interim = fragment.text

    @property
    def has_final(self) -> bool:
        return bool(self._finals)

    @property
    def final_count(self) -> int:
        return self._final_count

    def transcript(self) -> str:
        if self._finals:
            ordered = sorted(self._final_order, key=lambda k: (k < 0, abs(k)))
            joined = " ".join(self._finals[k] for k in ordered)
            return _normalize_whitespace(joined)
        return _normalize_whitespace(self._interim)

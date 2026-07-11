"""Agora caption decoding + collection — offline, synthetic payloads only."""

from __future__ import annotations

import json

from persephone_worker.providers.agora_captions import (
    AutoCaptionDecoder,
    CaptionCollector,
    CaptionFragment,
    JsonCaptionDecoder,
    ProtobufTextDecoder,
)


# --- tiny protobuf encoder (to build synthetic Agora "Text" payloads) ---------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _len_delim(field: int, data: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(data)) + data


def _varint_field(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _word(text: str, is_final: bool) -> bytes:
    body = _len_delim(1, text.encode("utf-8")) + _varint_field(4, 1 if is_final else 0)
    return body


def _text_message(words: list[tuple[str, bool]], seqnum: int | None = None) -> bytes:
    msg = b""
    if seqnum is not None:
        msg += _varint_field(3, seqnum)
    for text, is_final in words:
        msg += _len_delim(10, _word(text, is_final))
    return msg


# --- protobuf decoder ---------------------------------------------------------
def test_protobuf_decodes_words_and_final():
    payload = _text_message([("hello", True), ("world", True)], seqnum=5)
    frags = ProtobufTextDecoder().decode(payload)
    assert len(frags) == 1
    assert frags[0].text == "hello world"
    assert frags[0].is_final is True
    assert frags[0].seqnum == 5


def test_protobuf_interim_when_word_not_final():
    payload = _text_message([("partial", False)], seqnum=1)
    frag = ProtobufTextDecoder().decode(payload)[0]
    assert frag.is_final is False


def test_protobuf_malformed_returns_empty():
    assert ProtobufTextDecoder().decode(b"\xff\xff\xff not protobuf") == []
    assert ProtobufTextDecoder().decode(b"") == []


# --- json decoder -------------------------------------------------------------
def test_json_decoder_text_and_final():
    payload = json.dumps({"text": "hi there", "is_final": True, "seqnum": 2}).encode()
    frag = JsonCaptionDecoder().decode(payload)[0]
    assert frag.text == "hi there"
    assert frag.is_final is True
    assert frag.seqnum == 2


def test_json_decoder_words_list():
    payload = json.dumps(
        {"words": [{"text": "a", "is_final": True}, {"text": "b", "is_final": True}]}
    ).encode()
    frag = JsonCaptionDecoder().decode(payload)[0]
    assert frag.text == "a b"
    assert frag.is_final is True


def test_auto_decoder_prefers_json_then_protobuf():
    j = json.dumps({"text": "json path", "is_final": True}).encode()
    assert AutoCaptionDecoder().decode(j)[0].text == "json path"
    p = _text_message([("proto path", True)])
    assert AutoCaptionDecoder().decode(p)[0].text == "proto path"


# --- collector ----------------------------------------------------------------
def test_collector_interim_then_final():
    c = CaptionCollector()
    c.feed(CaptionFragment("hel", is_final=False, seqnum=1))
    assert c.has_final is False
    c.feed(CaptionFragment("hello", is_final=True, seqnum=1))
    assert c.has_final is True
    assert c.transcript() == "hello"


def test_collector_dedups_repeated_final_seqnum():
    c = CaptionCollector()
    c.feed(CaptionFragment("hello", is_final=True, seqnum=1))
    c.feed(CaptionFragment("hello", is_final=True, seqnum=1))  # repeat
    assert c.transcript() == "hello"


def test_collector_orders_by_seqnum_out_of_order():
    c = CaptionCollector()
    c.feed(CaptionFragment("world", is_final=True, seqnum=2))
    c.feed(CaptionFragment("hello", is_final=True, seqnum=1))
    assert c.transcript() == "hello world"


def test_collector_no_seqnum_keeps_arrival_order():
    c = CaptionCollector()
    c.feed(CaptionFragment("one", is_final=True))
    c.feed(CaptionFragment("two", is_final=True))
    assert c.transcript() == "one two"


def test_collector_interim_replaced_not_appended():
    c = CaptionCollector()
    c.feed(CaptionFragment("he", is_final=False))
    c.feed(CaptionFragment("hello", is_final=False))
    assert c.transcript() == "hello"  # not "he hello"


def test_collector_empty_final_result():
    c = CaptionCollector()
    c.feed(CaptionFragment("", is_final=True, seqnum=1))
    assert c.has_final is False
    assert c.transcript() == ""


def test_collector_normalizes_whitespace():
    c = CaptionCollector()
    c.feed(CaptionFragment("  hello    world  ", is_final=True, seqnum=1))
    assert c.transcript() == "hello world"


def test_collector_feed_bytes_uses_decoder():
    c = CaptionCollector()
    c.feed_bytes(json.dumps({"text": "from bytes", "is_final": True}).encode())
    assert c.transcript() == "from bytes"


def test_collector_ignores_malformed_bytes():
    c = CaptionCollector()
    c.feed_bytes(b"\x00\x01 garbage")
    assert c.transcript() == ""

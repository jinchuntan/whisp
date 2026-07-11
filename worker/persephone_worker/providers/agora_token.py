"""Agora RTC AccessToken2 ("007") builder — vendored official algorithm.

This is a faithful, dependency-free port of Agora's official open-source token
builder (``AccessToken2`` + ``RtcTokenBuilder2``):

    https://github.com/AgoraIO/Tools/tree/master/DynamicKey/AgoraDynamicKey/python3
    License: MIT (© Agora.io)

We vendor it (rather than ``pip install agora-token-builder``) so token
generation is always available, uses only the Python stdlib, and is fully
testable offline. No cryptography is invented here — the packing/signing scheme
is Agora's published format. Nothing in this module makes a network call.

Security: tokens are short-lived secrets. Callers MUST NOT log a complete token.
``inspect_token`` returns only non-sensitive framing fields (never the signature)
for safe diagnostics.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import zlib
from collections import OrderedDict

VERSION = "007"
VERSION_LENGTH = 3


# --- primitive packers (little-endian, matching the official implementation) --
def _pack_uint16(x: int) -> bytes:
    return struct.pack("<H", int(x))


def _pack_uint32(x: int) -> bytes:
    return struct.pack("<I", int(x))


def _pack_string(value: bytes) -> bytes:
    return _pack_uint16(len(value)) + value


def _pack_map_uint32(m: dict[int, int]) -> bytes:
    ordered = OrderedDict(sorted(m.items(), key=lambda kv: int(kv[0])))
    out = _pack_uint16(len(ordered))
    for k, v in ordered.items():
        out += _pack_uint16(k) + _pack_uint32(v)
    return out


class _ReadableBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def unpack_uint16(self) -> int:
        (val,) = struct.unpack_from("<H", self._data, self._pos)
        self._pos += 2
        return val

    def unpack_uint32(self) -> int:
        (val,) = struct.unpack_from("<I", self._data, self._pos)
        self._pos += 4
        return val

    def unpack_string(self) -> bytes:
        length = self.unpack_uint16()
        val = self._data[self._pos : self._pos + length]
        self._pos += length
        return val


# --- services -----------------------------------------------------------------
class _Service:
    def __init__(self, service_type: int) -> None:
        self._type = service_type
        self._privileges: dict[int, int] = {}

    def add_privilege(self, privilege: int, expire: int) -> None:
        self._privileges[privilege] = expire

    def service_type(self) -> int:
        return self._type

    def pack(self) -> bytes:
        return _pack_uint16(self._type) + _pack_map_uint32(self._privileges)


class ServiceRtc(_Service):
    kServiceType = 1
    kPrivilegeJoinChannel = 1
    kPrivilegePublishAudioStream = 2
    kPrivilegePublishVideoStream = 3
    kPrivilegePublishDataStream = 4

    def __init__(self, channel_name: str = "", uid: int | str = 0) -> None:
        super().__init__(ServiceRtc.kServiceType)
        self._channel_name = channel_name.encode("utf-8")
        self._uid = b"" if uid == 0 else str(uid).encode("utf-8")

    def pack(self) -> bytes:
        return super().pack() + _pack_string(self._channel_name) + _pack_string(self._uid)


# --- token --------------------------------------------------------------------
class AccessToken:
    def __init__(
        self,
        app_id: str = "",
        app_certificate: str = "",
        issue_ts: int = 0,
        expire: int = 900,
        *,
        salt: int | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_certificate = app_certificate
        self.issue_ts = issue_ts or int(time.time())
        self.expire = expire
        # Official uses random.randint(1, 99999999); secrets is a safer source.
        self.salt = salt if salt is not None else secrets.randbelow(99999999) + 1
        self.services: dict[int, _Service] = {}

    def add_service(self, service: _Service) -> None:
        self.services[service.service_type()] = service

    def _signing(self) -> bytes:
        signing = hmac.new(
            _pack_uint32(self.issue_ts), self.app_certificate.encode("utf-8"), hashlib.sha256
        ).digest()
        return hmac.new(_pack_uint32(self.salt), signing, hashlib.sha256).digest()

    def _signing_info(self) -> bytes:
        info = (
            _pack_string(self.app_id.encode("utf-8"))
            + _pack_uint32(self.issue_ts)
            + _pack_uint32(self.expire)
            + _pack_uint32(self.salt)
            + _pack_uint16(len(self.services))
        )
        for service_type in sorted(self.services):
            info += self.services[service_type].pack()
        return info

    def build(self) -> str:
        signing_info = self._signing_info()
        signature = hmac.new(self._signing(), signing_info, hashlib.sha256).digest()
        content = _pack_string(signature) + signing_info
        return VERSION + base64.b64encode(zlib.compress(content)).decode("utf-8")


class Role:
    PUBLISHER = 1
    SUBSCRIBER = 2


class RtcTokenBuilder:
    @staticmethod
    def build_token_with_uid(
        app_id: str,
        app_certificate: str,
        channel_name: str,
        uid: int | str,
        role: int,
        token_expire: int,
        privilege_expire: int = 0,
        *,
        issue_ts: int = 0,
        salt: int | None = None,
    ) -> str:
        token = AccessToken(
            app_id, app_certificate, issue_ts=issue_ts, expire=token_expire, salt=salt
        )
        service = ServiceRtc(channel_name, uid)
        service.add_privilege(ServiceRtc.kPrivilegeJoinChannel, privilege_expire)
        if role == Role.PUBLISHER:
            service.add_privilege(ServiceRtc.kPrivilegePublishAudioStream, privilege_expire)
            service.add_privilege(ServiceRtc.kPrivilegePublishVideoStream, privilege_expire)
            service.add_privilege(ServiceRtc.kPrivilegePublishDataStream, privilege_expire)
        token.add_service(service)
        return token.build()


def build_rtc_publisher_token(
    app_id: str,
    app_certificate: str,
    channel_name: str,
    uid: int,
    ttl_seconds: int,
    *,
    issue_ts: int = 0,
    salt: int | None = None,
) -> str:
    """Short-lived RTC token granting join + publish (audio/video/data) on a channel."""
    return RtcTokenBuilder.build_token_with_uid(
        app_id,
        app_certificate,
        channel_name,
        uid,
        Role.PUBLISHER,
        token_expire=ttl_seconds,
        privilege_expire=ttl_seconds,
        issue_ts=issue_ts,
        salt=salt,
    )


def inspect_token(token: str) -> dict[str, object]:
    """Decode a "007" token into NON-SENSITIVE framing fields for diagnostics.

    Returns app_id, issue_ts, expire, salt, and the RTC channel/uid. The
    signature is deliberately omitted. Never use this to log a full token.
    """
    if not token.startswith(VERSION):
        raise ValueError("not a 007 access token")
    raw = zlib.decompress(base64.b64decode(token[VERSION_LENGTH:]))
    buf = _ReadableBuffer(raw)
    buf.unpack_string()  # signature — intentionally discarded
    app_id = buf.unpack_string().decode("utf-8", "replace")
    issue_ts = buf.unpack_uint32()
    expire = buf.unpack_uint32()
    salt = buf.unpack_uint32()
    service_count = buf.unpack_uint16()
    out: dict[str, object] = {
        "version": VERSION,
        "app_id": app_id,
        "issue_ts": issue_ts,
        "expire": expire,
        "salt": salt,
        "service_count": service_count,
    }
    if service_count:
        service_type = buf.unpack_uint16()
        priv_count = buf.unpack_uint16()
        privileges: dict[int, int] = {}
        for _ in range(priv_count):
            # Read key before value explicitly: `d[k()] = v()` would evaluate the
            # value first in CPython and misalign the buffer.
            key = buf.unpack_uint16()
            privileges[key] = buf.unpack_uint32()
        out["service_type"] = service_type
        out["privileges"] = privileges
        if service_type == ServiceRtc.kServiceType:
            out["channel"] = buf.unpack_string().decode("utf-8", "replace")
            out["uid"] = buf.unpack_string().decode("utf-8", "replace")
    return out

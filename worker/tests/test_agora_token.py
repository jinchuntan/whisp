"""Vendored Agora AccessToken2 builder — offline, deterministic, no secrets leaked."""

from __future__ import annotations

from persephone_worker.providers.agora_token import (
    VERSION,
    Role,
    RtcTokenBuilder,
    ServiceRtc,
    build_rtc_publisher_token,
    inspect_token,
)

APP = "0123456789abcdef0123456789abcdef"
CERT = "fedcba9876543210fedcba9876543210"


def test_token_has_007_prefix():
    token = build_rtc_publisher_token(APP, CERT, "persephone-abc-1a2b3c", 20001, 300)
    assert token.startswith(VERSION)
    assert len(token) > 40


def test_token_is_deterministic_with_fixed_salt_and_ts():
    a = build_rtc_publisher_token(APP, CERT, "chan", 20001, 300, issue_ts=1_700_000_000, salt=42)
    b = build_rtc_publisher_token(APP, CERT, "chan", 20001, 300, issue_ts=1_700_000_000, salt=42)
    assert a == b


def test_token_round_trips_channel_uid_and_ttl():
    token = build_rtc_publisher_token(APP, CERT, "persephone-q-xyz", 20001, 300, issue_ts=1, salt=7)
    info = inspect_token(token)
    assert info["app_id"] == APP
    assert info["channel"] == "persephone-q-xyz"
    assert info["uid"] == "20001"
    assert info["expire"] == 300


def test_publisher_token_grants_join_and_publish_privileges():
    token = build_rtc_publisher_token(APP, CERT, "c", 1, 300, issue_ts=1, salt=7)
    privs = inspect_token(token)["privileges"]
    # join(1), publish audio(2), video(3), data(4)
    assert set(privs.keys()) == {
        ServiceRtc.kPrivilegeJoinChannel,
        ServiceRtc.kPrivilegePublishAudioStream,
        ServiceRtc.kPrivilegePublishVideoStream,
        ServiceRtc.kPrivilegePublishDataStream,
    }
    assert all(v == 300 for v in privs.values())


def test_subscriber_token_only_grants_join():
    token = RtcTokenBuilder.build_token_with_uid(
        APP, CERT, "c", 10001, Role.SUBSCRIBER, 300, 300, issue_ts=1, salt=7
    )
    privs = inspect_token(token)["privileges"]
    assert set(privs.keys()) == {ServiceRtc.kPrivilegeJoinChannel}


def test_different_channel_yields_different_token():
    a = build_rtc_publisher_token(APP, CERT, "chan-a", 20001, 300, issue_ts=1, salt=7)
    b = build_rtc_publisher_token(APP, CERT, "chan-b", 20001, 300, issue_ts=1, salt=7)
    assert a != b


def test_certificate_secret_is_not_embedded_in_plaintext():
    # The certificate is HMAC keying material; it must never appear in the token.
    token = build_rtc_publisher_token(APP, CERT, "c", 20001, 300)
    assert CERT not in token


def test_inspect_never_returns_signature():
    token = build_rtc_publisher_token(APP, CERT, "c", 20001, 300)
    assert "signature" not in inspect_token(token)

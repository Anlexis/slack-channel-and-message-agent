# CMN-C2-233 - Unit tests: SlackClient service (Slack Web API shape)
# Adapted from the established tool-calling golden pattern (kaonavi_client -> slack_client).
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.slack_client import SlackApiError, SlackClient


def test_post_message_success_with_injected_transport():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"ok": True, "channel": "CABCDEFGH", "ts": "1234.000001"}

    client = SlackClient("https://slack.example.test/api/", post=post)
    resp = client.post_message("release-announcements", "deploy complete", "tok123")
    assert resp["channel"] == "CABCDEFGH"
    assert resp["ts"] == "1234.000001"
    assert captured["url"] == "https://slack.example.test/api/chat.postMessage"
    # Slack Web API auth: the per-call token travels as a Bearer header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert captured["body"] == {"channel": "release-announcements", "text": "deploy complete"}


def test_create_channel_strips_hash_and_sends_is_private():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"ok": True, "channel": {"id": "CNEWCHAN1", "name": "releases"}}

    client = SlackClient("https://slack.example.test/api", post=post)
    resp = client.create_channel("#releases", "tok")
    assert resp["channel"]["id"] == "CNEWCHAN1"
    assert captured["url"] == "https://slack.example.test/api/conversations.create"
    assert captured["body"] == {"name": "releases", "is_private": False}


def test_invite_members_sends_comma_joined_users():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"ok": True, "channel": {"id": "CABCDEFGH", "name": "onboarding"}}

    client = SlackClient("https://slack.example.test/api", post=post)
    resp = client.invite_members("CABCDEFGH", ["alice", "bob"], "tok")
    assert resp["channel"]["name"] == "onboarding"
    assert captured["url"] == "https://slack.example.test/api/conversations.invite"
    # Documented request shape: users are sent comma-joined.
    assert captured["body"] == {"channel": "CABCDEFGH", "users": "alice,bob"}


def test_non_2xx_raises_slack_api_error():
    def post(url, headers, body):
        return 429, {"ok": False, "error": "ratelimited"}

    client = SlackClient("https://slack.example.test/api", post=post)
    with pytest.raises(SlackApiError) as exc:
        client.post_message("general", "hello there", "tok")
    assert exc.value.status_code == 429
    assert "ratelimited" in str(exc.value)


def test_ok_false_on_http_200_raises_slack_api_error():
    # Slack-specific: the Web API signals failure with ok:false even on 200.
    def post(url, headers, body):
        return 200, {"ok": False, "error": "channel_not_found"}

    client = SlackClient("https://slack.example.test/api", post=post)
    with pytest.raises(SlackApiError) as exc:
        client.post_message("nope", "hello there", "tok")
    assert "channel_not_found" in str(exc.value)


def test_default_stub_transport_post_message_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = SlackClient()
    assert client.uses_stub_transport is True
    resp = client.post_message("release-announcements", "deploy complete", "tok")
    assert resp.get("_stub") is True
    assert resp["ok"] is True
    assert resp["channel"].startswith("C")
    assert "." in resp["ts"]
    assert resp["message"]["text"] == "deploy complete"


def test_default_stub_transport_is_deterministic():
    client = SlackClient()
    first = client.post_message("release-announcements", "deploy complete", "tok")
    second = client.post_message("release-announcements", "deploy complete", "tok")
    assert first["channel"] == second["channel"]
    assert first["ts"] == second["ts"]


def test_default_stub_transport_echoes_id_shaped_channel():
    client = SlackClient()
    resp = client.post_message("CABCDEFGH", "deploy complete", "tok")
    assert resp["channel"] == "CABCDEFGH"


def test_default_stub_transport_create_channel_envelope():
    client = SlackClient()
    resp = client.create_channel("releases", "tok")
    assert resp.get("_stub") is True
    assert resp["channel"]["name"] == "releases"
    assert resp["channel"]["id"].startswith("C")


def test_injected_transport_disables_stub_flag():
    client = SlackClient(post=lambda url, headers, body: (200, {"ok": True}))
    assert client.uses_stub_transport is False

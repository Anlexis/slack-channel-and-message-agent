"""AgentCore Platform v1.0 - Slack Web API client.

Service layer: a thin wrapper around the Slack Web API methods this template
uses (chat.postMessage / conversations.create / conversations.invite).
Contains NO business logic, NO routing, and NO credentials - the bot token is
passed in per call by the node (which reads it via ctx.secrets). This module
imports no framework/SDK internals - pure stdlib (import-isolation, PB-4).

v1 LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented Slack Web API response shapes (``{"ok": true, "channel": ...,
    "ts": ...}`` for chat.postMessage; the ``{"ok": true, "channel": {...}}``
    envelope for conversations.create / conversations.invite, with synthetic
    ids derived from the request) so the pipeline is runnable and testable
    without a live Slack workspace or the ``requests`` package - it does NOT
    perform a live Slack call. This mirrors the established network-free stub
    transport shape used by other tool-calling templates, adapted to Slack.
    The rule it follows: never fake a live call; document the limitation.

    To perform real Slack calls, inject a live ``post`` transport
    (requests-based) at construction time; the method contracts and payload
    shapes are already Slack Web API exact, so no business-logic change is
    needed to go live. A live transport also requires a real bot token (see
    CallSlackApiNode - the stub runs without one because no request ever
    leaves the process). Slack-specific: the Web API signals failure with an
    ``{"ok": false, "error": "..."}`` body even on HTTP 200 - that shape is
    raised as SlackApiError here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://slack.com/api"


class SlackApiError(Exception):
    """Raised when the Slack Web API returns non-2xx or an ok:false body."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Slack API error {status_code}: {message}")


class SlackClient:
    """Slack Web API client (chat.postMessage / conversations.create / conversations.invite).

    Args:
        base_url: Slack Web API base URL (default https://slack.com/api).
        post: optional injected transport (tests or a live client). When none
            is injected, a deterministic NETWORK-FREE v1 stub is used (see the
            module docstring - it returns the documented shape without a live
            Slack call). Every method this template uses is an HTTP POST, so a
            single transport slot covers all three.
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._post is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, Any]":
        """Build the Slack Web API auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {api_token}",
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free v1 stub - returns the documented Slack shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the v1
        limitation and how to inject a live transport.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        # A channel reference that already looks like a Slack channel ID is
        # echoed back; a #name gets a synthetic ID derived from the request.
        requested = str(json_body.get("channel", "") or json_body.get("name", ""))
        if requested and requested[:1] in ("C", "G") and requested[1:9].isalnum() and requested.isupper():
            channel_id = requested
        else:
            channel_id = "C" + digest[:8].upper()
        if url.endswith("/chat.postMessage"):
            ts = f"{int(digest[:8], 16) % 2000000000}.{int(digest[8:13], 16) % 1000000:06d}"
            # Documented chat.postMessage shape: {"ok": true, "channel": "C...",
            # "ts": "...", "message": {...}}
            return 200, {
                "ok": True,
                "channel": channel_id,
                "ts": ts,
                "message": {"text": str(json_body.get("text", ""))},
                "_stub": True,  # marks the network-free v1 stub response
            }
        # conversations.create / conversations.invite - documented envelope:
        # {"ok": true, "channel": {"id": "C...", "name": "..."}}
        name = str(json_body.get("name", "") or "").lstrip("#")
        return 200, {
            "ok": True,
            "channel": {"id": channel_id, "name": name},
            "_stub": True,  # marks the network-free v1 stub response
        }

    def _resolve(self) -> Transport:
        return self._post or self._stub_transport

    def _call(self, method: str, body: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST one Slack Web API method; raise SlackApiError on failure."""
        url = f"{self._base_url}/{method}"
        transport = self._resolve()
        status, resp = transport(url, self._headers(api_token), body)
        if not (200 <= status < 300):
            raise SlackApiError(status, _err_message(resp))
        if isinstance(resp, dict) and resp.get("ok") is False:
            # Slack signals failure with ok:false on HTTP 200.
            raise SlackApiError(status, _err_message(resp))
        return resp

    # -- public API ---------------------------------------------------------

    def post_message(self, channel: str, text: str, api_token: str) -> "dict[str, Any]":
        """chat.postMessage - post a message to a channel.

        ``channel`` is a Slack channel ID or name; ``text`` is the message
        body. Returns the parsed response dict (containing ``channel`` and
        ``ts``). Raises SlackApiError on non-2xx or an ok:false body.
        """
        return self._call("chat.postMessage", {"channel": channel, "text": text}, api_token)

    def create_channel(self, name: str, api_token: str, is_private: bool = False) -> "dict[str, Any]":
        """conversations.create - create a public (default) or private channel.

        Returns the parsed response dict (containing the ``channel`` object).
        Raises SlackApiError on non-2xx or an ok:false body.
        """
        return self._call(
            "conversations.create",
            {"name": name.lstrip("#"), "is_private": bool(is_private)},
            api_token,
        )

    def invite_members(self, channel: str, users: "list[str]", api_token: str) -> "dict[str, Any]":
        """conversations.invite - invite members to a channel.

        ``users`` is the list of member ids/handles (sent comma-joined, the
        documented request shape). Returns the parsed response dict (containing
        the ``channel`` object). Raises SlackApiError on non-2xx or ok:false.
        """
        return self._call(
            "conversations.invite",
            {"channel": channel, "users": ",".join(users)},
            api_token,
        )


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a Slack error body."""
    if isinstance(body, dict):
        err = body.get("error")
        if err:
            return str(err)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)

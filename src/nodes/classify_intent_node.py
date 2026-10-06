"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of post_message / create_channel /
invite_member using a deterministic keyword heuristic, so the template is
testable and runnable without a live LLM (v1 Implementation Note - LLM
synthesis, docs/02_design.md). Every supported operation is a WRITE against
the Slack workspace and no read-only operation exists in this agent, so a
low-confidence / unknown classification is a hard status=error - the agent
never guesses a write (docs/02 Design Decision Record "Unknown intent").
Channel-management writes (create/invite) are matched on strict phrases;
post_message - the mildest write - uses broader messaging verbs.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("post_message", "create_channel", "invite_member")

# Deterministic keyword signals (checked in priority order: channel-management
# phrases first, so "create the #x channel and post a welcome note" style
# requests classify as the channel operation; the strict phrasing keeps a
# request like "post the newly created report" from matching create_channel).
_KEYWORDS = (
    (
        "create_channel",
        (
            "create a channel",
            "create channel",
            "create the channel",
            "create a new channel",
            "make a channel",
            "new channel",
            "set up a channel",
            "set up the channel",
            "open a channel",
            "チャンネルを作成",
            "チャンネル作成",
            "新規チャンネル",
            "チャンネルを作って",
        ),
    ),
    (
        "invite_member",
        (
            "invite",
            "add @",
            "add them to",
            "add her to",
            "add him to",
            "add to the channel",
            "add members",
            "bring into",
            "招待",
            "メンバー追加",
            "メンバーを追加",
        ),
    ),
    (
        "post_message",
        (
            "post",
            "send",
            "announce",
            "notify",
            "message",
            "share",
            "broadcast",
            "reply",
            "tell the",
            "投稿",
            "送信",
            "通知",
            "アナウンス",
            "メッセージ",
            "知らせ",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a Slack channel/message operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        if intent not in _VALID_INTENTS:
            # Write-only agent: no read-only default exists, so an unclassifiable
            # request is refused rather than guessed (never an accidental write).
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "ClassifyIntentNode: could not classify the request into a "
                    "supported Slack operation (post_message / create_channel / "
                    "invite_member) - refusing to guess a write"
                ],
            }

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent},
            state,
        )

        return {"intent": intent, "status": AgentStatus.SUCCESS.value}

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the hard-error guard in execute()
        # (returns a sentinel outside the _VALID_INTENTS set).
        return "unknown"

"""Guard the LiteLLM behaviour FITT's ollama tool loop depends on.

LiteLLM's `ollama_chat` provider transforms our OpenAI-shaped messages into
ollama's native `/api/chat` shape. In litellm < 1.84.0 that transform built a
fresh outgoing message and never copied assistant `tool_calls` onto it, so
ollama received `{"role": "assistant", "content": ""}` followed by an orphan
`{"role": "tool", ...}`. The model had no record of having made the call and
re-issued it every iteration until the loop cap.

These tests fail on litellm 1.83.14 and pass on >= 1.84.0. They exist so a
future dependency change can't silently reintroduce the spiral.

See docs/observed-issues.md, "litellm ollama_chat drops assistant tool_calls".
"""

from __future__ import annotations

import json
from typing import Any

from litellm.llms.ollama.chat.transformation import OllamaChatConfig


def _transform(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data = OllamaChatConfig().transform_request(
        model="gemma4:12b",
        messages=messages,  # type: ignore[arg-type]
        optional_params={},
        litellm_params={},
        headers={},
    )
    return list(data["messages"])


def _tool_exchange() -> list[dict[str, Any]]:
    """A minimal replayed turn: user ask, assistant tool call, tool result."""
    return [
        {"role": "user", "content": "add milk to my todos"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "todo_add",
                        "arguments": json.dumps({"text": "milk"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "added todo #1"},
    ]


def test_assistant_tool_calls_survive_the_ollama_transform() -> None:
    assistant = _transform(_tool_exchange())[1]

    assert assistant["role"] == "assistant"
    tool_calls = assistant.get("tool_calls")
    assert tool_calls, "assistant tool_calls dropped on the ollama_chat path"
    assert tool_calls[0]["function"]["name"] == "todo_add"


def test_tool_call_arguments_are_replayed_as_an_object() -> None:
    """ollama's native API wants an object, not the OpenAI JSON string."""
    assistant = _transform(_tool_exchange())[1]

    arguments = assistant["tool_calls"][0]["function"]["arguments"]
    assert arguments == {"text": "milk"}


def test_tool_result_keeps_its_tool_call_id() -> None:
    tool_result = _transform(_tool_exchange())[2]

    assert tool_result["role"] == "tool"
    assert tool_result.get("tool_call_id") == "call_1"


def test_plain_messages_carry_no_tool_calls_key() -> None:
    messages = _transform([{"role": "user", "content": "hello"}])

    assert "tool_calls" not in messages[0]

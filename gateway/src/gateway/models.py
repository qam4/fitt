"""Pydantic models for the OpenAI chat-completion wire format.

We don't re-declare the full OpenAI schema — instead we keep a
permissive Pydantic model that validates the parts we actively care
about (``model`` and ``messages``) and preserves the rest verbatim so
we can forward them to LiteLLM without information loss.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """One message in an OpenAI chat-completion conversation."""

    model_config = ConfigDict(extra="allow")  # tool_calls, name, etc.

    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: Any  # can be str, None (for tool calls), or a list of parts


class ChatCompletionRequest(BaseModel):
    """Minimal OpenAI-compatible chat-completion request.

    Everything outside the fields declared here is preserved in
    ``extra`` so LiteLLM receives the client's full request.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(..., description="Alias name configured in config.yaml")
    messages: list[ChatMessage] = Field(..., min_length=1)
    stream: bool = False
    # All the well-known OpenAI knobs, declared so IDE clients with
    # strict schemas play nicely, but preserved loosely.
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    n: int | None = None
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    seed: int | None = None

    def to_litellm_body(self) -> dict[str, Any]:
        """Return a dict suitable for forwarding to LiteLLM.

        Keeps the raw ``model`` value so the router can resolve it
        itself; the router strips the ``model`` key before calling
        LiteLLM.
        """
        return self.model_dump(exclude_none=True)

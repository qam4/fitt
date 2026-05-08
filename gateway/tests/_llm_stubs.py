"""Shared builders for stubbed LiteLLM responses in tests.

Extracted from the `_fake_response` / `_tool_call` duplicates
that lived in test_chat_tool_forwarding, test_cron_runner,
test_detach, and test_integration_clients. New tests should
import from here rather than rebuild the shape.

Why stub instead of hit a real model:

1. **Deterministic.** Tests pin code behaviour, not model
   behaviour. "When the model narrates JSON in content,
   cron_runner emits a tool_call_narrated event" is our
   invariant — whether the model *does* narrate is
   ``llm-checker toolcheck``'s job, not pytest's.
2. **Fast.** A stubbed dispatch returns in microseconds; a
   real one takes seconds. A test suite that runs in 60s gets
   run every commit; one that takes 5 minutes rots.
3. **Environment-independent.** CI has no GPU, no Ollama, no
   API keys; your laptop has some; the NAS has different
   bindings. A stub makes the same assertion everywhere.

The builder hierarchy:

* :func:`stub_reply` — natural-language reply, no tool calls.
* :func:`stub_tool_call` — one tool call, optional leading content.
* :func:`stub_tool_calls` — multiple tool calls in one turn
  (for duplicate-call or parallel-call scenarios).
* :func:`stub_narrated_tool_call` — the 2026-05-07 failure
  pattern: JSON-fenced tool call in ``content`` with no real
  ``tool_calls`` structure.

All return a "response object" that has ``.model_dump()`` and
``.usage`` — the two surfaces chat.py / agent_loop.py read.

For multi-round scenarios (model calls tool → sees result →
replies), use :func:`stub_sequence`:

.. code-block:: python

    responses = stub_sequence([
        stub_tool_call("read_file", {"project": "hub", "path": "x"}),
        stub_reply("File contents: foo"),
    ])
    monkeypatch.setattr("gateway.router.litellm.acompletion", responses)

``stub_sequence`` returns a callable suitable for
``monkeypatch.setattr`` on ``litellm.acompletion``. Each call
pops the next response; exhausting the list raises
``StopIteration`` (a clear test failure, not a silent
repeat)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

# --------------------------------------------------------------- primitives


def make_tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """One OpenAI-shape tool_call entry.

    Matches what LiteLLM dumps from a real model's response.
    Arguments are JSON-encoded per the OpenAI spec — the agent
    loop decodes them via :func:`gateway.agent_loop.tool_call_args`.
    """
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def make_response(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    in_tok: int = 5,
    out_tok: int = 3,
) -> Any:
    """Build a response object shaped like ``litellm.ModelResponse``.

    chat.py and agent_loop.py read only ``.model_dump()`` and
    ``.usage`` on the response, so we implement only those
    two surfaces. Anything that accesses other attributes is
    touching the shape wrong and we'd want it to fail loudly
    rather than pass with a mock."""

    class _Response:
        def __init__(self) -> None:
            self.usage = type(
                "Usage",
                (),
                {"prompt_tokens": in_tok, "completion_tokens": out_tok},
            )()

        def model_dump(self, **_: Any) -> dict[str, Any]:
            msg: dict[str, Any] = {"role": "assistant"}
            if content is not None:
                msg["content"] = content
            if tool_calls:
                msg["tool_calls"] = tool_calls
            return {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": msg,
                        "finish_reason": (
                            finish_reason
                            if finish_reason is not None
                            else ("tool_calls" if tool_calls else "stop")
                        ),
                    }
                ],
                "usage": {
                    "prompt_tokens": in_tok,
                    "completion_tokens": out_tok,
                },
            }

    return _Response()


# --------------------------------------------------------------- named patterns


def stub_reply(content: str, **kwargs: Any) -> Any:
    """The model produced a natural-language reply and stopped.

    No tool calls. finish_reason = "stop". The default shape
    chat.py / cron_runner treat as "the model is done."""
    return make_response(content=content, **kwargs)


def stub_tool_call(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    call_id: str = "c1",
    content: str | None = None,
    **kwargs: Any,
) -> Any:
    """The model emitted one real tool_call — what we want
    models to do when they decide to use a tool.

    Optionally preceded by a short content string (models often
    write "I'll check that now" before emitting the call).
    finish_reason auto-resolves to "tool_calls"."""
    return make_response(
        content=content,
        tool_calls=[make_tool_call(call_id, name, args or {})],
        **kwargs,
    )


def stub_tool_calls(
    calls: Iterable[tuple[str, dict[str, Any]]],
    *,
    content: str | None = None,
    **kwargs: Any,
) -> Any:
    """The model emitted multiple tool_calls in one turn.

    Useful for scenarios like:
    - Parallel reads (one turn, several read_file calls).
    - Accidental duplicates (same name+args twice; an
      observed qwen-coder failure mode).

    Each tuple is ``(name, args)``. Call ids are auto-generated
    as ``c1``, ``c2``, ... so tests can match on them."""
    return make_response(
        content=content,
        tool_calls=[
            make_tool_call(f"c{i}", name, args) for i, (name, args) in enumerate(calls, start=1)
        ],
        **kwargs,
    )


def stub_narrated_tool_call(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    preamble: str = "I'll do that now.",
    **kwargs: Any,
) -> Any:
    """The 2026-05-07 failure pattern: model writes JSON-fenced
    tool call in ``content`` with NO real ``tool_calls``
    structure.

    The agent loop treats this as a natural stop (no calls to
    execute, reply complete). The narration ends up as the
    user-facing reply. :func:`gateway.capabilities.detect_narrated_tool_call`
    is the detector that flags this to operators via the
    ``tool_call_narrated`` event.

    Use this stub to pin our behaviour when weak models fail
    the tool-call channel."""
    args_json = json.dumps(args or {}, indent=2)
    content = f'{preamble}\n\n```json\n{{"name": "{name}", "arguments": {args_json}}}\n```'
    return make_response(content=content, **kwargs)


# --------------------------------------------------------------- sequences


def stub_sequence(
    responses: Iterable[Any],
) -> Any:
    """Return an async callable that returns each response in
    order on successive calls.

    Suitable for ``monkeypatch.setattr(
    "gateway.router.litellm.acompletion", stub_sequence([...])
    )``. Matches LiteLLM's signature (``**kwargs``) and ignores
    them — tests that want to assert on dispatch args should
    capture them via a wrapping function rather than threading
    through the stub.

    Exhausting the list raises :class:`StopIteration` on the
    next call. That's intentional: a silent repeat of the last
    response would hide test bugs where the loop iterated more
    times than expected."""
    iterator = iter(list(responses))

    async def _next(**_: Any) -> Any:
        return next(iterator)

    return _next

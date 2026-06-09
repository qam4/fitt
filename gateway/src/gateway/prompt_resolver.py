"""Phase 12 task 5 — :class:`PromptResolver`.

Resolves the system prompt for an orchestration *step* against an
*alias*, along the two axes from requirements Story 2.5::

    prompt = resolve(step, alias)

* **Per-step, model-agnostic defaults** live here in code. They are
  first-draft content; tuning them against real models is later work
  (Story 7), and crucially it is a *data* edit, never a structural
  one.
* **Per-`(alias, step)` overrides** come from config (the optional
  ``prompts:`` block), mirroring the loose-dict-at-Config /
  strict-downstream pattern that ``tools:`` uses with
  :class:`ToolPolicy`. Adding or changing an override is config-only,
  no code change (Story 2.3).

The resolver is the spine of the model-agnostic guarantee (Story 8):
the agent loop asks for ``resolve(step, alias)`` and never branches on
a model name. Per-model variation, when needed, is an override keyed
by alias — data, not a code path.

This module is pure: no model calls, no I/O. It is fully
unit-testable with no backend (Phase 12 "build vs validate": this is
the build-with-fakes half).
"""

from __future__ import annotations

from typing import Any, Literal

PromptStep = Literal["plan", "execute", "compact", "recover"]
"""The orchestration steps, each its own prompt role (Story 2.5):

* ``plan`` — elicit an explicit plan (the planner pass).
* ``execute`` — carry out plan steps. Defaults to empty: execution
  uses the loop's existing system prefix (capability block + identity)
  unless an alias overrides it.
* ``compact`` — summarize an overflowing transcript into a structured
  anchor (consumed by Phase 8 compaction).
* ``recover`` — nudge a stalled turn to continue (ground-truth
  recovery).
"""

PROMPT_STEPS: tuple[PromptStep, ...] = ("plan", "execute", "compact", "recover")
"""Hardcoded for mypy-strict friendliness; a test pins it equal to
``get_args(PromptStep)`` so the two can't drift."""


# --------------------------------------------------------------- defaults

_DEFAULT_PLAN = (
    "When a request needs more than one step, FIRST call the "
    "`todowrite` tool to lay out a short, ordered plan (typically "
    "3-7 concrete, tool-oriented steps), then work the steps and mark "
    "each done as you finish it. If the request is a single action, "
    "just do it — no plan needed. Do not narrate a plan in prose when "
    "the tool is available; emit it via the tool."
)

_DEFAULT_EXECUTE = ""
"""Execution uses the loop's existing system prefix; the resolver
adds no extra framing by default. An alias may override this."""

_DEFAULT_COMPACT = (
    "Summarize the conversation so far into the exact structure below, "
    "preserving the plan and progress. Use terse bullets; preserve "
    "exact file paths, commands, identifiers, and error strings. Do "
    "not mention that summarization happened.\n"
    "## Goal\n## Constraints\n## Done\n## In Progress\n## Blocked\n"
    "## Next Steps\n## Key Decisions\n## Relevant Context"
)

_DEFAULT_RECOVER = (
    "You executed tool calls but produced no usable result. Process "
    "the tool results above and continue with the next step of the "
    "task. If a step failed, retry it differently or report honestly "
    "what blocked you — never fabricate a result."
)

_BUILTIN_DEFAULTS: dict[PromptStep, str] = {
    "plan": _DEFAULT_PLAN,
    "execute": _DEFAULT_EXECUTE,
    "compact": _DEFAULT_COMPACT,
    "recover": _DEFAULT_RECOVER,
}

_DEFAULTS_KEY = "defaults"
"""Reserved top-level key in the ``prompts:`` config block for global
per-step default overrides. An alias literally named ``defaults`` is
therefore not addressable for overrides — documented limitation,
acceptable for v1."""


# --------------------------------------------------------------- resolver


class PromptResolver:
    """Resolve ``(step, alias)`` to a system-prompt string.

    Resolution order, most specific first:

    1. per-alias override (``overrides[alias][step]``),
    2. global default override (``defaults[step]``, from the
       ``prompts.defaults`` config or a constructor arg),
    3. the built-in code default.

    Construct directly with explicit maps (tests), or via
    :meth:`from_config` (production).
    """

    __slots__ = ("_defaults", "_overrides")

    def __init__(
        self,
        *,
        defaults: dict[PromptStep, str] | None = None,
        overrides: dict[str, dict[PromptStep, str]] | None = None,
    ) -> None:
        self._defaults: dict[PromptStep, str] = {**_BUILTIN_DEFAULTS, **(defaults or {})}
        self._overrides: dict[str, dict[PromptStep, str]] = overrides or {}

    def resolve(self, step: PromptStep, alias: str) -> str:
        """Return the system prompt for ``step`` under ``alias``.

        Raises :class:`ValueError` on an unknown step (fail-loud: a
        typo'd step is a bug, not a silent empty prompt). An unknown
        alias is *not* an error — it simply has no overrides and gets
        the default.
        """
        if step not in self._defaults:
            raise ValueError(f"unknown prompt step {step!r}; expected one of {PROMPT_STEPS}")
        alias_over = self._overrides.get(alias)
        if alias_over is not None and step in alias_over:
            return alias_over[step]
        return self._defaults[step]

    @classmethod
    def from_config(cls, prompts_cfg: dict[str, Any] | None) -> PromptResolver:
        """Build from the optional ``prompts:`` config block.

        Expected shape (all keys optional)::

            prompts:
              defaults:            # global per-step default overrides
                plan: "..."
              <alias>:             # per-alias per-step overrides
                plan: "..."
                execute: "..."

        Fail-loud (Principle 11): an unknown step name or a non-string
        prompt value raises :class:`ValueError` at load time rather
        than degrading silently at runtime.
        """
        if not prompts_cfg:
            return cls()

        defaults: dict[PromptStep, str] = {}
        overrides: dict[str, dict[PromptStep, str]] = {}

        for key, value in prompts_cfg.items():
            if not isinstance(value, dict):
                raise ValueError(
                    f"prompts.{key} must be a mapping of step -> prompt string (got {type(value).__name__})"
                )
            parsed = cls._parse_step_map(str(key), value)
            if key == _DEFAULTS_KEY:
                defaults = parsed
            else:
                overrides[str(key)] = parsed

        return cls(defaults=defaults, overrides=overrides)

    @staticmethod
    def _parse_step_map(scope: str, raw: dict[Any, Any]) -> dict[PromptStep, str]:
        out: dict[PromptStep, str] = {}
        for step, prompt in raw.items():
            if step not in PROMPT_STEPS:
                raise ValueError(
                    f"prompts.{scope}.{step}: unknown step {step!r}; expected one of {PROMPT_STEPS}"
                )
            if not isinstance(prompt, str):
                raise ValueError(
                    f"prompts.{scope}.{step}: prompt must be a string (got {type(prompt).__name__})"
                )
            out[step] = prompt
        return out


__all__ = ["PROMPT_STEPS", "PromptResolver", "PromptStep"]

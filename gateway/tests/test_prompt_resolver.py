"""Tests for Phase 12 task 5 — PromptResolver.

Pure logic, no model. Covers resolution precedence (per-alias
override > global default > built-in), unknown-step fail-loud,
unknown-alias-falls-back, and the strict ``from_config`` parsing.
"""

from __future__ import annotations

from typing import get_args

import pytest

from gateway.prompt_resolver import (
    PROMPT_STEPS,
    PromptResolver,
    PromptStep,
)


def test_prompt_steps_matches_literal() -> None:
    """The hardcoded tuple must equal the Literal's args so they
    can't drift (the tuple exists only for mypy-strict friendliness)."""
    assert set(PROMPT_STEPS) == set(get_args(PromptStep))


def test_defaults_resolve_for_every_step() -> None:
    r = PromptResolver()
    for step in PROMPT_STEPS:
        # execute defaults to empty; the rest are non-empty drafts.
        result = r.resolve(step, "any-alias")
        assert isinstance(result, str)
    assert r.resolve("plan", "x")  # non-empty
    assert r.resolve("execute", "x") == ""


def test_unknown_alias_falls_back_to_default() -> None:
    r = PromptResolver()
    assert r.resolve("plan", "never-configured") == r.resolve("plan", "other")


def test_unknown_step_raises() -> None:
    r = PromptResolver()
    with pytest.raises(ValueError, match="unknown prompt step"):
        r.resolve("planx", "a")  # type: ignore[arg-type]


def test_per_alias_override_beats_default() -> None:
    r = PromptResolver(overrides={"fitt-weak": {"plan": "CUSTOM PLAN PROMPT"}})
    assert r.resolve("plan", "fitt-weak") == "CUSTOM PLAN PROMPT"
    # Other steps for that alias, and other aliases, still get defaults.
    assert r.resolve("recover", "fitt-weak") == r.resolve("recover", "other")
    assert r.resolve("plan", "other") != "CUSTOM PLAN PROMPT"


def test_global_default_override_beats_builtin_but_loses_to_alias() -> None:
    r = PromptResolver(
        defaults={"plan": "GLOBAL PLAN"},
        overrides={"fitt-weak": {"plan": "ALIAS PLAN"}},
    )
    assert r.resolve("plan", "some-other-alias") == "GLOBAL PLAN"
    assert r.resolve("plan", "fitt-weak") == "ALIAS PLAN"


# --------------------------------------------------------------- from_config


def test_from_config_none_is_all_defaults() -> None:
    r = PromptResolver.from_config(None)
    base = PromptResolver()
    for step in PROMPT_STEPS:
        assert r.resolve(step, "a") == base.resolve(step, "a")


def test_from_config_empty_is_all_defaults() -> None:
    r = PromptResolver.from_config({})
    assert r.resolve("plan", "a") == PromptResolver().resolve("plan", "a")


def test_from_config_parses_alias_and_defaults() -> None:
    r = PromptResolver.from_config(
        {
            "defaults": {"recover": "GLOBAL RECOVER"},
            "fitt-weak": {"plan": "WEAK PLAN", "execute": "WEAK EXEC"},
        }
    )
    assert r.resolve("plan", "fitt-weak") == "WEAK PLAN"
    assert r.resolve("execute", "fitt-weak") == "WEAK EXEC"
    assert r.resolve("recover", "fitt-weak") == "GLOBAL RECOVER"
    assert r.resolve("recover", "another") == "GLOBAL RECOVER"
    assert r.resolve("plan", "another") == PromptResolver().resolve("plan", "another")


def test_from_config_rejects_unknown_step() -> None:
    with pytest.raises(ValueError, match="unknown step"):
        PromptResolver.from_config({"fitt-weak": {"planning": "x"}})


def test_from_config_rejects_non_string_prompt() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        PromptResolver.from_config({"fitt-weak": {"plan": 123}})


def test_from_config_rejects_non_mapping_alias() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        PromptResolver.from_config({"fitt-weak": "not-a-dict"})

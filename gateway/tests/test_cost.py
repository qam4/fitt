"""Tests for the cost estimation function.

Covers Phase 1 Property 4 (cost calculation) plus unit tests for the
Ollama=zero and negative-token edge cases.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.config import ModelConfig
from gateway.cost import estimate_cost


def _openrouter(in_rate: Decimal | str, out_rate: Decimal | str) -> ModelConfig:
    return ModelConfig(
        id="test-model",
        backend="openrouter",
        model="some/model",
        cost_per_mtok_in=Decimal(str(in_rate)),
        cost_per_mtok_out=Decimal(str(out_rate)),
    )


def _ollama() -> ModelConfig:
    return ModelConfig(
        id="local",
        backend="ollama",
        model="qwen2.5-coder:14b",
        endpoint="http://localhost:11434",
    )


# ------------------------------------------------------------- unit tests


def test_cost_ollama_is_zero() -> None:
    assert estimate_cost(_ollama(), 10_000, 5_000) == Decimal("0")


def test_cost_zero_tokens_is_zero() -> None:
    assert estimate_cost(_openrouter("3", "15"), 0, 0) == Decimal("0")


def test_cost_calculation_known_values() -> None:
    # 1M input tokens @ $3, 500k output tokens @ $15
    # = $3 + $7.50 = $10.50
    model = _openrouter("3", "15")
    assert estimate_cost(model, 1_000_000, 500_000) == Decimal("10.500000")


def test_cost_calculation_small_values() -> None:
    # 1000 input tokens @ $3/mtok, 500 output tokens @ $15/mtok
    # = (0.001 * 3) + (0.0005 * 15) = 0.003 + 0.0075 = 0.0105
    model = _openrouter("3.00", "15.00")
    assert estimate_cost(model, 1000, 500) == Decimal("0.010500")


def test_cost_negative_tokens_clamped_to_zero() -> None:
    model = _openrouter("3", "15")
    # Malformed upstream: negative token counts clamp to 0
    assert estimate_cost(model, -100, -50) == Decimal("0")
    # Mixed: negative input ignored, positive output counted
    cost = estimate_cost(model, -100, 1_000_000)
    assert cost == Decimal("15.000000")


def test_cost_free_model_is_zero() -> None:
    # OpenRouter free models have 0 rates
    model = _openrouter("0", "0")
    assert estimate_cost(model, 123_456, 789_012) == Decimal("0")


# ------------------------------------------------------------- property test


# Phase 1, Property 4: Cost calculation
@given(
    input_tokens=st.integers(min_value=0, max_value=10_000_000),
    output_tokens=st.integers(min_value=0, max_value=10_000_000),
    in_rate=st.decimals(min_value="0", max_value="100", places=4, allow_nan=False),
    out_rate=st.decimals(min_value="0", max_value="100", places=4, allow_nan=False),
)
@settings(max_examples=200)
def test_property_cost_calculation(
    input_tokens: int, output_tokens: int, in_rate: Decimal, out_rate: Decimal
) -> None:
    """For any (tokens, rates), estimated cost matches the closed form."""
    model = _openrouter(in_rate, out_rate)
    expected = (Decimal(input_tokens) / Decimal(1_000_000)) * in_rate + (
        Decimal(output_tokens) / Decimal(1_000_000)
    ) * out_rate
    # Allow the same rounding tolerance the implementation uses.
    actual = estimate_cost(model, input_tokens, output_tokens)
    assert abs(actual - expected) <= Decimal("0.000001")


# Phase 1, Property 4 (invariant): Ollama is always free
@given(
    input_tokens=st.integers(min_value=0, max_value=10_000_000),
    output_tokens=st.integers(min_value=0, max_value=10_000_000),
)
@settings(max_examples=50)
def test_property_ollama_always_free(input_tokens: int, output_tokens: int) -> None:
    assert estimate_cost(_ollama(), input_tokens, output_tokens) == Decimal("0")

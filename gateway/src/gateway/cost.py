"""Pure cost estimation.

This module is deliberately small and side-effect-free so it can be
unit-tested and property-tested without any HTTP mocking or I/O. The
gateway's logger calls ``estimate_cost`` for each request to compute
the per-request USD estimate, which is then written to the structured
log. Aggregation (``fitt cost``) is done later by tailing the log.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from .config import ModelConfig

# We round costs to 6 decimal places (millionths of a dollar) for
# logging. Log aggregation then sums these to get MTD spend. 6 dp is
# more than enough precision for consumer rates and keeps the logs
# readable.
_QUANT = Decimal("0.000001")


def estimate_cost(model: ModelConfig, input_tokens: int, output_tokens: int) -> Decimal:
    """Return the estimated USD cost of one request.

    * Ollama models always return ``Decimal("0")`` — local inference is
      electricity, not money, and we don't try to estimate electricity.
    * Cloud models compute
      ``(in_tok / 1e6) * in_rate + (out_tok / 1e6) * out_rate``.

    Negative token counts are silently clamped to 0 so malformed
    upstream responses can't flip the cost sign.
    """
    if model.backend == "ollama":
        return Decimal("0")

    it = max(0, input_tokens)
    ot = max(0, output_tokens)
    per_mtok = Decimal(1_000_000)
    cost = (Decimal(it) / per_mtok) * model.cost_per_mtok_in + (
        Decimal(ot) / per_mtok
    ) * model.cost_per_mtok_out
    return cost.quantize(_QUANT, rounding=ROUND_HALF_EVEN)

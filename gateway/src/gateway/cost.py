"""Pure cost estimation.

This module is deliberately small and side-effect-free so it can be
unit-tested and property-tested without any HTTP mocking or I/O. The
gateway's logger calls ``estimate_cost`` for each request to compute
the per-request USD estimate, which is then written to the structured
log. Aggregation (``fitt cost``) is done later by tailing the log.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

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


# --------------------------------------------------------------- aggregation


def aggregate_monthly_spend(
    log_dir: Path,
    *,
    month_prefix: str | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Walk ``gateway.log`` (and rotated siblings) for one
    ``YYYY-MM`` window and return per-model totals.

    Used by both the ``fitt cost`` CLI (synchronous, prints a
    Rich table) and the dashboard's ``/dashboard/cost`` view
    (renders the same dict into HTML). Sharing the aggregation
    means the two surfaces never disagree on what a request
    costs.

    The file format we read is the structured-log line emitted
    by :func:`gateway.logging_config.log_chat_completion` —
    one JSON object per line with ``event="chat.completion"``,
    plus ``timestamp``, ``model``, ``input_tokens``,
    ``output_tokens``, ``cost_usd``. Lines that don't parse or
    don't match the prefix are silently skipped; an empty
    return is meaningful (no traffic this month).

    Returns ``(totals, prefix)``. ``prefix`` is the YYYY-MM
    string used to filter — useful when the caller passed
    ``None`` and wants to know what "current month" resolved
    to. Caller is responsible for handling the
    "log_dir doesn't exist" case before calling.
    """
    import json
    from collections import defaultdict
    from datetime import datetime
    from typing import Any as _Any

    if month_prefix is None:
        from datetime import UTC as _UTC

        month_prefix = datetime.now(_UTC).strftime("%Y-%m")

    totals: dict[str, dict[str, _Any]] = defaultdict(
        lambda: {
            "requests": 0,
            "cost_usd": Decimal("0"),
            "input_tokens": 0,
            "output_tokens": 0,
        }
    )

    if not log_dir.exists():
        return dict(totals), month_prefix

    # gateway.log is the current file; rotated files are
    # gateway.log.YYYY-MM-DD. Walk both.
    log_files = [log_dir / "gateway.log", *sorted(log_dir.glob("gateway.log.*"))]
    for path in log_files:
        if not path.exists():
            continue
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") != "chat.completion":
                    continue
                ts = str(event.get("timestamp", ""))
                if not ts.startswith(month_prefix):
                    continue
                model = str(event.get("model", "unknown"))
                totals[model]["requests"] += 1
                totals[model]["input_tokens"] += int(event.get("input_tokens", 0) or 0)
                totals[model]["output_tokens"] += int(event.get("output_tokens", 0) or 0)
                try:
                    c = Decimal(str(event.get("cost_usd", "0")))
                except Exception:
                    c = Decimal("0")
                totals[model]["cost_usd"] += c

    return dict(totals), month_prefix

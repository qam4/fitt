"""The ``fitt`` CLI — operator tooling for the gateway.

Three subcommands in Phase 1:

* ``fitt cost`` — parse the gateway's structured log, sum month-to-date
  USD by model, and print a table.
* ``fitt status`` — hit the local gateway's ``/v1/models`` endpoint and
  print a reachability summary.
* ``fitt config check`` — load and validate ``config.yaml`` and
  ``secrets.yaml`` without starting the server.

All three intentionally avoid touching the gateway process. Run them
from any shell on the machine where the gateway is installed.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console
from rich.table import Table

from .config import default_config_path, default_secrets_path, fitt_home, load_config
from .errors import ConfigError

# Force a wide console width so long model names aren't truncated in
# narrow terminals (and in CI/pytest runners where width is tiny).
_console = Console(width=140, soft_wrap=False)


@click.group()
def main() -> None:
    """FITT Gateway CLI."""


# --------------------------------------------------------------- fitt cost


@main.command()
@click.option(
    "--log-dir",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Directory containing gateway.log (default: ~/.fitt/logs)",
)
@click.option(
    "--month",
    type=str,
    default=None,
    help="YYYY-MM filter (default: current month)",
)
def cost(log_dir: Path | None, month: str | None) -> None:
    """Month-to-date spend aggregated from gateway logs."""
    log_dir = log_dir or fitt_home() / "logs"
    if not log_dir.exists():
        _console.print(f"[yellow]No log dir at {log_dir}. Has the gateway run?[/yellow]")
        sys.exit(0)

    prefix = month
    if prefix is None:
        from datetime import datetime

        prefix = datetime.now(UTC).strftime("%Y-%m")

    totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"requests": 0, "cost_usd": Decimal("0"), "input_tokens": 0, "output_tokens": 0}
    )

    # gateway.log is the current file; rotated files are gateway.log.YYYY-MM-DD
    log_files = [log_dir / "gateway.log", *sorted(log_dir.glob("gateway.log.*"))]
    seen_any = False
    for path in log_files:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as fh:
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
                ts = event.get("timestamp", "")
                if not ts.startswith(prefix):
                    continue
                seen_any = True
                model = event.get("model", "unknown")
                totals[model]["requests"] += 1
                totals[model]["input_tokens"] += int(event.get("input_tokens", 0) or 0)
                totals[model]["output_tokens"] += int(event.get("output_tokens", 0) or 0)
                try:
                    c = Decimal(str(event.get("cost_usd", "0")))
                except Exception:
                    c = Decimal("0")
                totals[model]["cost_usd"] += c

    if not seen_any:
        _console.print(f"[yellow]No chat.completion events found for {prefix}.[/yellow]")
        sys.exit(0)

    table = Table(title=f"FITT spend — {prefix}")
    table.add_column("Model", style="cyan")
    table.add_column("Requests", justify="right")
    table.add_column("Input tokens", justify="right")
    table.add_column("Output tokens", justify="right")
    table.add_column("Cost (USD)", justify="right", style="green")

    grand_total = Decimal("0")
    for model in sorted(totals.keys()):
        row = totals[model]
        grand_total += row["cost_usd"]
        table.add_row(
            model,
            str(row["requests"]),
            f"{row['input_tokens']:,}",
            f"{row['output_tokens']:,}",
            f"${row['cost_usd']:.4f}",
        )
    table.add_section()
    table.add_row("[bold]TOTAL[/bold]", "", "", "", f"[bold]${grand_total:.4f}[/bold]")
    _console.print(table)


# --------------------------------------------------------------- fitt status


@main.command()
@click.option("--url", default="http://localhost:8080", help="Gateway base URL")
def status(url: str) -> None:
    """Print aliases and their current reachability."""
    try:
        # /v1/models doesn't require auth.
        models = httpx.get(f"{url.rstrip('/')}/v1/models", timeout=5).json()
        ready = httpx.get(f"{url.rstrip('/')}/ready", timeout=10).json()
    except httpx.HTTPError as e:
        _console.print(f"[red]Could not reach gateway at {url}: {e}[/red]")
        sys.exit(1)

    table = Table(title="FITT aliases")
    table.add_column("Alias", style="cyan")
    table.add_column("Backend")
    table.add_column("Model")
    table.add_column("Reachable", justify="center")

    reach_info = ready.get("aliases", {})
    for m in models.get("data", []):
        alias = m["id"]
        info = reach_info.get(alias, {})
        is_ready = info.get("reachable", "?")
        table.add_row(
            alias,
            m.get("fitt_backend", "?"),
            m.get("fitt_resolved_model", "?"),
            "[green]yes[/green]"
            if is_ready is True
            else "[red]no[/red]"
            if is_ready is False
            else "?",
        )
    _console.print(table)


# --------------------------------------------------------------- fitt config


@main.group("config")
def config_group() -> None:
    """Configuration helpers."""


@config_group.command("check")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
@click.option("--secrets-file", type=click.Path(path_type=Path), default=None)
def config_check(config_file: Path | None, secrets_file: Path | None) -> None:
    """Validate config.yaml and secrets.yaml without starting the server."""
    cp = config_file or default_config_path()
    sp = secrets_file or default_secrets_path()
    try:
        cfg = load_config(cp, sp)
    except ConfigError as e:
        _console.print(f"[red]Configuration invalid:[/red] {e}")
        sys.exit(1)

    _console.print("[green]Configuration OK.[/green]")
    _console.print(f"  config:  {cp}")
    _console.print(f"  secrets: {sp}")
    _console.print(f"  aliases: {', '.join(cfg.alias_names())}")
    _console.print(f"  models:  {', '.join(m.id for m in cfg.models)}")


if __name__ == "__main__":
    main()

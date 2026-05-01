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

from .config import (
    Config,
    default_config_path,
    default_secrets_path,
    fitt_home,
    load_config,
)
from .errors import ConfigError
from .memory import MemoryStore
from .projects import (
    DuplicateProject,
    InvalidProjectName,
    InvalidProjectPath,
    Project,
    ProjectRegistry,
    UnknownProject,
)
from .sessions import (
    DuplicateSessionId,
    InvalidSessionId,
    ProtectedSession,
    SessionRegistry,
)

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


# --------------------------------------------------------------- fitt memory


def _open_memory(config_path: Path | None, secrets_path: Path | None) -> tuple[Config, MemoryStore]:
    cp = config_path or default_config_path()
    sp = secrets_path or default_secrets_path()
    cfg = load_config(cp, sp, load_secrets_too=False)
    store = MemoryStore(
        identity_dir=cfg.memory.identity_dir,
        sessions_dir=cfg.memory.sessions_dir,
        max_history_chars=cfg.memory.max_history_chars,
        enabled=cfg.memory.enabled,
    )
    return cfg, store


@main.group("memory")
def memory_group() -> None:
    """Inspect and manipulate FITT's persistent memory."""


@memory_group.command("show")
@click.option("--session", default="main", help="Session id (default: main)")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def memory_show(session: str, config_file: Path | None) -> None:
    """Print the identity + history that would be injected on the
    next chat request for this session."""
    _, store = _open_memory(config_file, None)
    if not store.enabled:
        _console.print("[yellow]Memory is disabled in config.[/yellow]")
        sys.exit(0)
    ctx = store.load_context(session)

    if ctx.system_prefix:
        _console.print("[bold]-- System prefix (identity) --[/bold]")
        _console.print(ctx.system_prefix)
        _console.print("")

    if ctx.history_messages:
        _console.print("[bold]-- History messages --[/bold]")
        for msg in ctx.history_messages:
            role = msg["role"]
            colour = "cyan" if role == "user" else "green"
            _console.print(f"[{colour}]{role}[/{colour}]: {msg['content']}")
        _console.print("")
    else:
        _console.print("[dim]No history for today.[/dim]")

    if ctx.truncated_bytes:
        _console.print(
            f"[yellow]{ctx.truncated_bytes} bytes truncated (budget: {store._max} chars).[/yellow]"
        )


@memory_group.command("append")
@click.option("--session", default="main", help="Session id (default: main)")
@click.option("--user", "user_msg", required=True, help="User content")
@click.option("--assistant", "assistant_msg", required=True, help="Assistant content")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def memory_append(
    session: str,
    user_msg: str,
    assistant_msg: str,
    config_file: Path | None,
) -> None:
    """Manually append a user/assistant turn to today's history.

    Useful for seeding context or writing regression tests.
    """
    _, store = _open_memory(config_file, None)
    if not store.enabled:
        _console.print("[red]Memory is disabled in config.[/red]")
        sys.exit(1)
    store.append_turn(session, user_msg, assistant_msg)
    _console.print(f"[green]Appended to {store.history_path(session)}.[/green]")


@memory_group.command("path")
@click.option("--session", default="main", help="Session id (default: main)")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def memory_path(session: str, config_file: Path | None) -> None:
    """Print the on-disk path of today's history file for this session."""
    _, store = _open_memory(config_file, None)
    _console.print(str(store.history_path(session)))


# --------------------------------------------------------------- fitt session


def _open_registry(config_path: Path | None) -> SessionRegistry:
    cp = config_path or default_config_path()
    cfg = load_config(cp, None, load_secrets_too=False)
    reg = SessionRegistry(cfg.memory.sessions_dir)
    reg.ensure_main()
    return reg


@main.group("session")
def session_group() -> None:
    """List and manage named conversation sessions."""


@session_group.command("list")
@click.option(
    "--include-archived",
    is_flag=True,
    default=False,
    help="Include archived sessions in the listing.",
)
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def session_list(include_archived: bool, config_file: Path | None) -> None:
    """Print every configured session."""
    reg = _open_registry(config_file)
    sessions = reg.all(include_archived=include_archived)

    table = Table(title="FITT sessions")
    table.add_column("Id", style="cyan")
    table.add_column("Name")
    table.add_column("Created (UTC)")
    table.add_column("Archived", justify="center")
    for s in sessions:
        table.add_row(
            s.id,
            s.name,
            s.created_at.isoformat().replace("+00:00", "Z"),
            "[red]yes[/red]" if s.archived else "",
        )
    _console.print(table)


@session_group.command("new")
@click.argument("session_id")
@click.option("--name", default=None, help="Human-readable display name.")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def session_new(session_id: str, name: str | None, config_file: Path | None) -> None:
    """Create a new session."""
    reg = _open_registry(config_file)
    try:
        s = reg.create(session_id, name)
    except (InvalidSessionId, DuplicateSessionId) as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    _console.print(f"[green]Created session {s.id!r} (name={s.name!r}).[/green]")


@session_group.command("rename")
@click.argument("session_id")
@click.option("--name", required=True, help="New display name.")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def session_rename(session_id: str, name: str, config_file: Path | None) -> None:
    """Rename an existing session."""
    reg = _open_registry(config_file)
    try:
        s = reg.rename(session_id, name)
    except ProtectedSession as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    _console.print(f"[green]Renamed {s.id!r} to {s.name!r}.[/green]")


@session_group.command("archive")
@click.argument("session_id")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def session_archive(session_id: str, config_file: Path | None) -> None:
    """Archive a session. History stays on disk; chat requests are rejected."""
    reg = _open_registry(config_file)
    try:
        s = reg.archive(session_id)
    except ProtectedSession as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    _console.print(f"[yellow]Archived {s.id!r}.[/yellow]")


@session_group.command("unarchive")
@click.argument("session_id")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def session_unarchive(session_id: str, config_file: Path | None) -> None:
    """Re-activate an archived session."""
    reg = _open_registry(config_file)
    s = reg.unarchive(session_id)
    _console.print(f"[green]Unarchived {s.id!r}.[/green]")


@session_group.command("path")
@click.argument("session_id")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def session_path(session_id: str, config_file: Path | None) -> None:
    """Print the on-disk history directory for this session."""
    cfg = load_config(config_file or default_config_path(), None, load_secrets_too=False)
    _console.print(str(cfg.memory.sessions_dir / session_id / "history"))


# --------------------------------------------------------------- fitt project


@main.group("project")
def project_group() -> None:
    """Register and manage projects FITT can operate on.

    A project is a logical code workspace: a name, a path, optionally
    an SSH host where the path lives. Tools that touch files (read,
    edit, grep, run tests, commit) take a `project` argument and
    dispatch to the registered host.
    """


def _open_project_registry() -> ProjectRegistry:
    reg = ProjectRegistry()
    reg.ensure_exists()
    return reg


@project_group.command("list")
def project_list() -> None:
    """Show every registered project."""
    reg = _open_project_registry()
    projects = reg.all()
    if not projects:
        _console.print(
            "[dim]No projects registered. Use `fitt project add <name>` to add one.[/dim]"
        )
        return

    table = Table(title="FITT projects")
    table.add_column("Name", style="cyan")
    table.add_column("Location")
    table.add_column("Path")
    table.add_column("Test command")
    for p in projects:
        loc = p.ssh_host or "(local)"
        table.add_row(p.name, loc, p.path, p.test_command or "-")
    _console.print(table)


@project_group.command("add")
@click.argument("name")
@click.option(
    "--path",
    required=True,
    help="Filesystem path on the execution host where the project lives.",
)
@click.option(
    "--ssh-host",
    default="",
    help="SSH host (e.g. laptop.tailnet). Empty = hub-local.",
)
@click.option(
    "--test-command",
    default="",
    help='Shell command to run tests (e.g. "uv run pytest -q").',
)
@click.option(
    "--build-command",
    default="",
    help='Shell command to build or lint (e.g. "uv run ruff check src tests").',
)
def project_add(
    name: str,
    path: str,
    ssh_host: str,
    test_command: str,
    build_command: str,
) -> None:
    """Register a new project."""
    reg = _open_project_registry()
    project = Project(
        name=name,
        path=path,
        ssh_host=ssh_host,
        test_command=test_command,
        build_command=build_command,
    )
    try:
        reg.add(project)
    except (DuplicateProject, InvalidProjectName, InvalidProjectPath) as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    loc = ssh_host or "(local)"
    _console.print(f"[green]Registered project {name!r} at {loc}:{path}.[/green]")


@project_group.command("update")
@click.argument("name")
@click.option("--path", default=None)
@click.option("--ssh-host", default=None)
@click.option("--test-command", default=None)
@click.option("--build-command", default=None)
def project_update(
    name: str,
    path: str | None,
    ssh_host: str | None,
    test_command: str | None,
    build_command: str | None,
) -> None:
    """Update one or more fields on an existing project.

    Only fields you pass are changed; others keep their current value.
    """
    reg = _open_project_registry()
    changes: dict[str, str] = {}
    if path is not None:
        changes["path"] = path
    if ssh_host is not None:
        changes["ssh_host"] = ssh_host
    if test_command is not None:
        changes["test_command"] = test_command
    if build_command is not None:
        changes["build_command"] = build_command

    if not changes:
        _console.print("[yellow]No fields to update. Pass at least one option.[/yellow]")
        sys.exit(1)

    try:
        updated = reg.update(name, **changes)
    except (UnknownProject, InvalidProjectPath) as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    _console.print(f"[green]Updated {updated.name!r}.[/green]")


@project_group.command("remove")
@click.argument("name")
def project_remove(name: str) -> None:
    """Remove a project from the registry.

    Does not touch the project's files on disk; just forgets about
    them in FITT's registry.
    """
    reg = _open_project_registry()
    try:
        reg.remove(name)
    except UnknownProject as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    _console.print(f"[yellow]Removed project {name!r}.[/yellow]")


@project_group.command("show")
@click.argument("name")
def project_show(name: str) -> None:
    """Print details of a single project."""
    reg = _open_project_registry()
    try:
        p = reg.get(name)
    except UnknownProject as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    _console.print(f"[bold cyan]{p.name}[/bold cyan]")
    _console.print(f"  location:      {p.ssh_host or '(local)'}")
    _console.print(f"  path:          {p.path}")
    _console.print(f"  test command:  {p.test_command or '-'}")
    _console.print(f"  build command: {p.build_command or '-'}")


if __name__ == "__main__":
    main()

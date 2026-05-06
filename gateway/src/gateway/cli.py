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


# --------------------------------------------------------------- fitt ssh


@main.group("ssh")
def ssh_group() -> None:
    """Manage the gateway's SSH identity and test reachability."""


@ssh_group.command("pubkey")
def ssh_pubkey() -> None:
    """Print the gateway's public SSH key.

    Generates the key pair on first use if it doesn't exist yet,
    matching the startup behaviour so running this command in a
    fresh container is enough to bootstrap. Paste the output into
    the satellite's authorized_keys file.
    """
    import asyncio

    from .ssh_identity import default_key_path, ensure_key, read_public_key

    key_path = default_key_path()
    try:
        asyncio.run(ensure_key(key_path))
        _console.print(read_public_key(key_path))
    except FileNotFoundError as e:
        _console.print(
            f"[red]ssh-keygen not available: {e}[/red]\n"
            f"Install it (on a Linux host: `apt install openssh-client`), "
            f"or run this CLI from inside the gateway container where "
            f"the image bundles openssh-client."
        )
        sys.exit(1)
    except RuntimeError as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)


@ssh_group.command("test")
@click.argument("ssh_host")
@click.option(
    "--command",
    default="uname -a && pwd",
    help="Command to run on the remote (default: uname -a && pwd).",
)
@click.option(
    "--timeout",
    type=int,
    default=15,
    help="Seconds to wait for the probe (default: 15).",
)
def ssh_test(ssh_host: str, command: str, timeout: int) -> None:
    """Probe an ssh host with the gateway's identity.

    Runs a harmless command (``uname -a && pwd`` by default) using
    the same key + options the ExecutionBackend uses at runtime. A
    success here means tools targeting a project with this
    ``ssh_host`` will work.

    The output shows the exact argv being dispatched so the user can
    copy/paste it into a shell for further debugging. On success it
    also classifies the remote shell (Git Bash, WSL, Linux, macOS,
    cmd.exe, ...) — useful when "SSH worked but what did I land in?"
    is the actual question.
    """
    import asyncio

    from .projects import Project
    from .ssh_identity import default_key_path, ensure_key
    from .ssh_probe import detect_shell
    from .tools import ExecutionBackend

    async def run() -> int:
        key_path = await ensure_key(default_key_path())
        backend = ExecutionBackend(ssh_key_path=key_path)
        # A throwaway Project: the backend only needs ssh_host + path,
        # and cd {path} without a valid path just surfaces as stderr.
        # We use "~" so the remote shell lands somewhere sane across
        # Linux / macOS / Git Bash.
        project = Project(
            name="ssh-test",
            ssh_host=ssh_host,
            path="~",
        )
        remote_cmd = ["sh", "-c", command]

        # Show the argv we're about to run. Users can paste this
        # into a terminal to debug without going through fitt.
        argv, _cwd = backend.build_ssh_argv(project, remote_cmd)
        _console.print("[dim]→[/dim] " + " ".join(_quote_argv(argv)))

        result = await backend.run_shell(
            project,
            remote_cmd,
            timeout_secs=timeout,
        )
        if result.timed_out:
            _console.print(
                f"[red]timed out after {timeout}s — host unreachable or sshd not responding[/red]"
            )
            return 1
        if result.exit != 0:
            _console.print(f"[red]exit={result.exit}[/red]\n{result.stderr.strip()}")
            return 1

        # Success path — print stdout, then the detection verdict.
        if result.stdout:
            _console.print(result.stdout.strip())
        detection = detect_shell(result.stdout)
        _console.print(f"[dim]detected:[/dim] {detection.label}")
        _console.print("[green]ok[/green]")
        return 0

    sys.exit(asyncio.run(run()))


def _quote_argv(argv: list[str]) -> list[str]:
    """Shell-quote an argv for display only. Not used for execution."""
    import shlex

    out = []
    for a in argv:
        # Double-quote args that contain spaces or shell metacharacters
        # so the line is copy-paste-safe in a POSIX shell.
        if any(c in a for c in " \t\"'$`\\;&|><*?[](){}#"):
            out.append(shlex.quote(a))
        else:
            out.append(a)
    return out


# --------------------------------------------------------------- audit


@main.group("audit")
def audit_group() -> None:
    """Verify and inspect the HMAC-chained audit log.

    The log at ``$FITT_HOME/audit.jsonl`` records every tool call
    the gateway processed (including rejected ones). Each entry
    chains to the previous via an HMAC keyed off
    ``$FITT_HOME/audit.key``; ``fitt audit verify`` walks the
    chain and reports the first inconsistency if any, and
    ``fitt audit tail`` prints recent entries."""


@audit_group.command("verify")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def audit_verify(config_file: Path | None) -> None:
    """Walk the audit log, re-compute every HMAC, and report.

    Exits 0 when the chain is intact, 1 when something's amiss.
    On failure, prints the first bad line number and the reason
    (malformed JSON, prev_hmac mismatch, HMAC mismatch). Use
    that line number to narrow inspection with
    ``sed -n '<n>p' audit.jsonl``."""
    from .audit import AuditLog, default_audit_paths
    from .config import fitt_home

    cfg = load_config(
        config_file or default_config_path(),
        default_secrets_path(),
        load_secrets_too=False,
    )
    _ = cfg  # currently unused; loaded for validation side effects
    log_path, key_path = default_audit_paths(fitt_home())
    audit_log = AuditLog(path=log_path, key_path=key_path)
    result = audit_log.verify()
    if result.ok:
        _console.print(f"[green]ok[/green] — {result.total_lines} entries verified")
        sys.exit(0)
    _console.print(f"[red]chain broken at line {result.bad_line}[/red]: {result.reason}")
    sys.exit(1)


@audit_group.command("tail")
@click.option(
    "-n",
    "--limit",
    type=int,
    default=20,
    help="Number of most-recent entries to show (default: 20).",
)
@click.option(
    "--tool",
    default=None,
    help="Filter by tool name (exact match).",
)
@click.option(
    "--session",
    default=None,
    help="Filter by session_key (exact match).",
)
@click.option(
    "--since",
    default=None,
    help=(
        "Only show entries newer than this. Accepts unix epoch "
        "(1730000000), ISO date (2026-05-06), or relative "
        "duration (1h, 30m, 7d)."
    ),
)
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def audit_tail(
    limit: int,
    tool: str | None,
    session: str | None,
    since: str | None,
    config_file: Path | None,
) -> None:
    """Print recent audit entries as a compact table."""
    from datetime import UTC, datetime
    from time import time as _now

    from .audit import AuditLog, default_audit_paths
    from .config import fitt_home

    cfg = load_config(
        config_file or default_config_path(),
        default_secrets_path(),
        load_secrets_too=False,
    )
    _ = cfg
    log_path, key_path = default_audit_paths(fitt_home())
    audit_log = AuditLog(path=log_path, key_path=key_path)
    entries = audit_log.iter_entries()

    since_ts = _parse_since(since) if since else None

    def matches(entry: dict[str, Any]) -> bool:
        if tool is not None and entry.get("tool") != tool:
            return False
        if session is not None and entry.get("session_key") != session:
            return False
        if since_ts is not None and entry.get("ts", 0) < since_ts:
            return False
        return True

    filtered = [e for e in entries if matches(e)]
    tail = filtered[-limit:]
    if not tail:
        _console.print(
            f"[dim](no entries match; log has {len(entries)} total, "
            f"{len(filtered)} after filters)[/dim]"
        )
        return
    for entry in tail:
        ts_str = datetime.fromtimestamp(entry.get("ts", _now()), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        decision = entry.get("decision", "?")
        colour = "green" if entry.get("ok") else "red"
        _console.print(
            f"[dim]{ts_str}[/dim] "
            f"[bold]{entry.get('tool', '?'):<20}[/bold] "
            f"[{colour}]{decision:<18}[/{colour}] "
            f"client={entry.get('client', '?')} "
            f"session={entry.get('session_key', '?')} "
            f"duration_ms={entry.get('duration_ms', 0)}"
        )
        if entry.get("error"):
            _console.print(f"  [red]error:[/red] {entry['error'][:200]}")


def _parse_since(s: str) -> float:
    """Parse --since into a unix-epoch float. Accepts:
    - a raw epoch ``1730000000`` or ``1730000000.5``
    - an ISO date ``2026-05-06`` (UTC midnight)
    - a relative duration ``30m`` / ``2h`` / ``7d`` (from now, backwards)
    """
    from datetime import UTC, date, datetime
    from time import time as _now

    s = s.strip()
    # epoch?
    try:
        return float(s)
    except ValueError:
        pass
    # relative?
    if s and s[-1] in "smhd" and s[:-1].isdigit():
        n = int(s[:-1])
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[s[-1]]
        return _now() - n * mult
    # ISO date?
    try:
        d = date.fromisoformat(s)
        return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()
    except ValueError:
        pass
    raise click.BadParameter(
        f"could not parse --since={s!r}. "
        "Use an epoch, an ISO date (YYYY-MM-DD), or a duration (30m, 2h, 7d)."
    )


if __name__ == "__main__":
    main()

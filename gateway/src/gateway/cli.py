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

import sys
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
from .scenarios import daily_news_summary, topic_brief
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

    from .cost import aggregate_monthly_spend

    totals, prefix = aggregate_monthly_spend(log_dir, month_prefix=month)
    if not totals:
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
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help=(
        "After printing the initial window, keep watching the "
        "audit file for new entries. Filters apply to follow "
        "output too. Ctrl-C exits cleanly."
    ),
)
@click.option(
    "--poll-interval",
    type=float,
    default=0.5,
    help="Seconds between file-stat polls in follow mode (default: 0.5).",
)
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def audit_tail(
    limit: int,
    tool: str | None,
    session: str | None,
    since: str | None,
    follow: bool,
    poll_interval: float,
    config_file: Path | None,
) -> None:
    """Print recent audit entries as a compact table.

    With ``-f`` / ``--follow``, after printing the initial
    window the command keeps watching the audit file and
    streams new entries as they appear. The filters (``--tool``,
    ``--session``, ``--since``) apply to streamed output too, so
    ``fitt audit tail -f --tool project_shell`` is a live view
    of shell calls running on the hub.
    """
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
    if not tail and not follow:
        _console.print(
            f"[dim](no entries match; log has {len(entries)} total, "
            f"{len(filtered)} after filters)[/dim]"
        )
        return
    for entry in tail:
        _print_audit_entry(entry)

    if not follow:
        return

    # Follow mode. Track the total count we've printed; poll the
    # log for new entries and emit whichever new ones pass the
    # filter. Ctrl-C is caught so the user gets a clean exit.
    seen_count = len(entries)
    try:
        while True:
            import time as _time

            _time.sleep(poll_interval)
            fresh = audit_log.iter_entries()
            if len(fresh) <= seen_count:
                continue
            new = fresh[seen_count:]
            seen_count = len(fresh)
            for entry in new:
                if matches(entry):
                    _print_audit_entry(entry)
    except KeyboardInterrupt:
        _console.print("[dim]interrupted.[/dim]")


def _print_audit_entry(entry: dict[str, Any]) -> None:
    """Render one audit entry to the console in the compact
    ``fitt audit tail`` shape. Factored out so both initial
    and follow paths use the same formatter."""
    from datetime import datetime
    from time import time as _now

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
    from datetime import date, datetime
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


# --------------------------------------------------------------- mcp


@main.group("mcp")
def mcp_group() -> None:
    """Inspect and manage MCP servers the gateway spawns.

    FITT runs MCP servers as subprocesses configured under
    ``mcp_servers:`` in config.yaml. These commands talk to the
    running gateway's ``/v1/mcp`` endpoint — you need the gateway
    up."""


def _mcp_gateway_url() -> str:
    (
        """Default to the gateway's local port. Override via
    ``FITT_GATEWAY_URL`` for remote management."""
        ""
    )
    import os as _os

    return _os.environ.get("FITT_GATEWAY_URL", "http://127.0.0.1:8421").rstrip("/")


def _mcp_bearer_token() -> str:
    """Read the first allowed_tokens entry from secrets.yaml for CLI auth."""
    cfg = load_config(default_config_path(), default_secrets_path())
    if cfg.secrets is None or not cfg.secrets.allowed_tokens:
        raise click.ClickException(
            "no allowed_tokens in secrets.yaml; cannot authenticate to the gateway."
        )
    return cfg.secrets.allowed_tokens[0].token


@mcp_group.command("list")
def mcp_list() -> None:
    """Show configured MCP servers and their running state."""
    import httpx

    try:
        r = httpx.get(
            f"{_mcp_gateway_url()}/v1/mcp",
            headers={
                "Authorization": f"Bearer {_mcp_bearer_token()}",
                "X-FITT-Client": "cli",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        raise click.ClickException(f"gateway unreachable: {e}") from None
    if r.status_code != 200:
        raise click.ClickException(f"HTTP {r.status_code}: {r.text}")

    servers = r.json().get("servers", [])
    if not servers:
        _console.print("[dim](no MCP servers configured)[/dim]")
        return
    for s in servers:
        status = "[green]running[/green]" if s.get("running") else "[red]stopped[/red]"
        _console.print(
            f"{s.get('name'):<20} {status}  "
            f"tools: {len(s.get('tools', []))}  "
            f"command: {' '.join(s.get('command', []))}"
        )


@mcp_group.command("restart")
@click.argument("name")
def mcp_restart(name: str) -> None:
    """Stop and re-spawn the named MCP server."""
    import httpx

    try:
        r = httpx.post(
            f"{_mcp_gateway_url()}/v1/mcp/{name}/restart",
            headers={
                "Authorization": f"Bearer {_mcp_bearer_token()}",
                "X-FITT-Client": "cli",
            },
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        raise click.ClickException(f"gateway unreachable: {e}") from None
    if r.status_code == 404:
        raise click.ClickException(f"no MCP server named {name!r}")
    if r.status_code != 200:
        raise click.ClickException(f"HTTP {r.status_code}: {r.text}")
    _console.print(f"[green]restarted[/green] {name}")


# --------------------------------------------------------------- capability gaps


@main.command("capability-gaps")
@click.option(
    "-n",
    "--limit",
    type=int,
    default=30,
    help="Number of top entries to print (default: 30).",
)
@click.option(
    "--since",
    default=None,
    help=(
        "Only show gaps newer than this. Accepts unix epoch, "
        "ISO date (YYYY-MM-DD), or relative duration (1h, 7d)."
    ),
)
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def capability_gaps_cmd(
    limit: int,
    since: str | None,
    config_file: Path | None,
) -> None:
    """Print capability-gap entries ranked by frequency.

    Each time the model replies with \"I'd need a tool to X\", one
    line lands in $FITT_HOME/capability_gaps.log. This command
    groups by canonical action text and shows the most-asked-for
    tools first — the natural backlog for 'what should I build
    next'."""
    from datetime import datetime

    from .capabilities import CapabilityGapLog, default_gap_log_path
    from .config import fitt_home

    cfg = load_config(
        config_file or default_config_path(),
        default_secrets_path(),
        load_secrets_too=False,
    )
    _ = cfg
    log = CapabilityGapLog(default_gap_log_path(fitt_home()))

    since_ts = _parse_since(since) if since else None
    gaps = log.read(since=since_ts)
    if not gaps:
        _console.print(
            "[dim](no capability gaps recorded" + (f" since {since}" if since else "") + ")[/dim]"
        )
        return
    from .capabilities import rank_gaps

    ranked = rank_gaps(gaps)[:limit]
    for action, count, most_recent in ranked:
        ts_str = datetime.fromtimestamp(most_recent.ts, UTC).strftime("%Y-%m-%d %H:%M")
        suggestion = (
            f" — model suggested: {most_recent.suggestion!r}" if most_recent.suggestion else ""
        )
        _console.print(f"[bold]{count}x[/bold] [dim]({ts_str})[/dim] {action}{suggestion}")


# --------------------------------------------------------------- fitt cron


def _open_cron_service() -> Any:
    """Open the CronService against the operator's ``$FITT_HOME``.

    CLI and gateway share the same file; mtime-based reload
    picks up the CLI's mutations on the gateway's next tick.
    """
    from .config import fitt_home
    from .cron import CronService, default_cron_path

    return CronService(default_cron_path(fitt_home()))


def _format_schedule_cli(schedule: Any) -> str:
    """Mirror of ``tools.cron_tools._format_schedule`` — the
    CLI output matches what the agent sees so operators can
    compare the two without mental re-mapping."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    if schedule.kind == "every":
        n = schedule.every_secs or 0
        if n % 3600 == 0:
            return f"every {n // 3600}h"
        if n % 60 == 0:
            return f"every {n // 60}m"
        return f"every {n}s"
    if schedule.kind == "at":
        if schedule.at_ts is None:
            return "at <unset>"
        return f"at {_dt.fromtimestamp(schedule.at_ts, tz=_UTC).isoformat()}"
    if schedule.kind == "cron":
        tz_suffix = (
            f" [{schedule.timezone}]" if schedule.timezone and schedule.timezone != "UTC" else ""
        )
        return f"cron {schedule.cron_expr}{tz_suffix}"
    return f"<unknown kind: {schedule.kind}>"


@main.group("cron")
def cron_group() -> None:
    """List, add, and remove scheduled crons.

    Crons live in ``$FITT_HOME/cron.json``. The running gateway
    picks up changes on its next poll (see ``cron.poll_interval_secs``
    in config.yaml) via mtime-based reload — no restart needed.
    """


@cron_group.command("list")
@click.option("--all", "all_", is_flag=True, help="Include disabled crons.")
def cron_list(all_: bool) -> None:
    """Show every cron, with id / state / schedule / name."""
    svc = _open_cron_service()
    jobs = svc.list(include_disabled=all_)
    if not jobs:
        _console.print(
            "[dim]No crons. Use `fitt cron add` or have the agent "
            "create one via the `cron_add` tool.[/dim]"
        )
        return

    table = Table(title="FITT crons")
    table.add_column("Id", style="cyan")
    table.add_column("State")
    table.add_column("Schedule")
    table.add_column("Name")
    table.add_column("Alias")
    table.add_column("Last run", justify="right")
    for j in jobs:
        state = "active" if j.enabled else "[yellow]paused[/yellow]"
        if j.silent:
            state += " (silent)"
        if j.approval_mode == "auto":
            state += " (auto-approve)"
        last = (
            f"{j.last_status or '?'}"
            if j.last_run_ts is None
            else _format_last_run(j.last_run_ts, j.last_status)
        )
        table.add_row(
            j.id,
            state,
            _format_schedule_cli(j.schedule),
            j.name,
            j.agent_alias or "(default)",
            last,
        )
    _console.print(table)


def _format_last_run(ts: float, status: str) -> str:
    """Relative-time summary for the last-run column."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from time import time as _now

    delta = _now() - ts
    if delta < 60:
        ago = f"{int(delta)}s ago"
    elif delta < 3600:
        ago = f"{int(delta // 60)}m ago"
    elif delta < 86400:
        ago = f"{int(delta // 3600)}h ago"
    else:
        ago = _dt.fromtimestamp(ts, _UTC).strftime("%Y-%m-%d")
    colour = "green" if status == "ok" else "red" if status == "error" else "dim"
    return f"[{colour}]{status or 'n/a'}[/{colour}] {ago}"


@cron_group.command("add")
@click.option("--name", required=True, help="Human label for the cron.")
@click.option(
    "--schedule",
    "schedule_spec",
    required=True,
    help=(
        "Schedule spec: 'every 60s', 'every 5m', 'in 30 minutes', "
        "'at <iso|epoch>', 'cron <5-field>'."
    ),
)
@click.option(
    "--message",
    required=True,
    help="Prompt handed to the agent when the cron fires.",
)
@click.option("--silent", is_flag=True, help="Suppress auto-delivery of the reply.")
@click.option(
    "--auto-approve",
    is_flag=True,
    help="Auto-approve ask-bucket tools inside this cron's firings.",
)
@click.option(
    "--alias",
    "agent_alias",
    default="",
    help="Model alias (e.g. fitt-smart). Empty = fitt-default.",
)
@click.option("--timezone", "tz", default="UTC", help="IANA tz for cron exprs (default UTC).")
def cron_add(
    name: str,
    schedule_spec: str,
    message: str,
    silent: bool,
    auto_approve: bool,
    agent_alias: str,
    tz: str,
) -> None:
    """Register a new cron.

    Goes straight to disk — CLI mutations skip the approval
    middleware because the operator IS the human. The gateway
    picks up the change on its next poll.
    """
    from .cron import CronError, CronJob, parse_schedule_spec

    try:
        schedule = parse_schedule_spec(schedule_spec, tz=tz)
    except CronError as e:
        _console.print(f"[red]invalid schedule:[/red] {e}")
        sys.exit(1)

    svc = _open_cron_service()
    approval_mode = "auto" if auto_approve else ""
    job = svc.add(
        CronJob(
            id="",
            name=name,
            message=message,
            schedule=schedule,
            silent=silent,
            approval_mode=approval_mode,  # type: ignore[arg-type]
            agent_alias=agent_alias,
            created_by_client="cli",
        )
    )
    _console.print(
        f"[green]Created[/green] {job.id} [{_format_schedule_cli(job.schedule)}] {job.name!r}"
    )


@cron_group.command("remove")
@click.argument("cron_id")
def cron_remove(cron_id: str) -> None:
    """Delete a cron by id."""
    svc = _open_cron_service()
    if not svc.remove(cron_id):
        _console.print(f"[red]no cron with id {cron_id!r}[/red]")
        sys.exit(1)
    _console.print(f"[yellow]Removed[/yellow] {cron_id}")


@cron_group.command("pause")
@click.argument("cron_id")
def cron_pause(cron_id: str) -> None:
    """Pause a cron — stays in cron.json, scheduler skips it."""
    from .cron import UnknownCron

    svc = _open_cron_service()
    try:
        svc.set_enabled(cron_id, False)
    except UnknownCron as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    _console.print(f"[yellow]Paused[/yellow] {cron_id}")


@cron_group.command("resume")
@click.argument("cron_id")
def cron_resume(cron_id: str) -> None:
    """Resume a paused cron."""
    from .cron import UnknownCron

    svc = _open_cron_service()
    try:
        svc.set_enabled(cron_id, True)
    except UnknownCron as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    _console.print(f"[green]Resumed[/green] {cron_id}")


# `fitt cron run <id>` (fire once immediately) is deferred. It
# requires a ``POST /v1/cron/<id>/run`` endpoint that doesn't
# exist yet; shipping the CLI command without the server-side
# half would just be a broken button. The workaround today is
# ``fitt cron add --schedule "in 5 seconds" --message "..."``
# which fires once and self-cleans (one-shot at schedules are
# removed by the scheduler after firing).


# --------------------------------------------------------------- fitt inbox


@main.command("inbox")
@click.option(
    "--since",
    default="24h",
    help=(
        "Only show events newer than this. Accepts unix epoch, "
        "ISO date (YYYY-MM-DD), or relative duration (30m, 24h, 7d). "
        "Default: 24h."
    ),
)
@click.option(
    "--kind",
    default=None,
    help="Filter by event kind (cron_fired, cron_completed, agent_message, ...).",
)
@click.option(
    "--session",
    default=None,
    help="Filter by session_key (e.g. 'main' or 'cron:<id>:<ts>').",
)
@click.option(
    "-n",
    "--limit",
    type=int,
    default=50,
    help="Max entries to show (default: 50, most recent).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON lines for scripts.")
def inbox_cmd(
    since: str,
    kind: str | None,
    session: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """Scroll through the event log.

    Same data the Telegram bot pushes — cron firings, detached
    tool results, agent messages. Source of truth lives at
    ``$FITT_HOME/events.jsonl``; this is the operator-side
    reader.
    """
    import json as _json
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from .config import fitt_home
    from .events import EventLog, default_events_path

    since_ts = _parse_since(since) if since else None
    log = EventLog(default_events_path(fitt_home()))
    entries = log.read(
        since=since_ts,
        kind=kind,
        session=session,
        limit=limit,
    )

    if as_json:
        for e in entries:
            click.echo(
                _json.dumps(
                    {
                        "ts": e.ts,
                        "kind": e.kind,
                        "session_key": e.session_key,
                        "title": e.title,
                        "body": e.body,
                        "meta": e.meta,
                    },
                    ensure_ascii=False,
                )
            )
        return

    if not entries:
        _console.print(
            f"[dim](no events since {since}"
            + (f", kind={kind!r}" if kind else "")
            + (f", session={session!r}" if session else "")
            + ")[/dim]"
        )
        return

    for e in entries:
        ts_str = _dt.fromtimestamp(e.ts, _UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        colour = _kind_colour(e.kind)
        title = e.title or e.kind
        _console.print(
            f"[dim]{ts_str}[/dim] [{colour}]{e.kind:<20}[/{colour}] "
            f"[bold]{title}[/bold] "
            f"[dim]session={e.session_key}[/dim]"
        )
        if e.body:
            body = e.body.replace("\n", "\n  ")
            if len(body) > 400:
                body = body[:397] + "..."
            _console.print(f"  {body}")


def _kind_colour(kind: str) -> str:
    if kind in ("cron_failed", "late_tool_rejected"):
        return "red"
    if kind in ("cron_completed", "late_tool_result", "agent_message"):
        return "green"
    if kind in ("cron_fired", "approval_requested"):
        return "yellow"
    return "cyan"


# --------------------------------------------------------------- fitt learn (Phase 5)


def _open_lessons_store() -> Any:
    """Open the LessonsStore against ``$FITT_HOME/identity/lessons.md``.

    CLI mutations go straight to disk; the running gateway
    picks up the change on the next request via the lessons
    store's mtime-based reload."""
    from .config import fitt_home
    from .lessons import LessonsStore, default_lessons_path

    identity_dir = fitt_home() / "identity"
    return LessonsStore(default_lessons_path(identity_dir))


@main.group("learn")
def learn_group() -> None:
    """List, add, and remove learned corrections (lessons).

    Lessons live in ``$FITT_HOME/identity/lessons.md``. They
    get injected into every request's system prompt as the
    ``[Learned corrections]`` block. Edit the file directly
    with your ``$EDITOR`` or use these commands; either way
    the running gateway sees the change on the next request.
    """


@learn_group.command("list")
def learn_list_cmd() -> None:
    """Print every current lesson."""
    store = _open_lessons_store()
    lessons = store.read()
    if not lessons:
        _console.print(
            "[dim]No lessons recorded. Add one with `fitt learn "
            'add "always use uv"` or let the agent record one '
            "via `learn_add`.[/dim]"
        )
        return

    table = Table(title="FITT lessons")
    table.add_column("Category", style="cyan")
    table.add_column("Text")
    for lsn in lessons:
        table.add_row(lsn.category or "-", lsn.text)
    _console.print(table)


@learn_group.command("add")
@click.argument("text")
@click.option(
    "--category",
    default=None,
    help="Optional tag (e.g. 'tooling', 'style').",
)
def learn_add_cmd(text: str, category: str | None) -> None:
    """Record a new lesson.

    Bypasses approval middleware — the CLI operator IS the
    human, same posture as ``fitt cron add``.
    """
    store = _open_lessons_store()
    try:
        lesson = store.add(text, category=category)
    except ValueError as e:
        _console.print(f"[red]{e}[/red]")
        sys.exit(1)
    _console.print(f"[green]Recorded[/green] {lesson.render()}")


@learn_group.command("remove")
@click.argument("substring")
def learn_remove_cmd(substring: str) -> None:
    """Remove lessons whose text contains ``substring``."""
    store = _open_lessons_store()
    removed = store.remove(substring)
    if removed == 0:
        _console.print(f"[yellow]No lessons matched {substring!r}.[/yellow]")
        return
    _console.print(f"[yellow]Removed {removed} lesson(s) matching {substring!r}.[/yellow]")


@learn_group.command("path")
def learn_path_cmd() -> None:
    """Print the on-disk path of ``lessons.md``.

    Useful for piping to ``$EDITOR`` or for scripts that want
    to inspect the file directly."""
    store = _open_lessons_store()
    _console.print(str(store.path))


# --------------------------------------------------------------- eval


@main.group("eval")
def eval_group() -> None:
    """Run the alias eval harness.

    Dispatches a curated set of tool-use prompts against an
    alias and scores whether the model emits the expected
    tool_calls. Richer coverage than the boot-time
    ``alias_probe`` single canary. Reports land in
    ``$FITT_HOME/eval/`` as markdown.

    Use before swapping a model (sanity-check the new binding
    without committing it) and after (make sure the behaviour
    didn't drift). See docs/choosing-a-model.md for when to
    re-run."""


@eval_group.command("alias")
@click.argument("alias")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=15.0,
    help="Per-case dispatch timeout in seconds (default: 15).",
)
@click.option(
    "--min-pass-rate",
    type=float,
    default=None,
    help=(
        "If set, exit with code 1 when the pass rate falls "
        "below this fraction (0.0-1.0). Useful for CI / "
        "pre-swap gates."
    ),
)
@click.option(
    "--suite",
    type=click.Choice(["default", "coding"]),
    default="default",
    help=(
        "Which suite to run. ``default`` (FITT's own tool "
        "shape) is the conservative bind check. ``coding`` "
        "tests the binding under a coding-agent system "
        "prompt + read/edit/glob/shell tools, useful for "
        "router-mode (X-FITT-Client: coding-agent) work."
    ),
)
@click.option(
    "--record",
    "record_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Capture every model dispatch this run makes to a replay "
        "cassette at the given path (Phase 12 record/replay). Run "
        "against a real backend once; the cassette then drives "
        "deterministic CI / fast local tests with no live model."
    ),
)
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def eval_alias_cmd(
    alias: str,
    timeout_s: float,
    min_pass_rate: float | None,
    suite: str,
    record_path: Path | None,
    config_file: Path | None,
) -> None:
    """Run an eval suite against one alias.

    Writes a markdown report to ``$FITT_HOME/eval/<alias>-<ts>.md``
    (or ``<alias>-coding-<ts>.md`` for the coding suite) and a
    rolling ``-latest.md`` that gets overwritten on every run.
    Prints a one-line summary to stdout."""
    import asyncio

    from .alias_eval import run_eval_suite, write_report
    from .alias_eval_coding import default_coding_cases
    from .config import fitt_home
    from .router import AliasRouter

    cfg = load_config(
        config_file or default_config_path(),
        default_secrets_path(),
    )
    if alias not in cfg.aliases:
        _console.print(
            f"[red]unknown alias[/red] {alias!r}. Configured aliases: {sorted(cfg.aliases)}"
        )
        sys.exit(2)

    cases = default_coding_cases() if suite == "coding" else None
    base_router = AliasRouter(cfg)
    recording: Any = None
    eval_router: Any = base_router
    if record_path is not None:
        from .record_replay import RecordingRouter

        recording = RecordingRouter(base_router)
        eval_router = recording
    report = asyncio.run(run_eval_suite(alias, eval_router, cases=cases, timeout_s=timeout_s))
    if recording is not None:
        recording.save(record_path)
        _console.print(
            f"[cyan]recorded {len(recording.cassette.interactions)} dispatch(es)[/cyan] "
            f"to {record_path}"
        )
    ts_path, latest_path = write_report(report, fitt_home(), suite=suite)

    colour = "green" if report.passed == report.total else "yellow"
    _console.print(
        f"[{colour}]{report.passed}/{report.total} passed[/{colour}] "
        f"({report.pass_rate * 100:.0f}%) — "
        f"suite={suite}, model=`{report.model_id or 'unknown'}`, "
        f"latest={latest_path}, audit={ts_path}"
    )

    if min_pass_rate is not None and report.pass_rate < min_pass_rate:
        _console.print(
            f"[red]pass rate below threshold[/red] "
            f"{report.pass_rate * 100:.0f}% < "
            f"{min_pass_rate * 100:.0f}%"
        )
        sys.exit(1)


@eval_group.command("all")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=15.0,
    help="Per-case dispatch timeout in seconds (default: 15).",
)
@click.option(
    "--suite",
    type=click.Choice(["default", "coding"]),
    default="default",
    help="Which suite to run for every alias (default or coding).",
)
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def eval_all_cmd(timeout_s: float, suite: str, config_file: Path | None) -> None:
    """Run a suite against every configured alias.

    Sequential across aliases (same rate-limit posture as the
    per-alias run). One summary line per alias; reports land
    in the same ``$FITT_HOME/eval/`` directory as the
    per-alias command."""
    import asyncio

    from .alias_eval import run_eval_suite, write_report
    from .alias_eval_coding import default_coding_cases
    from .config import fitt_home
    from .router import AliasRouter

    cfg = load_config(
        config_file or default_config_path(),
        default_secrets_path(),
    )
    router = AliasRouter(cfg)
    any_failed = False

    cases = default_coding_cases() if suite == "coding" else None

    async def _run_all() -> None:
        nonlocal any_failed
        for alias in cfg.alias_names():
            report = await run_eval_suite(alias, router, cases=cases, timeout_s=timeout_s)
            write_report(report, fitt_home(), suite=suite)
            colour = "green" if report.passed == report.total else "yellow"
            if report.passed != report.total:
                any_failed = True
            _console.print(
                f"[{colour}]{alias}: {report.passed}/{report.total} "
                f"passed[/{colour}] ({report.pass_rate * 100:.0f}%) — "
                f"suite={suite}, model=`{report.model_id or 'unknown'}`"
            )

    asyncio.run(_run_all())
    if any_failed:
        sys.exit(1)


# --------------------------------------------------------------- scenario


@main.group("scenario")
def scenario_group() -> None:
    """Run a multi-step scenario through the flat or planned loop.

    Phase 12 tasks 4 + 22: drive a whole multi-step turn (the
    ``daily_news_summary`` case — fetch live news, then summarize and
    deliver) against a real model and read the *structural* outcome.
    ``--mode flat`` runs the current flat loop (the task-4 baseline);
    ``--mode planned`` runs the plan -> execute orchestrator. Multi-
    sampled for a capability pass-rate (the task-2 conventions), since
    one run reads a non-deterministic loop as fact.

    Unlike ``fitt eval`` (single-shot tool-shape checks), this runs the
    real tool path: the gateway's actual registry, approval policy
    (auto-approved here so it doesn't block on a Telegram tap), and
    injected capability prompt."""


_SCENARIOS = {"daily_news_summary": daily_news_summary, "topic_brief": topic_brief}


@scenario_group.command("run")
@click.argument("alias")
@click.option(
    "--mode",
    type=click.Choice(["flat", "planned"]),
    default="flat",
    help="flat = current loop (task-4 baseline); planned = plan->execute.",
)
@click.option("--samples", type=int, default=5, help="How many runs to multi-sample (default: 5).")
@click.option(
    "--scenario",
    "scenario_name",
    type=click.Choice(sorted(_SCENARIOS)),
    default="daily_news_summary",
)
@click.option(
    "--planner-alias",
    default="",
    help="planned mode only: run the plan pass on this alias (defaults to the executor alias).",
)
@click.option(
    "--preview-chars",
    type=int,
    default=200,
    help="How many chars of each reply to print (default: 200; raise to read full replies and judge synthesis vs relay).",
)
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def scenario_run_cmd(
    alias: str,
    mode: str,
    samples: int,
    scenario_name: str,
    planner_alias: str,
    preview_chars: int,
    config_file: Path | None,
) -> None:
    """Run a scenario against ALIAS and print the per-sample outcomes
    plus the aggregate pass-rate."""
    import asyncio
    import os

    from .app import create_app
    from .capabilities import build_capability_block
    from .cron_runner import _AutoApproveWrapper
    from .router import AliasRouter
    from .scenario_eval import run_scenario_multi
    from .tools import ToolContext

    cfg = load_config(config_file or default_config_path(), default_secrets_path())
    if alias not in cfg.aliases:
        _console.print(
            f"[red]unknown alias[/red] {alias!r}. Configured aliases: {sorted(cfg.aliases)}"
        )
        sys.exit(2)
    if planner_alias and planner_alias not in cfg.aliases:
        _console.print(
            f"[red]unknown planner alias[/red] {planner_alias!r}. "
            f"Configured aliases: {sorted(cfg.aliases)}"
        )
        sys.exit(2)

    # The news scenario never calls project_shell; skip the ~2s boot
    # shell probe so the command starts promptly.
    os.environ.setdefault("FITT_SKIP_SHELL_PROBE", "1")
    app = create_app(cfg)
    state = app.state
    registry = state.tool_registry
    router = AliasRouter(cfg)
    # Auto-approve so an ASK-bucket tool (send_message) doesn't block on
    # a Telegram tap that won't come in this headless run. The deny list
    # is still enforced.
    approval = _AutoApproveWrapper(state.approval)
    system_prompt = build_capability_block(registry) if registry.list_names() else ""
    scenario = _SCENARIOS[scenario_name]()

    def make_ctx(session_key: str) -> ToolContext:
        return ToolContext(
            client="cli",
            session_key=session_key,
            projects=state.project_registry,
            backend=state.execution_backend,
            policy=registry.policy,
            audit=state.audit,
            cron=state.cron,
            events=state.events,
            local_shell=state.local_shell,
            lessons=state.lessons,
            turns=None,
            turn_id=None,
            web_search_backend=cfg.web.search_backend,
            plan_store=state.plan_store,
        )

    result = asyncio.run(
        run_scenario_multi(
            scenario,
            alias,
            mode,  # type: ignore[arg-type]
            samples=samples,
            alias_router=router,
            tool_registry=registry,
            approval=approval,
            make_tool_ctx=make_ctx,
            system_prompt=system_prompt,
            prompt_resolver=state.prompt_resolver,
            planner_alias=planner_alias,
            preview_chars=preview_chars,
        )
    )

    rate = result.pass_rate
    rate_str = f"{rate * 100:.0f}%" if rate is not None else "n/a (all transient)"
    _console.print(
        f"\n[bold]{result.scenario_name}[/bold] via [cyan]{mode}[/cyan] "
        f"on [cyan]{alias}[/cyan]"
        + (f" (planner=[cyan]{planner_alias}[/cyan])" if planner_alias else "")
    )
    _console.print(
        f"pass rate: [bold]{result.passes}/{result.valid}[/bold] = {rate_str}  "
        f"(transient excluded: {result.transient})"
    )
    election = result.plan_election_rate
    if election is not None:
        _console.print(f"plan election: {election * 100:.0f}%")
    _console.print(f"outcomes: {result.outcome_counts}")
    for i, s in enumerate(result.samples, 1):
        seq = " -> ".join(s.tool_sequence) or "(no tool calls)"
        colour = "green" if s.outcome == "completed" else "yellow"
        plan_tag = ""
        if s.plan_produced is True:
            plan_tag = " [green]planned[/green]"
        elif s.plan_produced is False:
            plan_tag = " [red]no-plan[/red]"
        _console.print(
            f"  [{i}] [{colour}]{s.outcome}[/{colour}]  status={s.loop_status} "
            f"iters={s.iterations} tokens={s.in_tokens}/{s.out_tokens}{plan_tag}"
        )
        _console.print(f"      tools: {seq}")
        if s.assistant_preview:
            _console.print(f"      reply: {s.assistant_preview!r}")


# --------------------------------------------------------------- profile


@main.group("profile")
def profile_group() -> None:
    """Build a per-alias capability profile (Phase 12 task 24).

    Runs the existing eval suites + reads declared model metadata into a
    per-dimension profile (declared facts + measured grades carrying
    capability AND cost), writes it to
    ``$FITT_HOME/eval/<alias>-profile.md``, and diffs against the stored
    baseline — the regression-catcher for a model swap.

    Declared facts (context window, thinking/vision/tools, size) come free
    from Ollama ``/api/tags``; measured grades come from the realistic
    (tool-calling) and coding suites. VRAM / token cost are backlogged
    (the data model has the fields; the probes aren't wired)."""


@profile_group.command("alias")
@click.argument("alias")
@click.option("--samples", type=int, default=5, help="Multi-sample count per case (default: 5).")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=30.0,
    help="Per-case dispatch timeout in seconds (default: 30).",
)
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def profile_alias_cmd(
    alias: str,
    samples: int,
    timeout_s: float,
    config_file: Path | None,
) -> None:
    """Profile ALIAS: declared facts + measured tool-calling/coding grades,
    written + diffed against the stored baseline."""
    import asyncio
    import os
    from datetime import UTC, datetime

    import httpx as _httpx

    from .alias_eval import realistic_cases, run_eval_suite_multi
    from .alias_eval_coding import default_coding_cases
    from .app import create_app
    from .capabilities import build_capability_block
    from .capability_profile import (
        CapabilityProfile,
        declared_from_ollama_tags,
        grade_from_samples,
        load_baseline,
        render_diff_markdown,
        render_profile_markdown,
        write_profile,
    )
    from .config import fitt_home
    from .router import AliasRouter

    cfg = load_config(config_file or default_config_path(), default_secrets_path())
    if alias not in cfg.aliases:
        _console.print(
            f"[red]unknown alias[/red] {alias!r}. Configured aliases: {sorted(cfg.aliases)}"
        )
        sys.exit(2)

    os.environ.setdefault("FITT_SKIP_SHELL_PROBE", "1")
    app = create_app(cfg)
    registry = app.state.tool_registry
    router = AliasRouter(cfg)
    primary = router.resolve(alias)[0]
    system_prompt = build_capability_block(registry) if registry.list_names() else ""

    # Declared facts: free + static, Ollama backends only. A non-Ollama
    # backend (no /api/tags) just carries no declared block.
    declared: list[Any] = []
    resource: Any = None
    if primary.backend == "ollama":
        try:
            resp = _httpx.get(f"{primary.endpoint}/api/tags", timeout=10.0)
            resp.raise_for_status()
            declared, resource = declared_from_ollama_tags(resp.json(), primary.model)
        except Exception as exc:
            _console.print(f"[yellow]could not read declared metadata:[/yellow] {exc}")

    _console.print(
        f"[cyan]profiling[/cyan] {alias} (model=`{primary.id}`), "
        f"{samples} samples/case — this runs the suites live, give it a minute..."
    )

    async def _measure() -> tuple[list[Any], list[Any]]:
        tool = await run_eval_suite_multi(
            alias,
            router,
            cases=realistic_cases(),
            samples=samples,
            timeout_s=timeout_s,
            system_prompt=system_prompt,
        )
        # Coding cases embed their own realistic system prompt in the
        # prompt text, so no separate system_prompt here.
        coding = await run_eval_suite_multi(
            alias,
            router,
            cases=default_coding_cases(),
            samples=samples,
            timeout_s=timeout_s,
        )
        return tool, coding

    tool_results, coding_results = asyncio.run(_measure())

    def _suite_grade(name: str, results: list[Any]) -> Any:
        passes = sum(r.passes for r in results)
        valid = sum(r.valid for r in results)
        total = sum(r.total for r in results)
        latencies = [s.latency_ms for r in results for s in r.samples]
        return grade_from_samples(
            name,
            passes=passes,
            valid=valid,
            samples=total,
            latencies_ms=latencies,
        )

    profile = CapabilityProfile(
        alias=alias,
        model_id=primary.id,
        captured_at=datetime.now(UTC),
        declared=declared,
        measured=[
            _suite_grade("tool-calling", tool_results),
            _suite_grade("coding", coding_results),
        ],
        resource=resource,
    )

    # Load the baseline BEFORE writing (write overwrites it for next time).
    baseline = load_baseline(alias, fitt_home())
    md_path, _json_path = write_profile(profile, fitt_home())

    _console.print("")
    _console.print(render_profile_markdown(profile))
    if baseline is not None:
        _console.print("")
        _console.print(render_diff_markdown(profile.diff(baseline)))
    else:
        _console.print("\n[dim](no baseline yet — this run becomes the baseline)[/dim]")
    _console.print(f"\n[cyan]profile written[/cyan] {md_path}")


# --------------------------------------------------------------- fitt watch


@main.command("watch")
@click.argument("session_id", default="main")
@click.option("--config-file", type=click.Path(path_type=Path), default=None)
def watch_cmd(session_id: str, config_file: Path | None) -> None:
    """Tail a session's per-turn event stream.

    Prints one line per turn event with color-coded severity.
    Ctrl-C exits. The last hour of history is printed on
    startup before the tailing loop begins so the operator
    sees the context of the current turn immediately."""
    from .cli_watch import run_watch

    cfg = load_config(
        config_file or default_config_path(),
        default_secrets_path(),
        load_secrets_too=False,
    )
    sys.exit(run_watch(session_id, cfg.memory.sessions_dir))


# --------------------------------------------------------------- fitt tasks


@main.command("tasks")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Also list at-home/manual, deferred, and done tasks (not just ready open work).",
)
@click.option(
    "--specs-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to .kiro/specs (default: found by walking up from the cwd).",
)
def tasks_cmd(show_all: bool, specs_dir: Path | None) -> None:
    """Cross-phase rollup of the spec task lists - what's open everywhere.

    Reads .kiro/specs/<phase>/tasks.md (the single source of truth) and
    prints the genuinely-open `[ ]` tasks grouped by phase. At-home /
    manual runtime steps and deferred/reshaped items are counted but not
    listed (they aren't pickable dev work); pass --all to see them too.

    Run it from anywhere in the repo. This is a derived view - it never
    edits the specs, so it can't drift out of sync with them."""
    from .task_rollup import collect_statuses, collect_tasks, find_specs_dir, roll_up

    base = Path(specs_dir) if specs_dir else find_specs_dir(Path.cwd())
    if base is None or not base.is_dir():
        _console.print(
            "[red]could not find .kiro/specs[/red] - run from inside the repo, or pass --specs-dir."
        )
        sys.exit(2)

    tasks = collect_tasks(base)
    statuses = collect_statuses(base)
    rollups = roll_up(tasks, statuses)
    by_phase: dict[str, list[Any]] = {}
    for t in tasks:
        by_phase.setdefault(t.phase, []).append(t)

    actionable = sum(r.actionable_open for r in rollups)
    phases_actionable = sum(1 for r in rollups if r.actionable_open)
    collapsed = sum(1 for r in rollups if r.collapsed)

    _console.print(
        f"\n[bold]{actionable} open[/bold] in {phases_actionable} active/blocked "
        f"phase(s)  [dim]({collapsed} shipped/shelved phase(s) collapsed)[/dim]\n"
    )

    for r in rollups:
        if r.collapsed:
            if show_all:
                _console.print(
                    f"[dim]{r.phase} [{r.status}] - {r.done} done, "
                    f"{len(r.open_tasks)} unticked box(es) (historical)[/dim]"
                )
            continue
        # active / blocked: enumerate the open work.
        flag = " [yellow](blocked)[/yellow]" if r.status == "blocked" else ""
        extras: list[str] = []
        if r.done:
            extras.append(f"{r.done} done")
        if r.at_home:
            extras.append(f"{r.at_home} at-home")
        if r.deferred:
            extras.append(f"{r.deferred} deferred")
        extra_str = f"  [dim]({', '.join(extras)})[/dim]" if extras else ""
        _console.print(f"[cyan]{r.phase}[/cyan]{flag} - {len(r.open_tasks)} open{extra_str}")
        for t in r.open_tasks:
            title = t.title if len(t.title) <= 88 else t.title[:85] + "..."
            _console.print(f"    [ ] {t.id}. {title}")
        if show_all:
            for t in by_phase.get(r.phase, []):
                if t.kind == "open":
                    continue
                title = t.title if len(t.title) <= 80 else t.title[:77] + "..."
                box = "[x]" if t.kind == "done" else "[ ]"
                tag = "" if t.kind == "done" else f" [dim]({t.kind.replace('_', '-')})[/dim]"
                _console.print(f"    [dim]{box} {t.id}. {title}[/dim]{tag}")
        _console.print("")

    if actionable == 0 and not show_all:
        _console.print(
            "[dim]No actionable open tasks in active/blocked phases. Run with "
            "--all to see shipped/shelved phases, or see BACKLOG.md for "
            "cross-cutting work.[/dim]\n"
        )


# --------------------------------------------------------------- fitt context


@main.group("context")
def context_group() -> None:
    """Inspect and refresh per-binding context windows.

    Phase 7 Slice 7.1: the gateway discovers each model's
    effective context window at boot. These commands surface
    the cache and re-run discovery without a process restart
    (e.g. after raising Ollama's ``OLLAMA_CONTEXT_LENGTH`` on
    a satellite)."""


@context_group.command("list")
def context_list() -> None:
    """Print the discovered context window for every binding."""
    import httpx

    try:
        r = httpx.get(
            f"{_mcp_gateway_url()}/v1/aliases",
            headers={
                "Authorization": f"Bearer {_mcp_bearer_token()}",
                "X-FITT-Client": "cli",
            },
            timeout=10.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        _console.print(f"[red]Could not reach gateway: {e}[/red]")
        sys.exit(1)

    body = r.json()
    table = Table(title="Per-binding context windows")
    table.add_column("Alias", style="cyan")
    table.add_column("Model")
    table.add_column("Backend")
    table.add_column("Context", justify="right")
    table.add_column("Source")

    for entry in body.get("aliases", []):
        primary = entry.get("primary", {})
        cw = entry.get("context_window")
        if cw is None:
            tokens = "[dim](unknown)[/dim]"
            source = "[dim]—[/dim]"
        elif cw.get("tokens") is None:
            tokens = "[red](failed)[/red]"
            source = cw.get("source", "?")
        else:
            tokens = f"{cw['tokens']:,}"
            source = cw.get("source", "?")
            # Highlight ollama's 2048 default — that's the
            # granite-style "operator forgot to raise it" case.
            if source == "default":
                tokens = f"[yellow]{tokens}[/yellow]"
        table.add_row(
            entry.get("id", "?"),
            primary.get("model", "?"),
            primary.get("backend", "?"),
            tokens,
            source,
        )

    _console.print(table)


@context_group.command("refresh")
@click.option(
    "--alias",
    default=None,
    help=(
        "Refresh only the given alias's primary model. Without this flag, refreshes every binding."
    ),
)
def context_refresh(alias: str | None) -> None:
    """Re-run context-window discovery against the live gateway.

    Use this after changing a backend's config (Ollama
    ``OLLAMA_CONTEXT_LENGTH``, OpenRouter context_length
    metadata change) when you want the gateway to pick up the
    new value without restarting."""
    import httpx

    cfg = load_config(default_config_path(), default_secrets_path())
    body: dict[str, str] = {}
    if alias is not None:
        # CLI takes an alias for the operator-friendly UX;
        # internally the cache keys by model_id, so resolve.
        try:
            chain = cfg.resolve_alias(alias)
        except KeyError:
            _console.print(f"[red]Unknown alias: {alias}[/red]")
            sys.exit(1)
        body["model_id"] = chain[0].id

    try:
        r = httpx.post(
            f"{_mcp_gateway_url()}/v1/internal/context-refresh",
            json=body,
            headers={
                "Authorization": f"Bearer {_mcp_bearer_token()}",
                "X-FITT-Client": "cli",
            },
            timeout=30.0,  # discovery can take a moment
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        _console.print(f"[red]Could not reach gateway: {e}[/red]")
        sys.exit(1)

    payload = r.json()
    if "error" in payload:
        err = payload["error"]
        _console.print(f"[red]{err.get('type', 'error')}:[/red] {err.get('message', '?')}")
        sys.exit(1)

    refreshed = payload.get("refreshed", [])
    table = Table(title="Refreshed context windows")
    table.add_column("Model id", style="cyan")
    table.add_column("Tokens", justify="right")
    table.add_column("Source")
    table.add_column("Detail")
    for entry in refreshed:
        tokens = (
            f"{entry['tokens']:,}" if entry.get("tokens") is not None else "[red](unknown)[/red]"
        )
        table.add_row(
            entry.get("model_id", "?"),
            tokens,
            entry.get("source", "?"),
            entry.get("detail", ""),
        )
    _console.print(table)


# --------------------------------------------------------------- fitt turn


@main.group("turn")
def turn_group() -> None:
    """Inspect captured turns (Phase 7 Slice 7.2).

    Each tool-using turn writes a sidecar JSON capturing the
    dispatched message list, the upstream response, the tool-
    call chain, and prompt-fill metrics. ``fitt turn list``
    shows the recent ones for a session; ``fitt turn show``
    pulls up the full body for one turn id."""


@turn_group.command("list")
@click.argument("session", default="main")
@click.option(
    "--limit",
    "-n",
    type=int,
    default=20,
    help="Number of recent turns to show (default 20).",
)
def turn_list(session: str, limit: int) -> None:
    """Print recent captured turns for SESSION."""
    import httpx

    try:
        r = httpx.get(
            f"{_mcp_gateway_url()}/v1/sessions/{session}/captures",
            params={"limit": limit},
            headers={
                "Authorization": f"Bearer {_mcp_bearer_token()}",
                "X-FITT-Client": "cli",
            },
            timeout=10.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        _console.print(f"[red]Could not reach gateway: {e}[/red]")
        sys.exit(1)

    captures = r.json().get("captures", [])
    if not captures:
        _console.print(
            f"[dim]No captured turns for session {session!r}. "
            f"Either traceability is disabled, the session is new, "
            f"or all turns came from a coding-agent client (which "
            f"doesn't capture by default).[/dim]"
        )
        return

    table = Table(title=f"Recent captures in session {session}")
    table.add_column("Turn id", style="cyan")
    table.add_column("When", style="dim")
    table.add_column("Model")
    table.add_column("Prompt", justify="right")
    table.add_column("Window %", justify="right")
    table.add_column("Tools", justify="right")
    table.add_column("Status")
    for entry in captures:
        from datetime import datetime as _dt

        ts = entry.get("started_at")
        when = _dt.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"
        pct = entry.get("prompt_pct_of_window")
        pct_str = f"{pct:.0f}%" if isinstance(pct, (int, float)) else "—"
        if isinstance(pct, (int, float)) and pct > 80:
            pct_str = f"[yellow]{pct_str}[/yellow]"
        warn = "⚠" if entry.get("narration_warning") else ""
        status = entry.get("status", "?")
        status_styled = f"[red]{status}[/red]" if status != "ok" else f"[green]{status}[/green]"
        table.add_row(
            entry.get("turn_id", "?")[:12],
            when,
            entry.get("model_used", "?"),
            f"{entry.get('prompt_tokens', 0):,}",
            pct_str,
            f"{entry.get('tool_calls_count', 0)}{warn}",
            status_styled,
        )
    _console.print(table)


@turn_group.command("show")
@click.argument("turn_id")
@click.option(
    "--session",
    default="main",
    help="Session containing the turn (default: main).",
)
def turn_show(turn_id: str, session: str) -> None:
    """Print the full capture for TURN_ID, including the
    dispatched messages, response, and tool calls.

    Use ``fitt turn list`` to find a turn id; the X-FITT-Turn-Id
    header on chat responses also carries it."""
    import json as _json

    import httpx

    try:
        r = httpx.get(
            f"{_mcp_gateway_url()}/v1/sessions/{session}/captures/{turn_id}",
            headers={
                "Authorization": f"Bearer {_mcp_bearer_token()}",
                "X-FITT-Client": "cli",
            },
            timeout=10.0,
        )
        if r.status_code == 404:
            _console.print(
                f"[red]Capture not found:[/red] turn {turn_id!r} in session {session!r}.\n"
                f"[dim]The turn may not exist, may be too old to be retained, "
                f"or capture may have been disabled for the originating client.[/dim]"
            )
            sys.exit(1)
        r.raise_for_status()
    except httpx.HTTPError as e:
        _console.print(f"[red]Could not reach gateway: {e}[/red]")
        sys.exit(1)

    cap = r.json()
    _console.print(f"[bold cyan]Turn {cap.get('turn_id')}[/bold cyan]")
    _console.print(
        f"  alias={cap.get('alias')} → model={cap.get('model_used')} ({cap.get('backend')})"
    )
    cw = cap.get("context_window") or 0
    pct = cap.get("prompt_pct_of_window") or 0
    _console.print(f"  prompt={cap.get('prompt_tokens'):,} / window={cw:,} ({pct:.1f}%)")
    _console.print(
        f"  finish_reason={cap.get('finish_reason')} "
        f"iterations={cap.get('iterations')} "
        f"status={cap.get('status')}"
    )
    if cap.get("narration_warning"):
        _console.print(
            "  [yellow]⚠ narration warning: shape suggests model narrated "
            "a tool call instead of emitting one[/yellow]"
        )
    _console.print()

    tool_calls = cap.get("tool_calls", [])
    if tool_calls:
        _console.print(f"[bold]Tool calls ({len(tool_calls)}):[/bold]")
        for tc in tool_calls:
            ok = tc.get("ok")
            icon = "✅" if ok else "❌"
            args_str = _json.dumps(tc.get("args", {}))[:120]
            summary = (tc.get("result_summary", "") or "")[:80]
            _console.print(f"  {icon} {tc.get('tool_name')}({args_str}) → {summary}")
        _console.print()

    _console.print("[bold]Dispatched messages:[/bold]")
    for msg in cap.get("dispatched_messages", []):
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            preview = content[:200] + ("..." if len(content) > 200 else "")
        else:
            preview = "(non-text content)"
        # Escape role brackets so Rich doesn't interpret them as
        # markup tags.
        _console.print(f"  \\[{role}] {preview}")
    _console.print()

    response = cap.get("response", {})
    choices = response.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        content = msg.get("content") or ""
        _console.print("[bold]Final response:[/bold]")
        _console.print(f"  {content[:500]}")


if __name__ == "__main__":
    main()

"""The concrete contract checks, and the fixtures they need.

Split from :mod:`tool_contracts` (the runner) so the runner stays pure and
unit-testable while this module owns the messy part: a real app against a
temp FITT home, a registered project, a git repo, a file tree.

The context is built the way the *live* paths build it (see
``cron_runner`` and ``chat._tool_ctx``) rather than hand-assembled per
check. A hand-assembled context is a second implementation of the wiring,
and a check that passes against a fake wiring proves less than nothing.

See `.kiro/specs/e2e-full-coverage/design.md` (D2).
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .tool_contracts import ContractCheck

# Tools we deliberately don't contract-check, each with a reason, so the
# gap between "chose not to" and "forgot" stays visible (R2.3).
EXEMPT: dict[str, str] = {
    "send_message": (
        "covered by a judged scenario with a recording sink instead — the "
        "interesting part is whether FITT decides to send the right thing"
    ),
    "memory_search": (
        "covered by the judged cross-session recall scenario, which is the "
        "only place scope='all' behaviour is meaningful"
    ),
    "web_search": (
        "hits the network; covered by the judged news_summary scenario and "
        "by test_web_search_e2e against a stub provider"
    ),
}


def build_project(tmp_home: Path) -> Path:
    """A small file tree the read-side tools can work against."""
    root = tmp_home / "sample-project"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\n\nMARKER = 'contract-probe'\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# sample\n\nA fixture project.\n", encoding="utf-8")
    return root


def init_git_repo(root: Path) -> None:
    """Make the fixture project a real git repo.

    The git tools shell out to git, so a fake directory would only prove
    the error path. Committer identity is set locally (never --global:
    the conventions forbid touching the operator's git config)."""

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )

    _git("init", "-q")
    _git("config", "user.email", "contract@example.invalid")
    _git("config", "user.name", "Contract Fixture")
    _git("add", ".")
    _git("commit", "-q", "-m", "fixture baseline")


def read_side_checks(project: str) -> list[ContractCheck]:
    """Read-mostly tools — the safe half of the surface, and the half the
    scope doc calls a core use case."""
    return [
        ContractCheck(
            tool="read_file",
            valid_args=lambda ctx: {"project": project, "path": "src/app.py"},
            side_effect=lambda res, ctx: _assert_contains(res, "MARKER"),
            invalid_args={"project": project, "path": "../../etc/passwd"},
        ),
        ContractCheck(
            tool="list_directory",
            valid_args=lambda ctx: {"project": project, "path": "."},
            side_effect=lambda res, ctx: _assert_contains(res, "src"),
            invalid_args={"project": project, "path": "no/such/dir"},
        ),
        ContractCheck(
            tool="glob_search",
            valid_args=lambda ctx: {"project": project, "pattern": "*.py"},
            side_effect=lambda res, ctx: _assert_contains(res, "app.py"),
            invalid_args={"project": "no-such-project", "pattern": "*"},
            # known_broken removed 2026-08-14: a local project is now
            # walked in Python instead of shelling out to `find`, so the
            # Windows FIND.EXE collision this layer found is gone.
        ),
        ContractCheck(
            tool="grep_repo",
            valid_args=lambda ctx: {"project": project, "pattern": "contract-probe"},
            side_effect=lambda res, ctx: _assert_contains(res, "app.py"),
            invalid_args={"project": "no-such-project", "pattern": "x"},
        ),
        ContractCheck(
            tool="list_capabilities",
            valid_args=lambda ctx: {},
            invalid_args=None,  # no arguments to get wrong
        ),
    ]


def state_side_checks(project: str) -> list[ContractCheck]:
    """Tools that mutate FITT's own state. Each works on a row it creates
    itself, so the suite is order-independent and repeatable (Property 4)."""
    return [
        ContractCheck(
            tool="todo_list",
            valid_args=lambda ctx: {},
            invalid_args=None,
        ),
        ContractCheck(
            tool="cron_list",
            valid_args=lambda ctx: {},
            invalid_args=None,
        ),
        ContractCheck(
            tool="learn_add",
            valid_args=lambda ctx: {"text": "contract fixture lesson"},
            side_effect=_assert_lesson_present,
            invalid_args={"text": ""},
        ),
        ContractCheck(
            tool="learn_list",
            valid_args=lambda ctx: {},
            invalid_args=None,
        ),
        ContractCheck(
            tool="learn_remove",
            # Removes what learn_add above planted. Ordering within this
            # list is fine; ordering against *other* checks is not, which
            # is why nothing here touches shared state it didn't create.
            valid_args=lambda ctx: {"substring": "contract fixture lesson"},
            invalid_args={"substring": ""},
        ),
    ]


@contextmanager
def stub_http_server() -> Iterator[str]:
    """A local one-route HTTP server, yielding its base URL.

    `http_get` is an in-process httpx call, so checking it needs a real
    listening socket. A local stub rather than a public URL keeps the
    layer deterministic and offline (Property 2) — a check that fails
    because someone's wifi dropped teaches nothing."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"contract": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            """Silence the default stderr access log."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Read only the port: we bound 127.0.0.1 explicitly, and
        # server_address[0] is typed str|bytes.
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def http_checks(base_url: str) -> list[ContractCheck]:
    return [
        ContractCheck(
            tool="http_get",
            valid_args=lambda ctx: {"url": f"{base_url}/probe"},
            side_effect=lambda res, ctx: _assert_contains(res, "contract"),
            # Not a bad host (that's a network timeout, slow and flaky) —
            # a URL the tool should reject before making any request.
            invalid_args={"url": "not-a-url"},
        )
    ]


def todo_checks() -> list[ContractCheck]:
    """The todo lifecycle, each step on the row the previous one made."""
    item = "contract fixture todo"
    return [
        ContractCheck(
            tool="todo_add",
            valid_args=lambda ctx: {"text": item},
            side_effect=lambda res, ctx: _assert_todo(ctx, item, present=True),
            invalid_args={"text": ""},
        ),
        ContractCheck(
            tool="todo_done",
            valid_args=lambda ctx: {"substring": item},
            invalid_args={"substring": ""},
        ),
        ContractCheck(
            tool="todo_remove",
            valid_args=lambda ctx: {"substring": item},
            side_effect=lambda res, ctx: _assert_todo(ctx, item, present=False),
            invalid_args={"substring": ""},
        ),
        ContractCheck(
            tool="todowrite",
            valid_args=lambda ctx: {
                "todos": [{"text": "contract fixture plan step", "status": "pending"}]
            },
            invalid_args={"todos": "not a list"},
        ),
    ]


def cron_checks() -> list[ContractCheck]:
    """Cron mutations. `cron_add` seeds the job the rest operate on, and
    the ids are read back from the service so nothing hardcodes state."""
    return [
        ContractCheck(
            tool="cron_add",
            valid_args=lambda ctx: {
                "text": "contract fixture reminder",
                "schedule_spec": "in 2 hours",
            },
            side_effect=lambda res, ctx: _assert_cron(ctx, present=True),
            invalid_args={"text": "x", "schedule_spec": "not a schedule at all"},
        ),
        ContractCheck(
            tool="cron_pause",
            valid_args=lambda ctx: {"id": _fixture_cron_id(ctx)},
            invalid_args={"id": "no-such-job"},
        ),
        ContractCheck(
            tool="cron_resume",
            valid_args=lambda ctx: {"id": _fixture_cron_id(ctx)},
            invalid_args={"id": "no-such-job"},
        ),
        ContractCheck(
            tool="cron_update",
            valid_args=lambda ctx: {
                "id": _fixture_cron_id(ctx),
                "text": "contract fixture reminder, edited",
            },
            invalid_args={"id": "no-such-job", "text": "x"},
        ),
        ContractCheck(
            tool="cron_remove",
            valid_args=lambda ctx: {"id": _fixture_cron_id(ctx)},
            side_effect=lambda res, ctx: _assert_cron(ctx, present=False),
            invalid_args={"id": "no-such-job"},
        ),
    ]


def write_side_checks(project: str) -> list[ContractCheck]:
    """Coverage only, per the scope note — prove they work, invest nothing.

    They write into the fixture project, which is a temp tree, so nothing
    here touches anything real."""
    return [
        ContractCheck(
            tool="write_file",
            valid_args=lambda ctx: {
                "project": project,
                "path": "contract.txt",
                "content": "written by the contract suite\n",
            },
            invalid_args={"project": project, "path": "../escape.txt", "content": "x"},
        ),
        ContractCheck(
            tool="edit_file",
            valid_args=lambda ctx: {
                "project": project,
                "path": "contract.txt",
                "old_str": "written",
                "new_str": "edited",
            },
            invalid_args={
                "project": project,
                "path": "contract.txt",
                "old_str": "string that is definitely not present",
                "new_str": "x",
            },
        ),
        ContractCheck(
            tool="git_commit",
            valid_args=lambda ctx: {"project": project, "message": "contract fixture commit"},
            invalid_args={"project": "no-such-project", "message": "x"},
        ),
        ContractCheck(
            tool="run_tests",
            valid_args=lambda ctx: {"project": project},
            # The fixture project has no tests; a non-zero exit is a
            # legitimate tool result, not a contract violation, so only
            # the error path is asserted here.
            skip_reason=(
                "needs a project with a real passing test command; the "
                "fixture tree has none, and a failing test run is a valid "
                "tool result rather than a contract breach"
            ),
        ),
        ContractCheck(
            tool="project_shell",
            valid_args=lambda ctx: {"project": project, "command": "echo contract"},
            invalid_args={"project": "no-such-project", "command": "echo x"},
            known_broken=(
                "genuinely needs a working POSIX shell on the hub, so this "
                "fails on a host without one — a deployment fact, not a code "
                "defect. The *caching* half was fixed 2026-08-14: a failed "
                "probe is now retried after 60s instead of disabling "
                "project_shell until the gateway restarts. Git Bash on this "
                "host still intermittently fails to fork (cygwin Win32 error "
                "299/5), which is an environment problem"
            ),
        ),
    ]


def spec_checks(project: str) -> list[ContractCheck]:
    """The spec tools read `.kiro/specs/`. The fixture project has none,
    so an empty-but-clean result is the contract here."""
    return [
        ContractCheck(
            tool="spec_list",
            valid_args=lambda ctx: {"project": project},
            invalid_args={"project": "no-such-project"},
        ),
        ContractCheck(
            tool="spec_read",
            valid_args=lambda ctx: {"project": project, "feature": "no-such-feature"},
            skip_reason=(
                "reading a nonexistent spec is the error path, and the fixture "
                "tree has no .kiro/specs to read a real one from"
            ),
        ),
        ContractCheck(
            tool="spec_next_task",
            valid_args=lambda ctx: {"project": project, "feature": "no-such-feature"},
            skip_reason="same as spec_read: no fixture spec to advance",
        ),
        ContractCheck(
            tool="spec_mark_task",
            valid_args=lambda ctx: {
                "project": project,
                "feature": "no-such-feature",
                "task_id": "1",
            },
            skip_reason="same as spec_read: no fixture spec to mark",
        ),
    ]


def _assert_todo(ctx: Any, item: str, *, present: bool) -> None:
    texts = [t.text for t in ctx.todos.read()]
    if present:
        assert any(item in t for t in texts), f"todo not stored: {texts!r}"
    else:
        assert not any(item in t for t in texts), f"todo still present after removal: {texts!r}"


def _assert_cron(ctx: Any, *, present: bool) -> None:
    jobs = [j for j in ctx.cron.list(include_disabled=True) if "contract fixture" in j.message]
    if present:
        assert jobs, "cron job was not created"
    else:
        assert not jobs, "cron job still present after removal"


def _fixture_cron_id(ctx: Any) -> str:
    """Look the fixture job's id up rather than hardcoding one."""
    for job in ctx.cron.list(include_disabled=True):
        if "contract fixture" in job.message:
            return str(job.id)
    raise AssertionError("fixture cron job missing — cron_add's check must run first")


def git_side_checks(project: str) -> list[ContractCheck]:
    """Coverage only, per the scope note: prove they work, invest nothing."""
    return [
        ContractCheck(
            tool="git_status",
            valid_args=lambda ctx: {"project": project},
            invalid_args={"project": "no-such-project"},
        ),
        ContractCheck(
            tool="git_diff",
            valid_args=lambda ctx: {"project": project},
            invalid_args={"project": "no-such-project"},
        ),
    ]


def _assert_contains(result: Any, needle: str) -> None:
    assert needle in result.payload, f"expected {needle!r} in payload, got {result.payload[:200]!r}"


def _assert_lesson_present(result: Any, ctx: Any) -> None:
    """The point of learn_add is the durable side effect, not the reply."""
    block = ctx.lessons.render_block()
    assert "contract fixture lesson" in block, f"lesson not in store: {block[:200]!r}"


def default_checks(project: str, *, http_base_url: str | None = None) -> list[ContractCheck]:
    """Every check this module knows about.

    Without a project, the file and git checks can't run at all — so they
    are *skipped with a reason*, not failed. A missing fixture says
    nothing about the tool, the same distinction the judged layer draws
    with unsupported/inconclusive."""
    needs_project = [
        *read_side_checks(project),
        *git_side_checks(project),
        *write_side_checks(project),
        *spec_checks(project),
    ]
    if not project:
        needs_project = [
            replace(
                c,
                skip_reason="no project registered — pass --project or `fitt project add`",
            )
            if c.tool != "list_capabilities"
            else c
            for c in needs_project
        ]
    http = (
        http_checks(http_base_url)
        if http_base_url
        else [
            ContractCheck(
                tool="http_get",
                valid_args=lambda ctx: {},
                skip_reason="no stub server supplied — see stub_http_server()",
            )
        ]
    )
    return [
        *needs_project,
        *state_side_checks(project),
        *todo_checks(),
        *cron_checks(),
        *http,
    ]

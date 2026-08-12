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
from dataclasses import replace
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
            known_broken=(
                "on a Windows hub `find` resolves to Windows FIND.EXE, so the "
                "tool returns 'FIND: Parameter format not correct' instead of "
                "matches. Found by this layer 2026-08-12; see observed-issues"
            ),
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


def default_checks(project: str) -> list[ContractCheck]:
    """Every check this module knows about.

    Without a project, the file and git checks can't run at all — so they
    are *skipped with a reason*, not failed. A missing fixture says
    nothing about the tool, the same distinction the judged layer draws
    with unsupported/inconclusive."""
    needs_project = [*read_side_checks(project), *git_side_checks(project)]
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
    return [*needs_project, *state_side_checks(project)]

"""The contract runner itself, against a fake registry.

Point of these tests: prove the runner catches the failure mode that
matters — a tool that *raises* on bad arguments instead of returning
ToolResult.error. That escapes the agent loop's error handling and kills
the turn, and no judged scenario reliably finds it because models rarely
send malformed arguments on purpose.
"""

from __future__ import annotations

from typing import Any

from gateway.tool_contracts import (
    ContractCheck,
    run_contract_check,
    run_contract_checks,
)
from gateway.tools import ApprovalBucket, Tool, ToolRegistry, ToolResult


def _tool(name: str, fn: Any) -> Tool:
    return Tool(
        name=name,
        description=name,
        schema={"type": "object", "properties": {}},
        callable=fn,
        default_bucket=ApprovalBucket.AUTO,
    )


def _registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


async def _well_behaved(args: dict[str, Any], ctx: Any) -> ToolResult:
    if "path" not in args:
        return ToolResult.error("path is required")
    return ToolResult.ok(f"read {args['path']}")


async def _raises_on_bad_args(args: dict[str, Any], ctx: Any) -> ToolResult:
    return ToolResult.ok(f"read {args['path']}")  # KeyError when absent


async def _always_errors(args: dict[str, Any], ctx: Any) -> ToolResult:
    return ToolResult.error("nope")


async def _returns_wrong_type(args: dict[str, Any], ctx: Any) -> Any:
    return "just a string"


def _check(tool: str, *, invalid: dict[str, Any] | None = None) -> ContractCheck:
    return ContractCheck(
        tool=tool,
        valid_args=lambda ctx: {"path": "f.txt"},
        invalid_args=invalid if invalid is not None else {},
    )


async def test_well_behaved_tool_passes() -> None:
    res = await run_contract_check(_check("read_file"), _tool("read_file", _well_behaved), None)

    assert res.passed, res.detail


async def test_tool_that_raises_on_bad_args_fails() -> None:
    """The failure this layer exists for."""
    res = await run_contract_check(
        _check("read_file"), _tool("read_file", _raises_on_bad_args), None
    )

    assert not res.passed
    assert "raised on invalid args" in res.detail


async def test_tool_that_errors_on_valid_args_fails() -> None:
    res = await run_contract_check(_check("read_file"), _tool("read_file", _always_errors), None)

    assert not res.passed
    assert "errored on valid args" in res.detail


async def test_tool_returning_a_non_toolresult_fails() -> None:
    res = await run_contract_check(
        _check("read_file"), _tool("read_file", _returns_wrong_type), None
    )

    assert not res.passed
    assert "not ToolResult" in res.detail


async def test_tool_reporting_success_on_invalid_args_fails() -> None:
    async def _too_permissive(args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult.ok("sure, whatever")

    res = await run_contract_check(_check("read_file"), _tool("read_file", _too_permissive), None)

    assert not res.passed
    assert "instead of an error" in res.detail


async def test_side_effect_assertion_is_honoured() -> None:
    def _missing(result: ToolResult, ctx: Any) -> None:
        raise AssertionError("file was not created")

    check = ContractCheck(
        tool="write_file",
        valid_args=lambda ctx: {"path": "f.txt"},
        side_effect=_missing,
        invalid_args={},
    )

    res = await run_contract_check(check, _tool("write_file", _well_behaved), None)

    assert not res.passed
    assert "side effect missing" in res.detail


async def test_check_for_an_unregistered_tool_is_skipped_not_failed() -> None:
    """Retrieval tools only exist when retrieval is configured; a
    switched-off feature is a deployment fact, not a defect."""
    report = await run_contract_checks(
        _registry(_tool("read_file", _well_behaved)),
        None,
        [_check("read_file"), _check("memory_search")],
    )

    by_tool = {r.tool: r for r in report.results}
    assert by_tool["memory_search"].skipped
    assert by_tool["memory_search"].passed  # skipped is not a failure
    assert report.skipped_count == 1


async def test_registered_tools_without_a_check_are_reported() -> None:
    """Coverage is derived from the registry, so a newly registered tool
    starts life visibly uncovered."""
    report = await run_contract_checks(
        _registry(_tool("read_file", _well_behaved), _tool("brand_new", _well_behaved)),
        None,
        [_check("read_file")],
    )

    assert report.unchecked == ["brand_new"]


async def test_exempt_tools_are_not_reported_as_unchecked() -> None:
    report = await run_contract_checks(
        _registry(_tool("read_file", _well_behaved), _tool("dangerous", _well_behaved)),
        None,
        [_check("read_file")],
        exempt={"dangerous": "needs a live remote"},
    )

    assert report.unchecked == []


async def test_tool_with_no_arguments_skips_the_invalid_half() -> None:
    async def _no_args(args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult.ok("fine")

    check = ContractCheck(tool="list_capabilities", valid_args=lambda ctx: {}, invalid_args=None)

    res = await run_contract_check(check, _tool("list_capabilities", _no_args), None)

    assert res.passed


async def test_report_render_names_failures_and_gaps() -> None:
    report = await run_contract_checks(
        _registry(_tool("read_file", _raises_on_bad_args), _tool("brand_new", _well_behaved)),
        None,
        [_check("read_file")],
    )

    rendered = report.render()

    assert "read_file: FAIL" in rendered
    assert "no check: brand_new" in rendered


# ------------------------------------------- the mutating-project guard
#
# `fitt eval contracts` runs against an OPERATOR-REGISTERED project — there
# is no throwaway fixture in that path. Pointed at a real repository,
# write_file and edit_file create files in it and git_commit makes a real
# commit. On 2026-08-14 a verification run against this repo committed
# `contract.txt` and swept up the author's whole uncommitted working tree
# under the message "contract fixture commit". Nothing was lost, but only
# because the sweep happened to be benign.


def _by_tool(checks: list) -> dict:  # type: ignore[type-arg]
    return {c.tool: c for c in checks}


def test_mutating_checks_are_skipped_by_default() -> None:
    from gateway.tool_contract_suite import MUTATES_THE_PROJECT, default_checks

    checks = _by_tool(default_checks("some-project", http_base_url="http://x"))

    for name in MUTATES_THE_PROJECT:
        assert name in checks, f"{name} vanished from the suite"
        assert checks[name].skip_reason, f"{name} would mutate the project by default"
        assert "--allow-project-writes" in checks[name].skip_reason


def test_git_commit_is_treated_as_mutating() -> None:
    """The specific one that wrote history."""
    from gateway.tool_contract_suite import MUTATES_THE_PROJECT

    assert "git_commit" in MUTATES_THE_PROJECT


def test_opting_in_runs_the_mutating_checks() -> None:
    from gateway.tool_contract_suite import MUTATES_THE_PROJECT, default_checks

    checks = _by_tool(
        default_checks("some-project", http_base_url="http://x", allow_project_writes=True)
    )

    for name in MUTATES_THE_PROJECT:
        assert not checks[name].skip_reason, f"{name} stayed skipped despite the opt-in"


def test_read_only_checks_are_unaffected_by_the_guard() -> None:
    """The point of the default is to keep the suite useful, not to gut it."""
    from gateway.tool_contract_suite import default_checks

    checks = _by_tool(default_checks("some-project", http_base_url="http://x"))

    for name in ("read_file", "list_directory", "grep_repo", "glob_search", "git_status"):
        assert not checks[name].skip_reason, f"{name} should still run"


def test_no_project_still_skips_everything_that_needs_one() -> None:
    """The pre-existing behaviour must survive the new guard."""
    from gateway.tool_contract_suite import default_checks

    checks = _by_tool(default_checks("", http_base_url="http://x"))

    assert checks["read_file"].skip_reason
    assert "no project registered" in checks["read_file"].skip_reason

"""Tests for :mod:`gateway.alias_eval` — the alias eval harness.

Four concerns:

* Per-case classification matches the shape-level rules. Happy
  path (right tool called), wrong_tool, narrated (positive
  case with text instead of tool_calls), no_tool_expected_but_called
  (negative case with a tool), truncated, empty_reply, and the
  shared dispatch-failure taxonomy (upstream_silent / unreachable
  / upstream_server_error).
* Suite aggregation: pass / fail counts, pass_rate math,
  model_id capture.
* Report rendering: human-readable markdown with the fields
  the operator needs (latency, tool_called, finish_reason,
  reply_preview for failures).
* Persistence: both timestamped and rolling files get
  written; the rolling file is what overwrites on re-runs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from gateway.alias_eval import (
    CaseResult,
    EvalCase,
    EvalReport,
    default_cases,
    default_eval_dir,
    render_report_markdown,
    run_eval_case,
    run_eval_suite,
    write_report,
)
from gateway.config import (
    AllowedToken,
    Config,
    LoggingConfig,
    MemoryConfig,
    ModelConfig,
    Secrets,
    ServerConfig,
)
from gateway.router import DispatchResult

# --------------------------------------------------------------- scaffolding


def _cfg(tmp_path: Path) -> Config:
    fitt_home = tmp_path / "fitt"
    fitt_home.mkdir(exist_ok=True)
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=8080),
        aliases={"fitt-smart": "nim-deepseek"},
        models=[
            ModelConfig(
                id="nim-deepseek",
                backend="openai",
                endpoint="https://integrate.api.nvidia.com/v1",
                model="deepseek-ai/deepseek-v4-flash",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            ),
        ],
        logging=LoggingConfig(dir=tmp_path / "logs", retention_days=7),
        memory=MemoryConfig(
            enabled=False,
            identity_dir=fitt_home / "identity",
            sessions_dir=fitt_home / "sessions",
        ),
    )
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="t", token="T" * 32)],
        api_keys={"nim-deepseek": "nvapi-test"},
    )
    return cfg


def _make_response(
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    content: str | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
        msg["content"] = None
    else:
        msg["content"] = content or ""
    return {
        "id": "r1",
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": ("tool_calls" if tool_calls is not None else finish_reason),
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 10},
    }


class _StubRouter:
    """Per-(alias, case-prompt) response stubbing. Cases don't
    share prompts across the default suite so prompt-keying is
    unambiguous."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._by_prompt: dict[str, Any] = {}

    def set_for_prompt(self, prompt: str, response: Any) -> None:
        self._by_prompt[prompt] = response

    async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
        user = body["messages"][0]["content"]
        r = self._by_prompt.get(user)
        if r is None:
            raise AssertionError(f"no stub configured for prompt: {user!r}")
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return await r()
        primary = self._config.resolve_alias(alias)[0]
        return DispatchResult(response=r, stream=None, model_used=primary, fallback_used=False)

    def resolve(self, alias: str) -> list[ModelConfig]:
        return self._config.resolve_alias(alias)


def _read_file_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


# --------------------------------------------------------------- per-case


async def test_positive_case_passes_when_expected_tool_called(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t1",
        prompt="read the file",
        tools=[_read_file_tool()],
        expected_tool="read_file",
    )
    router.set_for_prompt(
        case.prompt,
        _make_response(
            tool_calls=[
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        ),
    )

    r = await run_eval_case(case, "fitt-smart", router)  # type: ignore[arg-type]
    assert r.status == "pass"
    assert r.tool_called == "read_file"


async def test_system_prompt_lands_as_leading_system_message(
    tmp_path: Path,
) -> None:
    """The realistic suite passes a ``system_prompt``; it must
    arrive as a role=system message before the user prompt, so
    the model sees the prompt-size pressure live chat applies."""
    cfg = _cfg(tmp_path)

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, config: Config) -> None:
            self._config = config

        async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
            captured["messages"] = body["messages"]
            primary = self._config.resolve_alias(alias)[0]
            return DispatchResult(
                response=_make_response(
                    tool_calls=[
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ]
                ),
                stream=None,
                model_used=primary,
                fallback_used=False,
            )

        def resolve(self, alias: str) -> list[ModelConfig]:
            return self._config.resolve_alias(alias)

    router = _CapturingRouter(cfg)
    case = EvalCase(
        name="t1",
        prompt="read the file",
        tools=[_read_file_tool()],
        expected_tool="read_file",
    )
    r = await run_eval_case(
        case,
        "fitt-smart",
        router,  # type: ignore[arg-type]
        system_prompt="[Capabilities] You can call these tools: ...",
    )
    assert r.status == "pass"
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert "Capabilities" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "read the file"


async def test_no_system_prompt_means_user_message_first(
    tmp_path: Path,
) -> None:
    """Default/coding suites pass no system_prompt; the user
    message stays at index 0 (the StubRouter relies on this)."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t1",
        prompt="read the file",
        tools=[_read_file_tool()],
        expected_tool="read_file",
    )
    router.set_for_prompt(
        case.prompt,
        _make_response(
            tool_calls=[
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        ),
    )
    # No system_prompt → StubRouter's messages[0] keying works.
    r = await run_eval_case(case, "fitt-smart", router)  # type: ignore[arg-type]
    assert r.status == "pass"


async def test_positive_case_fails_when_wrong_tool_called(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t2",
        prompt="read the file",
        tools=[_read_file_tool()],
        expected_tool="read_file",
    )
    router.set_for_prompt(
        case.prompt,
        _make_response(
            tool_calls=[
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "grep_repo", "arguments": "{}"},
                }
            ]
        ),
    )

    r = await run_eval_case(case, "fitt-smart", router)  # type: ignore[arg-type]
    assert r.status == "wrong_tool"
    assert "grep_repo" in r.detail
    assert r.tool_called == "grep_repo"


async def test_positive_case_narrated_when_text_reply(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t3",
        prompt="read the file",
        tools=[_read_file_tool()],
        expected_tool="read_file",
    )
    router.set_for_prompt(
        case.prompt,
        _make_response(
            content=(
                "Sure, I'll read the file for you now. The contents "
                "appear to show a README with some documentation."
            )
        ),
    )

    r = await run_eval_case(case, "fitt-smart", router)  # type: ignore[arg-type]
    assert r.status == "narrated"
    assert "README" in r.reply_preview


async def test_positive_case_truncated_on_length_finish(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t4",
        prompt="read the file",
        tools=[_read_file_tool()],
        expected_tool="read_file",
    )
    router.set_for_prompt(
        case.prompt,
        _make_response(
            content="here is the first part of the reply",
            finish_reason="length",
        ),
    )

    r = await run_eval_case(case, "fitt-smart", router)  # type: ignore[arg-type]
    assert r.status == "truncated"
    assert r.finish_reason == "length"


async def test_negative_case_passes_when_no_tool_called(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t5",
        prompt="2+2",
        tools=[_read_file_tool()],
        expected_tool=None,
    )
    router.set_for_prompt(case.prompt, _make_response(content="4"))

    r = await run_eval_case(case, "fitt-smart", router)  # type: ignore[arg-type]
    assert r.status == "pass"


async def test_negative_case_fails_when_tool_unexpectedly_called(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t6",
        prompt="2+2",
        tools=[_read_file_tool()],
        expected_tool=None,
    )
    router.set_for_prompt(
        case.prompt,
        _make_response(
            tool_calls=[
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        ),
    )

    r = await run_eval_case(case, "fitt-smart", router)  # type: ignore[arg-type]
    assert r.status == "no_tool_expected_but_called"
    assert r.tool_called == "read_file"


async def test_dispatch_exception_becomes_server_error(tmp_path: Path) -> None:
    """A bare (non-HTTP) dispatch exception classifies via the
    shared taxonomy as ``upstream_server_error`` — the catch-all
    for transport failures with no status code (Phase 7.6)."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t7",
        prompt="read",
        tools=[_read_file_tool()],
        expected_tool="read_file",
    )
    router.set_for_prompt(case.prompt, RuntimeError("connection refused"))

    r = await run_eval_case(case, "fitt-smart", router)  # type: ignore[arg-type]
    assert r.status == "upstream_server_error"
    assert "connection refused" in r.detail


async def test_timeout_reachable_becomes_upstream_silent(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A timeout whose endpoint still answers a reachability
    ping is ``upstream_silent`` (slow / cold-loading), not a
    transport failure (Phase 7.6 Decision 2)."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t8",
        prompt="read",
        tools=[_read_file_tool()],
        expected_tool="read_file",
    )

    async def slow() -> DispatchResult:
        await asyncio.sleep(10.0)
        return DispatchResult(None, None, cfg.models[0], False)

    router.set_for_prompt(case.prompt, slow)

    from gateway import alias_eval
    from gateway.reachability import ReachabilityResult

    async def fake_reachable(model: Any, **_: Any) -> ReachabilityResult:
        return ReachabilityResult(model.id, True, 42)

    monkeypatch.setattr(alias_eval, "check_reachable_standalone", fake_reachable)

    r = await run_eval_case(
        case,
        "fitt-smart",
        router,
        timeout_s=0.05,  # type: ignore[arg-type]
    )
    assert r.status == "upstream_silent"
    assert r.reachable is True
    assert "reachable" in r.detail


async def test_timeout_unreachable_becomes_unreachable(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A timeout whose endpoint also fails the reachability ping
    is ``unreachable`` (host down)."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    case = EvalCase(
        name="t9",
        prompt="read",
        tools=[_read_file_tool()],
        expected_tool="read_file",
    )

    async def slow() -> DispatchResult:
        await asyncio.sleep(10.0)
        return DispatchResult(None, None, cfg.models[0], False)

    router.set_for_prompt(case.prompt, slow)

    from gateway import alias_eval
    from gateway.reachability import ReachabilityResult

    async def fake_unreachable(model: Any, **_: Any) -> ReachabilityResult:
        return ReachabilityResult(model.id, False, 2500, detail="connect timeout")

    monkeypatch.setattr(alias_eval, "check_reachable_standalone", fake_unreachable)

    r = await run_eval_case(
        case,
        "fitt-smart",
        router,
        timeout_s=0.05,  # type: ignore[arg-type]
    )
    assert r.status == "unreachable"
    assert r.reachable is False
    assert "unreachable" in r.detail


# --------------------------------------------------------------- suite


async def test_suite_aggregates_pass_fail(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    cases = [
        EvalCase(
            name="ok",
            prompt="p1",
            tools=[_read_file_tool()],
            expected_tool="read_file",
        ),
        EvalCase(
            name="bad",
            prompt="p2",
            tools=[_read_file_tool()],
            expected_tool="read_file",
        ),
    ]
    router.set_for_prompt(
        "p1",
        _make_response(
            tool_calls=[
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        ),
    )
    router.set_for_prompt(
        "p2",
        _make_response(
            content=(
                "I'll read that file for you, here is what it likely "
                "contains based on the filename."
            )
        ),
    )

    report = await run_eval_suite("fitt-smart", router, cases=cases)  # type: ignore[arg-type]
    assert report.total == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.pass_rate == 0.5
    assert report.model_id == "nim-deepseek"


def test_empty_suite_pass_rate_is_zero_not_nan() -> None:
    report = EvalReport(
        alias="x",
        model_id=None,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        cases=[],
    )
    # No cases — pass_rate is 0.0, not a division-by-zero.
    assert report.pass_rate == 0.0
    assert report.passed == 0
    assert report.total == 0


# --------------------------------------------------------------- report


def test_report_markdown_contains_key_fields() -> None:
    report = EvalReport(
        alias="fitt-smart",
        model_id="deepseek-v4-flash",
        started_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 11, 12, 0, 25, tzinfo=UTC),
        cases=[
            CaseResult(
                case_name="read_file_basic",
                status="pass",
                detail="called 'read_file' as expected",
                latency_ms=450,
                tool_called="read_file",
                finish_reason="tool_calls",
            ),
            CaseResult(
                case_name="narrated_bad",
                status="narrated",
                detail="model replied with 120 chars",
                latency_ms=600,
                finish_reason="stop",
                reply_preview="Sure, here's what I'll do...",
            ),
        ],
    )
    md = render_report_markdown(report)
    assert "# Eval report" in md
    assert "fitt-smart" in md
    assert "deepseek-v4-flash" in md
    assert "1/2 passed" in md
    assert "read_file_basic" in md
    assert "narrated_bad" in md
    assert "Sure, here's what I'll do" in md
    # Pass icon for the passing case; X icon for the failure.
    assert "✅" in md
    assert "❌" in md


# --------------------------------------------------------------- persistence


def test_write_report_creates_both_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    report = EvalReport(
        alias="fitt-smart",
        model_id="m",
        started_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 11, 12, 0, 10, tzinfo=UTC),
        cases=[
            CaseResult(
                case_name="ok",
                status="pass",
                detail="",
                latency_ms=100,
            ),
        ],
    )
    ts_path, latest_path = write_report(report, home)

    assert ts_path.exists()
    assert latest_path.exists()
    assert latest_path.name == "fitt-smart-latest.md"
    assert "2026-05-11T12-00-10" in ts_path.name
    # Both files have the same content on a fresh write.
    assert ts_path.read_text(encoding="utf-8") == latest_path.read_text(encoding="utf-8")


def test_write_report_namespaces_coding_suite(tmp_path: Path) -> None:
    """The coding suite's reports get a ``-coding-`` infix so
    they don't overwrite the default suite's rolling file.
    Each suite's latest is independently overwritten on
    re-run, but the two suites coexist."""
    home = tmp_path / "home"
    report = EvalReport(
        alias="fitt-smart",
        model_id="m",
        started_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 11, 12, 0, 10, tzinfo=UTC),
        cases=[CaseResult(case_name="c", status="pass", detail="", latency_ms=1)],
    )
    ts_path, latest_path = write_report(report, home, suite="coding")

    assert latest_path.name == "fitt-smart-coding-latest.md"
    assert "2026-05-11T12-00-10" in ts_path.name
    assert "fitt-smart-coding-2026-05-11T12-00-10.md" == ts_path.name
    # Default rolling path is NOT touched by a coding-suite write.
    default_rolling = home / "eval" / "fitt-smart-latest.md"
    assert not default_rolling.exists()


def test_write_report_default_suite_keeps_legacy_path(tmp_path: Path) -> None:
    """``suite='default'`` (the implicit value) writes to the
    pre-existing ``<alias>-latest.md`` path so existing
    operator-saved reports keep their names."""
    home = tmp_path / "home"
    report = EvalReport(
        alias="x",
        model_id="m",
        started_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 11, 12, 0, 10, tzinfo=UTC),
        cases=[CaseResult(case_name="c", status="pass", detail="", latency_ms=1)],
    )
    _, latest_default = write_report(report, home, suite="default")
    _, latest_implicit = write_report(report, home)
    assert latest_default == latest_implicit
    assert latest_default.name == "x-latest.md"


def test_rolling_latest_file_is_overwritten_on_rerun(tmp_path: Path) -> None:
    """Re-running against the same alias overwrites the
    ``-latest.md`` file; the timestamped file is preserved as
    an audit trail."""
    home = tmp_path / "home"
    r1 = EvalReport(
        alias="a",
        model_id="m",
        started_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 11, 12, 0, 1, tzinfo=UTC),
        cases=[CaseResult(case_name="c", status="pass", detail="", latency_ms=1)],
    )
    r2 = EvalReport(
        alias="a",
        model_id="m",
        started_at=datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 11, 13, 0, 1, tzinfo=UTC),
        cases=[
            CaseResult(
                case_name="c",
                status="narrated",
                detail="text instead of tool",
                latency_ms=1,
            )
        ],
    )
    ts1, latest = write_report(r1, home)
    ts2, latest2 = write_report(r2, home)

    # Two timestamped audit entries.
    assert ts1.exists()
    assert ts2.exists()
    assert ts1 != ts2
    # Rolling path is the same file, content is from r2.
    assert latest == latest2
    latest_text = latest.read_text(encoding="utf-8")
    assert "0/1 passed" in latest_text
    assert "narrated" in latest_text


# --------------------------------------------------------------- defaults


def test_default_cases_covers_the_core_shapes() -> None:
    """Pin the starter suite contract. We curate a small set;
    accidentally dropping to one or growing to twenty without
    review is a mistake."""
    cases = default_cases()
    assert 3 <= len(cases) <= 10, "starter suite should be small and curated"
    names = {c.name for c in cases}
    # Each listed name covers a documented pattern; losing one
    # without replacing the coverage is a regression.
    assert "read_file_basic" in names
    assert "no_tool_small_talk" in names
    assert "tool_disambiguation" in names


def test_default_coding_cases_covers_the_router_mode_shapes() -> None:
    """The coding suite mirrors the default suite's contract
    but exercises the tool surface a coding agent (OpenCode,
    Cursor, Claude Code) would offer."""
    from gateway.alias_eval_coding import default_coding_cases

    cases = default_coding_cases()
    assert 3 <= len(cases) <= 10
    names = {c.name for c in cases}
    assert "code_read_basic" in names
    assert "code_edit_basic" in names
    assert "code_glob_search" in names
    assert "code_shell_basic" in names
    assert "code_no_tool_small_talk" in names


def test_default_coding_cases_inject_realistic_system_prompt() -> None:
    """The coding suite pads each prompt with ~2K tokens of
    coding-agent system boilerplate so the eval reflects what
    real router-mode requests look like at the wire. Catches
    bindings that pass the bare-prompt default suite but
    narrate under realistic prompt size (the granite shape)."""
    from gateway.alias_eval_coding import default_coding_cases

    cases = default_coding_cases()
    for c in cases:
        # Each prompt carries the system-prompt block.
        # 1500-char threshold is generous; the real prompt is
        # roughly 4-5K chars (~1-1.5K tokens) before the user
        # message gets appended.
        assert len(c.prompt) > 1500, (
            f"case {c.name} prompt is {len(c.prompt)} chars; "
            "expected the realistic system-prompt block to ride along"
        )


def test_default_eval_dir(tmp_path: Path) -> None:
    assert default_eval_dir(tmp_path) == tmp_path / "eval"

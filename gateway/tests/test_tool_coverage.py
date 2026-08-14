"""Coverage view tests — pure, no registry, no model.

The behaviour that matters is Property 1: registering a tool makes it
appear uncovered with no other edit. A coverage number maintained by hand
is a number that goes stale the moment someone adds a tool, which is how
"7 of 34" stayed the quoted figure long after it stopped being true.
"""

from __future__ import annotations

from gateway.tool_coverage import build_coverage


def test_a_new_tool_shows_up_uncovered_with_no_other_edit() -> None:
    """Property 1. Driven off the registry, not off a curated list."""
    report = build_coverage(
        ["read_file", "brand_new_tool"],
        contract_checked=["read_file"],
        judged_intent=[],
    )

    assert [e.tool for e in report.uncovered] == ["brand_new_tool"]


def test_the_two_axes_are_reported_separately() -> None:
    """A contract check and a judged scenario answer different questions,
    so they must not collapse into one score."""
    report = build_coverage(
        ["read_file", "cron_add", "web_search"],
        contract_checked=["read_file", "cron_add"],
        judged_intent=["cron_add", "web_search"],
    )

    by_tool = {e.tool: e.status for e in report.entries}

    assert by_tool == {
        "read_file": "contract",
        "cron_add": "contract+judged",
        "web_search": "judged",
    }
    assert report.contract_count == 2
    assert report.judged_count == 2
    assert report.uncovered == []


def test_exempt_is_covered_but_named_with_its_reason() -> None:
    """ "Decided against" must stay distinguishable from "forgot"."""
    report = build_coverage(
        ["web_search"],
        contract_checked=[],
        judged_intent=[],
        exempt={"web_search": "hits the network"},
    )

    assert report.uncovered == []
    assert [e.tool for e in report.exempt_entries] == ["web_search"]
    assert "hits the network" in report.render()


def test_exemption_does_not_mask_a_real_check() -> None:
    """A tool that's both exempt and checked reports the check — the
    stronger fact — so the render doesn't understate coverage."""
    report = build_coverage(
        ["web_search"],
        contract_checked=["web_search"],
        judged_intent=[],
        exempt={"web_search": "hits the network"},
    )

    assert [e.status for e in report.entries] == ["contract"]
    assert report.exempt_entries == []


def test_a_check_for_an_unregistered_tool_is_flagged_as_an_orphan() -> None:
    """What a rename looks like.

    `run_contract_checks` skips a check whose tool isn't registered, on
    purpose — a switched-off feature is a deployment fact, not a defect.
    That means a renamed tool silently loses its check and nothing goes
    red, so the two cases have to be told apart somewhere."""
    report = build_coverage(
        ["read_file"],
        contract_checked=["read_file", "grep_repo_OLD_NAME"],
        judged_intent=[],
    )

    assert report.orphan_checks == ["grep_repo_OLD_NAME"]
    assert "a rename would look exactly like this" in report.render()


def test_scenario_intent_naming_a_missing_tool_is_also_an_orphan() -> None:
    report = build_coverage(["read_file"], contract_checked=[], judged_intent=["gone_tool"])

    assert report.orphan_checks == ["gone_tool"]


def test_render_states_that_the_denominator_is_tools_only() -> None:
    """The number is easy to over-read, and it was.

    An audit found auth, cost, fallback routing, approval resolution, the
    audit chain, rate limiting, boot warnings, the startup hooks and the
    CLI largely unmeasured while this command reported "0 uncovered" —
    all true, because none of them is a tool. Saying so in the output is
    cheaper than relying on the reader to remember the scope."""
    rendered = build_coverage(
        ["read_file"], contract_checked=["read_file"], judged_intent=[]
    ).render()

    assert "TOOLS ONLY" in rendered
    assert "0 uncovered" in rendered
    for subsystem in ("auth", "cost", "fallback", "audit chain", "boot warnings"):
        assert subsystem in rendered, f"the scope caveat no longer names {subsystem}"


def test_render_says_the_judged_column_is_intent_not_evidence() -> None:
    """Load-bearing caveat: a scenario can pass by another route while
    the tool it names never fires."""
    rendered = build_coverage(
        ["cron_add"], contract_checked=[], judged_intent=["cron_add"]
    ).render()

    assert "INTENT, not evidence" in rendered
    assert "fitt eval matrix" in rendered


def test_uncovered_tools_sort_first_in_the_render() -> None:
    """The gap is the reason to run this, so don't bury it."""
    rendered = build_coverage(
        ["aaa_covered", "zzz_missing"],
        contract_checked=["aaa_covered"],
        judged_intent=[],
    ).render()

    lines = [ln for ln in rendered.splitlines() if ln.startswith("  - ")]

    assert lines[0].startswith("  - zzz_missing")


def test_the_live_registry_is_fully_covered() -> None:
    """The standing claim, asserted rather than retyped.

    Every tool the gateway registers has a contract check, a judged
    scenario, or a recorded exemption. If this fails, a tool was added
    without one — which is exactly the moment to notice."""
    import tempfile
    from pathlib import Path

    from gateway.e2e_scenarios import seed_scenarios
    from gateway.tool_contract_suite import EXEMPT, default_checks
    from gateway.tools import build_core_tool_registry
    from gateway.tools.send_message import build_send_message_tool

    from ._fixtures import build_test_config

    config = build_test_config(Path(tempfile.mkdtemp()))
    registered = set(build_core_tool_registry(config).list_names())
    registered |= {build_send_message_tool().name, "project_shell"}

    scenarios = seed_scenarios()
    report = build_coverage(
        registered,
        contract_checked={c.tool for c in default_checks("p", http_base_url="http://x")},
        judged_intent={t for s in scenarios for t in s.exercises_tools},
        exempt=EXEMPT,
        conditional={t for s in scenarios for t in s.requires_tools},
    )

    assert report.uncovered == [], f"tools with no check: {[e.tool for e in report.uncovered]}"
    assert report.orphan_checks == []
    # memory_search is retrieval-gated, so it's absent here by config.
    assert report.off_deployment == ["memory_search"]


def test_a_conditional_tool_is_not_an_orphan() -> None:
    """Absent by configuration is not absent by mistake.

    Conflating the two is the memory_recall episode in miniature: three
    models were graded down for a tool that was never registered."""
    report = build_coverage(
        ["read_file"],
        contract_checked=[],
        judged_intent=["memory_search", "typo_tool"],
        conditional=["memory_search"],
    )

    assert report.off_deployment == ["memory_search"]
    assert report.orphan_checks == ["typo_tool"]
    assert "feature off, checked elsewhere" in report.render()

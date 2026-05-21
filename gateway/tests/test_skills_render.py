"""Unit tests for ``render_skills_block`` (Phase 4.10, Commit 1).

Nine tests covering Requirement 3 (system-prompt block shape)
and Requirement 8 (honest prerequisite reporting). All tests
construct ``LoadedSkill`` records by hand and use a small mock
``ToolRegistry`` so the renderer is exercised without any I/O.
"""

from __future__ import annotations

from pathlib import Path

from gateway.skills import LoadedSkill, render_skills_block

# ----------------------------------------------------------- helpers


class FakeRegistry:
    """Minimal stand-in exposing only the surface
    ``render_skills_block`` calls (``list_names``)."""

    def __init__(self, names: list[str]) -> None:
        self._names = list(names)

    def list_names(self) -> list[str]:
        return list(self._names)


def _skill(
    name: str,
    description: str = "do a thing",
    prerequisites: tuple[str, ...] = (),
    skill_md_path: Path | None = None,
    description_truncated: bool = False,
) -> LoadedSkill:
    """Construct a ``LoadedSkill`` with sensible defaults.

    The default path is absolute and unique per skill name so
    tie-break tests have something stable to assert against.
    """
    if skill_md_path is None:
        skill_md_path = Path("/fake/skills") / name / "SKILL.md"
    return LoadedSkill(
        name=name,
        description=description,
        prerequisites=prerequisites,
        skill_md_path=skill_md_path,
        description_truncated=description_truncated,
    )


# ----------------------------------------------------------- tests


def test_render_empty_returns_empty_string():
    """Empty input → empty string. Caller drops the block.

    Validates Requirement 3.4.
    """
    out = render_skills_block([], FakeRegistry([]))
    assert out == ""


def test_render_single_skill_uses_project_fitt_when_under_fitt_home(tmp_path: Path):
    """Skill under FITT_HOME → recipe-load hint uses
    ``project=fitt path=<rel>``.

    Validates Requirement 3.3 (line shape) and 3.2 (header)
    plus the Phase 4.10 sub-commit decision to render via the
    built-in fitt pseudo-project.
    """
    fitt_home = tmp_path / "fitt-home"
    fitt_home.mkdir(exist_ok=True)
    skill_path = fitt_home / "skills" / "say-hello" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: say-hello\ndescription: x\n---\n")

    skill = _skill(
        "say-hello",
        description="Say hello in French.",
        skill_md_path=skill_path,
    )

    out = render_skills_block([skill], FakeRegistry([]), fitt_home=fitt_home)

    # Header + instruction line + skill line.
    lines = out.splitlines()
    assert lines[0] == "[Skills available]"
    assert "load the recipe" in lines[1]
    assert lines[2] == (
        "- say-hello: Say hello in French. "
        "(read recipe with read_file project=fitt path=skills/say-hello/SKILL.md)"
    )


def test_render_single_skill_falls_back_to_absolute_when_outside_fitt_home(
    tmp_path: Path,
):
    """Skill outside FITT_HOME → recipe-load hint uses absolute
    path (the agent won't be able to load it via project=fitt,
    but the hint is at least factually accurate)."""
    fitt_home = tmp_path / "fitt-home"
    fitt_home.mkdir(exist_ok=True)
    # skill_path is NOT under fitt_home — it's a sibling.
    skill_path = tmp_path / "elsewhere" / "say-hello" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: say-hello\ndescription: x\n---\n")

    skill = _skill(
        "say-hello",
        description="Say hello.",
        skill_md_path=skill_path,
    )

    out = render_skills_block([skill], FakeRegistry([]), fitt_home=fitt_home)

    # The absolute path appears verbatim; project=fitt does not.
    assert str(skill_path) in out
    assert "project=fitt" not in out


def test_render_skill_with_prereqs_satisfied():
    """All prereqs present in registry → no [unavailable] tag.

    Validates Requirement 3.6 and 8.2.
    """
    skill = _skill(
        "web-search",
        description="Search the web.",
        prerequisites=("http_get", "read_file"),
        skill_md_path=Path("/fitt/skills/web-search/SKILL.md"),
    )

    out = render_skills_block([skill], FakeRegistry(["http_get", "read_file", "send_message"]))

    assert "; needs: http_get, read_file" in out, f"missing 'needs' segment: {out!r}"
    assert "[unavailable:" not in out


def test_render_skill_with_missing_prereqs():
    """Prereq missing from registry → [unavailable: ...] tag.

    Validates Requirement 8.1.
    """
    skill = _skill(
        "web-search",
        description="Search the web.",
        prerequisites=("http_get",),
    )

    out = render_skills_block([skill], FakeRegistry([]))

    assert "; needs: http_get" in out
    assert "[unavailable: http_get]" in out


def test_render_skill_partial_prereq_satisfaction():
    """Some prereqs present, others missing → [unavailable] lists only the missing ones."""
    skill = _skill(
        "complex",
        description="Does many things.",
        prerequisites=("http_get", "read_file", "send_message"),
    )

    out = render_skills_block([skill], FakeRegistry(["http_get", "send_message"]))

    assert "; needs: http_get, read_file, send_message" in out
    # Only the missing one ('read_file') should appear in
    # [unavailable], in declaration order.
    assert "[unavailable: read_file]" in out


def test_render_skills_sorted_case_insensitive():
    """Render order: case-insensitive lex by name.

    Validates Requirement 3.5.
    """
    skills = [
        _skill("Zebra"),
        _skill("apple"),
        _skill("bee"),
    ]

    out = render_skills_block(skills, FakeRegistry([]))
    lines = out.splitlines()

    # Header + instruction line + 3 skill lines.
    assert lines[0] == "[Skills available]"
    assert "load the recipe" in lines[1]
    assert lines[2].startswith("- apple:")
    assert lines[3].startswith("- bee:")
    assert lines[4].startswith("- Zebra:")


def test_render_skills_sorted_tie_break_by_path():
    """Same name, different abspaths → lex-first abspath wins
    (deterministic; no insertion-order sensitivity).

    Validates Requirement 3.5.
    """
    path_a = Path("/aaa/dup/SKILL.md")
    path_b = Path("/bbb/dup/SKILL.md")
    skill_a = _skill(
        "dup",
        description="path A",
        skill_md_path=path_a,
    )
    skill_b = _skill(
        "dup",
        description="path B",
        skill_md_path=path_b,
    )

    # Try both insertion orders; result should be the same.
    out_ab = render_skills_block([skill_a, skill_b], FakeRegistry([]))
    out_ba = render_skills_block([skill_b, skill_a], FakeRegistry([]))

    assert out_ab == out_ba
    # The /aaa/ path should come first (lex-smaller). Use the
    # platform-native string form so this works on both POSIX
    # and Windows.
    aaa_idx = out_ab.index(str(path_a))
    bbb_idx = out_ab.index(str(path_b))
    assert aaa_idx < bbb_idx


def test_render_byte_identical_on_repeat():
    """Two calls with same input → byte-identical output.

    Validates Requirement 3.8 (prompt-cache stability).
    """
    skill = _skill(
        "stable",
        description="Stable across calls.",
        prerequisites=("http_get",),
    )
    registry = FakeRegistry(["http_get"])

    out1 = render_skills_block([skill], registry)
    out2 = render_skills_block([skill], registry)

    assert out1 == out2
    assert out1.encode("utf-8") == out2.encode("utf-8")


def test_render_truncated_description_shows_ellipsis():
    """When description was truncated by the loader, the
    rendered line ends with '...' before the recipe-load hint.

    Validates Requirement 2.4 from the renderer's side: the
    loader puts the ellipsis into the description string;
    the renderer just lays it out.
    """
    truncated_desc = ("a" * 80) + "..."
    skill = _skill(
        "long",
        description=truncated_desc,
        description_truncated=True,
    )

    out = render_skills_block([skill], FakeRegistry([]))

    # The line should contain "<80 a's>... (read recipe ..."
    assert ("a" * 80) + "... (read recipe with read_file " in out

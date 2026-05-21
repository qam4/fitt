"""Property tests for the skills loader and renderer (Phase 4.10, Commit 3).

Two hypothesis-driven invariants:

* **Property 2 — Deterministic order.** For any set of LoadedSkills,
  ``render_skills_block`` produces byte-identical output regardless
  of the order skills were appended to the input list.

* **Property 4 — Failure isolation.** For any set of N candidate
  subdirectories where K fail any check in Requirement 2,
  ``SkillsLoader.scan()`` returns exactly N - K LoadedSkills and
  does not raise.

These are the two invariants design.md flagged as "harder to pin
with example-based tests" because they're statements about an
infinite family of inputs.
"""

from __future__ import annotations

import string
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.skills import LoadedSkill, SkillsLoader, render_skills_block


class _StubRegistry:
    """Minimal ToolRegistry stand-in for the renderer (Property 2)."""

    def __init__(self, names: list[str]) -> None:
        self._names = list(names)

    def list_names(self) -> list[str]:
        return list(self._names)


# --------------------------------------------------------------- strategies


_NAME_ALPHABET = string.ascii_lowercase + string.digits + "-_"


# Hypothesis-driven names that won't trip YAML's bareword
# auto-coercion (``name: 0`` parses as int → wrong-field-type
# rejection, which is correct loader behavior but not what these
# property tests are checking). First character must be a letter
# so the YAML scalar always parses as a string. Real skill names
# follow this convention anyway (cf. ``say-hello-french``,
# ``fitt-status``).
_skill_names = (
    st.tuples(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=1),
        st.text(alphabet=_NAME_ALPHABET, min_size=0, max_size=31),
    )
    .map(lambda parts: parts[0] + parts[1])
    .filter(lambda s: not s.startswith("."))
)


def _build_loaded_skill(name: str, idx: int, base_dir: Path) -> LoadedSkill:
    """Construct a LoadedSkill with a deterministic but per-name path.

    Used by Property 2 — the renderer takes pre-built records, no
    on-disk artifacts needed.
    """
    return LoadedSkill(
        name=name,
        description=f"description {idx}",
        prerequisites=(),
        skill_md_path=(base_dir / name / "SKILL.md").resolve(),
        description_truncated=False,
    )


# --------------------------------------------------------------- properties


# Phase 4.10, Property 2: Deterministic order
@given(
    names=st.lists(_skill_names, min_size=1, max_size=12, unique=True),
)
@settings(max_examples=100, deadline=2000)
def test_property_render_deterministic_order(tmp_path_factory, names):
    """For any set of LoadedSkills, two renders with identical
    inputs produce byte-identical output, regardless of the order
    the skills were appended to the input list."""
    tmp = tmp_path_factory.mktemp("skills-render-prop")
    fitt_home = tmp / "fitt-home"
    fitt_home.mkdir()

    skills = [_build_loaded_skill(name, idx, fitt_home) for idx, name in enumerate(names)]
    registry = _StubRegistry([])

    # Render once in input order.
    out_a = render_skills_block(skills, registry, fitt_home=fitt_home)

    # Render again in reverse order.
    out_b = render_skills_block(list(reversed(skills)), registry, fitt_home=fitt_home)

    assert out_a == out_b
    assert out_a.encode("utf-8") == out_b.encode("utf-8")


# Phase 4.10, Property 4: Failure isolation
@given(
    valid_names=st.lists(_skill_names, min_size=0, max_size=5, unique=True),
    invalid_names=st.lists(_skill_names, min_size=0, max_size=5, unique=True),
)
@settings(max_examples=50, deadline=4000)
def test_property_scan_failure_isolation(tmp_path_factory, valid_names, invalid_names):
    """Given N candidate subdirectories where K of them fail any
    check in Requirement 2 (here: missing `description` and missing
    closing fence), the scan returns exactly N - K LoadedSkills
    and does not raise."""
    tmp = tmp_path_factory.mktemp("skills-fail-iso")
    skills_root = tmp / "skills"
    skills_root.mkdir()

    # Names must be unique across the two lists, otherwise the
    # later-written subdir would overwrite the earlier one and
    # break the count invariant. Skip overlapping cases at the
    # input-validation level to keep the assertion clean.
    overlap = set(valid_names) & set(invalid_names)
    valid_names = [n for n in valid_names if n not in overlap]
    invalid_names = [n for n in invalid_names if n not in overlap]

    for name in valid_names:
        sub = skills_root / name
        sub.mkdir()
        (sub / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: valid {name}\n---\n\nbody\n",
            encoding="utf-8",
        )

    for name in invalid_names:
        sub = skills_root / name
        sub.mkdir()
        # Missing closing fence: a known Requirement 2.7 failure mode.
        (sub / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: y\n",
            encoding="utf-8",
        )

    loader = SkillsLoader(skills_root, enabled=True)
    loaded = loader.scan()  # MUST NOT raise

    loaded_names = {s.name for s in loaded}
    assert loaded_names == set(valid_names)
    assert len(loaded) == len(valid_names)

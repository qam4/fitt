"""Unit tests for the skills loader (Phase 4.10, Commit 1).

Twenty-two tests covering every requirement enumerated in
``.kiro/specs/phase4.10-skills-loader/requirements.md``. The
tests exercise ``SkillsLoader.scan()`` over real on-disk
``SKILL.md`` files written into ``tmp_path``, asserting both
the returned ``LoadedSkill`` records and the structured log
records emitted along the way.

The integration test for the gateway-side wiring lives in
``test_skills_e2e.py`` (Commit 2). The renderer's tests live
in ``test_skills_render.py`` (also Commit 1).

Note: ``conftest.py`` autouse-creates ``tmp_path/fitt-home/``
for every test, so we use a dedicated ``tmp_path/skills/``
subdirectory as the loader's root rather than ``tmp_path``
itself (via the ``skills_root`` fixture).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from gateway.skills import SkillsLoader

# ----------------------------------------------------------- helpers


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    """A fresh, empty skills root for each test.

    Use this instead of ``tmp_path`` directly: ``tmp_path`` is
    pre-populated by the autouse ``isolate_fitt_home`` fixture
    in ``conftest.py``, which would otherwise show up as a
    spurious skipped subdirectory.
    """
    root = tmp_path / "skills"
    root.mkdir()
    return root


def _write_skill(
    skills_dir: Path,
    name: str,
    description: str = "say hello",
    prerequisites: list[str] | None = None,
    extra_frontmatter: str = "",
    body: str = "Say hello.",
) -> Path:
    """Write a SKILL.md under ``skills_dir/<name>/``.

    Returns the path to the SKILL.md. Prerequisites is rendered
    as a YAML list when non-empty; ``extra_frontmatter`` is
    inserted verbatim before the closing fence so callers can
    inject malformed YAML or unknown keys.
    """
    sub = skills_dir / name
    sub.mkdir(parents=True, exist_ok=True)
    skill_md = sub / "SKILL.md"

    fm_lines = [f"name: {name}", f"description: {description}"]
    if prerequisites is not None:
        if prerequisites:
            fm_lines.append("prerequisites:")
            for p in prerequisites:
                fm_lines.append(f"  - {p}")
        else:
            fm_lines.append("prerequisites: []")
    if extra_frontmatter:
        fm_lines.append(extra_frontmatter)

    content = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body + "\n"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


def _events(caplog: pytest.LogCaptureFixture, event_name: str) -> list[dict]:
    """Filter ``caplog.records`` to those carrying ``event=<event_name>``."""
    out = []
    for r in caplog.records:
        ev = getattr(r, "event", None)
        if ev == event_name:
            out.append(r.__dict__)
    return out


# ----------------------------------------------------------- scan-shape tests


def test_loader_empty_skills_dir(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """Directory exists, contains no subdirs → [], no warnings.

    Validates Requirement 1.6.
    """
    caplog.set_level(logging.DEBUG, logger="gateway.skills")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    completes = _events(caplog, "skills.scan_complete")
    assert len(completes) == 1
    assert completes[0]["loaded_count"] == 0
    assert completes[0]["skipped_count"] == 0
    assert not _events(caplog, "skills.skipped")


def test_loader_missing_skills_dir(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """skills_dir does not exist → [], one INFO with not-found.

    Validates Requirement 1.5.
    """
    caplog.set_level(logging.INFO, logger="gateway.skills")
    missing = tmp_path / "does-not-exist"

    loader = SkillsLoader(missing, enabled=True)
    result = loader.scan()

    assert result == []
    completes = _events(caplog, "skills.scan_complete")
    assert len(completes) == 1
    assert completes[0]["discriminator"] == "not-found"


def test_loader_skills_dir_is_file(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """skills_dir resolves to a regular file → [], WARNING with not-a-directory.

    Validates Requirement 5.5.
    """
    caplog.set_level(logging.INFO, logger="gateway.skills")
    f = tmp_path / "skills-file"
    f.write_text("not a dir")

    loader = SkillsLoader(f, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "not-a-directory"


def test_loader_disabled_skips_scan(skills_root: Path, monkeypatch: pytest.MonkeyPatch):
    """enabled=False → [], no filesystem reads.

    Validates Requirement 4.7.
    """
    _write_skill(skills_root, "would-load")

    def _no_filesystem_reads(*args, **kwargs):
        raise AssertionError("disabled=False must not touch the filesystem")

    monkeypatch.setattr(Path, "iterdir", _no_filesystem_reads)

    loader = SkillsLoader(skills_root, enabled=False)
    result = loader.scan()
    assert result == []


def test_loader_subdir_without_skill_md(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """Subdir present without SKILL.md → skipped, INFO with no SKILL.md.

    Validates Requirement 1.3.
    """
    caplog.set_level(logging.INFO, logger="gateway.skills")
    (skills_root / "no-skill-md-here").mkdir()

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "no SKILL.md"


def test_loader_dotfile_subdir_ignored(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """Dotfile-prefixed subdirs at skills_root are silently ignored.

    Validates Requirement 1.4.
    """
    caplog.set_level(logging.DEBUG, logger="gateway.skills")
    (skills_root / ".git").mkdir()
    (skills_root / ".git" / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\n")
    (skills_root / ".hidden").mkdir()

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    assert not _events(caplog, "skills.skipped")


# ----------------------------------------------------------- happy-path tests


def test_loader_valid_minimal_skill(skills_root: Path):
    """Happy path: one valid SKILL.md → one LoadedSkill with right fields."""
    skill_md = _write_skill(skills_root, "say-hello", description="Say hello in French.")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert len(result) == 1
    s = result[0]
    assert s.name == "say-hello"
    assert s.description == "Say hello in French."
    assert s.prerequisites == ()
    assert s.skill_md_path == skill_md.resolve()
    assert s.description_truncated is False


def test_loader_with_prerequisites(skills_root: Path):
    """prerequisites list parsed as a tuple of strings."""
    _write_skill(skills_root, "web-search", prerequisites=["http_get", "read_file"])

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert len(result) == 1
    assert result[0].prerequisites == ("http_get", "read_file")


def test_loader_unknown_frontmatter_keys(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """Unknown frontmatter keys → skill loaded, DEBUG log fires.

    Validates Requirement 2.5.
    """
    caplog.set_level(logging.DEBUG, logger="gateway.skills")
    _write_skill(
        skills_root,
        "with-extras",
        extra_frontmatter="version: 1.0\nplatforms: [linux]",
    )

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert len(result) == 1
    unknowns = _events(caplog, "skills.unknown_keys")
    assert len(unknowns) == 1
    assert sorted(unknowns[0]["unknown_keys"]) == ["platforms", "version"]


def test_loader_name_mismatch(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """Frontmatter name != dirname → loaded with dirname, WARNING.

    Validates Requirement 2.3.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "real-dirname"
    sub.mkdir()
    (sub / "SKILL.md").write_text(
        "---\nname: different-name\ndescription: x\n---\nbody",
        encoding="utf-8",
    )

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert len(result) == 1
    assert result[0].name == "real-dirname"  # dirname wins
    mismatches = _events(caplog, "skills.name_mismatch")
    assert len(mismatches) == 1


def test_loader_description_too_long(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """description > 80 codepoints → first 80 + '...', truncated=True.

    Validates Requirement 2.4.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    long_desc = "a" * 200
    _write_skill(skills_root, "long-desc", description=long_desc)

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert len(result) == 1
    s = result[0]
    assert s.description_truncated is True
    assert s.description == ("a" * 80) + "..."
    assert len(s.description) == 83  # 80 + 3 dots
    truncs = _events(caplog, "skills.description_truncated")
    assert len(truncs) == 1
    assert truncs[0]["original_chars"] == 200


# ----------------------------------------------------------- failure-mode tests


def test_loader_missing_open_fence(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """SKILL.md without leading '---' → skipped, no-frontmatter-fence.

    Validates Requirement 2.6.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "no-fence"
    sub.mkdir()
    (sub / "SKILL.md").write_text("not frontmatter\nname: x\n", encoding="utf-8")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "no-frontmatter-fence"


def test_loader_missing_close_fence(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """Opening '---' but no close within 200 lines → skipped.

    Validates Requirement 2.7.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "unclosed"
    sub.mkdir()
    body = "\n".join(["name: x", "description: y"] + ["filler"] * 250)
    (sub / "SKILL.md").write_text("---\n" + body + "\n", encoding="utf-8")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "unclosed-frontmatter"


def test_loader_malformed_yaml(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """Frontmatter is invalid YAML → skipped, malformed-yaml.

    Validates Requirement 2.8.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "bad-yaml"
    sub.mkdir()
    (sub / "SKILL.md").write_text("---\n: : :\n---\nbody\n", encoding="utf-8")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "malformed-yaml"


def test_loader_missing_required_name(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """No 'name' field → skipped, missing-required-field.

    Validates Requirement 2.9.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "no-name"
    sub.mkdir()
    (sub / "SKILL.md").write_text("---\ndescription: x\n---\nbody\n", encoding="utf-8")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "missing-required-field"


def test_loader_missing_required_description(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """No 'description' field → skipped, missing-required-field.

    Validates Requirement 2.9.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "no-desc"
    sub.mkdir()
    (sub / "SKILL.md").write_text("---\nname: x\n---\nbody\n", encoding="utf-8")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "missing-required-field"


def test_loader_whitespace_only_name(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """name is whitespace-only → skipped (treated as missing).

    Validates Requirement 2.9.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "ws-name"
    sub.mkdir()
    (sub / "SKILL.md").write_text(
        '---\nname: "   "\ndescription: x\n---\nbody\n',
        encoding="utf-8",
    )

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert skipped[0]["reason"] == "missing-required-field"


def test_loader_wrong_type_name(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """name: 123 (int) → skipped, wrong-field-type.

    Validates Requirement 2.10.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "int-name"
    sub.mkdir()
    (sub / "SKILL.md").write_text(
        "---\nname: 123\ndescription: x\n---\nbody\n",
        encoding="utf-8",
    )

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert skipped[0]["reason"] == "wrong-field-type"


def test_loader_wrong_type_prerequisites(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """prerequisites: 'not-a-list' → skipped, wrong-field-type.

    Validates Requirement 2.10.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "bad-prereqs"
    sub.mkdir()
    (sub / "SKILL.md").write_text(
        "---\nname: x\ndescription: y\nprerequisites: not-a-list\n---\nbody\n",
        encoding="utf-8",
    )

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert skipped[0]["reason"] == "wrong-field-type"


def test_loader_failure_isolation(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """Mixed valid/invalid skills → valid ones loaded, invalid skipped.

    Validates Requirements 5.1 and 5.2.
    """
    caplog.set_level(logging.WARNING, logger="gateway.skills")
    _write_skill(skills_root, "alpha", description="alpha desc")
    sub = skills_root / "broken-middle"
    sub.mkdir()
    (sub / "SKILL.md").write_text("---\nname: broken-middle\n---\nbody\n", encoding="utf-8")
    _write_skill(skills_root, "zulu", description="zulu desc")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    names = {s.name for s in result}
    assert names == {"alpha", "zulu"}
    skipped = _events(caplog, "skills.skipped")
    assert len(skipped) == 1
    assert "broken-middle" in skipped[0]["path"]


def test_loader_duplicate_name_case_insensitive(
    skills_root: Path, caplog: pytest.LogCaptureFixture
):
    """Two subdirs with same case-folded name → keep lex-first, WARNING.

    Validates Requirement 1.8.

    Skipped on case-insensitive filesystems (Windows default,
    macOS default APFS) where the OS itself can't have both
    'WebSearch' and 'websearch' simultaneously. The duplicate-
    name code path still gets exercised on Linux CI.
    """
    if sys.platform != "linux":
        pytest.skip("requires case-sensitive filesystem")

    caplog.set_level(logging.WARNING, logger="gateway.skills")
    _write_skill(skills_root, "WebSearch", description="upper")
    _write_skill(skills_root, "websearch", description="lower")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert len(result) == 1
    skipped = _events(caplog, "skills.skipped")
    assert any(s["reason"] == "duplicate-name" for s in skipped)


def test_loader_unreadable_skill_md(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """SKILL.md exists but can't be read → skipped, read-failed.

    Validates Requirement 1.7. POSIX-only because Windows file
    permissions don't have an easy "remove read for everyone"
    knob from pytest, and the IOError code path is what we're
    pinning here.
    """
    if os.name != "posix":
        pytest.skip("posix-only permissions test")

    caplog.set_level(logging.WARNING, logger="gateway.skills")
    sub = skills_root / "unreadable"
    sub.mkdir()
    f = sub / "SKILL.md"
    f.write_text("---\nname: x\ndescription: y\n---\n", encoding="utf-8")
    f.chmod(0o000)
    try:
        loader = SkillsLoader(skills_root, enabled=True)
        result = loader.scan()
    finally:
        f.chmod(0o644)

    assert result == []
    skipped = _events(caplog, "skills.skipped")
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "read-failed"


def test_loader_summary_log(skills_root: Path, caplog: pytest.LogCaptureFixture):
    """Mixed loads/skips → exactly one scan_complete with right counts.

    Validates Requirement 6.3.
    """
    caplog.set_level(logging.INFO, logger="gateway.skills")
    _write_skill(skills_root, "good-1")
    _write_skill(skills_root, "good-2")
    bad = skills_root / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("not frontmatter\n", encoding="utf-8")

    loader = SkillsLoader(skills_root, enabled=True)
    result = loader.scan()

    assert len(result) == 2
    completes = _events(caplog, "skills.scan_complete")
    assert len(completes) == 1
    assert completes[0]["loaded_count"] == 2
    assert completes[0]["skipped_count"] == 1
    assert completes[0]["root"] == str(skills_root)

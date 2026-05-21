"""Skills-as-markdown loader (Phase 4.10).

A skill is a directory under ``$FITT_HOME/skills/<name>/`` with a
``SKILL.md`` file at its root. ``SKILL.md`` carries YAML
frontmatter (name, description, prerequisites) and a free-form
markdown body. The loader runs at gateway boot, parses every
``SKILL.md``, and surfaces a ``[Skills available]`` block in
the system prompt listing each skill's name + description.

The skill body is **not** injected into the prompt. The agent
reads it on demand via the existing ``read_file`` tool when it
decides a skill applies. That trade — one line of prompt cost
per skill, body fetched lazily — is what lets FITT carry an
unbounded library of recipes without paying token cost on every
turn.

Two halves live in this module:

* ``SkillsLoader`` — boot-time scanner. Pure I/O around the
  skills directory, returns a list of ``LoadedSkill`` records.
  Never raises; per-skill failures log a warning and skip.

* ``render_skills_block`` — pure renderer. Takes the loaded
  skills plus the live ``ToolRegistry`` (for prerequisite
  honesty per Requirement 8) and returns the system-prompt
  fragment. Empty input returns ``""`` so the caller drops the
  block entirely.

Edit-and-restart is the contract: skills aren't re-read inside a
process lifetime. That guarantees prompt-cache stability across
requests (Requirements 3.7, 3.8) and matches how
``identity.md`` already works today.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from .tools import ToolRegistry

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- bounds

_DESCRIPTION_RENDER_LIMIT = 80
"""Codepoint cap for the description as it appears in the
system prompt. The frontmatter accepts up to 1000 codepoints
(Requirement 2.2) so the operator can write a useful sentence;
anything beyond 80 gets truncated with ``...`` for prompt
budget. See design.md Decision 5."""

_NAME_MAX_LEN = 64
_DESCRIPTION_MAX_LEN = 1000
_PREREQ_MAX_COUNT = 32
_PREREQ_NAME_MAX_LEN = 200
_FRONTMATTER_MAX_LINES = 200
"""Hard cap on the number of lines we'll search for the closing
``---`` fence (Requirement 2.7). A SKILL.md with no closing
fence within 200 lines is rejected — bounded reading prevents a
malicious symlink to ``/dev/zero`` from hanging the loader."""

_FENCE = "---"


# --------------------------------------------------------------- public types


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    """One successfully parsed ``SKILL.md`` ready for rendering.

    Fields:

    * ``name`` — the skill's directory basename. Always wins
      over a frontmatter ``name`` field that disagrees with it
      (Requirement 2.3 — directory is canonical).

    * ``description`` — already truncated to
      ``_DESCRIPTION_RENDER_LIMIT`` codepoints if the original
      exceeded it. Whitespace stripped, internal newlines
      collapsed to single spaces (Requirement 3.3).

    * ``prerequisites`` — tool names this skill expects to find
      in the ``ToolRegistry`` at render time. Empty tuple is
      fine (no declared dependencies).

    * ``skill_md_path`` — absolute path to the ``SKILL.md``
      file. Rendered into the recipe-load hint and used as the
      tie-break when two skills sort to the same name.

    * ``description_truncated`` — True when truncation
      happened. Doesn't affect rendering (the truncated string
      is what ``description`` already contains); used by the
      WARNING log line for honesty.
    """

    name: str
    description: str
    prerequisites: tuple[str, ...]
    skill_md_path: Path
    description_truncated: bool


# --------------------------------------------------------------- exceptions


class SkillSkipped(Exception):
    """Base for every per-skill failure mode.

    Subclasses carry a stable ``reason`` code used as the
    structured ``reason`` field in the
    ``event="skills.skipped"`` WARNING line (Requirement 6.2).
    The codes form a closed enumeration shared between the
    loader and tests so reviewers can grep for a known set.
    """

    reason = "unknown"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class MissingOpenFence(SkillSkipped):
    reason = "no-frontmatter-fence"


class MissingCloseFence(SkillSkipped):
    reason = "unclosed-frontmatter"


class MalformedYaml(SkillSkipped):
    reason = "malformed-yaml"


class MissingRequiredField(SkillSkipped):
    reason = "missing-required-field"


class WrongFieldType(SkillSkipped):
    reason = "wrong-field-type"


class FieldOutOfBounds(SkillSkipped):
    reason = "field-out-of-bounds"


class ReadFailed(SkillSkipped):
    reason = "read-failed"


# --------------------------------------------------------------- loader


class SkillsLoader:
    """Boot-time scanner for ``$FITT_HOME/skills/``.

    Constructed once with the configured skills root and the
    enable flag, called once via ``scan()`` at gateway startup.
    The result is cached on ``app.state.skills`` and re-used by
    every chat request inside one process lifetime.

    The loader never raises out of ``scan()``. Per-skill
    failures log one structured WARNING and exclude that skill;
    Skills_Root being missing or being a file logs one INFO/
    WARNING and returns an empty list. This is what
    Requirement 5 mandates and what makes the loader safe to
    add to the gateway's boot path.
    """

    def __init__(self, skills_dir: Path, enabled: bool = True) -> None:
        self._skills_dir = skills_dir
        self._enabled = enabled

    # --------------------------------------------------------- public

    def scan(self) -> list[LoadedSkill]:
        """Walk ``skills_dir``, parse every ``SKILL.md``, return
        the LoadedSkills sorted by name (case-insensitive,
        ties broken by absolute path).

        Returns ``[]`` and logs an INFO line when:

        * ``enabled=False`` (no filesystem reads at all —
          Requirement 4.7).
        * ``skills_dir`` doesn't exist (Requirement 1.5).

        Returns ``[]`` and logs a WARNING when ``skills_dir``
        exists but is not a directory (Requirement 5.5).

        Always emits one ``skills.scan_complete`` summary line
        at the end (Requirement 6.3).
        """
        if not self._enabled:
            _log.info(
                "skills.scan_complete",
                extra={
                    "event": "skills.scan_complete",
                    "loaded_count": 0,
                    "skipped_count": 0,
                    "root": str(self._skills_dir),
                    "disabled": True,
                },
            )
            return []

        root = self._skills_dir
        if not root.exists():
            _log.info(
                "skills.scan_complete",
                extra={
                    "event": "skills.scan_complete",
                    "loaded_count": 0,
                    "skipped_count": 0,
                    "root": str(root),
                    "discriminator": "not-found",
                },
            )
            return []

        if not root.is_dir():
            _log.warning(
                "skills.skipped",
                extra={
                    "event": "skills.skipped",
                    "path": str(root),
                    "reason": "not-a-directory",
                },
            )
            _log.info(
                "skills.scan_complete",
                extra={
                    "event": "skills.scan_complete",
                    "loaded_count": 0,
                    "skipped_count": 1,
                    "root": str(root),
                    "discriminator": "not-a-directory",
                },
            )
            return []

        # Enumerate one level. Dotfiles silently ignored
        # (Requirement 1.4). Sorted for deterministic log
        # order; the rendered output gets sorted again per
        # Requirement 3.5.
        candidates = sorted(
            (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: str(p),
        )

        loaded: list[LoadedSkill] = []
        skipped = 0

        for sub in candidates:
            skill_md = sub / "SKILL.md"

            # Subdir without SKILL.md — Requirement 1.3.
            if not skill_md.exists():
                _log.info(
                    "skills.skipped",
                    extra={
                        "event": "skills.skipped",
                        "path": str(sub),
                        "reason": "no SKILL.md",
                    },
                )
                continue

            try:
                skill = self._load_one(skill_md)
            except SkillSkipped as exc:
                skipped += 1
                _log.warning(
                    "skills.skipped",
                    extra={
                        "event": "skills.skipped",
                        "path": str(skill_md),
                        "reason": exc.reason,
                        "detail": str(exc),
                    },
                )
                continue
            except Exception as exc:
                # Belt-and-suspenders. The typed exceptions
                # cover every documented failure mode in
                # Requirement 2; this catch is for bugs we
                # didn't anticipate. Still skips, still logs,
                # never propagates.
                skipped += 1
                _log.warning(
                    "skills.skipped",
                    extra={
                        "event": "skills.skipped",
                        "path": str(skill_md),
                        "reason": "unexpected-error",
                        "detail": f"{type(exc).__name__}: {exc}",
                    },
                )
                continue

            loaded.append(skill)
            _log.info(
                "skills.loaded",
                extra={
                    "event": "skills.loaded",
                    "skill_name": skill.name,
                    "description_chars": len(skill.description),
                    "prerequisites_count": len(skill.prerequisites),
                },
            )

        loaded, dup_skipped = _dedupe_by_name(loaded)
        skipped += dup_skipped

        _log.info(
            "skills.scan_complete",
            extra={
                "event": "skills.scan_complete",
                "loaded_count": len(loaded),
                "skipped_count": skipped,
                "root": str(root),
            },
        )
        return loaded

    # --------------------------------------------------------- internals

    def _load_one(self, skill_md: Path) -> LoadedSkill:
        """Parse a single ``SKILL.md`` into a ``LoadedSkill``.

        Raises a ``SkillSkipped`` subclass on every failure
        mode in Requirement 2. Caller catches and logs.
        """
        try:
            text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ReadFailed(f"{type(exc).__name__}: {exc}") from exc

        yaml_text, _body = _split_frontmatter(text)
        raw = _parse_frontmatter(yaml_text)

        # The frontmatter ``name`` field is informational; the
        # canonical name is the directory basename. Mismatch
        # logs a warning but doesn't reject the skill
        # (Requirement 2.3).
        canonical_name = skill_md.parent.name
        frontmatter_name, description, prerequisites = _validate_fields(raw)

        if frontmatter_name != canonical_name:
            _log.warning(
                "skills.name_mismatch",
                extra={
                    "event": "skills.name_mismatch",
                    "frontmatter_name": frontmatter_name,
                    "directory_name": canonical_name,
                    "path": str(skill_md),
                },
            )

        # Unknown frontmatter keys log at DEBUG, don't reject
        # (Requirement 2.5).
        unknown_keys = sorted(set(raw.keys()) - {"name", "description", "prerequisites"})
        if unknown_keys:
            _log.debug(
                "skills.unknown_keys",
                extra={
                    "event": "skills.unknown_keys",
                    "skill_name": canonical_name,
                    "unknown_keys": unknown_keys,
                },
            )

        rendered_description, truncated = _truncate_description(description, canonical_name)

        return LoadedSkill(
            name=canonical_name,
            description=rendered_description,
            prerequisites=tuple(prerequisites),
            skill_md_path=skill_md.resolve(),
            description_truncated=truncated,
        )


# --------------------------------------------------------------- frontmatter helpers


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split ``text`` into ``(yaml_text, body)`` along the
    leading ``---`` fences.

    Raises:

    * ``MissingOpenFence`` — first line is not ``---``
      (Requirement 2.6).
    * ``MissingCloseFence`` — opening fence found but no
      closing ``---`` within ``_FRONTMATTER_MAX_LINES``
      (Requirement 2.7).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise MissingOpenFence("first line is not '---'")

    # Search for the closing fence. The opening fence is line 0;
    # we scan from line 1 up to the cap. Lines past the cap mean
    # the file forgot to close the fence — the parser bound is
    # a hang protection, not an artificial limit.
    close_idx = None
    for i in range(1, min(len(lines), _FRONTMATTER_MAX_LINES)):
        if lines[i].strip() == _FENCE:
            close_idx = i
            break

    if close_idx is None:
        raise MissingCloseFence(f"no closing '---' within {_FRONTMATTER_MAX_LINES} lines")

    yaml_text = "\n".join(lines[1:close_idx])
    body = "\n".join(lines[close_idx + 1 :])
    return yaml_text, body


def _parse_frontmatter(yaml_text: str) -> dict[str, Any]:
    """Parse the YAML between the fences, raising
    ``MalformedYaml`` on any error (Requirement 2.8) or if the
    result isn't a mapping."""
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise MalformedYaml(str(exc)) from exc

    if raw is None:
        # Empty frontmatter (just ``---\n---``). Treat as
        # missing required fields rather than malformed YAML;
        # the user-facing error is more actionable.
        raise MissingRequiredField("frontmatter is empty")

    if not isinstance(raw, dict):
        raise WrongFieldType(f"frontmatter root is {type(raw).__name__}, expected mapping")

    return raw


def _validate_fields(
    raw: dict[str, Any],
) -> tuple[str, str, list[str]]:
    """Pull ``name``, ``description``, ``prerequisites`` from
    the parsed frontmatter and validate types + bounds per
    Requirement 2.2.

    Returns the validated values. Raises a ``SkillSkipped``
    subclass on any failure.
    """
    name = raw.get("name")
    description = raw.get("description")
    prerequisites = raw.get("prerequisites", [])

    # Required fields present (Requirement 2.9 — including
    # whitespace-only).
    if name is None:
        raise MissingRequiredField("missing 'name'")
    if description is None:
        raise MissingRequiredField("missing 'description'")

    # Type checks (Requirement 2.10).
    if not isinstance(name, str):
        raise WrongFieldType(f"name is {type(name).__name__}, expected str")
    if not isinstance(description, str):
        raise WrongFieldType(f"description is {type(description).__name__}, expected str")

    name_stripped = name.strip()
    description_stripped = description.strip()

    if not name_stripped:
        raise MissingRequiredField("'name' is whitespace-only")
    if not description_stripped:
        raise MissingRequiredField("'description' is whitespace-only")

    # Length bounds (Requirement 2.2).
    if len(name_stripped) > _NAME_MAX_LEN:
        raise FieldOutOfBounds(f"name {len(name_stripped)} codepoints exceeds {_NAME_MAX_LEN}")
    if len(description_stripped) > _DESCRIPTION_MAX_LEN:
        raise FieldOutOfBounds(
            f"description {len(description_stripped)} codepoints exceeds {_DESCRIPTION_MAX_LEN}"
        )

    # Prerequisites: must be a list of strings, each within bounds
    # (Requirement 2.10 + 2.2).
    if prerequisites is None:
        prerequisites_list: list[str] = []
    elif isinstance(prerequisites, list):
        prerequisites_list = []
        for entry in prerequisites:
            if not isinstance(entry, str):
                raise WrongFieldType(f"prerequisites entry is {type(entry).__name__}, expected str")
            entry_stripped = entry.strip()
            if not entry_stripped:
                raise FieldOutOfBounds("prerequisites entry is empty")
            if len(entry_stripped) > _PREREQ_NAME_MAX_LEN:
                raise FieldOutOfBounds(
                    f"prerequisites entry {len(entry_stripped)} codepoints "
                    f"exceeds {_PREREQ_NAME_MAX_LEN}"
                )
            prerequisites_list.append(entry_stripped)
    else:
        raise WrongFieldType(f"prerequisites is {type(prerequisites).__name__}, expected list")

    if len(prerequisites_list) > _PREREQ_MAX_COUNT:
        raise FieldOutOfBounds(
            f"prerequisites has {len(prerequisites_list)} entries, exceeds {_PREREQ_MAX_COUNT}"
        )

    return name_stripped, description_stripped, prerequisites_list


def _truncate_description(description: str, name: str) -> tuple[str, bool]:
    """Apply Requirement 2.4's render-time truncation cap.

    Returns ``(rendered, was_truncated)``. The frontmatter cap
    (1000 codepoints) is much higher than the render cap (80)
    because the operator should be able to write a sentence;
    only the displayed form is short.
    """
    if len(description) <= _DESCRIPTION_RENDER_LIMIT:
        return description, False

    truncated = description[:_DESCRIPTION_RENDER_LIMIT] + "..."
    _log.warning(
        "skills.description_truncated",
        extra={
            "event": "skills.description_truncated",
            "skill_name": name,
            "original_chars": len(description),
            "rendered_chars": len(truncated),
        },
    )
    return truncated, True


def _dedupe_by_name(skills: list[LoadedSkill]) -> tuple[list[LoadedSkill], int]:
    """Drop case-insensitive duplicate names per Requirement 1.8.

    Two skills with the same case-folded name (e.g.
    ``WebSearch/`` and ``websearch/`` on a case-insensitive
    filesystem) keep the lex-first by absolute path. Returns
    ``(deduped, dropped_count)`` and logs one WARNING per
    duplicate.
    """
    by_lower: dict[str, list[LoadedSkill]] = {}
    for s in skills:
        by_lower.setdefault(s.name.lower(), []).append(s)

    deduped: list[LoadedSkill] = []
    dropped = 0
    for group in by_lower.values():
        if len(group) == 1:
            deduped.append(group[0])
            continue

        # Keep the lex-first absolute path. Stable sort by
        # ``str(path)`` matches Requirement 1.8's "ascending
        # case-sensitive lexicographic order".
        group_sorted = sorted(group, key=lambda s: str(s.skill_md_path))
        kept = group_sorted[0]
        for skipped in group_sorted[1:]:
            dropped += 1
            _log.warning(
                "skills.skipped",
                extra={
                    "event": "skills.skipped",
                    "path": str(skipped.skill_md_path),
                    "reason": "duplicate-name",
                    "detail": (
                        f"case-insensitive duplicate of '{kept.name}' at {kept.skill_md_path}"
                    ),
                },
            )
        deduped.append(kept)

    return deduped, dropped


# --------------------------------------------------------------- renderer


def render_skills_block(
    skills: Iterable[LoadedSkill],
    tool_registry: ToolRegistry,
    *,
    fitt_home: Path | None = None,
) -> str:
    """Render the ``[Skills available]`` system-prompt block.

    Returns ``""`` when ``skills`` is empty (Requirement 3.4 —
    caller drops the block entirely; no header, no placeholder
    line).

    Sort: case-insensitive lex by name, ties broken by case-
    sensitive lex of ``skill_md_path`` (Requirement 3.5).

    Per-skill line shape (Requirement 3.3 + 3.6 + 8.1):

        - <name>: <description> (read recipe with read_file project=fitt path=<rel>)[; needs: a, b][[unavailable: a]]

    The recipe-load hint resolves the SKILL.md via the built-in
    ``fitt`` pseudo-project (see
    :func:`gateway.tools.fileops._maybe_resolve_builtin_fitt_project`).
    That gives the agent a working tool call shape without
    requiring operator-side ``projects.yaml`` configuration.
    Falls back to an absolute path when the skill lives outside
    ``$FITT_HOME`` — that recipe won't be loadable as-is via
    ``project=fitt``, but the hint is at least factually
    accurate about where the file lives.

    The ``; needs:`` segment appears only when the skill
    declares prerequisites; the ``[unavailable: ...]`` segment
    appears only when one or more declared prerequisites are
    missing from the live ToolRegistry. Both segments stack
    in that order on the same line, before the line's
    terminating newline.

    ``fitt_home`` defaults to the gateway's configured FITT_HOME
    when omitted (production caller); tests pass an explicit
    ``Path`` for determinism.
    """
    skills_list = list(skills)
    if not skills_list:
        return ""

    if fitt_home is None:
        # Lazy import to avoid the gateway.config -> gateway.tools
        # circular when this module is imported during config
        # bootstrap.
        from .config import fitt_home as _fitt_home

        fitt_home = _fitt_home()
    fitt_home_resolved = fitt_home.resolve()

    # Live tool name set for the prerequisite check
    # (Requirement 8.1). One snapshot per render; the set is
    # tiny and the check is per-prerequisite.
    available_tools = set(tool_registry.list_names())

    sorted_skills = sorted(
        skills_list,
        key=lambda s: (s.name.casefold(), str(s.skill_md_path)),
    )

    lines = [
        "[Skills available]",
        (
            "Each skill below provides a recipe for a specific task. "
            "When the user's request matches a skill's description, "
            "load the recipe with the read_file call shown in "
            "parentheses, then follow it."
        ),
    ]
    for s in sorted_skills:
        # Description with embedded newlines collapsed
        # (Requirement 3.3).
        desc = s.description.replace("\r", " ").replace("\n", " ").strip()

        recipe_hint = _format_recipe_hint(s.skill_md_path, fitt_home_resolved)
        line = f"- {s.name}: {desc} {recipe_hint}"

        if s.prerequisites:
            line += "; needs: " + ", ".join(s.prerequisites)

        missing = [p for p in s.prerequisites if p not in available_tools]
        if missing:
            line += "[unavailable: " + ", ".join(missing) + "]"

        lines.append(line)

    return "\n".join(lines)


def _format_recipe_hint(skill_md_path: Path, fitt_home_resolved: Path) -> str:
    """Build the ``(read recipe with ...)`` segment of a skill line.

    When the skill lives under ``$FITT_HOME``, emit the
    ``project=fitt`` form so the model can call the existing
    ``read_file`` tool against the built-in pseudo-project.
    Otherwise fall back to an absolute path — that recipe won't
    be loadable as-is via the standard fileops surface, but the
    hint is at least factually accurate about where the file
    lives.
    """
    try:
        rel = skill_md_path.resolve().relative_to(fitt_home_resolved)
    except ValueError:
        return f"(read recipe with read_file {skill_md_path})"

    rel_posix = rel.as_posix()
    return f"(read recipe with read_file project=fitt path={rel_posix})"

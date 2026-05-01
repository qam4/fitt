"""Project registry: tells FITT where each project lives.

A project is a logical code workspace: a name, a filesystem path,
optionally an SSH host to reach that path, and optional default
commands (build / test). The registry is the source of truth for
tools that need to touch a project's files: a tool call carries a
``project`` argument, the tool looks the project up here, and the
SSH backend wraps the operation in ``ssh <host> '...'`` when
``ssh_host`` is non-empty.

Projects live in ``~/.fitt/projects.yaml``. Editing the file by
hand or via the CLI has the same effect; the gateway re-reads on
every lookup so changes are visible without a restart.

Structure::

    projects:
      - name: home-ai-cluster
        ssh_host: ""                    # empty = hub-local
        path: /share/Public/home-ai-cluster
        test_command: "cd gateway && uv run pytest -q"
        build_command: "cd gateway && uv run ruff check src tests"

      - name: retro-ai
        ssh_host: laptop.tailnet
        path: /home/fred/code/retro-ai
        test_command: "pytest -q"

The registry is deliberately re-read on every call rather than
watched with a file listener, matching the pattern in
``sessions.py``. Projects tend to be small (dozens of entries at
most) and lookups are not on a hot path, so the simplicity is
worth more than a marginal perf win.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import GatewayError

_log = logging.getLogger(__name__)

PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
"""Lowercase letters, digits, dot, underscore, hyphen. Must start
with a letter or digit. Matches common project-naming conventions
(kebab-case, snake_case, dotted names)."""

_MAX_NAME_LEN = 64
_DEFAULT_CONFIG_FILENAME = "projects.yaml"


# ----------------------------------------------------------------- data


@dataclass(frozen=True)
class Project:
    """One registered project."""

    name: str
    path: str
    ssh_host: str = ""
    test_command: str = ""
    build_command: str = ""

    @property
    def is_local(self) -> bool:
        """True if the project lives on the hub (no ssh_host)."""
        return self.ssh_host == ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "ssh_host": self.ssh_host,
            "test_command": self.test_command,
            "build_command": self.build_command,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Project:
        return cls(
            name=str(raw["name"]),
            path=str(raw["path"]),
            ssh_host=str(raw.get("ssh_host", "") or ""),
            test_command=str(raw.get("test_command", "") or ""),
            build_command=str(raw.get("build_command", "") or ""),
        )


# ----------------------------------------------------------------- errors


class ProjectError(GatewayError):
    """Base for project-registry errors."""


class InvalidProjectName(ProjectError):
    """Name doesn't match the allowed pattern."""


class DuplicateProject(ProjectError):
    """A project with the given name is already registered."""


class UnknownProject(ProjectError):
    """The requested project name isn't in the registry."""

    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(f"unknown project {name!r}; known: {sorted(available)}")
        self.name = name
        self.available = sorted(available)


class InvalidProjectPath(ProjectError):
    """The project's path is empty or otherwise malformed."""


# ----------------------------------------------------------------- registry


def default_projects_path() -> Path:
    """Return the default ``projects.yaml`` path.

    Honours ``FITT_PROJECTS_PATH`` for tests; otherwise lives next
    to the other FITT runtime files under ``FITT_HOME``.
    """
    env = os.environ.get("FITT_PROJECTS_PATH")
    if env:
        return Path(env)
    from .config import fitt_home

    return fitt_home() / _DEFAULT_CONFIG_FILENAME


@dataclass
class _LoadedIndex:
    projects: dict[str, Project] = field(default_factory=dict)


class ProjectRegistry:
    """On-disk project registry backed by a YAML file.

    Re-reads on every public method call so edits to the file (via
    CLI or hand edit) are visible immediately.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._path = config_path or default_projects_path()

    # ---------- lifecycle -----------------------------------------

    def ensure_exists(self) -> None:
        """Create an empty projects.yaml if none exists.

        Does not create the parent directory tree; that's the
        caller's responsibility (normally the gateway startup
        already created ``$FITT_HOME``).
        """
        if self._path.exists():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._save(_LoadedIndex())
        _log.info("projects.file_created", extra={"path": str(self._path)})

    # ---------- read ----------------------------------------------

    def all(self) -> list[Project]:
        idx = self._load()
        return sorted(idx.projects.values(), key=lambda p: p.name)

    def get(self, name: str) -> Project:
        idx = self._load()
        project = idx.projects.get(name)
        if project is None:
            raise UnknownProject(name, list(idx.projects.keys()))
        return project

    def known_names(self) -> list[str]:
        return sorted(self._load().projects.keys())

    # ---------- write ---------------------------------------------

    def add(self, project: Project) -> Project:
        self._validate(project)
        idx = self._load()
        if project.name in idx.projects:
            raise DuplicateProject(f"project {project.name!r} already registered")
        idx.projects[project.name] = project
        self._save(idx)
        _log.info(
            "projects.added",
            extra={"project_name": project.name, "ssh_host": project.ssh_host or "(local)"},
        )
        return project

    def update(self, name: str, **fields: Any) -> Project:
        """Update one or more fields on an existing project.

        Unknown field names are ignored with a warning (rather than
        raised) so forward-compatibility is easy: a newer CLI writing
        a field an older gateway doesn't know about doesn't crash.
        """
        idx = self._load()
        existing = idx.projects.get(name)
        if existing is None:
            raise UnknownProject(name, list(idx.projects.keys()))
        allowed = {"path", "ssh_host", "test_command", "build_command"}
        replacement = existing.to_dict()
        for key, value in fields.items():
            if key not in allowed:
                _log.warning("projects.update.unknown_field", extra={"field": key})
                continue
            replacement[key] = value
        updated = Project.from_dict(replacement)
        self._validate(updated)
        idx.projects[name] = updated
        self._save(idx)
        return updated

    def remove(self, name: str) -> None:
        idx = self._load()
        if name not in idx.projects:
            raise UnknownProject(name, list(idx.projects.keys()))
        del idx.projects[name]
        self._save(idx)
        _log.info("projects.removed", extra={"project_name": name})

    # ---------- internals -----------------------------------------

    def _validate(self, project: Project) -> None:
        if not PROJECT_NAME_PATTERN.match(project.name):
            raise InvalidProjectName(
                f"project name {project.name!r} is invalid; must match "
                f"{PROJECT_NAME_PATTERN.pattern}"
            )
        if len(project.name) > _MAX_NAME_LEN:
            raise InvalidProjectName(
                f"project name {project.name!r} exceeds {_MAX_NAME_LEN} characters"
            )
        if not project.path or not project.path.strip():
            raise InvalidProjectPath(f"project {project.name!r} has empty path")

    def _load(self) -> _LoadedIndex:
        if not self._path.exists():
            return _LoadedIndex()
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            _log.warning(
                "projects.read_failed",
                extra={"path": str(self._path), "error": str(e)},
            )
            return _LoadedIndex()
        return _parse_index(raw)

    def _save(self, idx: _LoadedIndex) -> None:
        """Write the registry atomically (temp file + rename).

        Same pattern as ``sessions.py``: a crash mid-write cannot
        leave a half-written file.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "projects": [p.to_dict() for p in idx.projects.values()],
        }
        fd, tmp = tempfile.mkstemp(
            prefix="projects-",
            suffix=".yaml.tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(payload, fh, sort_keys=False, default_flow_style=False)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _parse_index(raw: Any) -> _LoadedIndex:
    idx = _LoadedIndex()
    if not isinstance(raw, dict):
        return idx
    entries = raw.get("projects", [])
    if not isinstance(entries, list):
        return idx
    for entry in entries:
        if not isinstance(entry, dict):
            _log.warning("projects.entry_skip", extra={"reason": "not_a_dict"})
            continue
        try:
            project = Project.from_dict(entry)
        except (KeyError, ValueError, TypeError) as e:
            _log.warning(
                "projects.entry_skip",
                extra={"reason": "invalid", "error": str(e)},
            )
            continue
        if not PROJECT_NAME_PATTERN.match(project.name):
            _log.warning(
                "projects.entry_skip",
                extra={"reason": "invalid_name", "project_name": project.name},
            )
            continue
        idx.projects[project.name] = project
    return idx

"""Hardcoded deny list for shell-executing tools.

This list is **not user-configurable**. Changing it requires a code
change, a PR, a test. The point is to catch the small set of
patterns that are so obviously destructive that no policy toggle
should enable them. Philosophy:

* **Pattern-based allowlists are the wrong primitive.** See the
  FAQ entry on ``sleep *`` — operators can't write a shell grammar
  in YAML, so any opt-in "trust this pattern" is a smuggler's
  market. That's why approval is per-tool-invocation, not
  per-command-pattern.

* **Deny patterns are different.** They're the *floor*, not the
  ceiling. False negatives (something nasty slips through) aren't
  unique to us — the approval middleware still catches most
  risky calls at the ASK bucket. False positives (benign command
  incorrectly blocked) are rare and easy to patch by editing the
  pattern.

* **Phase 4 has no tool that feeds model-controlled strings into
  a shell context.** Curated tools (``write_file``,
  ``git_commit``, etc.) use argv isolation or stdin, so no
  pattern-match is needed. This module exists to be *ready* for a
  future ``project_shell`` tool (see follow-up F2) without a
  scramble when that lands.

The API is a single ``check(command)`` function that returns the
matched pattern (``str``) on a hit, or ``None`` if the command is
allowed. Callers use the return value to populate a
``ApprovalDecision.blocked(detail=...)`` with a human-readable
reason.

Every pattern in ``DENY_PATTERNS`` has a positive test (catches
the destructive form) and a negative test (doesn't over-match a
benign command). See ``test_deny_list.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DenyPattern:
    """A single deny rule: the pattern and a human-readable label.

    The label goes into the approval-decision detail so the user
    (and the model) sees *why* the command was rejected. Lives
    next to the pattern so a pattern change means an explanation
    change, no drift."""

    pattern: re.Pattern[str]
    label: str


# Patterns are evaluated in order; first match wins. Ordering
# doesn't matter for correctness (we block on any hit) but it
# affects which label gets reported, so put the narrower patterns
# first when a command could match multiple.
DENY_PATTERNS: list[DenyPattern] = [
    # ---- filesystem nukes ----------------------------------------
    DenyPattern(
        re.compile(r"\brm\s+-rf?\s+/(?:$|\s|\*)"),
        "rm -rf against root filesystem",
    ),
    DenyPattern(
        re.compile(r"\brm\s+-rf?\s+~(?:$|\s)"),
        "rm -rf against home directory",
    ),
    DenyPattern(
        re.compile(r"\brm\s+-rf?\s+\$HOME\b"),
        "rm -rf against $HOME",
    ),
    DenyPattern(
        re.compile(r"\brm\s+-rf?\s+\.git(?:$|\s|/)"),
        "rm -rf against .git directory",
    ),
    # ---- destructive git -----------------------------------------
    DenyPattern(
        re.compile(r"\bgit\s+push\s+(?:\S+\s+)*(?:--force|-f)(?![a-zA-Z-])"),
        "git push --force / -f (rewrites remote history)",
    ),
    DenyPattern(
        re.compile(r"\bgit\s+reset\s+--hard\s+origin\b"),
        "git reset --hard against origin (discards local work)",
    ),
    # ---- arbitrary code from the internet ------------------------
    DenyPattern(
        re.compile(r"\bcurl\b[^|]*\|\s*(?:bash|sh|zsh|python|python3)\b"),
        "curl piped to an interpreter",
    ),
    DenyPattern(
        re.compile(r"\bwget\b[^|]*\|\s*(?:bash|sh|zsh|python|python3)\b"),
        "wget piped to an interpreter",
    ),
    # ---- block-device disasters ----------------------------------
    DenyPattern(
        re.compile(r"\bdd\b[^\n]*\bof=/dev/sd"),
        "dd writing to a block device",
    ),
    DenyPattern(
        re.compile(r"\bmkfs\.[a-z0-9]+\b"),
        "mkfs (reformat a filesystem)",
    ),
    DenyPattern(
        re.compile(r"\bchmod\s+-R\s+777\s+/"),
        "chmod -R 777 against root",
    ),
    # ---- fork bomb ------------------------------------------------
    DenyPattern(
        re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
        "fork bomb",
    ),
    # ---- host takedown -------------------------------------------
    DenyPattern(
        re.compile(r"\bshutdown\s+-h\b"),
        "system shutdown",
    ),
    DenyPattern(
        re.compile(r"\breboot\s+(?:-f|--force)\b"),
        "forced reboot",
    ),
    # ---- database drops ------------------------------------------
    DenyPattern(
        re.compile(r"\bDROP\s+(?:DATABASE|SCHEMA|TABLE)\b", re.IGNORECASE),
        "SQL DROP DATABASE/SCHEMA/TABLE",
    ),
    # ---- cloud-bucket nukes --------------------------------------
    DenyPattern(
        re.compile(r"\baws\s+s3\s+rb\s+\S+\s+.*--force\b"),
        "aws s3 rb --force (remove bucket and contents)",
    ),
    # ---- docker wipe ----------------------------------------------
    DenyPattern(
        re.compile(
            r"\bdocker\s+(?:system\s+)?prune\s+(?=.*--volumes)(?=.*--all)",
        ),
        "docker prune with --volumes --all (nukes all containers + volumes)",
    ),
]


def check(command: str) -> DenyPattern | None:
    """Return the first matching DenyPattern, or None if ``command``
    is allowed.

    Matching is substring-sensitive (``re.search``). Commands are
    expected to be the full command string that would be executed,
    not an argv list — we want to catch ``sh -c "rm -rf /"`` as
    well as a direct ``rm -rf /``.
    """
    for entry in DENY_PATTERNS:
        if entry.pattern.search(command):
            return entry
    return None

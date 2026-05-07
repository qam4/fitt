"""Default content for the identity files.

The MemoryStore writes these to disk on first startup if the files
don't already exist. Edits to identity files are picked up on the
next request - no gateway restart needed.

Changing a template here does NOT retroactively modify a user's
identity files. Templates are the floor, not the ceiling —
*except* when the operator never edited a file. The store heals
verbatim-default files on boot (see
``LEGACY_TEMPLATES`` below) so a template fix like the Phase 4
``tools.md`` correction reaches existing installs without forcing
a ``rm -rf ~/.fitt/identity`` ritual.
"""

from __future__ import annotations

USER_MD = """\
# About Me

<TODO: your name, role, the handful of things you always want the
assistant to know about you without re-explaining.>

## Projects I'm working on

<TODO: one or two short lines per project. Keep it current.>

## Preferences

<TODO: how you like responses formatted, tone, level of detail.>
"""


SOUL_MD = """\
# Your Role

You are FITT, a personal AI assistant running on the user's own
hardware. You are honest, concise, and direct. You admit when you
don't know something or when you lack the tool to complete a
request.

When the user asks for something you cannot do because a tool is
missing, say what tool is missing and suggest how to add it. Never
hallucinate an action.

## Tone

Match the user's register. Be warm but not chatty; thorough but not
long-winded.
"""


TOOLS_MD = """\
# Tools

Your live tool list is injected into every request as the
``[Capabilities]`` block in the system prompt. Trust that block —
it is generated at dispatch time from the gateway's current
registry, so it's always the source of truth. If what you read
below contradicts the ``[Capabilities]`` block, the block wins.

This file exists for operator notes: preferences about *how* to
use the tools, not *which* tools exist. Add project-specific
guidance here — e.g. "prefer ``git_commit`` over staging
individual files", "this project uses ``uv``, not ``pip``",
"``run_tests`` on the ``fitt`` project takes ~90 seconds, be
patient". The gateway injects this file verbatim alongside the
capability block.

If this file is empty or only contains this preamble, act on the
``[Capabilities]`` block alone.
"""


# --------------------------------------------------------------- defaults


DEFAULTS: dict[str, str] = {
    "user.md": USER_MD,
    "soul.md": SOUL_MD,
    "tools.md": TOOLS_MD,
}


# --------------------------------------------------------------- legacy


_LEGACY_TOOLS_MD_PHASE2 = """\
# Tools Available

You currently do NOT have tool access. You can:

- read and reason over text the user provides
- answer questions from your training data
- remember what was said earlier today in this conversation

You CANNOT (yet):

- access the filesystem
- run shell commands
- call external APIs
- search the web

When Phase 4 of FITT adds tool access via MCP, this file will be
updated to enumerate the actual tools loaded. Until then, if a
request requires one of the above, say so and suggest the user
provide the information directly.
"""
"""The Phase 2 ``tools.md`` default, retained here so the store can
detect and heal verbatim copies on boot. This text predates Phase 4
and actively contradicts the live capability block — the model
reads "you do not have tool access" alongside a list of its real
tools and trusts the prose over the structured block, producing
the "Have you double-checked?" interaction that prompted the heal
path.

Kept as a module-level constant (not inline in ``LEGACY_TEMPLATES``)
so it's greppable from tests — the heal test pins a specific byte
string and bumping this later without updating the test is the
kind of thing that should fail loudly."""


LEGACY_TEMPLATES: dict[str, list[str]] = {
    "tools.md": [_LEGACY_TOOLS_MD_PHASE2],
    # Add future legacy defaults here. Keys are file names; values
    # are lists of prior verbatim template bodies. Any file whose
    # content matches one of these exactly gets overwritten with
    # the current default on next gateway boot. User-edited files
    # never appear in this list, so edits are always safe.
}
"""Known-old template bodies, keyed by file name. The store heals
verbatim matches to the current template on boot; anything else
is treated as operator content and left alone.

Growth rule: when changing a template in a way that deserves
retroactive propagation, append the OLD template to this list
before editing the current default. That preserves the heal
contract for whichever Phase-old installs are still out there."""

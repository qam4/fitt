"""Coding-agent eval suite (router-mode use case).

Why this exists
---------------

FITT is explicitly **not** a coding agent (per project-overview
steering). But router mode (``X-FITT-Client: coding-agent``)
exists so operators can drive OpenCode / Cursor / Claude Code
through FITT's gateway for alias resolution + cost tracking
+ audit. Those coding agents own their own system prompt,
tool surface, and approval UX; FITT bypasses memory injection
and tool merging for them.

When picking a model to bind to ``fitt-default`` or
``fitt-smart`` for router-mode coding use, the existing
:mod:`gateway.alias_eval` default suite is *necessary but not
sufficient*. It tests the model under FITT's prompts and tool
shapes; OpenCode brings a different prompt and tool surface.
A model that passes the default suite at 5/5 may still narrate
``edit_file`` calls under OpenCode's harness, or vice versa.

This module ships a parallel suite that mimics what a coding
agent's request looks like at the wire:

* ~2K-token coding-agent system prompt (derived, not lifted —
  real OpenCode / Cursor prompts evolve and we don't want our
  eval to break on their releases).
* Tool schemas shaped like the ones coding agents typically
  offer: ``read_file``, ``edit_file``, ``glob_search``, and
  ``shell``. Same OpenAI-style ``tool_calls`` contract.
* Cases that exercise: read-shape, edit-shape (write-side),
  search-shape, shell-shape, and a no-tool case for the
  over-eager check.

Same shape-level classification as the default suite (real
``tool_calls`` field is the signal, not regex on content).
The :class:`EvalCase` and :class:`CaseResult` dataclasses
from :mod:`gateway.alias_eval` are reused unchanged — this
is just a different cases list.

Deliberately NOT in scope
-------------------------

* **Not a code-quality eval.** "Did the model emit an
  ``edit_file`` tool_call?" is what we measure. Whether the
  proposed edit was correct is a different question — that
  one only honest live-coding can answer.
* **Not bound to a specific coding agent.** The system
  prompt and tool shapes are derived plausibly, not lifted
  from OpenCode / Cursor / Claude Code. Bindings that pass
  here are likely-but-not-guaranteed to work in any one of
  them; bindings that fail here are very unlikely to work
  in any of them.
* **Not a load test.** Five cases, sequential, ~30-60s
  same as the default suite. Adding more cases dilutes
  signal and costs tokens.

Output
------

Persisted to ``$FITT_HOME/eval/<alias>-coding-latest.md``
plus the timestamped audit trail
``$FITT_HOME/eval/<alias>-coding-<timestamp>.md``. Same
markdown format as the default suite. The dashboard's eval
view surfaces both reports side-by-side when both exist.
"""

from __future__ import annotations

from typing import Any

from .alias_eval import EvalCase

# --------------------------------------------------------------- system prompt


# Padded to ~2K tokens of plausible coding-agent boilerplate.
# Derived from publicly-documented coding-agent shapes
# (OpenCode, Aider, Cursor, Claude Code) without lifting
# any of them verbatim. Goal: realistic prompt-size pressure
# on small-parameter models, not framework-specific behaviour.
_CODING_AGENT_SYSTEM_PROMPT = """\
You are a coding assistant operating inside an integrated
development environment. You help the user read, understand,
and modify a software project. The project is a multi-file
codebase you can navigate using the tools provided. You do
NOT have direct shell access outside the project's working
directory; the ``shell`` tool is your only way to run
commands and it's gated to the project root.

# Operating principles

You are direct and concise in your explanations, but thorough
and complete when writing code. When the user asks for a
change, you read the relevant code first to understand its
shape, conventions, and surrounding context, then propose
the smallest correct change that satisfies the request.
You match the project's existing style: indentation,
quotation marks, naming conventions, and import patterns
follow what's already in the codebase, not what you'd
write from scratch.

You communicate by emitting structured tool calls. Every
file edit goes through the ``edit_file`` tool — never paste
diffs or code blocks in your reply when an edit is the
goal. Every shell command goes through the ``shell`` tool.
Every file read goes through ``read_file``. You DO NOT
fabricate file contents, command outputs, or test results —
if you need to know something, call the tool.

# Working with the project

Before any non-trivial change, you read the relevant files
to confirm what's there. You favour ``glob_search`` to find
files by name pattern when the user mentions a file you
haven't seen, and ``grep_search`` when the user describes
behaviour rather than file location. You read first; you
edit second; you verify with ``shell`` (running tests, a
quick build, or a grep) when the change is large enough
that verification matters.

You are honest about uncertainty. When you don't know what
a function does, you say so and read it. When the user
asks for something the project's structure doesn't support,
you explain the constraint instead of fabricating a
workaround. When a tool fails — file not found, command
exits non-zero, edit conflicts with a concurrent change —
you report the failure and ask before retrying with a
different approach.

# Style

Match the project's language and idioms. Don't introduce
new dependencies without asking. Don't refactor neighbouring
code unless the user asked. Don't add comments that explain
what the code obviously does; do add comments when the
'why' is non-obvious. Don't write tests unless asked, but
when you do write tests, write them in the project's
existing test style and harness, not your favourite
framework. Avoid speculative generality; build what was
asked for, not what might be useful later.

When you finish a change, you summarise what you did in
plain English at the end of your reply. The summary lists
the files touched, what changed in each, and any
follow-ups the user should know about (a test that's now
failing, a related code path you noticed but didn't touch,
a TODO you saw on the way through). Keep the summary short
— the diff is the source of truth, the prose is just the
TL;DR. If the change was trivial (a typo, a one-line
config tweak), the summary can be one sentence.

# Tool use discipline

Tool calls happen in the ``tool_calls`` field of your
response, not as text in ``content``. The harness parses
``tool_calls``; text that looks like a tool call but lives
in ``content`` is ignored and the user sees the raw text
instead of the tool's effect. This is a common failure
mode for models that haven't been post-trained for
OpenAI-format tool calling — be careful to emit the
structured field.

When the user asks a meta question — "what tools do you
have," "what version of Python is this," "how big is
this project" — answer with prose, not a tool call,
unless the question genuinely requires a tool to answer.
Don't call ``shell`` to run ``pwd`` if the user asked
"where am I"; you already know.

When the user makes small talk that doesn't require a
tool, reply in prose. The over-eager habit of reaching
for a tool on every turn — calling ``read_file`` when
the user said "thanks" — wastes tokens and confuses
the user. Only call a tool when the answer genuinely
requires one.
"""


# --------------------------------------------------------------- tools


def _read_file_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Project-relative file path.",
                    },
                },
                "required": ["path"],
            },
        },
    }


def _edit_file_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Apply an edit to a file. The edit is described as a unified diff "
                "or a search-and-replace pair, depending on the harness's "
                "preferred shape."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Project-relative file path.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "Exact text to replace.",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    }


def _glob_search_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "Find files by name pattern (e.g. '**/*.py').",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern relative to the project root.",
                    },
                },
                "required": ["pattern"],
            },
        },
    }


def _shell_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "Run a shell command from the project root. Stdout, stderr, and "
                "exit code are returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute.",
                    },
                },
                "required": ["command"],
            },
        },
    }


# --------------------------------------------------------------- cases


def default_coding_cases() -> list[EvalCase]:
    """Return the canonical coding-agent suite.

    Five cases. Each one is paired with the
    :data:`_CODING_AGENT_SYSTEM_PROMPT` via the
    :attr:`EvalCase.system_prompt` field — same as the default
    suite's cases would do if they had a non-default prompt.
    Note: ``EvalCase`` doesn't currently carry a system_prompt
    field; the runner injects the prompt via the
    ``messages`` array. Until the runner gains explicit
    system-prompt support per case, the prompt rides as a
    leading system message inside the case's ``prompt``
    parameter via the dispatcher.

    Implementation note: the runner sends the ``prompt`` as
    a single user message. To inject the coding-agent system
    prompt, we currently prepend it to the user prompt with
    a separator. Cleaner would be to extend
    :class:`EvalCase` with an optional ``system_prompt``
    field; pick that up if/when adding more suites makes the
    string-prepending pattern hurt.

    The cases:

    * ``code_read_basic`` — single read_file tool offered;
      user asks the model to read a specific file.
    * ``code_edit_basic`` — read_file + edit_file offered;
      user asks for a small edit.
    * ``code_glob_search`` — glob_search offered alone;
      user asks to find files.
    * ``code_shell_basic`` — shell offered alone; user asks
      to run tests. This case is the most narration-prone
      historically — small models often emit shell commands
      as text in ``content`` instead of via ``tool_calls``.
    * ``code_no_tool_small_talk`` — full set offered;
      user makes small talk. Catches over-eager bindings.
    """
    read_tool = _read_file_tool()
    edit_tool = _edit_file_tool()
    glob_tool = _glob_search_tool()
    shell_tool = _shell_tool()

    full_set = [read_tool, edit_tool, glob_tool, shell_tool]

    # The coding-agent system prompt rides as the leading
    # block of every prompt; the runner currently doesn't
    # support a separate system_prompt field on EvalCase.
    sys_block = _CODING_AGENT_SYSTEM_PROMPT.rstrip() + "\n\n---\n\n"

    return [
        EvalCase(
            name="code_read_basic",
            prompt=(
                sys_block
                + "Read the file `src/main.py`. Use the read_file tool; don't paraphrase the file."
            ),
            tools=[read_tool],
            expected_tool="read_file",
            description=(
                "Baseline read-shape under realistic coding-agent prompt size. "
                "Catches bindings that wobble on tool-call discipline above ~2K tokens."
            ),
        ),
        EvalCase(
            name="code_edit_basic",
            prompt=(
                sys_block + "In `src/main.py`, change the variable `count` to `total`. "
                "Use the edit_file tool — don't paste a diff in your reply."
            ),
            tools=[read_tool, edit_tool],
            expected_tool="edit_file",
            description=(
                "Edit-shape with a sibling read tool offered. Catches "
                "bindings that pick read when an edit was asked for, or "
                "that emit the edit as a markdown diff in content."
            ),
        ),
        EvalCase(
            name="code_glob_search",
            prompt=(
                sys_block
                + "Find every Python file under the `tests/` directory. Use the glob_search tool."
            ),
            tools=[glob_tool],
            expected_tool="glob_search",
            description=(
                "Search-shape with a single tool. Confirms the binding "
                "doesn't only fire for read/edit names."
            ),
        ),
        EvalCase(
            name="code_shell_basic",
            prompt=(
                sys_block + "Run the project's test suite. Use the shell tool; the project's "
                "test command is `pytest -q`."
            ),
            tools=[shell_tool],
            expected_tool="shell",
            description=(
                "Shell-shape — the case that most often narrates on small "
                "models, since 'run a command' has a strong text-completion "
                "shape. A failure here is a strong signal the binding will "
                "narrate `shell` calls in real coding sessions."
            ),
        ),
        EvalCase(
            name="code_no_tool_small_talk",
            prompt=(
                sys_block
                + "What's a good way to remember which side of a fork goes on which side of the plate?"
            ),
            tools=full_set,
            expected_tool=None,
            description=(
                "All four tools offered; user asks something with no code "
                "relevance. A good binding answers in prose. Catches "
                "bindings that reach for `shell` to look something up "
                "instead of just answering."
            ),
        ),
    ]

# Requirements Document

Phase 4.10 — Skills-as-Markdown Loader.

## Introduction

This phase adds a skills loader to the FITT gateway. A skill is
a directory under `~/.fitt/skills/<skill-name>/` containing a
`SKILL.md` file with YAML frontmatter (name, description,
prerequisites) and a markdown body that describes a recipe the
agent can follow using existing tools (`project_shell`,
`http_get`, `read_file`, `send_message`, ...).

The loader runs at gateway startup, scans the skills directory,
parses every `SKILL.md`, and injects a `[Skills available]`
section into the system prompt listing each skill's name,
description, and `SKILL.md` path. The skill body is **not**
injected — it is read on demand by the agent via the existing
`read_file` tool when the agent decides a skill applies.

This is the cleanest single architectural upgrade FITT can make
in the OpenClaw/Hermes shape (per `docs/prior-art.md`). It opens
the door to dropping in MIT-licensed skill markdown from those
projects unchanged, and turns "fitt can't do X" moments into
markdown drops instead of code changes.

The phase is explicitly half-day scope and does NOT include:

- Skill discovery / installation tooling (Phase 7+ if at all).
- Frontmatter strict-mode validation (warnings only, never
  fatal).
- Platform gating (FITT runs where it runs).
- A skills marketplace, sync, or hot-reload.
- New tool dispatch — skills are markdown the agent reads, not
  code the gateway invokes.

## Glossary

- **Skill**: A directory under the configured skills root
  containing exactly one `SKILL.md` file plus optional
  `scripts/`, `references/`, `templates/` subdirectories. The
  skill body teaches the agent how to perform a task using
  existing tools.
- **SKILL.md**: A markdown file with YAML frontmatter (between
  two `---` lines at the top of the file) followed by a
  markdown body. The frontmatter declares `name`,
  `description`, and optional `prerequisites`. The body is
  free-form prose addressed to the agent.
- **Skills_Root**: The directory the loader scans for skills.
  Default `$FITT_HOME/skills/`. Configurable via
  `memory.skills_dir` in `config.yaml`.
- **Skills_Loader**: The gateway component that scans
  Skills_Root, parses each `SKILL.md`, and produces an ordered
  list of `LoadedSkill` records.
- **LoadedSkill**: A successfully parsed skill, carrying
  `name` (from directory name), `description`, `prerequisites`,
  and the absolute path to its `SKILL.md`.
- **Skills_Block**: The `[Skills available]` section the
  Skills_Loader produces, injected into the system prompt
  alongside the existing `[Capabilities]` block.
- **Gateway**: The FastAPI process under `gateway/`. Owns
  the system-prompt assembly path that the Skills_Block
  attaches to.
- **ToolRegistry**: The registry of FITT tools available at
  request time. Used by Requirement 8 to determine whether a
  skill's prerequisites are satisfied.

## Requirements

### Requirement 1: Scan the skills directory at startup

**User Story:** As an operator, I want the gateway to discover
every skill I have dropped under `~/.fitt/skills/` when it
starts, so I can add a new skill by creating one directory and
restarting the service.

#### Acceptance Criteria

1. WHEN the Gateway starts, THE Skills_Loader SHALL enumerate
   the immediate (one-level, non-recursive) subdirectories of
   Skills_Root and select those that contain a regular file (or
   symbolic link resolving to a regular file) whose case-
   sensitive basename equals the literal string `SKILL.md`.
2. WHEN the Skills_Loader selects a subdirectory under 1.1, THE
   Skills_Loader SHALL set the candidate skill's `name` to the
   subdirectory's basename verbatim.
3. IF an enumerated subdirectory of Skills_Root does not
   contain a `SKILL.md` matching 1.1, THEN THE Skills_Loader
   SHALL exclude that subdirectory from the LoadedSkills
   list and log exactly one INFO line carrying the
   subdirectory's absolute path and the literal reason
   `no SKILL.md`.
4. THE Skills_Loader SHALL exclude from enumeration any file
   or subdirectory at the Skills_Root level whose basename
   begins with `.`, and SHALL emit no log line for such
   exclusions.
5. IF Skills_Root does not exist on disk OR exists but is not
   a directory, THEN THE Skills_Loader SHALL produce an empty
   LoadedSkills list and log exactly one INFO line containing
   the absolute path checked and a discriminator distinguishing
   `not-found` from `not-a-directory`.
6. IF Skills_Root exists, is a directory, and contains zero
   subdirectories that satisfy 1.1, THEN THE Skills_Loader
   SHALL produce an empty LoadedSkills list without logging
   a warning.
7. IF a candidate `SKILL.md` cannot be opened for reading
   (permission denied, I/O error, or the path is not a regular
   file once resolved), THEN THE Skills_Loader SHALL exclude
   that skill from the LoadedSkills list and log exactly one
   WARNING line carrying the candidate's absolute path and a
   reason category distinguishing the failure mode.
8. IF two or more candidate subdirectories produce the same
   `name` after case-insensitive comparison (which can occur on
   case-insensitive filesystems such as Windows), THEN THE
   Skills_Loader SHALL load only the candidate whose absolute
   path sorts first under ascending case-sensitive
   lexicographic order, exclude the rest, and log one WARNING
   line per excluded duplicate naming both the kept and the
   skipped paths.

### Requirement 2: Parse SKILL.md frontmatter

**User Story:** As a skill author, I want my `SKILL.md` to
declare its name and description in a YAML block at the top of
the file, so the agent sees a one-line summary of what the
skill does without paying the token cost of the body.

#### Acceptance Criteria

1. WHEN the Skills_Loader reads a `SKILL.md`, THE Skills_Loader
   SHALL parse a YAML frontmatter block whose first line is the
   exact three-character string `---` followed by a line
   terminator, and whose terminating line is the same exact
   three-character string followed by a line terminator.
2. THE Skills_Loader SHALL accept these frontmatter fields with
   the listed bounds:
   - `name` (string, required, 1-64 codepoints after stripping
     leading and trailing whitespace).
   - `description` (string, required, 1-1000 codepoints after
     stripping leading and trailing whitespace).
   - `prerequisites` (list of strings, optional, default empty
     list, at most 32 entries, each entry 1-200 codepoints
     after stripping).
3. WHEN the frontmatter `name` field is not byte-identical
   under case-sensitive string comparison to the skill's
   directory basename, THE Skills_Loader SHALL log one
   WARNING naming both values, retain the directory basename
   as the canonical name, and continue loading the skill.
4. WHEN the frontmatter `description` field exceeds 80
   Unicode codepoints (counted after stripping leading and
   trailing whitespace), THE Skills_Loader SHALL render the
   in-prompt description as the first 80 codepoints followed
   by the literal three-character ellipsis `...`, and SHALL
   log one WARNING naming the skill and the original
   codepoint count.
5. IF the parsed frontmatter contains keys other than `name`,
   `description`, and `prerequisites`, THEN THE Skills_Loader
   SHALL preserve the skill in the LoadedSkills list and log
   exactly one DEBUG line per skill listing the unknown key
   names in sorted order.
6. IF a `SKILL.md`'s first line is not the literal `---`
   delimiter, THEN THE Skills_Loader SHALL log one WARNING
   naming the file path and exclude the skill.
7. IF a `SKILL.md` opens with `---` but no matching closing
   `---` line is found within the first 200 lines, THEN THE
   Skills_Loader SHALL log one WARNING naming the file path
   and exclude the skill.
8. IF the YAML between the delimiters fails to parse, THEN
   THE Skills_Loader SHALL log one WARNING with the file
   path and the YAML parser's error message text, and
   exclude the skill.
9. IF the parsed frontmatter is missing `name` or
   `description`, OR contains a `name` or `description`
   that consists entirely of whitespace, THEN THE
   Skills_Loader SHALL log one WARNING naming the file
   path and the offending field and exclude the skill.
10. IF the parsed frontmatter contains `name` or
    `description` whose YAML-decoded value is not a string,
    OR a `prerequisites` value that is not a list of
    strings, THEN THE Skills_Loader SHALL log one WARNING
    naming the file path, the offending field, and the
    received YAML type, and exclude the skill.

### Requirement 3: Inject the Skills_Block into the system prompt

**User Story:** As the agent, I want to see a list of skills
with one-line descriptions in every chat request's system
prompt, so I can decide which (if any) skill to follow for the
current user request.

#### Acceptance Criteria

1. WHEN the Gateway assembles the system prompt for a chat
   request and the Skills_Loader holds at least one
   LoadedSkill, THE Gateway SHALL insert the Skills_Block
   into the system prompt such that the line immediately
   preceding the Skills_Block is the last line of the
   `[Capabilities]` block and the line immediately
   following the Skills_Block is the first line of the next
   existing section (identity, lessons, or any subsequent
   content), with exactly one `\n` newline separating the
   Skills_Block from each neighbour.
2. IF the Skills_Loader holds at least one LoadedSkill,
   THEN THE Skills_Block SHALL begin with the literal
   header line `[Skills available]` terminated by a single
   `\n` newline, followed by exactly one rendered line per
   LoadedSkill.
3. THE Skills_Block SHALL render each LoadedSkill as
   exactly one line of the form `- <name>: <description>
   (read recipe with read_file <absolute_path>)`, where
   `<name>` is the LoadedSkill's `name`, `<absolute_path>`
   is the absolute filesystem path to that skill's
   `SKILL.md`, and `<description>` is the LoadedSkill's
   `description` with all `\r` and `\n` characters
   replaced by a single space and with leading and
   trailing whitespace removed.
4. IF the Skills_Loader holds an empty list of
   LoadedSkills, THEN THE Gateway SHALL omit the
   Skills_Block from the system prompt entirely, emitting
   no `[Skills available]` header, no rendered skill line,
   and no blank line in place of the block, so that the
   resulting system prompt is byte-identical to one
   assembled with no skills feature present.
5. THE Skills_Block SHALL list LoadedSkills in ascending
   order using a case-insensitive lexicographic comparison
   of `name`, with ties broken by ascending case-sensitive
   lexicographic comparison of the absolute path to the
   skill's `SKILL.md`, so that the rendered order is
   fully determined by the LoadedSkills set with no
   dependence on iteration or insertion order.
6. IF a LoadedSkill has one or more prerequisites, THEN
   THE Skills_Block SHALL append the literal string
   `; needs: ` followed by the prerequisite names joined
   by `, ` (comma followed by a single space), in the
   order they appear in the LoadedSkill's `prerequisites`
   field, to that skill's line, placed immediately before
   the line's terminating `\n` newline and after the
   closing `)` of the recipe-load hint.
7. WHILE the Gateway process is alive, THE Gateway SHALL
   NOT create, modify, rename, or delete any file or
   directory under Skills_Root as a side effect of
   assembling the Skills_Block.
8. WHEN the Gateway assembles the Skills_Block for two
   chat requests within one process lifetime and no
   external change has occurred to Skills_Root or to the
   ToolRegistry between those requests, THE Gateway SHALL
   produce a byte-identical Skills_Block for both requests.

### Requirement 4: Configuration

**User Story:** As an operator, I want to change where the
loader looks for skills and to disable the loader entirely,
so I can test skill markdown in a tempdir before promoting it
and so I can revert to pre-skills behaviour without removing
the directory.

#### Acceptance Criteria

1. THE Gateway SHALL accept `memory.skills_dir` as an
   optional string field in `config.yaml`, treating both
   absent and YAML-null values as equivalent and
   defaulting in those cases to the absolute path
   `$FITT_HOME/skills/`.
2. WHEN the configured `memory.skills_dir` value, after
   `~` expansion per 4.3, contains no leading `/`, no
   leading `~`, and no Windows drive-letter prefix
   (`<letter>:`), THE Gateway SHALL resolve it as a path
   relative to `$FITT_HOME` at config-load time.
3. WHEN the configured `memory.skills_dir` value contains
   a leading `~`, THE Gateway SHALL expand it via
   `Path.expanduser()` at config-load time and before
   applying 4.2.
4. IF `memory.skills_dir` is present in `config.yaml` and
   its YAML-decoded value is not a string and not YAML-null,
   THEN THE Gateway SHALL fail to start, emit one ERROR log
   line naming the field and the received YAML type, and
   exit with a non-zero status code.
5. THE Gateway SHALL accept `memory.skills_enabled` as an
   optional boolean field in `config.yaml`, treating both
   absent and YAML-null values as equivalent and
   defaulting in those cases to `true`.
6. IF `memory.skills_enabled` is present in `config.yaml`
   and its YAML-decoded value is not a boolean and not
   YAML-null, THEN THE Gateway SHALL fail to start, emit
   one ERROR log line naming the field and the received
   YAML type, and exit with a non-zero status code.
7. WHILE `memory.skills_enabled` resolves to `false`, THE
   Skills_Loader SHALL produce an empty LoadedSkills
   list and SHALL NOT read, stat, or enumerate any path
   under Skills_Root.
8. WHILE the Skills_Loader holds an empty LoadedSkills
   list because of 4.7, THE Gateway SHALL omit the
   Skills_Block from the system prompt entirely (no
   header, no body, and no placeholder line).
9. THE `configs/config.example.yaml` file SHALL document
   `memory.skills_dir` and `memory.skills_enabled` with
   placeholder values matching their documented defaults
   (`skills/` and `true`) and exactly one comment
   sentence per field explaining its purpose.

### Requirement 5: Failure isolation

**User Story:** As an operator, I want one badly-written skill
to never block gateway startup or break unrelated skills, so
I can ship new skill markdown without fearing a typo will
take FITT down.

#### Acceptance Criteria

1. IF parsing one skill fails for any reason listed in
   Requirement 2, THEN THE Skills_Loader SHALL continue
   parsing every remaining candidate subdirectory under
   Skills_Root and SHALL log one WARNING per failed skill
   carrying the candidate's absolute path and a category
   tag identifying which clause of Requirement 2 was
   triggered.
2. THE Skills_Loader SHALL catch every exception raised
   during a per-skill parse, exclude the affected skill
   from the LoadedSkills list, preserve all
   previously-loaded skills, and SHALL NOT propagate the
   exception out of the scan method to the Gateway boot
   path.
3. WHEN the Skills_Loader finishes a startup scan that
   skipped one or more skills, THE Skills_Loader SHALL
   log exactly one INFO summary line carrying integer
   `loaded_count` and integer `skipped_count` fields.
4. WHEN the Skills_Loader finishes a startup scan that
   skipped zero skills, THE Skills_Loader SHALL still
   log exactly one INFO summary line as in 5.3 with
   `skipped_count` equal to `0`.
5. IF Skills_Root exists but is not a directory (for
   example a regular file at that path), THEN THE
   Skills_Loader SHALL log exactly one WARNING naming
   the absolute path and a discriminator indicating
   `not-a-directory`, return an empty LoadedSkills
   list, and SHALL NOT propagate any exception.
6. THE Skills_Loader SHALL complete its startup scan
   within 5 seconds when Skills_Root contains up to
   1000 candidate subdirectories on a typical
   developer machine, so the Gateway boot is not
   blocked by an unbounded scan.

### Requirement 6: Operator visibility

**User Story:** As an operator, I want gateway logs to make
it obvious which skills loaded successfully and which were
skipped, so I can confirm a new SKILL.md drop took effect
without reading the system prompt by hand.

#### Acceptance Criteria

1. WHEN the Skills_Loader successfully loads a skill
   during the startup scan, THE Skills_Loader SHALL
   log exactly one INFO line carrying the structured
   fields `event="skills.loaded"` (string),
   `name` (string), `description_chars` (non-negative
   integer), and `prerequisites_count` (non-negative
   integer).
2. WHEN the Skills_Loader skips a candidate during
   the startup scan, THE Skills_Loader SHALL log
   exactly one WARNING line carrying the structured
   fields `event="skills.skipped"`, `path` (the
   absolute filesystem path of the candidate
   `SKILL.md` or candidate subdirectory), and
   `reason` (a string drawn from a documented closed
   enumeration of skip-reason codes shared between
   the loader and tests).
3. WHEN the Skills_Loader finishes the startup scan,
   THE Skills_Loader SHALL log exactly one INFO line
   carrying the structured fields
   `event="skills.scan_complete"`,
   `loaded_count` (non-negative integer),
   `skipped_count` (non-negative integer), and
   `root` (the absolute filesystem path of
   Skills_Root). This summary line SHALL appear in
   log output after every per-skill `skills.loaded`
   and `skills.skipped` entry from the same scan.

### Requirement 7: End-to-end integration test

**User Story:** As a developer working on this phase, I want
a single test that proves "operator drops a SKILL.md and the
agent sees it on the next request," so the phase's promise
is pinned by an executable test rather than by hand
verification.

#### Acceptance Criteria

1. THE test suite SHALL include one integration test that,
   using pytest's `tmp_path` fixture, creates a directory
   and writes one `SKILL.md` file containing valid YAML
   frontmatter (a `name` field equal to the containing
   directory's basename, a `description` field of 10-200
   codepoints, and an optional empty `prerequisites` list)
   followed by a non-empty markdown body, and configures
   the Gateway under test with `memory.skills_dir` set to
   the test directory's absolute path either via the
   `FITT_HOME`-style env var override mechanism or via a
   direct config override accepted by the Gateway.
2. WHEN the test sends one chat request through the
   Gateway's chat handler, THE test SHALL assert that the
   system prompt the Gateway produces for the upstream
   model call contains the literal string
   `[Skills available]` and contains the skill's
   description text (or its truncated form per 2.4) on the
   line that begins with `- <name>:`.
3. THE test SHALL assert that the absolute filesystem
   path of the test's `SKILL.md`, exactly as written by
   the test setup, appears in the Skills_Block's
   recipe-load hint enclosed in
   `(read recipe with read_file <absolute_path>)`.
4. THE test SHALL run under `uv run pytest` without
   network access, without any real call to LiteLLM,
   Ollama, OpenRouter, or any other model provider, and
   without any real call to the OS user-home directory
   outside of `tmp_path`; upstream model calls SHALL be
   intercepted with the same fixture mechanism the
   existing gateway chat tests use.
5. IF the Gateway under test produces a system prompt
   that lacks `[Skills available]`, lacks the skill's
   description, lacks the absolute `SKILL.md` path, or
   lacks the `- <name>:` prefix on the skill's line,
   THEN the test SHALL fail with an assertion message
   identifying the missing element so the developer can
   diagnose the gap without re-running with a debugger.

### Requirement 8: Honesty about missing prerequisites

**User Story:** As the agent, when a skill lists a prerequisite
that the current FITT installation does not satisfy, I want
the Skills_Block to say so, so I do not pick a skill that
will fail at the first tool call (per FITT principle 8: be
honest about capabilities).

#### Acceptance Criteria

1. WHEN the Skills_Block is rendered and a LoadedSkill's
   `prerequisites` field lists one or more FITT tool names
   that are not present in the ToolRegistry, THE
   Skills_Block SHALL append `[unavailable: <missing tool
   names>]` to that skill's line, where `<missing tool
   names>` is a comma-separated list of the missing tool
   names in the order they appear in the `prerequisites`
   field.
2. IF every tool name in a LoadedSkill's `prerequisites`
   field is present in the ToolRegistry at the time the
   Skills_Block is rendered, THEN THE Skills_Block SHALL
   NOT append any `[unavailable: ...]` marker to that
   skill's line.
3. IF a LoadedSkill has an empty or absent `prerequisites`
   field, THEN THE Skills_Block SHALL NOT append any
   `[unavailable: ...]` marker to that skill's line.
4. WHEN a skill file lists prerequisites that are not
   present in the ToolRegistry, THE Skills_Loader SHALL
   load the skill into the LoadedSkills list and SHALL
   NOT raise a parse failure or omit the skill.

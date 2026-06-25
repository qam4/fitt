# Phase 4.10 — Skills-as-Markdown Loader: Tasks

**Status:** shipped

## Scope

Three commits, ~half-day total. Sets up the
operator-drops-markdown-and-it-works loop and pins
the contract with a real integration test. After
this lands, "fitt can't do X" complaints that map
to a CLI become markdown drops instead of code
patches.

The work is sequenced so each commit leaves the
gateway in a working state:

- Commit 1 ships the loader and renderer as a
  pure module with full unit-test coverage. No
  wiring yet, so the gateway behaves identically.
- Commit 2 wires the module into the gateway
  boot path and chat handler, and pins the
  end-to-end contract with the integration test
  Requirement 7 mandates.
- Commit 3 adds property tests, NAS-side smoke
  verification, README updates, and one sample
  skill in the docs (not vendored).

Each commit's tests must pass before the next
commit starts. Commit 3's NAS verification is the
final gate before declaring the phase done.

## Commit 1: Loader, renderer, unit tests

Goal: a fully tested `gateway/src/gateway/skills.py`
module that turns a directory of `SKILL.md` files
into a list of `LoadedSkill` records and a rendered
`[Skills available]` block string. No wiring into
the gateway yet — pure module + tests.

- [x] 1a. Add `gateway/src/gateway/skills.py` with
        the `LoadedSkill` frozen dataclass per
        design.md Components and Interfaces / Data
        Models. Fields: `name`, `description`,
        `prerequisites: tuple[str, ...]`,
        `skill_md_path: Path`,
        `description_truncated: bool`.
- [x] 1b. Add the typed exception hierarchy in the
        same module: `SkillSkipped(Exception)` as
        the base, with subclasses
        `MissingOpenFence`, `MissingCloseFence`,
        `MalformedYaml`, `MissingRequiredField`,
        `WrongFieldType`, `FieldOutOfBounds`,
        `ReadFailed`. Each carries a short reason
        code (`no-frontmatter-fence`,
        `unclosed-frontmatter`, `malformed-yaml`,
        `missing-required-field`,
        `wrong-field-type`, `field-out-of-bounds`,
        `read-failed`) used as the WARNING
        `reason` field per Requirement 6.2.
- [x] 1c. Implement `_split_frontmatter`,
        `_parse_frontmatter`, `_validate_fields`
        per design.md's Components section.
        `_validate_fields` enforces the bounds in
        Requirement 2.2 (name 1-64 codepoints,
        description 1-1000 codepoints,
        prerequisites <=32 entries each 1-200
        codepoints).
- [x] 1d. Implement `SkillsLoader.__init__` and
        `SkillsLoader.scan()`. Honor Requirement
        4.7 (`enabled=False` -> empty list, no
        filesystem reads), Requirement 1.5
        (missing path -> empty list, INFO log
        with `not-found` discriminator), and
        Requirement 5.5 (path-is-file -> empty
        list, WARNING with `not-a-directory`).
- [x] 1e. Implement `_load_one(path)` that calls
        the helpers above and returns a
        `LoadedSkill`. Catch every typed
        exception inside the scan loop, log
        `event="skills.skipped"` with `path` and
        `reason` per Requirement 6.2, and
        continue. Outer `except Exception` is the
        belt-and-suspenders catch per Decision 7.
- [x] 1f. Implement description truncation in
        `_load_one` per Requirement 2.4: if
        codepoint length after strip > 80, set
        `description = first_80_codepoints + "..."`
        and `description_truncated = True`.
- [x] 1g. Implement duplicate-name handling per
        Requirement 1.8: case-insensitive name
        comparison; keep the lex-first absolute
        path; WARNING per duplicate. The check
        runs after all `_load_one` calls so it
        sees the full set.
- [x] 1h. End-of-scan summary line per
        Requirement 6.3:
        `event="skills.scan_complete"`,
        `loaded_count`, `skipped_count`, `root`.
        Per-skill `skills.loaded` lines fire from
        `_load_one`'s success path per
        Requirement 6.1.
- [x] 1i. Implement `render_skills_block(skills,
        tool_registry) -> str` per Components
        and Interfaces. Sort: case-insensitive
        lex by name, ties broken by case-sensitive
        lex of `skill_md_path`. Empty input ->
        empty string per Requirement 3.4.
- [x] 1j. Per-line format per Requirement 3.3:
        `- <name>: <description> (read recipe
        with read_file <abs_path>)`. Append
        `; needs: <comma-list>` if prereqs is
        non-empty (Requirement 3.6). Append
        `[unavailable: <comma-list>]` for any
        prereq missing from
        `tool_registry.list_names()` per
        Requirement 8.1.
- [x] 1k. Write `gateway/tests/test_skills_loader.py`
        with the 22 unit tests listed in design.md
        Testing Strategy. Use `tmp_path` for the
        skills_dir; assert log records via
        `caplog`.
- [x] 1l. Write `gateway/tests/test_skills_render.py`
        with the 9 unit tests listed in design.md.
        Use a small mock ToolRegistry that
        exposes `list_names()` returning a
        configurable set.
- [x] 1m. Run `uv run ruff format src tests`,
        `uv run ruff check src tests`, `uv run
        mypy src`, `uv run pytest -q` in
        `gateway/`. All green before committing.
- [x] 1n. Commit. Suggested message:
        `Phase 4.10/1: skills loader + renderer (no wiring yet)`.

## Commit 2: Wire into chat handler + config + integration test

Goal: the gateway loads skills at boot, renders
them into the system prompt on each chat request,
and ships an integration test that pins the
end-to-end contract.

- [x] 2a. Add `skills_dir: Path` and
        `skills_enabled: bool = True` fields to
        `MemoryConfig` in
        `gateway/src/gateway/config.py`. Default
        for `skills_dir` is `fitt_home() /
        "skills"`. Add a `_expand_skills_dir`
        validator that mirrors the existing
        `_expand` validator on `identity_dir` /
        `sessions_dir` (handles `~`, relative
        paths under FITT_HOME, str -> Path
        coercion).
- [x] 2b. In `gateway/src/gateway/app.py` (or
        wherever `create_app` instantiates the
        gateway components — check the actual
        boot path), construct a `SkillsLoader`
        with `config.memory.skills_dir` and
        `config.memory.skills_enabled`, call
        `.scan()`, and stash the resulting list
        on `app.state.skills`.
- [x] 2c. In `gateway/src/gateway/chat.py`,
        compute `skills_block` alongside
        `capability_block`. When `router_mode`,
        skip the renderer (skills are
        FITT-internal, not part of router-mode
        passthrough — same posture as memory and
        capability_block today).
- [x] 2d. Extend `_inject_memory` to accept a
        `skills_block: str = ""` parameter and
        concatenate parts in order
        `[capability_block, skills_block,
        ctx.system_prefix]`, dropping empty
        strings. Verify the existing system-
        prompt layering tests still pass.
- [x] 2e. Update `configs/config.example.yaml`
        per Requirement 4.9: add `skills_dir:
        skills/` and `skills_enabled: true`
        under the existing `memory:` block, each
        with one comment sentence explaining it.
- [x] 2f. Write `gateway/tests/test_skills_e2e.py`
        per Requirement 7. The test creates
        `tmp_path/skills/say-hello-french/SKILL.md`
        with valid frontmatter and a non-empty
        markdown body, builds a Config with
        `memory.skills_dir = tmp_path/skills/`
        and other fields defaulted, constructs
        the FastAPI app via `create_app(config)`,
        uses
        `httpx.AsyncClient(transport=ASGITransport(app=app))`,
        mocks LiteLLM via the same `respx`-based
        fixture other chat tests use, sends one
        chat request through
        `/v1/chat/completions`, captures the
        upstream request body, and asserts
        `[Skills available]` plus the line
        `- say-hello-french: <description>
        (read recipe with read_file <abs_path>)`
        appears in the system message including
        the literal absolute path to the test
        SKILL.md.
- [x] 2g. Add a second integration test:
        `memory.skills_enabled: false` -> the
        captured upstream system message does
        NOT contain `[Skills available]`
        (Property 6 from design.md).
- [x] 2h. Run the full lint + mypy + pytest pass
        in `gateway/`. The existing chat handler
        tests must still pass (the
        `_inject_memory` signature change is
        backwards compatible because of the
        default value).
- [ ] 2i. Commit. Suggested message:
        `Phase 4.10/2: wire skills loader into gateway boot + chat handler`.

## Commit 2.5: Built-in `fitt` pseudo-project for the recipe-load hint

Goal: fix the runtime gap discovered during NAS smoke testing
of Commit 2 — the rendered recipe-load hint pointed at an
absolute path, but FITT's `read_file` requires
`project=<name> path=<rel>`. Add a built-in `fitt` pseudo-
project rooted at `$FITT_HOME` with a hard-coded subdir
allowlist (`skills/` only for v1) so the hint is executable
without operator-side `projects.yaml` config.

Architecturally narrow: doesn't expose secrets/, ssh/,
audit logs, or anything else. Widening the allowlist later
(e.g. `sessions/` for Phase 7's session search) is a one-
line code change with deliberate review.

- [x] 2.5a. In `gateway/src/gateway/tools/fileops.py`, add
        `_FITT_BUILTIN_NAME = "fitt"`,
        `_FITT_BUILTIN_ALLOWLIST = ("skills",)`,
        `_maybe_resolve_builtin_fitt_project`,
        `_path_is_in_fitt_allowlist`,
        `_enforce_fitt_allowlist`, and
        `_reject_fitt_for_writes`. Plumb them through
        `_resolve_project_for_tool` so the read-side fileops
        (`read_file`, `list_directory`) honor the allowlist
        and the write-side (`write_file`, `edit_file`) reject
        `project=fitt` outright. `grep_repo` and `glob_search`
        also reject `project=fitt` because a recursive search
        across FITT_HOME would defeat the allowlist.
- [x] 2.5b. In `gateway/src/gateway/skills.py`, change
        `render_skills_block` to emit
        `(read recipe with read_file project=fitt path=<rel>)`
        when the SKILL.md lives under `$FITT_HOME`. Falls back
        to an absolute path when the operator points
        `skills_dir` outside FITT_HOME (the recipe won't load,
        but the hint stays factually accurate).
- [x] 2.5c. Add an instruction line to the rendered
        `[Skills available]` block: "Each skill below
        provides a recipe for a specific task. When the
        user's request matches a skill's description, load
        the recipe with the read_file call shown in
        parentheses, then follow it." Borrowed from
        OpenClaw's `formatSkillsForPrompt`; tells the agent
        explicitly how to use the recipe-load hint.
- [x] 2.5d. Add `gateway/tests/test_tools_fileops.py` cases
        covering: read_file `project=fitt` with allowlisted
        path, rejection of non-allowlisted subdirs (secrets,
        ssh), rejection of `..` traversal, rejection of root
        listing (forces a subdir choice), list_directory on
        allowlisted subdir, grep_repo / glob_search /
        write_file / edit_file all rejected on
        `project=fitt`.
- [x] 2.5e. Update `gateway/tests/test_skills_render.py` for
        the new line shape and instruction-line position.
        Add a test for the realistic skills_dir-under-FITT_HOME
        case (renders `project=fitt path=...`) and one for
        the fallback case (skills_dir outside FITT_HOME →
        absolute path).
- [x] 2.5f. Update `gateway/tests/test_skills_e2e.py` so the
        primary integration test puts skills_dir under
        FITT_HOME and asserts the `project=fitt` recipe-load
        hint is what the agent sees.
- [x] 2.5g. Run lint + mypy + pytest pass in `gateway/` and
        `telegram-bot/`. All green.
- [x] 2.5h. Commit. Suggested message:
        `Phase 4.10/2.5: built-in 'fitt' pseudo-project for recipe-load hint`.

## Commit 3: Property tests, NAS smoke, docs

Goal: pin the harder properties with hypothesis,
verify the loop end-to-end on the NAS, and
document the operator workflow.

- [x] 3a. Write
        `gateway/tests/test_skills_properties.py`
        per design.md Testing Strategy. Two
        hypothesis tests, each min 100
        iterations, tagged
        `# Phase 4.10, Property 2: Deterministic order`
        and
        `# Phase 4.10, Property 4: Failure isolation`
        per the conventions doc.
- [x] 3b. Update `gateway/README.md` config
        reference with the two new
        `memory.skills_dir` /
        `memory.skills_enabled` fields. Cross-
        reference `docs/quickstart.md` for the
        operator workflow (next sub-task).
- [x] 3c. Add a "Adding a skill" section to
        `docs/quickstart.md`. Three-paragraph
        operator recipe: create
        `~/.fitt/skills/<skill-name>/SKILL.md`;
        write frontmatter + body; restart the
        gateway and confirm via `gateway.log`
        that `event="skills.loaded"` fires.
- [x] 3d. Sample skill drop. Add
        `docs/sample-skills/say-hello-french/SKILL.md`
        as a copy-pasteable starting point. NOT
        installed by the gateway; just a doc-side
        example the user can copy into
        `~/.fitt/skills/`.
- [x] 3e. Run lint + mypy + pytest pass in
        `gateway/`. All green.
- [x] 3f. Commit. Suggested message:
        `Phase 4.10/3: property tests, README, sample skill`.

## Verification

- [x] 4a. On the NAS, drop
        `~/.fitt/skills/say-hello-french/SKILL.md`
        from the docs/sample-skills/ template.
        Restart `fitt-gateway`. `gateway.log`
        should show `event="skills.loaded"` for
        say-hello-french and
        `event="skills.scan_complete"
        loaded_count=1 skipped_count=0`.
- [x] 4b. Send a Telegram message: "say hello in
        French to Frédéric". Confirm the agent
        picks up the skill (its response is in
        French, follows the recipe shape) and
        the system prompt sent for that turn
        (visible via `fitt watch` event log or
        the audit log if log_bodies is enabled)
        contains `[Skills available]` and the
        skill's line. (Verified live with the
        `fitt-status` skill instead — the
        hello-french case turned out to be too
        trivial for the model to load; see
        "Trivial skills won't fire" in
        docs/quickstart.md.)
- [x] 4c. Drop a deliberately-malformed
        `~/.fitt/skills/broken/SKILL.md` (e.g.
        missing closing `---`). Restart.
        Confirm `gateway.log` shows
        `event="skills.skipped"
        reason="unclosed-frontmatter"` for
        `broken`, the good skill
        (`say-hello-french`) still loads, and
        the next chat request's system prompt
        still contains `[Skills available]`
        with only `say-hello-french` listed.
        (Failure-isolation property test
        covers this; manual NAS verification
        skipped.)
- [x] 4d. Set `memory.skills_enabled: false` in
        `~/.fitt/config.yaml`. Restart. Confirm
        the next chat turn's system prompt does
        NOT contain `[Skills available]`.
        (E2E test pins this; manual NAS
        verification skipped.)
- [x] 4e. Restore `memory.skills_enabled: true`.

## Deferred — see design.md "Future Extensions"

These have spec coverage but no tasks here. Each
is a clean addition on top of the work above.

- **Hot-reload via `POST /admin/reload-skills`**.
  ~half day. Add when restart-to-apply becomes
  annoying. The Phase 4.8c HTTP read endpoints
  are the design template.
- **Sample skills bundled in the FITT repo**
  (`web-search`, `gh-setup`, etc.). Each is
  half-a-day and ships independently. Hold for
  now; Commit 3 ships exactly one sample
  (say-hello-french) as the existence proof.
- **Skills hub / sync** (Hermes pattern). Out of
  scope; revisit if/when daily friction calls
  for it.
- **Per-skill model overrides** (`agent_alias:
  fitt-smart`). Adds a config field on
  `LoadedSkill` and a routing override in
  `chat.py`. Half-day. Add when one skill needs
  a different alias than the chat default.
- **Curator-style auto-archival of stale skills**
  (Hermes pattern). Multi-day. Don't until the
  pile is big enough to be a problem.

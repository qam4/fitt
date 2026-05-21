# Design: FITT Phase 4.10 — Skills-as-Markdown Loader

## Overview

Phase 4.10 adds a markdown-based skills loader to the FITT
gateway. A skill is a directory under `~/.fitt/skills/<name>/`
containing a `SKILL.md` file with YAML frontmatter (name,
description, prerequisites) and a free-form markdown body.
The loader runs at gateway boot, parses each `SKILL.md`, and
exposes a `[Skills available]` block in the system prompt
listing each skill's name, description, and `SKILL.md` path.

The skill body is **not** injected — the agent reads it via the
existing `read_file` tool when it decides a skill applies. This
trades a tiny system-prompt cost (one line per skill) for an
unbounded library of agent-driveable recipes that the operator
can grow without code changes.

This is the highest-leverage opportunistic upgrade identified
by the OpenClaw and Hermes audits (`docs/prior-art.md`). Once
shipped, FITT can absorb MIT-licensed skill markdown from those
projects unchanged, and "fitt can't do X" complaints become
markdown drops instead of code patches.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Gateway boot                                                   │
│                                                                 │
│   Config(... memory={skills_dir, skills_enabled, ...})          │
│                            │                                    │
│                            ▼                                    │
│   SkillsLoader(skills_dir, skills_enabled).scan()               │
│                            │                                    │
│                            ▼                                    │
│   list[LoadedSkill]  ─── cached on app.state.skills             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Per chat request                                               │
│                                                                 │
│   build_capability_block(tool_registry)                         │
│                  │                                              │
│                  ▼                                              │
│   render_skills_block(skills, tool_registry)  ◄── new module    │
│                  │                                              │
│                  ▼                                              │
│   "[Skills available]\n- web-search: ...\n..."                  │
│                  │                                              │
│                  ▼                                              │
│   _inject_memory(body, ctx, capability_block, skills_block)     │
│                  │                                              │
│                  ▼                                              │
│   final messages: [system: capabilities + skills + identity +   │
│                              lessons, ...history..., user]      │
└─────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

#### Decision 1: Boot-time scan, not per-request

The Hermes audit noted both reference systems' sensitivity to
prompt-cache invalidation: changing the system prompt mid-
session forces a full cache rebuild, which is expensive on
cloud models. A per-request rescan would change the
Skills_Block whenever the operator edits a SKILL.md, breaking
caching for the rest of the day.

We trade hot-reload for cache stability. Restart-to-apply is
already the contract for `identity.md` (no daemon thread, no
`watchdog`); skills follow the same convention.

A future "reload" admin endpoint could be added (Phase 7+) but
isn't required for v1.

#### Decision 2: Skills_Block sits inside the system prompt prefix, between Capabilities and identity

Today's system prompt assembly path
(`gateway/src/gateway/chat.py::_inject_memory`):

```
[Capabilities]\n\n  +  identity\n\n[Learned corrections]...  +  history
```

We insert the Skills_Block as:

```
[Capabilities]\n\n[Skills available]\n...\n\n  +  identity + lessons
```

The skills block goes immediately after `[Capabilities]` (the
two are conceptually paired — "what you can do directly" then
"what you can do via recipes") and before identity. This is
what Requirement 3.1 mandates.

Mechanically this means `_inject_memory` gains a parameter
`skills_block: str = ""` and concatenates it the same way it
concatenates `capability_block`.

#### Decision 3: Render is a pure function of (skills, tool_registry)

`render_skills_block(skills, tool_registry)` takes the already-
loaded list and the live ToolRegistry and returns a string. No
I/O, no caching, no ordering surprise — sort is deterministic
per Requirement 3.5. Empty input → empty string (the caller
decides not to inject anything).

This shape is testable without spinning up a Gateway, and
matches how `build_capability_block(tool_registry)` works
today.

#### Decision 4: PyYAML is already in the dep tree; use it

The gateway already depends on PyYAML for `config.yaml`. The
frontmatter parser delegates to `yaml.safe_load` on the body
between the two `---` lines. No new dependencies, no hand-
rolled YAML.

The "find the fence" logic is FITT-specific and ~20 lines of
straight-line code: read up to 200 lines, split on `---`, hand
the middle to `safe_load`. Errors caught at the call site per
Requirement 2.8.

#### Decision 5: Description truncation at 80 codepoints

Hermes uses 60. OpenClaw is looser. FITT picks 80 because:

- One terminal line with `  - <name-up-to-32>: <description-up-to-80> (read recipe with read_file ...)` is roughly 200 chars before the path — already eats one line on a 200-col terminal. Tight enough to keep the system prompt scannable.
- The Skills_Block is in the system prompt, paying token cost on every request. 80 chars × 20 skills = 1.6KB just in descriptions. We can't afford 200-char descriptions.
- Codepoints, not bytes — emoji and accented characters count as 1 each. Avoids the bug where "café" is 4 codepoints but 5 bytes in UTF-8.

Truncated descriptions render with a literal `...` suffix
(per Requirement 2.4).

#### Decision 6: Prerequisite check uses the live ToolRegistry, no caching

When rendering, we call `tool_registry.has(name)` for each
prerequisite (or `tool_registry.list_names()` once and check
membership). The check happens at render time, not load time,
so a tool registered later in startup is honored.

Rationale: the load order between `SkillsLoader.scan()` and
ToolRegistry registration is undefined today. Caching at load
time would create a bug where a skill says
`[unavailable: ...]` even though the tool was registered
after the loader ran.

Cost: a few extra string comparisons per request. Negligible
compared to the LLM call.

#### Decision 7: Failure isolation through one try-except per skill

Per Requirement 5.1 and 5.2, one bad skill must not affect
others. The scan loop wraps each candidate in:

```python
for sub in candidates:
    try:
        skill = self._load_one(sub)
        if skill: out.append(skill)
    except Exception as exc:
        log.warning("skills.skipped", path=str(sub), reason=type(exc).__name__)
```

The `_load_one` method itself raises typed exceptions for each
skip case (so the WARNING reason field per Requirement 6.2 is
a closed enum drawn from the exception class names). The
outer `Exception` is the belt-and-suspenders catch for
anything we forgot.

#### Decision 8: Test the integration via the existing chat handler test fixture

Phase 1 set the precedent for "spin up a real Gateway, fire a
chat request through `httpx.AsyncClient`, mock LiteLLM via
respx." Phase 4.10 follows that exactly — `tests/test_skills_loader.py`
plus `tests/test_skills_e2e.py` that adds a SKILL.md to a
tmp dir, points `memory.skills_dir` at it, sends a chat
request, and asserts the system prompt sent to LiteLLM
contains the expected text.

This is what Requirement 7 asks for. The test takes ~50ms to
run (no network, no real LLM), making it cheap to keep in the
default suite.

## Components and Interfaces

### Component 1: `gateway/src/gateway/skills.py` — SkillsLoader

Boot-time scanner for `~/.fitt/skills/`.

```python
class SkillsLoader:
    """Boot-time scanner for ~/.fitt/skills/."""

    def __init__(self, skills_dir: Path, enabled: bool = True) -> None:
        self._skills_dir = skills_dir
        self._enabled = enabled

    def scan(self) -> list[LoadedSkill]:
        """Walk skills_dir, parse every SKILL.md, return LoadedSkills.

        Never raises. Per-skill failures log WARNING and skip.
        End-of-scan summary line carries loaded_count, skipped_count.

        When `enabled=False`, returns []. When skills_dir doesn't
        exist or isn't a directory, returns [] with one INFO line
        naming the path.
        """
```

Internal helpers:

- `_load_one(path: Path) -> LoadedSkill` — parse one
  SKILL.md, raise typed exceptions on every failure mode
  in Requirement 2.
- `_split_frontmatter(text: str) -> tuple[str, str]` — find
  the two `---` lines, return `(yaml_text, body)`. Raises
  `MissingOpenFence`, `MissingCloseFence`.
- `_parse_frontmatter(yaml_text: str) -> dict` —
  `yaml.safe_load` wrapped to raise `MalformedYaml`.
- `_validate_fields(raw: dict) -> tuple[str, str, tuple[str, ...]]` —
  checks types/lengths per Requirement 2.2 and 2.10.
  Raises `MissingRequiredField`, `WrongFieldType`,
  `FieldOutOfBounds`.

The typed exceptions inherit from `SkillSkipped(Exception)`
so the outer scan loop catches one base class.

### Component 2: `gateway/src/gateway/skills.py` — render_skills_block

Pure renderer. Same module as the loader to keep skill-side
code colocated.

```python
def render_skills_block(
    skills: Iterable[LoadedSkill],
    tool_registry: ToolRegistry,
) -> str:
    """Build the [Skills available] block.

    Returns "" when `skills` is empty (per Requirement 3.4 — caller
    omits the block).
    """
```

Sort: case-insensitive lex by name, ties broken by case-
sensitive lex of `skill_md_path` (Requirement 3.5).

Per-skill line:

```
- <name>: <description> (read recipe with read_file <abs_path>)[; needs: a, b][[unavailable: a]]
```

The `; needs:` segment is added when prerequisites is non-
empty (Requirement 3.6). The `[unavailable: ...]` segment
is added when any prerequisite name is missing from the
ToolRegistry (Requirement 8.1). Both segments appear before
the line's terminating newline.

### Component 3: `gateway/src/gateway/config.py` — MemoryConfig additions

Two new fields on the existing `MemoryConfig` Pydantic model:

```python
class MemoryConfig(BaseModel):
    # ... existing fields ...
    skills_dir: Path = Field(default_factory=lambda: fitt_home() / "skills")
    skills_enabled: bool = True

    @field_validator("skills_dir", mode="before")
    @classmethod
    def _expand_skills_dir(cls, v: object) -> object:
        # Same expand-and-resolve treatment as `identity_dir` and
        # `sessions_dir` get today.
        ...
```

Pydantic raises `ValidationError` when `skills_dir` is non-
string or `skills_enabled` is non-bool, satisfying
Requirements 4.4 and 4.6 (gateway exits non-zero on config
errors today; nothing extra needed).

### Component 4: `gateway/src/gateway/chat.py` — wire it up

Two changes:

1. Inject the renderer call alongside `build_capability_block`:

   ```python
   skills_block = (
       render_skills_block(app.state.skills, tool_registry)
       if not router_mode else ""
   )
   ```

2. `_inject_memory` gains a `skills_block: str = ""` parameter and concatenates after `capability_block` and before identity/lessons:

   ```python
   parts = [p for p in [capability_block, skills_block, ctx.system_prefix] if p]
   system_prefix = "\n\n".join(parts)
   ```

### Component 5: `gateway/src/gateway/app.py` — boot wiring

After config load, before serving:

```python
loader = SkillsLoader(config.memory.skills_dir, config.memory.skills_enabled)
app.state.skills = loader.scan()
```

### Component 6: `configs/config.example.yaml`

Two lines added to the existing `memory:` block:

```yaml
memory:
  # ... existing settings ...
  skills_dir: skills/        # Where SkillsLoader looks for SKILL.md files (relative to FITT_HOME).
  skills_enabled: true       # Set to false to skip the skills loader and omit the [Skills available] block.
```

## Data Models

### `LoadedSkill` (frozen dataclass)

```python
@dataclass(frozen=True)
class LoadedSkill:
    """One successfully parsed SKILL.md."""
    name: str
    description: str             # truncated to 80 codepoints if needed
    prerequisites: tuple[str, ...]   # tool names
    skill_md_path: Path          # absolute path to SKILL.md
    description_truncated: bool  # for log honesty
```

Fields:

- `name`: from the directory basename. Must match
  `[a-zA-Z0-9_-]{1,64}` after parsing (Requirement 2.2).
- `description`: from frontmatter, truncated per
  Requirement 2.4 with `description_truncated=True` set.
- `prerequisites`: tuple of FITT tool names this skill expects.
  Empty tuple is fine (no prereqs).
- `skill_md_path`: absolute filesystem path. Used in the
  rendered recipe-load hint and as the tie-break for sorting.
- `description_truncated`: True when truncation happened. Used
  by the WARNING log line; doesn't affect rendering (the
  truncated string is what `description` already contains).

### Frontmatter schema (operator-authored)

```yaml
---
name: web-search                # required, string, 1-64 codepoints
description: "Search the web"   # required, string, 1-1000 codepoints (rendered ≤80)
prerequisites:                  # optional, list of strings, ≤32 entries
  - http_get
---
```

Unknown keys are ignored (DEBUG log). Wrong types or out-of-
bounds values cause the skill to be skipped with a structured
WARNING.

### Skill directory layout

```
~/.fitt/skills/
├── web-search/
│   ├── SKILL.md              # required
│   └── scripts/              # optional, scripts the recipe calls
├── gh-setup/
│   ├── SKILL.md
│   └── references/           # optional, extra docs the agent reads
└── say-hello-french/
    └── SKILL.md
```

The loader only cares about `SKILL.md` at the top of each
subdirectory. The `scripts/`, `references/`, `templates/`
subdirectories are convention for the recipe author; the
loader never reads them.

### Wire-by-wire data flow

```
Boot:
  config.yaml  →  Config(memory.skills_dir=Path('~/.fitt/skills'))
        ↓
  SkillsLoader(skills_dir, enabled=True).scan()
        ↓ (per subdir)
  _load_one(path)
    → _split_frontmatter(text)        # raises on missing fences
    → _parse_frontmatter(yaml_text)   # raises on YAML errors
    → _validate_fields(raw)           # raises on missing/wrong-type/out-of-bounds
    → return LoadedSkill(...)
        ↓
  list[LoadedSkill]  →  app.state.skills

Per request:
  chat handler calls render_skills_block(app.state.skills, tool_registry)
        ↓
  for each skill (sorted by name):
    line = f"- {name}: {desc} (read recipe with read_file {abs_path})"
    if prereqs:           line += f"; needs: {', '.join(prereqs)}"
    if missing:           line += f"[unavailable: {', '.join(missing)}]"
    lines.append(line)
        ↓
  block = "[Skills available]\n" + "\n".join(lines) if lines else ""
        ↓
  _inject_memory(body, ctx, capability_block, skills_block=block)
        ↓
  final system message: capability_block + "\n\n" + skills_block + "\n\n" + identity + lessons
```

## Error Handling

| Failure mode | Behavior | Requirement |
|---|---|---|
| `skills_dir` missing | Empty list, INFO log naming path + `not-found` | 1.5 |
| `skills_dir` is a file | Empty list, WARNING log + `not-a-directory` | 1.5, 5.5 |
| Subdir without `SKILL.md` | Skip, INFO log + `no SKILL.md` | 1.3 |
| `SKILL.md` permission denied / I/O error | Skip, WARNING log + `read-failed` | 1.7 |
| Duplicate name (case-insensitive) | Keep lex-first, WARNING per duplicate | 1.8 |
| Missing opening `---` | Skip, WARNING + reason `no-frontmatter-fence` | 2.6 |
| Missing closing `---` within 200 lines | Skip, WARNING + reason `unclosed-frontmatter` | 2.7 |
| Frontmatter YAML invalid | Skip, WARNING + reason `malformed-yaml` + parser error text | 2.8 |
| Missing `name` or `description` | Skip, WARNING + reason `missing-required-field` | 2.9 |
| Wrong YAML type for any field | Skip, WARNING + reason `wrong-field-type` | 2.10 |
| Field out of length / count bounds | Skip, WARNING + reason `field-out-of-bounds` | 2.2 |
| Description >80 codepoints | Load, truncate, WARNING + reason `description-truncated` | 2.4 |
| Frontmatter `name` ≠ dirname | Load, WARNING; canonical name is dirname | 2.3 |
| Unknown frontmatter keys | Load, DEBUG log of unknown key names | 2.5 |
| `skills_enabled: false` | Empty list, no scan, no Skills_Block | 4.7, 4.8 |
| Non-string `skills_dir` in config | Gateway exits non-zero with ValidationError | 4.4 |
| Non-bool `skills_enabled` in config | Gateway exits non-zero with ValidationError | 4.6 |

Per Requirement 5.2, exceptions never propagate out of the
scan method. The outer `try/except Exception` catches anything
the typed-exception layer missed.

## Tools and Dependencies

No new dependencies. Uses:

- **PyYAML** (already a dep) — frontmatter parser via `yaml.safe_load`.
- **pathlib** (stdlib) — path handling.
- **dataclasses** (stdlib) — `LoadedSkill`.
- **logging** (stdlib via existing `_log = logging.getLogger(__name__)`) — structured log fields.

Dev only:

- **pytest** + **httpx.AsyncClient** + **respx** — already in
  the dev deps for chat tests; the integration test reuses the
  same fixtures.

## Security

- The loader reads files under `skills_dir`, which is operator-
  authored. No untrusted-input concern at load time.
- The Skills_Block goes into the model's system prompt. Skills
  the operator drops there can prompt-inject the model — but
  the operator could already do that by editing `identity.md`.
  Same trust boundary.
- Per Requirement 5.2, exceptions never propagate out of the
  scan method. A YAML parse error or a permissions issue won't
  crash the gateway.
- The `read_file` tool the recipe-load hint points at (FITT
  already has) is path-restricted via the existing tool's
  approval bucket and `path_security` checks. Skills cannot
  bootstrap reading arbitrary host files just by being listed.
- Symlinks under `skills_dir` are resolved at scan time
  (Requirement 1.1's "or symbolic link resolving to a regular
  file"). If the operator points a symlink at `/etc/passwd`,
  it'd be parsed as YAML, fail with `MalformedYaml`, and skip.
  No data exfiltration path; just confusing logs.

## Correctness Properties

### Property 1: Empty input → omitted block

*For any* gateway request where `app.state.skills` is empty,
the system prompt sent to LiteLLM contains no
`[Skills available]` substring. Verified by sending one request
with the loader returning `[]` and asserting the substring is
absent.

**Validates: Requirements 3.4, 4.8**

### Property 2: Deterministic order

*For any* set of LoadedSkills, two calls to
`render_skills_block(skills, registry)` with identical inputs
produce byte-identical output, regardless of the order skills
were appended to the input list.

**Validates: Requirements 3.5, 3.7, 3.8**

### Property 3: Truncation invariant

*For any* SKILL.md whose description is >80 codepoints, the
rendered line for that skill contains exactly the first 80
codepoints of the original description (after whitespace
strip), followed by `...`.

**Validates: Requirements 2.4**

### Property 4: Failure isolation

*For any* set of N candidate subdirectories where K of them
fail any check in Requirement 2, exactly N − K LoadedSkills
are returned and the scan does not raise.

**Validates: Requirements 5.1, 5.2**

### Property 5: Prerequisite honesty

*For any* skill whose `prerequisites` list contains a tool
name not in `tool_registry.list_names()`, the skill's
rendered line contains `[unavailable: <missing>]`.

**Validates: Requirements 8.1, 8.2, 8.3**

### Property 6: skills_enabled=false is total

*For any* configuration with `memory.skills_enabled: false`,
the scan returns `[]` AND the system prompt sent on subsequent
requests contains no `[Skills available]` substring AND
`skills_dir` is never opened/stat'd/read.

**Validates: Requirements 4.7, 4.8**

## Testing Strategy

### Unit tests for the loader (`tests/test_skills_loader.py`)

- `test_loader_empty_skills_dir` — directory exists, contains
  no subdirs → `scan()` returns `[]`, no warning logs.
- `test_loader_missing_skills_dir` — `skills_dir` does not
  exist → `[]`, one INFO line with `not-found` discriminator.
- `test_loader_skills_dir_is_file` — path resolves to a file
  → `[]`, one WARNING with `not-a-directory`.
- `test_loader_disabled_skips_scan` — `enabled=False` → `[]`,
  asserts `Path.exists` was never called for `skills_dir`.
- `test_loader_subdir_without_skill_md` — present but no
  `SKILL.md` → skipped, INFO with `no SKILL.md`.
- `test_loader_dotfile_subdir_ignored` — `.git/`,
  `.dotfolder/` silently ignored.
- `test_loader_valid_minimal_skill` — happy path, asserts
  LoadedSkill fields.
- `test_loader_with_prerequisites` — `prerequisites: [http_get]`
  parsed correctly.
- `test_loader_unknown_frontmatter_keys` — extra keys → skill
  loaded, DEBUG log fires.
- `test_loader_name_mismatch` — frontmatter `name: x`,
  dirname `y` → loaded with `name=y`, WARNING fires.
- `test_loader_description_too_long` — description = 200
  chars → loaded with description = first 80 + `...`,
  `description_truncated=True`.
- `test_loader_missing_open_fence` — file lacks leading `---`
  → skipped, WARNING.
- `test_loader_missing_close_fence` — opens with `---` but
  no close in 200 lines → skipped.
- `test_loader_malformed_yaml` — frontmatter is `: : :` →
  skipped.
- `test_loader_missing_required_name` — only `description` in
  frontmatter → skipped.
- `test_loader_missing_required_description` — only `name` →
  skipped.
- `test_loader_whitespace_only_name` — `name: "   "` →
  skipped.
- `test_loader_wrong_type_name` — `name: 123` → skipped with
  `wrong-field-type` reason.
- `test_loader_wrong_type_prerequisites` — `prerequisites: "not-a-list"`
  → skipped.
- `test_loader_failure_isolation` — three skills, middle one
  malformed → first and third loaded, middle skipped.
- `test_loader_duplicate_name_case_insensitive` — `WebSearch/`
  and `websearch/` both valid → keeps lex-first, WARNING
  fires.
- `test_loader_unreadable_skill_md` — file with no read perms
  → skipped with `read-failed` reason.
- `test_loader_summary_log` — scan with mixed loads/skips →
  exactly one `skills.scan_complete` line with correct
  counts.

### Unit tests for the renderer (`tests/test_skills_render.py`)

- `test_render_empty_returns_empty_string` —
  `render_skills_block([], registry)` → `""`.
- `test_render_single_skill_no_prereqs` — exact line format
  including absolute path.
- `test_render_skill_with_prereqs_satisfied` — `; needs: http_get`
  appended; no `[unavailable: ...]`.
- `test_render_skill_with_missing_prereqs` —
  `[unavailable: http_get]` appended.
- `test_render_skill_partial_prereq_satisfaction` — mix of
  present and missing prereqs.
- `test_render_skills_sorted_case_insensitive` — `Zebra`,
  `apple`, `bee` → output order: apple, bee, Zebra.
- `test_render_skills_sorted_tie_break_by_path` — same name,
  different abspaths → lex-first abspath wins.
- `test_render_byte_identical_on_repeat` — two calls with
  same input → identical output bytes.
- `test_render_truncated_description_shows_ellipsis` —
  description was truncated → line ends with
  `... (read recipe ...)`.

### Integration test (`tests/test_skills_e2e.py`)

Per Requirement 7, one integration test that:

1. Uses `tmp_path` to create
   `skills_dir/say-hello-french/SKILL.md`.
2. Constructs a Config with `memory.skills_dir` pointing at
   it.
3. Spins up the gateway with that config.
4. Sends one chat request via `httpx.AsyncClient`, with
   LiteLLM intercepted by the same fixture other chat tests
   use.
5. Asserts the captured upstream request body contains:
   - `[Skills available]` (header).
   - The skill's description on the line starting with
     `- say-hello-french:`.
   - The absolute path of the test SKILL.md inside the
     `(read recipe with read_file <abs_path>)` segment.
6. Tagged with `# Phase 4.10, Requirement 7` per the
   conventions doc.

### Property tests (`tests/test_skills_properties.py`, hypothesis)

- **Phase 4.10, Property 2: Deterministic order** — generate
  a list of LoadedSkills with random names/paths/prereqs;
  shuffle; assert `render_skills_block` output is identical
  across shuffles.
- **Phase 4.10, Property 4: Failure isolation** — generate
  N=2..10 frontmatter fragments where K of them are
  malformed; write to tmp dirs; assert `scan()` returns
  exactly N−K LoadedSkills.

### Manual / smoke tests

- Drop `~/.fitt/skills/say-hello-french/SKILL.md`, restart
  gateway, confirm via Telegram: ask "say hello in French to
  Frédéric", expect a French greeting that follows the
  recipe.
- Drop a malformed SKILL.md in a different subdir; restart;
  confirm `gateway.log` shows `skills.skipped` with the right
  reason and that the good skill still loads.
- Set `memory.skills_enabled: false` in config; restart;
  confirm `[Skills available]` is absent from the next
  chat's system prompt (confirmed via `fitt watch` event
  log).

## Known Concerns (tracked, not blocking)

- **No hot-reload.** Edit-and-restart is the contract.
  Phase 7+ could add a `POST /admin/reload-skills` endpoint
  if it becomes annoying. Hold for now.
- **Token cost in the Skills_Block grows linearly.** 20
  skills × ~150 chars per line = 3KB system-prompt overhead.
  Cloud cost ~$0.03/M input tokens for sonnet, so 3KB costs
  ~$0.0001 per request. Negligible at hub scale; revisit if
  FITT ever has 200 skills.
- **Symlink loops.** `pathlib.iterdir()` doesn't recurse, so
  symlink loops in `skills_dir` aren't a hang risk. The
  per-skill `_load_one` reads at most 200 lines from
  `SKILL.md` (the close-fence search bound) so a malicious
  symlink to `/dev/zero` is bounded.
- **No skill versioning.** A skill is whatever `SKILL.md`
  says today. If the operator wants version history they use
  git in `~/.fitt/`. Same convention as identity.

## Future Extensions (explicit non-goals for Phase 4.10)

- Skill discovery / installation tool (Phase 7+ if at all).
- Skill marketplace / hub sync.
- Hot-reload via filesystem watch or admin endpoint.
- Auto-generated skill from agent's own work (curator
  pattern from Hermes).
- Per-skill platform gating (`platforms: [linux]`).
- Per-skill model overrides (`agent_alias: fitt-smart`).
- Skills that author-declare new tools (would require a
  separate plugin loader; out of scope here).
- Skill body injection (the body stays on disk; agent reads
  it via `read_file` only when it picks the skill).
- Default-shipped skills bundled in the FITT repo.
  Operator-authored only for v1; the OpenClaw / Hermes skill
  markdown is drop-in usable but not vendored.

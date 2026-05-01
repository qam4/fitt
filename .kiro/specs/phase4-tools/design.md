# Phase 4 — Agentic Tools: Design

## Topology

```
                                                  +-------------------------+
                                                  | Clients                 |
                                                  |  - IDE (Continue)       |
                                                  |  - Telegram             |
                                                  |  - Open WebUI           |
                                                  |  - fitt CLI             |
                                                  +-----------+-------------+
                                                              |
                                                              | Bearer token
                                                              | carries client:
                                                              v
+---------------- Hub (gateway) ----------------------------------------------+
|                                                                             |
|   Auth middleware                                                           |
|     | identifies client from token                                          |
|     v                                                                       |
|   Chat handler                                                              |
|     | injects system prompt: identity + session + capabilities              |
|     | merges client-supplied `tools` with FITT-registered tools             |
|     | dispatches to LiteLLM                                                 |
|     v                                                                       |
|   Tool-call interceptor (model wants to call a tool)                        |
|     | tool registry lookup                                                  |
|     v                                                                       |
|   Approval middleware                                                       |
|     | checks: deny list -> policy bucket -> client default                  |
|     | for `ask`: notify origin (or Telegram fallback), await decision       |
|     v                                                                       |
|   Tool dispatcher                                                           |
|     +---> inline Python tool                                                |
|     |       +--- hub-local project: run in-process                          |
|     |       +--- ssh_host project: wrap in `ssh <host> '...'`               |
|     |                                                                       |
|     +---> MCP tool                                                          |
|             +--- forward to the appropriate MCP server subprocess           |
|                                                                             |
|   Audit logger                                                              |
|     | appends to $FITT_HOME/audit.jsonl with HMAC chain                     |
|                                                                             |
+-----------------------------------------------------------------------------+
                                      |
                                      v
                       +-----------------------------+
                       | Execution hosts             |
                       |  - hub (localhost)          |
                       |  - laptop.tailnet (ssh)     |
                       |  - desktop.tailnet (ssh)    |
                       |  - ...                      |
                       +-----------------------------+
```

Five principles drive the design.

1. **Tools are data + policy + implementation.** A tool has a
   JSON schema (data), an approval bucket (policy), and a callable
   (implementation). Adding a tool = adding all three. The
   registry is the single source of truth.
2. **Execution follows the project.** Every file/git/shell tool
   takes a `project` argument. The project registry says where
   that project lives. The SSH backend wraps the command
   accordingly. Tools don't know whether they're running locally
   or remotely — the backend handles it.
3. **Forward, don't replace.** Client-supplied tools (Continue's
   Agent mode) stay in the request verbatim. FITT-registered tools
   are appended. The dispatcher routes by name: if FITT owns it,
   FITT runs it; otherwise the tool call passes back to the
   client.
4. **Approval is a pipeline, not a boolean.** A tool call runs
   through: deny-list check → policy lookup → client default →
   per-tool override → per-cron override (Phase 4.5) → decision.
   Each layer can short-circuit with Block, Auto, or Ask.
5. **Audit is immutable.** Every tool call (successful or not,
   approved or denied, before or after dispatch) appends one line
   to an HMAC-chained JSONL file. Tampering is detectable.

## Repository layout

Additions only; nothing existing moves.

```
home-ai-cluster/
  gateway/
    src/gateway/
      tools/                         # NEW package
        __init__.py                  # public registry API
        _types.py                    # Tool, ToolResult, ApprovalDecision dataclasses
        registry.py                  # ToolRegistry: register, lookup, list
        backend.py                   # ExecutionBackend: local / ssh dispatch
        inline.py                    # inline Python tool implementations
        mcp_client.py                # MCP server supervisor + tool surfacing
        deny_list.py                 # hardcoded destructive-pattern blocker
      approval.py                    # NEW approval middleware + Telegram inline UI
      audit.py                       # NEW append-only audit log w/ HMAC chain
      capabilities.py                # NEW system-prompt block + gap logging
      projects.py                    # NEW project registry (schema, I/O, watcher)
      # existing modules updated:
      config.py                      # add client: tag on AllowedToken
      chat.py                        # tool-call interception + forwarding
      cli.py                         # add `fitt project`, `fitt audit`, `fitt capability-gaps`
    tests/
      test_projects.py               # NEW
      test_tools_registry.py         # NEW
      test_tools_inline.py           # NEW
      test_tools_ssh_backend.py      # NEW (with mocked subprocess)
      test_approval.py               # NEW
      test_audit.py                  # NEW
      test_deny_list.py              # NEW
      test_mcp_client.py             # NEW (with mocked stdio)
      test_capabilities.py           # NEW
      test_chat_tool_forwarding.py   # NEW
  configs/
    config.example.yaml              # add: tools:, projects:, mcpServers: blocks
    secrets.example.yaml             # add: client: tag example on allowed_tokens
```

## Data model

### Project registry

Stored as `$FITT_HOME/projects.yaml`:

```yaml
# ~/.fitt/projects.yaml
projects:
  - name: home-ai-cluster
    ssh_host: ""                     # empty = hub-local
    path: /share/Public/home-ai-cluster
    test_command: "cd gateway && uv run pytest -q"
    build_command: "cd gateway && uv run ruff check src tests"

  - name: retro-ai
    ssh_host: laptop-home.tailnet     # ssh to here to touch the code
    path: /home/fred/code/retro-ai
    test_command: "pytest -q"
    build_command: ""                 # optional

  - name: work-service
    ssh_host: work-box.tailnet
    path: /home/fred/code/work-service
    test_command: "./test.sh"
```

Loaded at gateway start, hot-reloaded via file watch. Schema
validated on load; malformed entries log a warning and are
skipped.

### Client tags on tokens

Extension to `secrets.yaml`:

```yaml
allowed_tokens:
  - name: personal-ide
    token: <32+ chars>
    client: ide
  - name: personal-telegram
    token: <32+ chars>
    client: telegram
  - name: personal-cli
    token: <32+ chars>
    client: cli
  # webui is allocated its own token by install-open-webui / compose
```

`client` is optional; missing means `webui` (least-trusted). The
`Secrets.client_for(token)` helper returns the tag.

### Tool registry

In-memory after startup. Populated from two sources:

1. **Inline tools.** Python decorators in `gateway/tools/inline.py`
   register each tool with its schema, bucket, callable, and
   "does this tool need a project context?" flag.
2. **MCP tools.** When an MCP server is spawned and probed, its
   reported tools get added with name prefix `mcp.<server>.<tool>`.
   Default bucket for MCP tools: `ask`. Per-tool overrides in
   `config.yaml`.

Tool entry structure (Python dataclass):

```python
@dataclass
class Tool:
    name: str                        # "read_file", "mcp.slack.send"
    description: str                 # one-line, user-visible
    schema: dict                     # JSON schema for arguments
    bucket: ApprovalBucket           # auto/ask/trust_session/yolo/block
    callable: ToolCallable           # async (args, context) -> result
    requires_project: bool           # does `args` include `project`?
    kind: Literal["inline", "mcp"]
```

### Approval bucket

```python
class ApprovalBucket(Enum):
    AUTO = "auto"
    ASK = "ask"
    TRUST_SESSION = "trust_session"
    YOLO = "yolo"
    BLOCK = "block"
```

### Tool policy

Per-tool in `config.yaml`:

```yaml
tools:
  read_file:         { default: auto }
  write_file:        { default: ask }
  edit_file:         { default: ask }
  list_directory:    { default: auto }
  grep_repo:         { default: auto }
  glob_search:       { default: auto }
  git_status:        { default: auto }
  git_diff:          { default: auto }
  git_commit:        { default: ask }
  run_tests:         { default: ask }
  http_get:          { default: auto, deny_hosts: ["internal.corp.example"] }
  spec_read:         { default: auto }
  spec_next_task:    { default: auto }
  spec_mark_task:    { default: auto }
  spec_list:         { default: auto }
  list_capabilities: { default: auto }

  # Client overrides
  per_client:
    ide:
      write_file:  auto              # IDE user is watching; skip prompt
      edit_file:   auto
      git_commit:  auto
    webui:
      write_file:  block              # Open WebUI: read-only
      edit_file:   block
      run_tests:   block
      git_commit:  block

  # MCP wildcards
  "mcp.slack.*":      { default: ask }
  "mcp.jira.search_*": { default: auto }
  "mcp.jira.create_*": { default: ask }
```

Resolution order:
1. Deny list (code, non-overridable).
2. Per-tool, per-client override if present.
3. Per-tool default.
4. Wildcard match (for MCP).
5. Client default (`auto` for ide, `ask` for telegram, `ask` for
   cli, `block`-writes for webui).
6. Global fallback: `ask`.

### Audit entry

```python
@dataclass
class AuditEntry:
    ts: float                        # unix epoch
    session_key: str                 # "main", or UUID for unnamed
    client: str                      # "ide" | "telegram" | "webui" | "cli"
    tool: str                        # tool name
    args: dict                       # sanitised (secrets redacted)
    approval: Literal[               # how we got here
        "auto", "approved", "rejected",
        "timeout", "trust_session",
        "yolo", "blocked"]
    outcome: Literal["success", "error", "not_executed"]
    duration_ms: int                 # 0 for not-executed
    error: str                       # empty if success
    prev_hmac: str                   # hex digest of prior entry
    hmac: str                        # hex digest of this entry
```

Written as one JSON line per entry to `$FITT_HOME/audit.jsonl`.
The HMAC is computed over the concatenation of `prev_hmac` and
the serialised entry-without-hmac, keyed with a secret that lives
in `$FITT_HOME/audit.key` (generated on first write, 0600 perms).

## Module design

### `gateway/projects.py`

```python
class ProjectRegistry:
    def __init__(self, path: Path) -> None: ...
    def start_watcher(self) -> None: ...   # inotify/watchdog, hot-reload
    def stop_watcher(self) -> None: ...

    def get(self, name: str) -> Project: ...           # raise UnknownProject
    def list(self) -> list[Project]: ...
    def known_names(self) -> set[str]: ...

    def add(self, project: Project) -> None: ...      # CLI path
    def remove(self, name: str) -> None: ...
```

File watch via `watchfiles` (cross-platform, async). On change:
reload the YAML, swap the in-memory registry atomically, log the
diff.

### `gateway/tools/registry.py`

```python
class ToolRegistry:
    def __init__(self, policy: ToolPolicy) -> None: ...

    def register(self, tool: Tool) -> None: ...
    def unregister(self, name: str) -> None: ...       # for MCP shutdown

    def lookup(self, name: str) -> Tool: ...           # raise UnknownTool
    def list_names(self) -> list[str]: ...
    def describe_all(self) -> list[dict]: ...          # for capabilities block

    def resolve_bucket(
        self, tool: Tool, client: str, session_key: str,
    ) -> ApprovalBucket: ...
```

`resolve_bucket` walks the resolution chain above. Session-level
trust is tracked in an in-memory `dict[session_key, set[tool_name]]`
for the `trust_session` bucket.

### `gateway/tools/backend.py`

```python
class ExecutionBackend:
    def __init__(self, projects: ProjectRegistry) -> None: ...

    async def run_inline(
        self, tool: Tool, args: dict, project: Project | None,
    ) -> ToolResult: ...

    async def run_shell(
        self, project: Project, cmd: list[str], cwd: str | None = None,
        timeout_secs: int = 300,
    ) -> ShellResult: ...
```

`run_shell` is the sharp edge. Pseudocode:

```python
async def run_shell(self, project, cmd, cwd=None, timeout_secs=300):
    if project.ssh_host:
        remote = shlex.join(cmd)
        if cwd:
            remote = f"cd {shlex.quote(cwd)} && {remote}"
        local_argv = ["ssh", project.ssh_host, remote]
    else:
        local_argv = cmd
        cwd = cwd or project.path

    proc = await asyncio.create_subprocess_exec(
        *local_argv,
        cwd=cwd if not project.ssh_host else None,
        stdout=PIPE, stderr=PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_secs)
    except asyncio.TimeoutError:
        proc.kill()
        return ShellResult(exit=-1, stdout="", stderr="timeout", timed_out=True)
    return ShellResult(
        exit=proc.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=False,
    )
```

SSH auth: uses the gateway's SSH key (in the container's home
directory, bind-mounted from `$FITT_HOME/ssh/id_ed25519`).
Gateway never prompts for passwords; key-based only. Documented
in Phase 4's install notes.

### `gateway/tools/inline.py`

Each tool is a small function decorated with metadata. Example:

```python
@tool(
    name="read_file",
    description="Read a file from a registered project.",
    schema={
        "type": "object",
        "properties": {
            "project": {"type": "string"},
            "path":    {"type": "string", "description": "relative to project root"},
        },
        "required": ["project", "path"],
    },
    bucket=ApprovalBucket.AUTO,
    requires_project=True,
)
async def read_file(args: dict, context: ToolContext) -> ToolResult:
    project = context.projects.get(args["project"])
    path = _resolve_safe(project.path, args["path"])   # no .. escape
    if project.ssh_host:
        result = await context.backend.run_shell(
            project, ["cat", path], timeout_secs=30,
        )
        if result.exit != 0:
            return ToolResult.error(result.stderr)
        return ToolResult.ok(result.stdout)
    else:
        return ToolResult.ok(Path(path).read_text())
```

All inline tools follow this shape. `ToolContext` bundles the
registry, backend, project registry, session info.

### `gateway/tools/mcp_client.py`

Subprocess supervisor. On gateway start, reads `config.mcpServers`,
spawns each as a child process (stdin/stdout JSON-RPC), sends
`initialize` + `tools/list`, registers reported tools with prefix
`mcp.<server>.<tool>`.

Crash handling:
- Detect on stderr close or stdin write error.
- Exponential backoff restart: 1s, 2s, 4s, ... cap 5 min.
- After 5 consecutive failures: mark server as dead, deregister
  its tools, wait for explicit `fitt mcp restart <name>` or
  gateway restart.

Tool invocation: JSON-RPC `tools/call` with args. Result back.
All MCP tool calls go through the standard approval pipeline.

### `gateway/tools/deny_list.py`

```python
# Hardcoded; NOT user-configurable. Changing this requires a code change.
DENY_PATTERNS: list[re.Pattern] = [
    re.compile(r"rm\s+-rf\s+/($|\s|\*)"),
    re.compile(r"rm\s+-rf\s+~($|\s)"),
    re.compile(r"rm\s+-rf\s+\$HOME"),
    re.compile(r"rm\s+-rf\s+\.git($|\s|/)"),
    re.compile(r"git\s+push\s+.*--force"),
    re.compile(r"git\s+reset\s+--hard\s+origin"),
    re.compile(r"curl\s.*\|\s*(bash|sh|zsh)"),
    re.compile(r"wget\s.*\|\s*(bash|sh|zsh)"),
    re.compile(r"chmod\s+-R\s+777\s+/"),
    re.compile(r"dd\s+.*of=/dev/sd"),
    re.compile(r"mkfs\."),
    re.compile(r":\(\)\s*{\s*:\|:&\s*};:"),   # fork bomb
    re.compile(r"shutdown\s+-h"),
    re.compile(r"reboot\s+(-f|--force)"),
    re.compile(r"DROP\s+(DATABASE|SCHEMA|TABLE)\s", re.IGNORECASE),
    re.compile(r"aws\s+s3\s+rb\s+.*--force"),
    re.compile(r"docker\s+(system\s+)?prune\s+.*--volumes.*--all"),
    # ... more ...
]

def check(command: str) -> None:
    """Raise DestructiveOperationBlocked if command matches any deny pattern."""
    for pat in DENY_PATTERNS:
        if pat.search(command):
            raise DestructiveOperationBlocked(command, pat.pattern)
```

Tested exhaustively. Every pattern in the list has a test.

### `gateway/approval.py`

```python
class ApprovalMiddleware:
    def __init__(
        self, registry: ToolRegistry,
        telegram_notifier: TelegramNotifier | None,
    ) -> None: ...

    async def check(
        self, tool: Tool, args: dict, context: ToolContext,
    ) -> ApprovalDecision:
        # 1. deny-list check (for shell-executing tools)
        cmd = self._render_command(tool, args)
        if cmd and deny_list.check(cmd):  # raises on match
            return ApprovalDecision.blocked()

        bucket = self._registry.resolve_bucket(tool, context.client, context.session)
        if bucket == ApprovalBucket.AUTO:
            return ApprovalDecision.auto()
        if bucket == ApprovalBucket.BLOCK:
            return ApprovalDecision.blocked()
        if bucket == ApprovalBucket.YOLO:
            if self._yolo_active(context.client, context.session):
                return ApprovalDecision.auto_yolo()

        # ASK or TRUST_SESSION: need human input
        if bucket == ApprovalBucket.TRUST_SESSION:
            if self._session_trusted(context.session, tool.name):
                return ApprovalDecision.trust_session()

        # Route the ask
        origin_client = context.client
        if origin_client == "telegram" or self._needs_fallback(origin_client):
            # Telegram handles its own; WebUI/other clients fall back to Telegram
            return await self._telegram_ask(tool, args, context)
        elif origin_client == "ide":
            # IDE: policy said `ask`, but the IDE doesn't have a native ask UI
            # via FITT; the client (Continue) surfaces tool calls natively.
            # For Phase 4, IDE asks become auto-approves; user sees in Continue.
            return ApprovalDecision.auto_ide()

        # default: telegram fallback
        return await self._telegram_ask(tool, args, context)
```

Telegram ask flow:
1. Post a message with tool name, args summary, three buttons:
   `Approve`, `Reject`, `Trust session`.
2. Create an asyncio.Future. Store it keyed by callback-data.
3. Wait with timeout (2 hours default).
4. When the user clicks, the Telegram bot handler resolves the
   future with the decision.
5. On timeout: auto-reject.

### `gateway/audit.py`

```python
class AuditLog:
    def __init__(self, path: Path, key_path: Path) -> None: ...

    def append(self, entry: AuditEntry) -> None:
        # synchronous, file-locked; audit is never on the hot path
        with self._lock:
            entry.prev_hmac = self._last_hmac
            entry.hmac = self._hmac(entry)
            line = json.dumps(asdict(entry))
            self.path.write_text(... append ...)
            self._last_hmac = entry.hmac

    def verify(self) -> VerifyResult: ...  # walks the chain
```

Secret redaction before writing is a pass through the args dict
via `gateway.audit.redact()` — regex list for key names and value
formats.

### `gateway/capabilities.py`

```python
def build_capability_block(registry: ToolRegistry) -> str:
    """Build the [Capabilities] system-prompt block."""
    lines = ["[Capabilities] You can call these tools:"]
    for t in registry.describe_all():
        lines.append(f"- `{t['name']}`: {t['description']}")
    lines.append("")
    lines.append(
        "When a request needs a capability not listed above, "
        "reply in this format: 'I'd need a tool to X. Consider "
        "adding [suggestion].' so we can track the gap."
    )
    return "\n".join(lines)


def parse_gap(model_reply: str) -> GapReport | None:
    """Return a GapReport if the reply matches the standard missing-tool format."""
    # regex match: "I'd need a tool to (.*?)\. Consider adding (.*)"
    ...


class CapabilityGapLog:
    def append(self, gap: GapReport) -> None: ...
    def read(self, since: float | None = None) -> list[GapReport]: ...
```

The chat handler, after the model replies, runs `parse_gap`; if
match, writes to `$FITT_HOME/capability_gaps.log`.

### Tool forwarding in `chat.py`

New logic in the request handler:

```python
async def handle_chat_completion(request):
    body = await request.json()
    client_supplied_tools = body.get("tools", [])

    # Append FITT-registered tools
    fitt_tools = [t.to_openai_schema() for t in registry.list_all()]
    merged = client_supplied_tools + fitt_tools
    body["tools"] = merged

    # Dispatch to LiteLLM as normal
    response = await litellm.acompletion(**body)

    if response.choices[0].finish_reason == "tool_calls":
        for call in response.choices[0].message.tool_calls:
            if call.function.name in registry.known_names():
                # FITT owns this tool; execute it
                result = await execute_tool(call, context)
                # Insert tool result message, loop back to model
                ...
            else:
                # Client owns this tool; return to client to execute
                # (standard OpenAI semantics)
                pass
```

Multi-turn tool loops are bounded: max 10 tool calls per request
before bailing with "tool loop exceeded" error.

## Interactions

### How a single tool call flows end-to-end

1. Client sends chat completion request. Bearer token identified →
   `client=telegram`.
2. Auth middleware tags the context: `ctx.client = "telegram"`,
   `ctx.session = "main"`.
3. Chat handler merges client tools (none, for Telegram) with
   FITT tools. Dispatches to LiteLLM.
4. Model replies with `tool_calls: [{name: "read_file", args:
   {project: "home-ai-cluster", path: "README.md"}}]`.
5. Chat handler recognises `read_file` as FITT-owned. Calls
   `execute_tool`.
6. `execute_tool`:
   - `registry.lookup("read_file")` → Tool entry.
   - `approval.check(tool, args, ctx)` → `auto` (read-only,
     Telegram, `read_file` policy is auto).
   - `backend.run_inline(tool, args, project)` → executes, returns
     `ToolResult.ok(contents)`.
   - `audit.append(entry)`.
7. Tool result inserted back into the messages list.
8. Another LiteLLM call with the tool result. Model composes the
   final reply.
9. Reply streamed back to Telegram.

### How Continue's IDE Agent-mode flows

1. IDE sends request with `tools: [{read_file, edit_file,
   run_terminal_command, ...}]` (Continue's own toolkit).
2. Auth: `ctx.client = "ide"`.
3. Chat handler appends FITT's tools. Sends merged set to the
   model.
4. Model replies with `tool_calls: [{name: "edit_existing_file",
   args: {...}}]`. This is a Continue-owned tool (not in FITT's
   registry).
5. Chat handler sees the name isn't FITT-owned. Passes the tool
   call back to the client verbatim.
6. Continue receives the tool call, shows the diff in VS Code,
   user clicks Accept.
7. Continue sends the tool result in the next request. FITT
   forwards to the model. Loop continues.

### How a cron-fired session will flow (preview, Phase 4.5)

Phase 4.5 adds a scheduler that fires a session per cron job with
`approval_mode` policy set on the cron. The approval middleware
checks that policy override before any per-client default.

## Tests

### Unit

- `test_projects.py`: registry load, hot-reload, add/remove,
  malformed entries logged and skipped, unknown names raise.
- `test_tools_registry.py`: register, unregister, lookup, resolve
  bucket across per-tool / per-client / fallback ladders.
- `test_tools_inline.py`: each inline tool in isolation with
  stubbed backend.
- `test_tools_ssh_backend.py`: mock `asyncio.create_subprocess_exec`;
  verify the right argv is built for hub-local vs ssh_host projects.
  Timeout handling.
- `test_approval.py`: each bucket produces the right decision.
  Telegram ask is tested with a mock notifier.
- `test_audit.py`: entries are chained correctly; verify detects
  tampering at every position; redaction strips secrets.
- `test_deny_list.py`: one test per hardcoded pattern, asserting
  the pattern catches a known destructive form and lets benign
  similar-looking commands through.
- `test_mcp_client.py`: spawn a mock MCP server (fixture script),
  verify initialize + tools/list + tools/call round-trip. Crash
  recovery with exponential backoff.
- `test_capabilities.py`: capability-block generation; gap
  detection regex on representative model outputs.
- `test_chat_tool_forwarding.py`: when the request carries
  client-supplied tools, they aren't stripped. FITT tools are
  appended. Tool-call dispatch respects the name-based split.

### Property-based

Candidates via Hypothesis:

- **Audit chain integrity**: generate a random sequence of entries,
  append them, verify the chain. Tamper at a random index, verify
  that `verify()` catches it.
- **Policy resolution determinism**: for any (tool, client,
  session, override-set), the resolved bucket is stable and
  matches the documented precedence.
- **Deny-list non-bypass**: for a fixed set of destructive
  patterns and a generator of minor string mutations (whitespace,
  alias commands), ensure the deny list still catches them.

### Integration

- Full Telegram-to-tool-execution roundtrip against a stub
  Telegram server (respx-based), stub LLM via LiteLLM's mocking,
  stub SSH via subprocess monkeypatching.
- Continue-style request forwarding: verify `tools` array merging
  and name-based dispatch routing.

## Configuration additions

### `config.yaml`

```yaml
tools:
  # per-tool policies as shown above

  per_client:
    ide:
      write_file: auto
      edit_file: auto
      git_commit: auto
    webui:
      write_file: block
      edit_file: block
      run_tests: block
      git_commit: block

mcpServers:
  # zero or more MCP servers to spawn and expose
  #
  # example:
  # - name: brave-search
  #   command: ["npx", "-y", "@modelcontextprotocol/server-brave-search"]
  #   env:
  #     BRAVE_API_KEY_VAR: BRAVE_API_KEY   # read from process env

capability_gaps:
  log: ~/.fitt/capability_gaps.log
```

### `secrets.yaml`

```yaml
allowed_tokens:
  - name: ide
    token: <32+ chars>
    client: ide
  - name: telegram
    token: <32+ chars>
    client: telegram
  # webui token managed by compose / install script
```

### `projects.yaml` (new file)

Shown above under Data model.

## Rollout and migration

**Order of ops (to keep the tree green):**

1. Project registry + CLI + tests. No tool system yet.
2. Client tag on tokens. Backwards compatible (missing tag =
   webui).
3. Tool registry + inline tool scaffolding. Register
   `list_capabilities` and `spec_*` tools first (read-only,
   hub-local, no SSH needed).
4. SSH backend + first `read_file`. End-to-end path from registry
   to execution. Exercise against a real satellite.
5. Remaining read-only inline tools (`list_directory`, `grep_repo`,
   `glob_search`, `git_status`, `git_diff`).
6. Write tools (`write_file`, `edit_file`, `git_commit`) behind
   `ask` bucket. Approval middleware goes in here.
7. `run_tests` + `http_get`.
8. Deny list. Covers all shell-executing tools so far.
9. Audit log. Every tool call flows through.
10. MCP client + supervisor. Spawn one real MCP server end-to-end
    as part of the test.
11. Capability-awareness system prompt + gap logging.
12. Tool forwarding / Continue merge logic in `chat.py`.
13. Integration test: Continue end-to-end, then Telegram end-to-end.

Each step is a reviewable commit.

## Open design decisions for review

1. **Tool call loop limit.** 10 per request feels right, but
   picked without data. May be too few for `spec_runner` style
   flows in Phase 6. Consider making it configurable per-client
   (IDE tolerates more than Telegram).

2. **Approval timeout on Telegram.** 2 hours is arbitrary. Too
   short and overnight tasks time out; too long and stale prompts
   linger. Could be per-bucket.

3. **SSH key location.** `$FITT_HOME/ssh/id_ed25519` seems clean
   (bind-mounted with the rest of FITT state) but conflates two
   concerns (FITT state + SSH identity). Alternative: document
   that the user provides a key via a known path and the compose
   file mounts it.

4. **Capability gap format.** Relying on the model to emit a
   specific string format is fragile. Fallback: also log any reply
   containing "can't", "don't have", "would need" patterns as a
   soft gap candidate.

5. **Partial trust for MCP tools.** All MCP tools default to
   `ask`. Some servers expose entirely-read-only tools that
   should be `auto`. We handle this via wildcards
   (`mcp.jira.search_*`). But a badly-behaved server could
   mislabel. Accept the risk; audit catches anything destructive.

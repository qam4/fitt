"""Tool-output artifact store: hoist large tool results off the
model's context onto disk.

Context
-------

When a tool returns a large payload (a verbose ``ls -la``, a
multi-megabyte ``cat`` of a log file, a wide ``grep_repo`` hit
list), today we stuff the full text into the ``{"role": "tool",
"content": ...}`` message that goes back to the LLM. Two
downstream consequences:

* **Context bloat.** A 200 KB tool result pushes everything else
  — identity, lessons, earlier turns — toward the context-window
  cliff. The rest of the session gets summarised, forgotten, or
  silently truncated by the upstream router.
* **Poisoning reach.** The same full text gets persisted into
  tomorrow's history via :class:`~gateway.memory.PersistedToolCall`'s
  ``result_summary`` (which is capped at 300 chars, so actually
  fine) and — more importantly — sits verbatim in the in-flight
  turn's message list, where every subsequent LLM dispatch on
  that turn has to re-tokenise the whole blob. A 10-step
  tool-using turn pays the 200 KB cost 10 times.

Fix (the Claude Code "layer 0" pattern, per the 2026 compaction
survey in ``docs/hallucinations-and-poisoning.md``): any tool
result over a threshold gets written to disk as an artifact. The
in-context content becomes a short preview plus a path pointer
plus a truthful byte count. The model can ``read_file`` the
artifact if it actually needs more. Most of the time it doesn't
— the preview is enough to decide what to do next.

What this module does NOT do
----------------------------

* **No compaction, no summarisation.** Artifact storage is
  mechanical: preview head + footer line saying "full output
  here." Summarisation is a separate layer (see proposed item 5
  in the hallucinations doc).
* **No fresh tool.** MVP relies on existing ``read_file`` to
  access artifacts; the preview footer names the path. If the
  model starts asking for more often enough to be annoying, we
  add a dedicated ``read_tool_artifact`` later.
* **No compression, no dedup.** Artifacts are plain UTF-8 files.
  Disk is cheap; operational clarity wins over bytes.

On-disk layout
--------------

::

    $FITT_HOME/sessions/<session_key>/artifacts/<YYYY-MM-DD>/<uuid>.txt

Same shape as ``history/<YYYY-MM-DD>.md``: per-session, per-day,
one flat directory per day. The Phase 5 history pruner's walk
over ``sessions/*/`` extends to ``artifacts/`` with the same
age-based cutoff — no separate retention knob. Session-scoped so
archiving or deleting a session takes its tool artifacts with it.

Threshold
---------

Default 8192 bytes. That's a conservative floor: most
``read_file`` outputs (source files under ~200 lines) fit
comfortably; chatty ``ls``/``grep`` outputs tend to blow past it;
shell commands that ``cat`` large artifacts or dump help text
exceed it easily. Overridable via ``memory.tool_output_max_inline_bytes``.

When to hoist
-------------

Counted on ``len(payload.encode('utf-8'))``, not ``len(payload)``,
because the model's tokenizer cares about bytes (roughly) and
multi-byte characters in a log file shouldn't get a free pass.
Hoisting applies to both success and error payloads — a 500 KB
failing shell command is exactly the kind of thing we want on
disk, not in tomorrow's context.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- defaults

DEFAULT_MAX_INLINE_BYTES = 8 * 1024
"""Threshold above which a tool result gets hoisted to disk. 8
KB. Chosen small enough to catch anything meaningfully "big" for
a chat context and large enough that the average tool invocation
(a short file read, a status check) sails through."""

DEFAULT_PREVIEW_BYTES = 2 * 1024
"""How many bytes of the full payload to keep inline as a
preview. 2 KB — enough for the model to see the shape, detect an
error message at the top, or confirm the content matches
expectations, without paying the full size. Preview is always
smaller than the threshold; when the caller configures a preview
larger than the threshold we clamp it (hoisting would produce a
preview larger than the original, which is absurd)."""


# Anything non-printable that would break an artifact filename or
# directory name. Session keys are validated upstream
# (``sessions.py`` caps them at 64 chars, lowercase + digits +
# hyphens), but we're paranoid-by-default when building
# filesystem paths from external input.
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]")


# --------------------------------------------------------------- data


@dataclass(frozen=True, slots=True)
class HoistResult:
    """The outcome of running one tool payload through the store.

    ``content`` is always what the caller should use as the
    model-facing tool-message content. ``artifact_path`` is
    populated only when hoisting happened; callers can log it,
    surface it in events, or ignore it.
    """

    content: str
    """Payload as it should appear in the ``role: tool`` message
    sent back to the LLM. Either the original payload (below
    threshold) or ``preview + footer`` (above)."""

    artifact_path: Path | None = None
    """Where the full payload was written, or ``None`` when no
    hoisting was needed. Absolute, for unambiguous use by
    ``read_file`` and by log lines."""

    original_bytes: int = 0
    """Byte count of the unmodified payload. Useful in event
    logs and for debugging — operators can tell at a glance
    how chatty a given tool is."""

    hoisted: bool = False
    """Mirrors ``artifact_path is not None``; present as its own
    field so callers can write ``if result.hoisted`` without
    importing ``Path`` just to compare to ``None``."""


# --------------------------------------------------------------- store


class ArtifactStore:
    """Persist over-threshold tool outputs to disk and return
    context-safe replacements.

    The store is a pure-function wrapper over a session-rooted
    directory: it doesn't hold any per-call state and is safe to
    share across concurrent tool dispatches. The only shared
    resource is the filesystem, and we use ``uuid4`` filenames so
    two simultaneous writes can never collide.

    Construction is cheap (no IO). All IO happens on
    :meth:`maybe_hoist` and it's tolerant of write failures — if
    we can't write the artifact, we log a warning and return the
    payload unchanged. Truncating-a-big-output-into-context is a
    worse outcome than passing-a-big-output-through, so the
    default on failure is to degrade toward "no hoisting" rather
    than silently drop data.
    """

    def __init__(
        self,
        *,
        sessions_dir: Path,
        max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES,
        preview_bytes: int = DEFAULT_PREVIEW_BYTES,
    ) -> None:
        self._sessions_dir = sessions_dir
        # Guard against a pathological config where the preview
        # is larger than the threshold — the preview would then
        # be bigger than the payload we're trying to slim down.
        # Clamp so ``preview_bytes <= max_inline_bytes // 2``;
        # the "half" is conservative so the footer line (a
        # handful of extra bytes) doesn't push us back over.
        if max_inline_bytes < 1:
            raise ValueError("max_inline_bytes must be >= 1")
        if preview_bytes < 1:
            raise ValueError("preview_bytes must be >= 1")
        if preview_bytes > max_inline_bytes // 2:
            preview_bytes = max(1, max_inline_bytes // 2)
        self._max_inline_bytes = max_inline_bytes
        self._preview_bytes = preview_bytes

    # ------------------------------------------------------------ API

    @property
    def max_inline_bytes(self) -> int:
        return self._max_inline_bytes

    @property
    def preview_bytes(self) -> int:
        return self._preview_bytes

    def maybe_hoist(
        self,
        payload: str,
        *,
        session_key: str,
        tool_name: str,
        now: datetime | None = None,
    ) -> HoistResult:
        """Hoist ``payload`` to disk if it's over threshold.

        Returns a :class:`HoistResult` that callers plug into
        the ``role: tool`` message's ``content`` field. Below
        threshold: ``content`` is the original payload,
        ``artifact_path`` is ``None``, ``hoisted=False``.
        Above threshold: ``content`` is a preview + footer
        pointing at the on-disk artifact.

        Errors (filesystem unwritable, session dir can't be
        created) are logged at WARNING and the payload is
        returned unchanged — worse than hoisting, better than
        losing the data.
        """
        encoded = payload.encode("utf-8", errors="replace")
        size = len(encoded)
        if size <= self._max_inline_bytes:
            return HoistResult(
                content=payload, artifact_path=None, original_bytes=size, hoisted=False
            )

        now = now or datetime.now(UTC)
        try:
            artifact_path = self._write_artifact(
                payload=payload,
                session_key=session_key,
                tool_name=tool_name,
                now=now,
            )
        except Exception as exc:
            # Fall back to passing the full payload through.
            # We'd rather the model see the content than fail
            # silently because the disk is full or permissions
            # are wrong.
            _log.warning(
                "tool_artifacts.write_failed",
                extra={
                    "session_key": session_key,
                    "tool_name": tool_name,
                    "error": str(exc),
                },
            )
            return HoistResult(
                content=payload, artifact_path=None, original_bytes=size, hoisted=False
            )

        preview = _utf8_safe_prefix(payload, self._preview_bytes)
        footer = (
            f"\n\n[tool output truncated — full {size} bytes written to "
            f"{artifact_path}. Use read_file to inspect it if needed.]"
        )
        return HoistResult(
            content=preview + footer,
            artifact_path=artifact_path,
            original_bytes=size,
            hoisted=True,
        )

    # ------------------------------------------------------------ internals

    def _write_artifact(
        self,
        *,
        payload: str,
        session_key: str,
        tool_name: str,
        now: datetime,
    ) -> Path:
        """Write payload to a unique per-day file under the
        session's artifact dir. Returns the absolute path on
        success; raises on any IO failure so the caller can
        decide whether to degrade gracefully."""
        safe_session = _sanitize_for_path(session_key) or "unknown"
        safe_tool = _sanitize_for_path(tool_name) or "tool"
        day = now.strftime("%Y-%m-%d")
        day_dir = self._sessions_dir / safe_session / "artifacts" / day
        day_dir.mkdir(parents=True, exist_ok=True)
        # uuid4 avoids collisions between concurrent dispatches
        # without needing a lock. Tool name in the filename is
        # a debugging affordance — operators grepping ``ls``
        # can see what produced each artifact.
        artifact_name = f"{safe_tool}-{uuid.uuid4().hex[:12]}.txt"
        artifact_path = day_dir / artifact_name
        artifact_path.write_text(payload, encoding="utf-8", errors="replace")
        return artifact_path.resolve()


# --------------------------------------------------------------- helpers


def _utf8_safe_prefix(text: str, max_bytes: int) -> str:
    """Return a prefix of ``text`` that encodes to at most
    ``max_bytes`` UTF-8 bytes without splitting a multi-byte
    character.

    Naive slicing on ``text.encode("utf-8")[:max_bytes]`` can
    produce a half-encoded character that then raises on decode.
    We budget the byte cap against the encoded length and walk
    characters in order; this is O(n) in the preview length, not
    the full payload length."""
    if max_bytes <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if used + ch_bytes > max_bytes:
            break
        out.append(ch)
        used += ch_bytes
    return "".join(out)


def _sanitize_for_path(value: str) -> str:
    """Replace any character outside ``[A-Za-z0-9._-]`` with
    ``_``. Paranoia layer for directory / filename construction
    from any caller-provided identifier; the upstream session and
    tool-name validators already restrict the input, but we
    re-check here because the caller is trusting a filesystem
    path to be safe."""
    return _UNSAFE_PATH_CHARS.sub("_", value)


# --------------------------------------------------------------- default path


def default_artifact_dir(sessions_dir: Path, session_key: str) -> Path:
    """Resolve the artifact base dir for one session. Mirrors
    the ``history`` layout one level deeper: callers that want
    to list all artifacts for a session can point at this."""
    return sessions_dir / _sanitize_for_path(session_key) / "artifacts"

"""One-line hardening against Windows' ANSI codepage eating our output.

Why this module exists, given that every file we open already passes
``encoding="utf-8"``: the recurring failure isn't file I/O, it's the
*process boundary*. On Windows, ``sys.stdout`` only defaults to UTF-8
when it's attached to a terminal. Redirect it — to a file, a pipe, an
NSSM service capture, a kiro-monitor log — and Python falls back to the
ANSI codepage (cp1252 here), so the first non-ASCII character we print
raises ``UnicodeEncodeError``.

That makes the bug structurally hard to notice:

* it never reproduces in an interactive shell, which is where we test;
* CI is ubuntu-latest, where UTF-8 is the default, so it can't fail there;
* it fires *after* the real work, so the crash looks unrelated to it —
  and it can skip the exit-code path entirely, turning a failed run into
  a reported success.

So every entry point calls :func:`make_output_encoding_safe` before it
prints anything, and the launchers we control (NSSM install scripts,
compose) also set ``PYTHONUTF8=1``, which covers third-party libraries
and default file encodings in the same process.
"""

from __future__ import annotations

import sys


def make_output_encoding_safe() -> None:
    """Force UTF-8 on stdout/stderr, tolerating streams that can't be.

    Reconfigures the streams *in place* rather than handing them to a
    logger or a rich ``Console``: rich resolves ``sys.stdout`` on every
    write, and that late binding is what lets click's test runner (and
    any other redirect) capture output. Binding a stream at import time
    breaks all of it.

    ``errors="replace"`` is the belt-and-braces half — if a stream
    refuses UTF-8, a stray glyph degrades to ``?`` instead of taking the
    process down. Safe to call more than once.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # Captured stdout (pytest, some CI runners) is a plain
            # in-memory object with no encoding to fix.
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - exotic streams
            pass

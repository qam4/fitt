"""Docker HEALTHCHECK probe.

Runs inside the container. Reads ``FITT_PORT`` from the environment
(the same knob `docker-compose.yml` sets, so the probe and the
uvicorn bind stay in lockstep) and returns 0 iff ``/health`` answers
200.

Kept in a standalone file - rather than an inline `python -c ...`
CMD - because quoting an os.environ.get call inside a Dockerfile's
CMD is fragile and previously led to a silently-broken healthcheck
on my QNAP when I tried.
"""

from __future__ import annotations

import os
import sys
import urllib.request


def _probe() -> int:
    port = os.environ.get("FITT_PORT", "8421")
    url = f"http://localhost:{port}/health"
    try:
        resp = urllib.request.urlopen(url, timeout=3)
    except Exception as exc:
        sys.stderr.write(f"healthcheck: {type(exc).__name__}: {exc}\n")
        return 1
    return 0 if resp.status == 200 else 1


if __name__ == "__main__":
    sys.exit(_probe())

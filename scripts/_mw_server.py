"""Shared helper: reuse a persistent MobileWorld server instead of self-starting.

`mw test` starts a fresh server on every invocation when no `--aw_host` is
given (see MobileWorld's prerequisite._start_server_background), and tears it
down when the run ends. A `run_nl.py` flow does this once *per leg*. That ~few
seconds of uvicorn boot + adb-keyboard checks is pure overhead repeated every
run.

This module ensures a single long-lived server on port 6800 and hands callers
the `--aw_host` URL to reuse it. The server is spawned detached
(start_new_session=True) so it OUTLIVES the run that started it and the next run
reuses it for free.

Server-side env caveat (see CLAUDE.md "性能旋钮" / "MobileWorld fork"):
MobileWorld's server-side knobs — MW_WAIT_SECONDS (re-read per WAIT) and
MW_ADB_TIMEOUT (read once at import) — are honoured by the SERVER process, not
the `mw test` client. A reused server keeps whatever env it was *born* with, so
we bake those into the env at first spawn. Once a server is up, per-run changes
to MW_* do NOT take effect — restart it (RELAY_MW_SERVER_RESTART=1 or kill the
pid) to pick up new server-side env or new fork patches.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

DEFAULT_PORT = 6800

# Server-side env baked into the persistent server at spawn time. Mirror the
# values run_test.py used to inject into the per-run child (MW_WAIT_SECONDS),
# falling back to the caller's exported value.
_SERVER_ENV_KEYS = ("MW_WAIT_SECONDS", "MW_ADB_TIMEOUT")
_SERVER_ENV_DEFAULTS = {"MW_WAIT_SECONDS": "0.2"}


def _health_url(port: int) -> str:
    return f"http://localhost:{port}/health"


def _is_alive(port: int, timeout: float = 2.0) -> bool:
    try:
        requests.get(_health_url(port), timeout=timeout)
        return True
    except Exception:
        return False


def _pidfile(port: int) -> Path:
    return Path(tempfile.gettempdir()) / f"mw_server_{port}.pid"


def _spawn(port: int, base_env: dict[str, str]) -> bool:
    """Spawn a detached persistent server. Returns True once /health answers."""
    log_path = Path(tempfile.gettempdir()) / f"mw_server_{port}.log"
    # Same uvicorn target the framework uses; stream to a logfile (never PIPE —
    # an undrained PIPE deadlocks the server, see the fork patch #3 note).
    server_log = open(log_path, "w")

    env = dict(base_env)
    for k in _SERVER_ENV_KEYS:
        v = base_env.get(k) or _SERVER_ENV_DEFAULTS.get(k)
        if v is not None:
            env[k] = v

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mobile_world.core.server:app",
         "--host", "0.0.0.0", "--port", str(port)],
        stdout=server_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach: outlive this run, get reused next time
        env=env,
    )
    _pidfile(port).write_text(str(proc.pid))
    baked = " ".join(f"{k}={env[k]}" for k in _SERVER_ENV_KEYS if k in env)
    print(f"▶ started persistent mw server pid={proc.pid} port={port} "
          f"[{baked}]  logs→{log_path}", file=sys.stderr)

    for _ in range(30):
        if proc.poll() is not None:
            tail = ""
            try:
                tail = log_path.read_text()[-2000:]
            except Exception:
                pass
            print(f"▶ mw server failed to start:\n{tail}", file=sys.stderr)
            return False
        if _is_alive(port):
            print(f"▶ mw server healthy at http://localhost:{port}", file=sys.stderr)
            return True
        time.sleep(1)
    print("▶ mw server did not become healthy within timeout", file=sys.stderr)
    return False


def ensure_server(base_env: dict[str, str], port: int = DEFAULT_PORT) -> str | None:
    """Return an --aw_host URL for a live server, reusing or starting one.

    Reuse precedence:
      1. RELAY_AW_HOST / explicit url already in env → trust it, no probe.
      2. A healthy server already on `port` → reuse it.
      3. Otherwise spawn a detached persistent server and reuse that.

    Set RELAY_MW_SERVER_RESTART=1 to kill any existing server first (pick up
    new server-side env or fork patches). Set RELAY_NO_PERSIST_SERVER=1 to
    opt out entirely (return None → caller lets `mw test` self-start).
    """
    if base_env.get("RELAY_NO_PERSIST_SERVER") == "1":
        return None

    explicit = base_env.get("RELAY_AW_HOST")
    if explicit:
        return explicit

    if base_env.get("RELAY_MW_SERVER_RESTART") == "1":
        kill_server(port)

    if _is_alive(port):
        print(f"▶ reusing persistent mw server at http://localhost:{port} "
              f"(server-side MW_* env is whatever it was started with; "
              f"RELAY_MW_SERVER_RESTART=1 to rebake)", file=sys.stderr)
        return f"http://localhost:{port}"

    if _spawn(port, base_env):
        return f"http://localhost:{port}"
    # Spawn failed — fall back to letting `mw test` try its own start.
    return None


def kill_server(port: int = DEFAULT_PORT) -> None:
    """Kill the persistent server we started (by pidfile)."""
    pf = _pidfile(port)
    if not pf.exists():
        return
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 9)
        print(f"▶ killed mw server pid={pid}", file=sys.stderr)
    except (ValueError, ProcessLookupError):
        pass
    finally:
        pf.unlink(missing_ok=True)

"""Streaming frame capture over scrcpy-server (roadmap P2-S1).

`adb exec-out screencap -p` measures ~1.2 s/frame and is the dominant per-step
cost of the native runtime. This module keeps a resident scrcpy video stream
open instead — push scrcpy-server, start it via `app_process` with
``raw_stream=true`` (a pure Annex-B H.264 elementary stream, no scrcpy framing),
decode it on a daemon thread with PyAV, and keep only the latest frame — so
`screencap()` becomes a buffer read (milliseconds).

Host-side only: the on-device Android app captures via MediaProjection and
never imports this. Opt-in through the existing backend seam
(``RELAY_CAPTURE_BACKEND=scrcpy``; default ``screencap`` is unchanged). Any
startup or mid-run failure logs at **warning** and the caller falls back to
exec-out screencap (repo convention: fallbacks must be loud).

The scrcpy-server binary is NOT vendored. It is located, in order, from:
``RELAY_SCRCPY_SERVER`` (explicit path) → next to the ``scrcpy`` executable on
PATH → the usual share/ install locations. The server version string must match
the binary exactly (the server refuses a mismatch); it is read from
``RELAY_SCRCPY_VERSION`` or ``scrcpy --version``.

Env knobs (all optional):

- ``RELAY_SCRCPY_SERVER``   — path to the scrcpy-server binary
- ``RELAY_SCRCPY_VERSION``  — server version (default: parse `scrcpy --version`)
- ``RELAY_SCRCPY_MAX_FPS``  — encoder frame-rate cap (default 10; frames are
  only produced on screen change anyway)
- ``RELAY_SCRCPY_MAX_SIZE`` — long-side downscale in px, 0 = native (default 0:
  consumers size tap coordinates off the frame, so resolution must match the
  screencap path)
- ``RELAY_SCRCPY_BIT_RATE`` — H.264 bitrate (default 8000000)
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

_DEVICE_SERVER_PATH = "/data/local/tmp/relay-scrcpy-server.jar"

# Fallback locations when scrcpy isn't on PATH (package installs put the
# server under share/; a manual placement next to the binary also works).
_SERVER_SEARCH_PATHS = (
    "~/.local/share/scrcpy/scrcpy-server",
    "/usr/local/share/scrcpy/scrcpy-server",
    "/usr/share/scrcpy/scrcpy-server",
)


def find_server() -> Path | None:
    """Locate the scrcpy-server binary, or None (documented placement — the
    binary is not vendored; see module docstring)."""
    env = os.getenv("RELAY_SCRCPY_SERVER")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p
        logger.warning(f"RELAY_SCRCPY_SERVER={env} does not exist")
        return None
    candidates: list[Path] = []
    exe = shutil.which("scrcpy")
    if exe:
        candidates.append(Path(exe).resolve().parent / "scrcpy-server")
    candidates += [Path(p).expanduser() for p in _SERVER_SEARCH_PATHS]
    for c in candidates:
        if c.is_file():
            return c
    return None


def client_version() -> str | None:
    """The exact version string the server binary expects as its first arg
    (the server hard-refuses a mismatch)."""
    env = os.getenv("RELAY_SCRCPY_VERSION")
    if env:
        return env
    try:
        out = subprocess.run(
            ["scrcpy", "--version"], capture_output=True, text=True, timeout=10
        ).stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return None
    # First line looks like: "scrcpy 3.3.4 <https://github.com/Genymobile/scrcpy>"
    for tok in out.split():
        if tok[:1].isdigit():
            return tok
    return None


class ScrcpyStream:
    """One resident scrcpy H.264 stream with a latest-frame buffer.

    ``start()`` raises RuntimeError (with the reason) on any setup failure so
    the caller can fall back; after a successful start, a lost stream flips
    ``alive`` to False and ``screencap()`` returns None — it never raises into
    the step loop.
    """

    def __init__(self, adb_base: list[str]) -> None:
        self._adb = list(adb_base)
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.local_port: int | None = None
        # Random 31-bit session id: unique abstract-socket name per stream, so
        # concurrent leg subprocesses never cross-connect.
        self._scid = f"{int.from_bytes(os.urandom(4), 'big') & 0x7FFFFFFF:08x}"
        self._lock = threading.Condition()
        self._frame: Any | None = None  # latest av.VideoFrame (converted lazily)
        self.frame_seq = 0
        self._alive = False
        self._stop = False
        self._pending = b""

    # ------------------------------------------------------------ lifecycle

    def start(self, first_frame_timeout: float = 10.0) -> None:
        try:
            import av  # noqa: F401 — deferred: optional extra `stream`
        except ImportError as e:
            raise RuntimeError(
                "PyAV is not installed (uv sync --extra stream)"
            ) from e
        server = find_server()
        if server is None:
            raise RuntimeError(
                "scrcpy-server not found — install scrcpy or set RELAY_SCRCPY_SERVER"
            )
        version = client_version()
        if not version:
            raise RuntimeError(
                "cannot determine scrcpy-server version — set RELAY_SCRCPY_VERSION"
            )
        deadline = time.monotonic() + first_frame_timeout

        self._check(["push", str(server), _DEVICE_SERVER_PATH], "push scrcpy-server")
        out = self._check(
            ["forward", "tcp:0", f"localabstract:scrcpy_{self._scid}"], "adb forward"
        )
        self.local_port = int(out.strip().splitlines()[-1])

        server_cmd = " ".join([
            f"CLASSPATH={_DEVICE_SERVER_PATH}",
            "app_process", "/", "com.genymobile.scrcpy.Server", version,
            f"scid={self._scid}",
            "log_level=warn",
            "video=true", "audio=false", "control=false",
            # raw_stream drops every scrcpy protocol extra (device meta, frame
            # meta, codec meta, dummy byte): the socket carries H.264 only.
            "raw_stream=true",
            # tunnel_forward makes the SERVER listen (we adb-forwarded to it);
            # the default direction would need adb reverse instead.
            "tunnel_forward=true",
            "cleanup=false",
            "video_codec=h264",
            f"max_size={os.getenv('RELAY_SCRCPY_MAX_SIZE', '0')}",
            f"max_fps={os.getenv('RELAY_SCRCPY_MAX_FPS', '10')}",
            f"video_bit_rate={os.getenv('RELAY_SCRCPY_BIT_RATE', '8000000')}",
        ])
        self._proc = subprocess.Popen(
            self._adb + ["shell", server_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            self._sock = self._connect(deadline)
            self._alive = True
            self._thread = threading.Thread(
                target=self._decode_loop, name="scrcpy-decode", daemon=True
            )
            self._thread.start()
            with self._lock:
                self._lock.wait_for(
                    lambda: self._frame is not None or not self._alive,
                    timeout=max(0.1, deadline - time.monotonic()),
                )
                got_frame = self._frame is not None
            if not got_frame:
                raise RuntimeError(
                    f"no frame within {first_frame_timeout}s{self._server_tail()}"
                )
        except Exception:
            self.close()
            raise

    def _check(self, args: list[str], what: str) -> str:
        res = subprocess.run(
            self._adb + args, capture_output=True, text=True, timeout=20, check=False
        )
        if res.returncode != 0:
            raise RuntimeError(f"{what} failed rc={res.returncode}: {res.stderr.strip()}")
        return res.stdout or ""

    def _connect(self, deadline: float) -> socket.socket:
        """Connect through the adb forward until the server's abstract socket
        is actually up. adb accepts the local TCP connect immediately either
        way; if the device-side listener isn't there yet the connection EOFs
        on first read — retry. A read timeout (connected, encoder still
        warming) means the tunnel is live: keep the socket, the decode loop
        will get the bytes."""
        while True:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    f"scrcpy-server exited rc={self._proc.returncode}{self._server_tail()}"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(f"scrcpy-server never accepted{self._server_tail()}")
            try:
                s = socket.create_connection(("127.0.0.1", self.local_port), timeout=2.0)
            except OSError:
                time.sleep(0.1)
                continue
            s.settimeout(1.0)
            try:
                first = s.recv(1)
            except socket.timeout:
                s.settimeout(None)
                return s
            except OSError:
                s.close()
                time.sleep(0.1)
                continue
            if first:
                self._pending = first
                s.settimeout(None)
                return s
            s.close()  # EOF — listener not up yet
            time.sleep(0.1)

    def _server_tail(self) -> str:
        """Last server output for error messages (best-effort, non-blocking)."""
        if self._proc is None or self._proc.stdout is None:
            return ""
        try:
            os.set_blocking(self._proc.stdout.fileno(), False)
            tail = (self._proc.stdout.read() or b"").decode(errors="replace").strip()
        except (OSError, ValueError):
            return ""
        return f"; server said: {tail[-500:]}" if tail else ""

    # ---------------------------------------------------------- decode loop

    def _decode_loop(self) -> None:
        import av

        codec = av.CodecContext.create("h264", "r")
        sock, buf = self._sock, self._pending
        try:
            while not self._stop:
                data = buf or sock.recv(1 << 16)
                buf = b""
                if not data:
                    break
                for packet in codec.parse(data):
                    for frame in codec.decode(packet):
                        with self._lock:
                            self._frame = frame
                            self.frame_seq += 1
                            self._lock.notify_all()
        except OSError as e:
            if not self._stop:
                logger.warning(f"scrcpy stream read failed: {e}")
        except Exception as e:  # noqa: BLE001 — decoder must never kill the process
            logger.warning(f"scrcpy decode failed: {e}")
        finally:
            with self._lock:
                self._alive = False
                self._lock.notify_all()

    # -------------------------------------------------------------- reading

    @property
    def alive(self) -> bool:
        return self._alive

    def screencap(self, timeout: float = 5.0) -> Any | None:
        """Latest frame as a PIL RGB image, or None when the stream is gone.

        scrcpy only encodes on screen CHANGE, so an old frame timestamp is
        fine — a static screen means the buffered frame IS the current screen.
        """
        with self._lock:
            self._lock.wait_for(
                lambda: self._frame is not None or not self._alive, timeout=timeout
            )
            # A dead stream must return None even though a last frame is still
            # buffered: the screen may have moved on — the caller falls back to
            # exec-out screencap instead of trusting a stale frame.
            frame = self._frame if self._alive else None
        if frame is None:
            return None
        try:
            return frame.to_image().convert("RGB")
        except Exception as e:  # noqa: BLE001 — a bad frame must not kill the step
            logger.warning(f"scrcpy frame convert failed: {e}")
            return None

    def wait_for_new_frame(self, after_seq: int, timeout: float) -> int:
        """Block until a frame newer than `after_seq` lands (S2 settle-detection
        primitive). Returns the current frame_seq (unchanged on timeout)."""
        with self._lock:
            self._lock.wait_for(
                lambda: self.frame_seq > after_seq or not self._alive, timeout=timeout
            )
            return self.frame_seq

    # ------------------------------------------------------------- teardown

    def close(self) -> None:
        self._stop = True
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            self._proc = None
        if self.local_port is not None:
            subprocess.run(
                self._adb + ["forward", "--remove", f"tcp:{self.local_port}"],
                capture_output=True, timeout=10, check=False,
            )
            self.local_port = None

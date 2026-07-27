"""Screen recorder via `adb shell screenrecord`.

`screenrecord` caps each invocation at 180s, so we loop in a background
thread, writing chunks to the device and pulling each one as it finishes.
On `.stop()` we wait for the in-flight chunk to flush, pull it, and
optionally concat all chunks with ffmpeg into a single mp4.

Callers may pass an explicit `adb_base` argv prefix (AndroidBackend passes
its instance's, carrying the per-instance serial); the default resolves the
factory backend, which honors `RELAY_ANDROID_SERIAL`.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from agents.runtime._adb import adb_base as _default_adb_base

_CHUNK_SECONDS = 180
_DEVICE_DIR = "/sdcard"
# A chunk that dies this fast (without a stop request) never recorded at all.
# After _MAX_FAST_FAILS consecutive ones the device simply can't record (no
# H.264 encoder / DRM surface / screenrecord missing) — give up loudly instead
# of a ~1.5s Popen+pull restart storm for the rest of the run.
_FAST_FAIL_SECONDS = 5
_MAX_FAST_FAILS = 3


@dataclass
class Recording:
    out_dir: Path
    final_path: Path
    _thread: threading.Thread | None = None
    _stop_evt: threading.Event = field(default_factory=threading.Event)
    _chunks: list[Path] = field(default_factory=list)
    _proc: subprocess.Popen | None = None

    def stop(self) -> Path | None:
        if not self._thread:
            return None
        self._stop_evt.set()
        # The loop thread may start one more chunk between our event set and
        # its next check; keep terminating whatever screenrecord is in flight
        # until the thread exits, instead of terminating once and then
        # waiting out a full 180s chunk.
        deadline = time.monotonic() + _CHUNK_SECONDS + 30
        while self._thread.is_alive() and time.monotonic() < deadline:
            proc = self._proc
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            self._thread.join(timeout=2)
        if self._thread.is_alive():
            # A hung adb pull can outlive the deadline; finalizing now would
            # rename/unlink chunk files the loop thread still touches. Leave
            # the chunks unmerged rather than race it.
            logger.warning(
                "recorder: capture thread still busy after stop deadline; "
                f"leaving raw chunks in {self.out_dir}"
            )
            return self.out_dir if self._chunks else None
        return self._finalize()

    def _finalize(self) -> Path | None:
        if not self._chunks:
            logger.warning("recorder: no chunks captured")
            return None
        if len(self._chunks) == 1:
            self._chunks[0].rename(self.final_path)
            logger.info(f"recorder: saved → {self.final_path}")
            return self.final_path
        if not shutil.which("ffmpeg"):
            logger.warning(
                f"recorder: ffmpeg not found; leaving {len(self._chunks)} "
                f"chunks in {self.out_dir}"
            )
            return self.out_dir
        listfile = self.out_dir / "concat.txt"
        listfile.write_text(
            "".join(f"file '{p.name}'\n" for p in self._chunks),
            encoding="utf-8",
        )
        res = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-c", "copy", str(self.final_path)],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            logger.warning(f"ffmpeg concat failed: {res.stderr[-400:]}")
            return self.out_dir
        for p in self._chunks:
            p.unlink(missing_ok=True)
        listfile.unlink(missing_ok=True)
        logger.info(f"recorder: saved → {self.final_path}")
        return self.final_path


def start(
    out_dir: Path, *, basename: str = "recording", adb_base: list[str] | None = None
) -> Recording:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = list(adb_base) if adb_base is not None else _default_adb_base()
    rec = Recording(
        out_dir=out_dir,
        final_path=out_dir / f"{basename}.mp4",
    )

    def _loop() -> None:
        idx = 0
        fast_fails = 0
        while not rec._stop_evt.is_set():
            idx += 1
            device_path = f"{_DEVICE_DIR}/relay_rec_{idx:03d}.mp4"
            local_path = out_dir / f"chunk_{idx:03d}.mp4"
            logger.info(f"recorder: chunk {idx} → {device_path}")
            rec._proc = subprocess.Popen(
                base + [
                    "shell", "screenrecord",
                    "--time-limit", str(_CHUNK_SECONDS),
                    device_path,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            t0 = time.monotonic()
            rec._proc.wait()
            elapsed = time.monotonic() - t0
            # Device needs a moment to finalize the mp4 trailer after SIGTERM.
            time.sleep(1.0)
            pull = subprocess.run(
                base + ["pull", device_path, str(local_path)],
                capture_output=True, text=True,
            )
            subprocess.run(
                base + ["shell", "rm", "-f", device_path],
                capture_output=True, text=True,
            )
            if pull.returncode == 0 and local_path.exists() and local_path.stat().st_size > 0:
                fast_fails = 0
                rec._chunks.append(local_path)
                logger.info(
                    f"recorder: chunk {idx} pulled ({local_path.stat().st_size} bytes, {elapsed:.1f}s)"
                )
            else:
                logger.warning(f"recorder: pull failed for chunk {idx}: {pull.stderr.strip()}")
                if elapsed < _FAST_FAIL_SECONDS and not rec._stop_evt.is_set():
                    fast_fails += 1
                    if fast_fails >= _MAX_FAST_FAILS:
                        logger.warning(
                            f"recorder: screenrecord died instantly {fast_fails} "
                            "times in a row — device cannot record; giving up "
                            "recording for this run"
                        )
                        break
            # If we exited well before the time limit, the recorder was
            # stopped externally — don't immediately spin up another chunk.
            if elapsed < _CHUNK_SECONDS - 5 and rec._stop_evt.is_set():
                break

    rec._thread = threading.Thread(target=_loop, name="relay-recorder", daemon=True)
    rec._thread.start()
    return rec

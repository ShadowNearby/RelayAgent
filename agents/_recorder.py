"""Screen recorder via `adb shell screenrecord`.

`screenrecord` caps each invocation at 180s, so we loop in a background
thread, writing chunks to the device and pulling each one as it finishes.
On `.stop()` we wait for the in-flight chunk to flush, pull it, and
optionally concat all chunks with ffmpeg into a single mp4.

Honors `APPCARDS_ANDROID_SERIAL` via `agents._adb.adb_base()`.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from agents._adb import adb_base

_CHUNK_SECONDS = 180
_DEVICE_DIR = "/sdcard"


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
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._thread.join(timeout=_CHUNK_SECONDS + 30)
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


def start(out_dir: Path, *, basename: str = "recording") -> Recording:
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = Recording(
        out_dir=out_dir,
        final_path=out_dir / f"{basename}.mp4",
    )

    def _loop() -> None:
        idx = 0
        while not rec._stop_evt.is_set():
            idx += 1
            device_path = f"{_DEVICE_DIR}/appcards_rec_{idx:03d}.mp4"
            local_path = out_dir / f"chunk_{idx:03d}.mp4"
            logger.info(f"recorder: chunk {idx} → {device_path}")
            rec._proc = subprocess.Popen(
                adb_base() + [
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
                adb_base() + ["pull", device_path, str(local_path)],
                capture_output=True, text=True,
            )
            subprocess.run(
                adb_base() + ["shell", "rm", "-f", device_path],
                capture_output=True, text=True,
            )
            if pull.returncode == 0 and local_path.exists() and local_path.stat().st_size > 0:
                rec._chunks.append(local_path)
                logger.info(
                    f"recorder: chunk {idx} pulled ({local_path.stat().st_size} bytes, {elapsed:.1f}s)"
                )
            else:
                logger.warning(f"recorder: pull failed for chunk {idx}: {pull.stderr.strip()}")
            # If we exited well before the time limit, the recorder was
            # stopped externally — don't immediately spin up another chunk.
            if elapsed < _CHUNK_SECONDS - 5 and rec._stop_evt.is_set():
                break

    rec._thread = threading.Thread(target=_loop, name="appcards-recorder", daemon=True)
    rec._thread.start()
    return rec

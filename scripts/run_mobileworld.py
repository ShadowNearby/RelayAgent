#!/usr/bin/env python3
"""Run one MobileWorld real-device goal.

MobileWorld can be provided either as an installed Python package or as a git
submodule under this repository. This entry
loads RelayAgent's .env, ensures the MobileWorld server is reachable,
optionally prelaunches the target app, optionally records the device screen,
then runs:

    uv run mw test "<goal>" ...
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import _recorder  # noqa: E402
from agents._adb import adb_base, force_stop  # noqa: E402
from agents.runtime_config import load_dotenv  # noqa: E402

DEFAULT_MOBILEWORLD_SUBMODULE = REPO_ROOT / "third_party" / "MobileWorld"
DEFAULT_ENV_FILE = Path(".env")
DEFAULT_SERVER_URL = "http://127.0.0.1:6800"
DEFAULT_SERVER_LOG = Path("artifacts") / "mobileworld_server.log"
DEFAULT_APP = "com.google.android.apps.maps"
DEFAULT_MODEL = "qwen"


def _resolve_mobileworld_runtime(
    source: str,
    mobileworld_dir: Path | None,
) -> tuple[list[str], Path]:
    """Return the MobileWorld command prefix and cwd.

    `module` mode assumes the `mobile_world` Python package is importable from
    the current Python environment. `submodule` mode runs `uv run mw` from a repo-local
    MobileWorld checkout, typically a git submodule.
    """
    if mobileworld_dir is not None:
        path = mobileworld_dir.expanduser()
        if path.is_absolute():
            raise RuntimeError(
                "--mobileworld-dir must be a repo-relative path, e.g. third_party/MobileWorld"
            )
        path = (REPO_ROOT / path).resolve()
        if not path.is_dir():
            raise RuntimeError(f"MobileWorld directory not found: {path}")
        return ["uv", "run", "mw"], path

    submodule_dir = DEFAULT_MOBILEWORLD_SUBMODULE
    if source in {"auto", "submodule"} and submodule_dir.is_dir():
        return ["uv", "run", "mw"], submodule_dir
    if source == "submodule":
        raise RuntimeError(
            "MobileWorld submodule not found at third_party/MobileWorld. "
            "Add it as a git submodule or use --mobileworld-source module."
        )
    if source in {"auto", "module"} and importlib.util.find_spec("mobile_world") is not None:
        return [
            sys.executable,
            "-c",
            "from mobile_world.core.cli import main; main()",
        ], REPO_ROOT
    if source == "module":
        raise RuntimeError(
            "MobileWorld Python module is not installed in this uv environment. "
            "Add it as a dependency or use --mobileworld-source submodule."
        )
    raise RuntimeError(
        "No MobileWorld runtime found. Add a repo-local git submodule at "
        "third_party/MobileWorld, or install MobileWorld as a Python dependency."
    )


def _server_health_ok(server_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{server_url.rstrip('/')}/health", timeout=2) as res:
            return 200 <= res.status < 300
    except (OSError, urllib.error.URLError):
        return False


def _wait_for_server(server_url: str, *, timeout_s: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _server_health_ok(server_url):
            return True
        time.sleep(1.0)
    return False


def _run_adb(args: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        adb_base() + args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _prelaunch(package: str) -> None:
    print(f"Prelaunching {package} on device...", flush=True)
    force_stop(package)
    res = _run_adb(
        [
            "shell",
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ]
    )
    if res.returncode != 0 or "No activities found" in (res.stdout + res.stderr):
        raise RuntimeError(
            f"Failed to launch {package}: stdout={res.stdout.strip()!r} "
            f"stderr={res.stderr.strip()!r}"
        )
    time.sleep(6)
    focus = _run_adb(["shell", "dumpsys", "window"], timeout=10)
    for line in (focus.stdout or "").splitlines():
        if "mCurrentFocus" in line:
            print(line.strip(), flush=True)
            break


def _resolve_record_dir(value: str | None) -> Path:
    if value:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "recordings" / f"mobileworld_{ts}"


def _resolve_repo_path(path: Path) -> Path:
    path = path.expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("goal", help="Natural-language goal passed to `mw test`.")
    p.add_argument("--app", default=DEFAULT_APP,
                   help="App package to force-stop and launcher-start before running.")
    p.add_argument("--no-prelaunch", action="store_true",
                   help="Do not prelaunch the target app.")
    p.add_argument("--serial", default=os.getenv("RELAY_ANDROID_SERIAL") or os.getenv("ANDROID_SERIAL"),
                   help="Android device serial; sets RELAY_ANDROID_SERIAL and ANDROID_SERIAL.")
    p.add_argument("--mobileworld-source", choices=["auto", "module", "submodule"], default="auto",
                   help="How to locate MobileWorld. auto uses a repo-local submodule if present, "
                        "otherwise the installed Python module.")
    p.add_argument("--mobileworld-dir", type=Path, default=None,
                   help="Repo-local MobileWorld checkout, usually a git submodule. "
                        "Relative paths are resolved from RelayAgent root.")
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE,
                   help="Env file containing LLM_API_KEY and optional LLM_BASE_URL/LLM_MODEL.")
    p.add_argument("--server-url", default=DEFAULT_SERVER_URL,
                   help="MobileWorld server URL.")
    p.add_argument("--no-start-server", action="store_true",
                   help="Require an already-running MobileWorld server.")
    p.add_argument("--server-log", type=Path, default=DEFAULT_SERVER_LOG,
                   help="Log file for an auto-started MobileWorld server.")
    p.add_argument("--agent-type", default="general_e2e",
                   help="MobileWorld agent type.")
    p.add_argument("--model-name", "--model_name", dest="model_name", default=None,
                   help="Model name; defaults to LLM_MODEL then qwen.")
    p.add_argument("--llm-base-url", "--llm_base_url", dest="llm_base_url", default=None,
                   help="LLM base URL; defaults to the LLM_BASE_URL set in .env.")
    p.add_argument("--api-key", "--api_key", dest="api_key", default=None,
                   help="LLM API key; defaults to LLM_API_KEY. Prefer .env over this flag.")
    p.add_argument("--max-round", "--max_round", dest="max_round", default="25",
                   help="Max MobileWorld agent rounds.")
    p.add_argument("--timeout", default="600",
                   help="Wall-clock timeout passed to MobileWorld.")
    p.add_argument("--record", nargs="?", const="", default=None, metavar="DIR",
                   help="Record device screen. Optional DIR overrides recordings/mobileworld_<ts>/.")
    p.add_argument("--record-dir", default=None,
                   help="Record device screen into DIR. Equivalent to --record DIR.")
    p.add_argument("--llm-calls-out", "--llm_calls_out", dest="llm_calls_out", default=None,
                   metavar="PATH",
                   help="Write per-LLM-call records (latency + prompt/completion/cached tokens) "
                        "as JSON to PATH. Activates a non-invasive probe (agents.mw_llm_probe) in "
                        "the mw test subprocess; MobileWorld's own source is left untouched.")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra and extra[0] == "--":
        extra = extra[1:]

    env_file = _resolve_repo_path(args.env_file)
    server_log = _resolve_repo_path(args.server_log)
    if not env_file.is_file():
        parser.error(f"env file not found: {env_file}")
    try:
        mw_cmd, mw_cwd = _resolve_mobileworld_runtime(args.mobileworld_source, args.mobileworld_dir)
    except RuntimeError as e:
        parser.error(str(e))

    env_vars = load_dotenv(env_file)
    llm_base_url = (
        args.llm_base_url
        or os.getenv("LLM_BASE_URL")
        or env_vars.get("LLM_BASE_URL")
    )
    if not llm_base_url:
        parser.error(f"LLM_BASE_URL is empty; put it in {env_file} or pass --llm-base-url")
    api_key = args.api_key or os.getenv("LLM_API_KEY") or env_vars.get("LLM_API_KEY")
    model_name = args.model_name or os.getenv("LLM_MODEL") or env_vars.get("LLM_MODEL") or DEFAULT_MODEL
    if not api_key:
        parser.error(f"LLM_API_KEY is empty; put it in {env_file} or export it")

    if args.serial:
        os.environ["RELAY_ANDROID_SERIAL"] = args.serial
        os.environ["ANDROID_SERIAL"] = args.serial

    server_proc: subprocess.Popen[bytes] | None = None
    server_log_fh = None
    rec: _recorder.Recording | None = None
    rc = 1

    try:
        if not _server_health_ok(args.server_url):
            if args.no_start_server:
                raise RuntimeError(f"MobileWorld server is not reachable at {args.server_url}")
            print("MobileWorld server not reachable; starting it in background...", flush=True)
            server_log.parent.mkdir(parents=True, exist_ok=True)
            server_log_fh = server_log.open("ab")
            server_proc = subprocess.Popen(
                [*mw_cmd, "server"],
                cwd=mw_cwd,
                stdin=subprocess.DEVNULL,
                stdout=server_log_fh,
                stderr=subprocess.STDOUT,
            )
            if not _wait_for_server(args.server_url):
                raise RuntimeError(f"MobileWorld server did not become healthy; see {server_log}")
            print(
                f"MobileWorld server is healthy (pid={server_proc.pid}, log={server_log})",
                flush=True,
            )
        else:
            print(f"Reusing MobileWorld server at {args.server_url}", flush=True)

        record_arg = args.record_dir if args.record_dir is not None else args.record
        if record_arg is not None:
            out_dir = _resolve_record_dir(record_arg)
            os.environ.setdefault("RELAY_SKIP_STEP_SCREENSHOT", "1")
            rec = _recorder.start(out_dir)
            print(f"Recording device screen to {out_dir}", flush=True)
            time.sleep(1)

        if not args.no_prelaunch and args.app:
            _prelaunch(args.app)

        cmd = [
            *mw_cmd,
            "test",
            args.goal,
            "--agent-type",
            args.agent_type,
            "--model-name",
            model_name,
            "--llm-base-url",
            llm_base_url,
            "--aw-host",
            args.server_url,
            "--api-key",
            api_key,
            "--max-round",
            str(args.max_round),
            "--timeout",
            str(args.timeout),
            *extra,
        ]
        child_env = os.environ.copy()
        if args.llm_calls_out:
            calls_out = _resolve_repo_path(Path(args.llm_calls_out))
            child_env["RELAY_MW_LLM_CALLS_OUT"] = str(calls_out)
            probe_dir = str(Path(__file__).resolve().parent / "_mw_probe")
            existing_pp = child_env.get("PYTHONPATH", "")
            child_env["PYTHONPATH"] = (
                os.pathsep.join([probe_dir, existing_pp]) if existing_pp else probe_dir
            )
            print(f"Per-call LLM probe active -> {calls_out}", flush=True)

        print(f"Running MobileWorld goal: {args.goal}", flush=True)
        rc = subprocess.run(cmd, cwd=mw_cwd, check=False, env=child_env).returncode
        return rc
    finally:
        if rec is not None:
            print("Stopping screen recording...", flush=True)
            final = rec.stop()
            if final:
                print(f"Recording saved: {final}", flush=True)
        if server_proc is not None:
            server_proc.send_signal(signal.SIGTERM)
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait()
        if server_log_fh is not None:
            server_log_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())

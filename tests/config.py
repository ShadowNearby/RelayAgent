import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

def _find_adb() -> Path:
    env = os.environ.get("ADB")
    if env:
        return Path(env)
    which = shutil.which("adb")
    if which:
        return Path(which)
    return Path("adb")
MANIFESTS = ROOT / "manifests"
RESULTS_ROOT = ROOT / "test-results"
MATCH_INTENT_SCRIPT = ROOT / "examples" / "match_intent.py"

CLI_REMAP_DEVICE_RESOLUTION = "1440x3120"
CLI_REMAP_DEVICE_DENSITY = "480"

ADB = _find_adb()
TRAJ_SUBDIR = "adb-traj"

# Real-device tests are disabled by default so normal unittest discovery stays safe.
RUN_REAL_ADB_TESTS = True

# Expensive artifacts are opt-in. traj.jsonl event logging is always kept.
CAPTURE_TRAJ = False

RESULT_TIMEOUT_SECONDS = 120.0
RESULT_STABLE_SECONDS = 6.0
RESULT_POLL_SECONDS = 2.0
RESULT_SCROLL_EVERY_SECONDS = 4.0

FULL_RESULT_MAX_SCROLLS = 8
FULL_RESULT_REPEAT_LIMIT = 1

LAUNCH_SETTLE_SECONDS = 3.0
ACTION_SETTLE_SECONDS = 0.5
SCROLL_SETTLE_SECONDS = 0.5
BLOCKER_TIMEOUT_SECONDS = 0.5
BLOCKER_SETTLE_SECONDS = 0.3

# Screen recording via `adb shell screenrecord`. The recording starts before
# entry steps and stops after the flow ends (or on error). Saved as .mp4.
SCREEN_RECORD = True
SCREEN_RECORD_BITRATE = "4000000"
SCREEN_RECORD_TIME_LIMIT = 180  # seconds; max duration before auto-stop

# Vision summary is opt-in. Put secrets in tests/config_local.py.
VISION_SUMMARY = False
VISION_BASE_URL = ""  # set via tests/config_local.py or VISION_BASE_URL env var
VISION_API_KEY = ""
VISION_MODEL = "qwen"
VISION_MAX_IMAGES = 8
VISION_TIMEOUT_SECONDS = 120
VISION_REQUIRED = False

try:
    from tests.config_local import *  # noqa: F401,F403
except ModuleNotFoundError as exc:
    if exc.name != "tests.config_local":
        raise

import base64
import hashlib
import json
import re
import subprocess
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from tests import config


ROOT = config.ROOT
MANIFESTS = config.MANIFESTS
RESULTS_ROOT = config.RESULTS_ROOT
TRAJ_ROOT = RESULTS_ROOT / config.TRAJ_SUBDIR
ADB = Path(config.ADB)
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"

RUN_REAL_ADB_TESTS = config.RUN_REAL_ADB_TESTS
CAPTURE_TRAJ = config.CAPTURE_TRAJ
RESULT_TIMEOUT_SECONDS = float(config.RESULT_TIMEOUT_SECONDS)
RESULT_STABLE_SECONDS = float(config.RESULT_STABLE_SECONDS)
RESULT_POLL_SECONDS = float(config.RESULT_POLL_SECONDS)
RESULT_SCROLL_EVERY_SECONDS = float(config.RESULT_SCROLL_EVERY_SECONDS)
FULL_RESULT_MAX_SCROLLS = int(config.FULL_RESULT_MAX_SCROLLS)
FULL_RESULT_REPEAT_LIMIT = int(config.FULL_RESULT_REPEAT_LIMIT)
LAUNCH_SETTLE_SECONDS = float(config.LAUNCH_SETTLE_SECONDS)
ACTION_SETTLE_SECONDS = float(config.ACTION_SETTLE_SECONDS)
SCROLL_SETTLE_SECONDS = float(config.SCROLL_SETTLE_SECONDS)
BLOCKER_TIMEOUT_SECONDS = float(config.BLOCKER_TIMEOUT_SECONDS)
BLOCKER_SETTLE_SECONDS = float(config.BLOCKER_SETTLE_SECONDS)
SCREEN_RECORD = config.SCREEN_RECORD
SCREEN_RECORD_BITRATE = str(config.SCREEN_RECORD_BITRATE)
SCREEN_RECORD_TIME_LIMIT = int(config.SCREEN_RECORD_TIME_LIMIT)
VISION_SUMMARY = config.VISION_SUMMARY
VISION_BASE_URL = config.VISION_BASE_URL
VISION_API_KEY = config.VISION_API_KEY
VISION_MODEL = config.VISION_MODEL
VISION_MAX_IMAGES = int(config.VISION_MAX_IMAGES)
VISION_TIMEOUT_SECONDS = int(config.VISION_TIMEOUT_SECONDS)
VISION_REQUIRED = config.VISION_REQUIRED


def _bounds_center(bounds: str) -> tuple[int, int]:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        raise AssertionError(f"Invalid bounds: {bounds!r}")
    x1, y1, x2, y2 = (int(part) for part in match.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _box_center(box: list[int]) -> tuple[int, int]:
    x1, y1, x2, y2 = box
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _path_for_traj(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _merge_visible_text_pages(texts: list[str]) -> str:
    seen = set()
    merged = []
    for text in texts:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line in seen:
                continue
            seen.add(line)
            merged.append(line)
    return "\n".join(merged)


@unittest.skipUnless(
    RUN_REAL_ADB_TESTS,
    "set RUN_REAL_ADB_TESTS = True in tests/config_local.py",
)
class ManifestRealAdbTests(unittest.TestCase):
    original_ime: str | None = None
    run_id = time.strftime("%Y%m%d-%H%M%S")

    @classmethod
    def setUpClass(cls) -> None:
        if not ADB.exists():
            raise unittest.SkipTest(f"adb not found at {ADB}")

        devices = cls.adb("devices").stdout
        connected = [
            line for line in devices.splitlines()
            if line.strip() and "\tdevice" in line
        ]
        if not connected:
            raise unittest.SkipTest(f"no adb device connected:\n{devices}")

        cls.original_ime = cls.adb(
            "shell", "settings", "get", "secure", "default_input_method",
        ).stdout.strip()
        cls.adb("shell", "ime", "set", ADB_KEYBOARD_IME)

    def setUp(self) -> None:
        test_name = self.id().rsplit(".", 1)[-1]
        self.traj_dir = TRAJ_ROOT / self.run_id / test_name
        self.traj_dir.mkdir(parents=True, exist_ok=True)
        self.traj_path = self.traj_dir / "traj.jsonl"
        self.traj_index = 0
        self.record_traj("test_start", test=self.id())

    def tearDown(self) -> None:
        if SCREEN_RECORD and hasattr(self, "_screenrecord_proc"):
            if self._screenrecord_proc.poll() is None:
                self.adb("shell", "killall", "-2", "screenrecord", check=False)
                self._screenrecord_proc.terminate()
        self.record_traj("test_end", test=self.id())

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.original_ime:
            cls.adb("shell", "ime", "set", cls.original_ime, check=False)

    @classmethod
    def adb(cls, *args: str, timeout: int = 30,
            check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [str(ADB), *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"adb {' '.join(args)} failed with {proc.returncode}:\n"
                f"{proc.stdout}"
            )
        return proc

    def record_traj(self, event: str, **data: object) -> None:
        row = {
            **data,
            "index": self.traj_index,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
        }
        self.traj_index += 1
        with self.traj_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def capture_traj(self, label: str) -> dict[str, str | None]:
        if not CAPTURE_TRAJ:
            snapshot = {
                "label": label,
                "xml": None,
                "screenshot": None,
            }
            self.record_traj(
                "snapshot_skipped",
                reason="CAPTURE_TRAJ disabled",
                **snapshot,
            )
            return snapshot

        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label).strip("_")
        prefix = f"{self.traj_index:03d}_{safe}"
        xml_path = self.traj_dir / f"{prefix}.xml"
        png_path = self.traj_dir / f"{prefix}.png"
        remote_xml = "/sdcard/appagentcards-traj.xml"
        remote_png = "/sdcard/appagentcards-traj.png"

        xml_ok = False
        png_ok = False
        dump = self.adb(
            "shell", "uiautomator", "dump", remote_xml,
            timeout=20,
            check=False,
        )
        if dump.returncode == 0:
            xml = self.adb("exec-out", "cat", remote_xml, timeout=20,
                           check=False)
            if xml.returncode == 0:
                xml_path.write_text(xml.stdout, encoding="utf-8")
                xml_ok = True

        shot = self.adb("shell", "screencap", "-p", remote_png,
                        timeout=20, check=False)
        if shot.returncode == 0:
            pull = self.adb("pull", remote_png, str(png_path), timeout=20,
                            check=False)
            png_ok = pull.returncode == 0 and png_path.exists()

        snapshot = {
            "label": label,
            "xml": _path_for_traj(xml_path) if xml_ok else None,
            "screenshot": _path_for_traj(png_path) if png_ok else None,
        }
        self.record_traj(
            "snapshot",
            **snapshot,
        )
        return snapshot

    def start_screenrecord(self) -> None:
        if not SCREEN_RECORD:
            self.record_traj(
                "screenrecord_skipped",
                reason="SCREEN_RECORD disabled",
            )
            return

        self._screenrecord_remote = "/sdcard/appagentcards-record.mp4"
        self.adb("shell", "rm", "-f", self._screenrecord_remote, check=False)
        self._screenrecord_proc = subprocess.Popen(
            [
                str(ADB), "shell",
                f"screenrecord --time-limit {SCREEN_RECORD_TIME_LIMIT} "
                f"--bit-rate {SCREEN_RECORD_BITRATE} {self._screenrecord_remote}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.8)
        self.record_traj(
            "screenrecord_started",
            remote=self._screenrecord_remote,
            bitrate=SCREEN_RECORD_BITRATE,
            time_limit=SCREEN_RECORD_TIME_LIMIT,
        )

    def stop_screenrecord(self) -> None:
        if not SCREEN_RECORD:
            return
        if not hasattr(self, "_screenrecord_proc"):
            return

        self.adb("shell", "killall", "-2", "screenrecord", check=False)
        time.sleep(1.2)
        if self._screenrecord_proc.poll() is None:
            self._screenrecord_proc.terminate()
            try:
                self._screenrecord_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._screenrecord_proc.kill()

        local_path = self.traj_dir / "screen_record.mp4"
        pull = self.adb(
            "pull", self._screenrecord_remote, str(local_path),
            timeout=30, check=False,
        )
        if pull.returncode == 0 and local_path.exists():
            self.record_traj(
                "screenrecord_stopped",
                file=_path_for_traj(local_path),
                size_bytes=local_path.stat().st_size,
            )
        else:
            self.record_traj(
                "screenrecord_pull_failed",
                remote=self._screenrecord_remote,
                pull_stdout=pull.stdout,
            )

    def load_manifest(self, app_id: str) -> dict:
        path = MANIFESTS / f"{app_id}.yaml"
        if not path.exists():
            # ctrip's package id is already filename-safe but keep lookup generic.
            for candidate in MANIFESTS.glob("*.yaml"):
                data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                if data["app_id"] == app_id:
                    return data
            raise AssertionError(f"No manifest for {app_id}")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def capability_by_id(self, card: dict, capability_id: str | None) -> dict | None:
        if not capability_id:
            return None

        for capability in card["embedded_agent"]["capabilities"]:
            if capability["id"] == capability_id:
                return capability

        raise AssertionError(
            f"No capability {capability_id!r} in {card['app_id']}"
        )

    def run_real_manifest_flow(
        self,
        app_id: str,
        prompt: str,
        expected_capability: str | None = None,
    ) -> None:
        card = self.load_manifest(app_id)
        capability = self.capability_by_id(card, expected_capability)
        entry = card["embedded_agent"]["entry"]
        invocation = card["embedded_agent"]["invocation"]

        self.record_traj(
            "flow_start",
            app_id=app_id,
            app_name=card["app_name"],
            card_version=card["card_version"],
            expected_capability=expected_capability,
            prompt=prompt,
        )
        try:
            self.launch_app(app_id)
            self.capture_traj("after_launch")
            self.dismiss_common_blockers()
            self.start_screenrecord()
            self.run_manifest_action_flow(
                "prepare_fresh_conversation",
                entry.get("x_prepare_fresh_conversation"),
            )

            for index, step in enumerate(entry["primary"].get("steps", [])):
                self.record_traj("entry_step", step_index=index, step=step)
                if "tap" in step:
                    self.tap_selector(step["tap"])
                elif "wait" in step:
                    self.wait_step(step["wait"])
                elif "swipe" in step:
                    self.swipe_step(step["swipe"])
                self.capture_traj(f"after_entry_step_{index}")
                self.dismiss_common_blockers()

            self.record_traj("focus_input", selector=invocation["input"]["field"])
            self.tap_selector(invocation["input"]["field"])
            time.sleep(0.5)
            self.capture_traj("after_focus_input")

            self.adb("shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT",
                     check=False)
            self.record_traj("clear_text")
            self.adb_keyboard_text(prompt)
            self.capture_traj("after_text_input")
            self.assert_text_visible(prompt)
            before_submit_text = self.visible_text()

            self.record_traj("submit", selector=invocation["submit"]["trigger"])
            self.tap_selector(invocation["submit"]["trigger"])
            self.capture_traj("after_submit")
            self.wait_for_result_stable(before_submit_text)
            stable_snapshot = self.capture_traj("after_result_stable")
            full_result = self.collect_full_result_by_scrolling(stable_snapshot)
            self.summarize_full_result_with_vision(app_id, prompt, full_result)
            post_result_flow = (
                capability.get("x_post_result_flow") if capability else None
            )
            if post_result_flow:
                self.run_manifest_action_flow(
                    "post_result",
                    post_result_flow,
                    capability_id=expected_capability or "unknown",
                )
            self.record_traj("flow_end", app_id=app_id)
            self.stop_screenrecord()
        except Exception as exc:
            self.record_traj("flow_error", error=repr(exc))
            self.capture_traj("failure")
            self.stop_screenrecord()
            raise

    def launch_app(self, app_id: str) -> None:
        self.record_traj("launch_app", app_id=app_id)
        self.adb("shell", "am", "force-stop", app_id, check=False)
        proc = self.adb(
            "shell", "monkey", "-p", app_id,
            "-c", "android.intent.category.LAUNCHER", "1",
            timeout=20,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("Events injected: 1", proc.stdout)
        time.sleep(LAUNCH_SETTLE_SECONDS)

    def dump_tree(self) -> ET.Element:
        remote = "/sdcard/appagentcards-window.xml"
        self.adb("shell", "uiautomator", "dump", remote, timeout=20)
        xml = self.adb("exec-out", "cat", remote, timeout=20).stdout
        try:
            return ET.fromstring(xml)
        except ET.ParseError as exc:
            raise AssertionError(f"Could not parse UI dump:\n{xml[:1000]}") from exc

    def iter_nodes(self) -> list[ET.Element]:
        return list(self.dump_tree().iter("node"))

    def visible_text(self) -> str:
        texts = []
        for node in self.iter_nodes():
            text = node.attrib.get("text", "").strip()
            if text:
                texts.append(text)
        return "\n".join(texts)

    def find_node(self, selector: dict, timeout: float = 10) -> ET.Element:
        deadline = time.monotonic() + timeout
        last_seen = ""
        while time.monotonic() < deadline:
            nodes = self.iter_nodes()
            last_seen = "\n".join(
                f"text={n.attrib.get('text', '')!r} "
                f"desc={n.attrib.get('content-desc', '')!r} "
                f"res={n.attrib.get('resource-id', '')!r}"
                for n in nodes
            )
            for node in nodes:
                if self.node_matches(node, selector):
                    return node
            time.sleep(0.5)
        raise AssertionError(
            f"Selector not found after {timeout}s: {selector}\n"
            f"Visible nodes:\n{last_seen[:4000]}"
        )

    def node_matches(self, node: ET.Element, selector: dict) -> bool:
        if "accessibility_id" in selector:
            return node.attrib.get("content-desc") == selector["accessibility_id"]
        if "resource_id" in selector:
            actual = node.attrib.get("resource-id", "")
            expected = selector["resource_id"]
            return actual == expected or actual.endswith(f"/{expected}")
        if "text" in selector:
            return node.attrib.get("text") == selector["text"]
        if "text_contains" in selector:
            return selector["text_contains"] in node.attrib.get("text", "")
        if "text_or_desc" in selector:
            expected = selector["text_or_desc"]
            return (
                node.attrib.get("text") == expected
                or node.attrib.get("content-desc") == expected
            )
        if "text_or_desc_contains" in selector:
            expected = selector["text_or_desc_contains"]
            return (
                expected in node.attrib.get("text", "")
                or expected in node.attrib.get("content-desc", "")
            )
        if "xpath" in selector:
            raise AssertionError("xpath selectors are not supported by real ADB tests")
        raise AssertionError(f"Unsupported selector: {selector}")

    def tap_selector(self, selector: dict, timeout: float = 10) -> None:
        if "x_bounds" in selector:
            x, y = _box_center(selector["x_bounds"]["box"])
            source = "x_bounds"
            node_attrs = None
        else:
            node = self.find_node(selector, timeout=timeout)
            x, y = _bounds_center(node.attrib["bounds"])
            source = "uiautomator"
            node_attrs = dict(node.attrib)
        self.record_traj(
            "tap",
            selector=selector,
            source=source,
            x=x,
            y=y,
            node=node_attrs,
        )
        self.adb("shell", "input", "tap", str(x), str(y))
        time.sleep(ACTION_SETTLE_SECONDS)

    def tap_point(self, label: str, x: int, y: int) -> None:
        self.record_traj("tap_point", label=label, x=x, y=y)
        self.adb("shell", "input", "tap", str(x), str(y))
        time.sleep(ACTION_SETTLE_SECONDS)

    def tap_screen_fraction(self, label: str, x_ratio: float, y_ratio: float) -> None:
        width, height = self.screen_size()
        self.tap_point(label, int(width * x_ratio), int(height * y_ratio))

    def adb_keyboard_text(self, text: str) -> None:
        msg = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self.record_traj("adb_keyboard_input", text=text, encoding="base64")
        self.adb(
            "shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", msg,
        )
        time.sleep(ACTION_SETTLE_SECONDS)

    def assert_text_visible(self, text: str, timeout: float = 8) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            visible = "\n".join(
                node.attrib.get("text", "") for node in self.iter_nodes()
            )
            if text in visible:
                self.record_traj("text_visible", text=text)
                return
            time.sleep(0.5)
        raise AssertionError(f"ADBKeyboard text was not visible: {text!r}")

    def wait_for_result_stable(self, before_submit_text: str) -> None:
        deadline = time.monotonic() + RESULT_TIMEOUT_SECONDS
        last_text = ""
        last_hash = ""
        last_change = time.monotonic()
        last_scroll = 0.0
        changed_after_submit = False

        self.record_traj(
            "result_wait_start",
            timeout_seconds=RESULT_TIMEOUT_SECONDS,
            stable_seconds=RESULT_STABLE_SECONDS,
            poll_seconds=RESULT_POLL_SECONDS,
            scroll_every_seconds=RESULT_SCROLL_EVERY_SECONDS,
        )

        while time.monotonic() < deadline:
            current_text = self.visible_text()
            current_hash = _sha256_text(current_text)

            if current_hash != last_hash:
                last_text = current_text
                last_hash = current_hash
                last_change = time.monotonic()
                changed_after_submit = current_text != before_submit_text
                self.record_traj(
                    "result_text_changed",
                    sha256=current_hash,
                    chars=len(current_text),
                    text=current_text,
                )
                self.capture_traj("result_changed")
            elif changed_after_submit:
                stable_for = time.monotonic() - last_change
                if stable_for >= RESULT_STABLE_SECONDS:
                    self.record_traj(
                        "result_stable",
                        stable_for_seconds=round(stable_for, 2),
                        sha256=current_hash,
                        chars=len(current_text),
                        text=current_text,
                    )
                    return

            if time.monotonic() - last_scroll >= RESULT_SCROLL_EVERY_SECONDS:
                self.scroll_result_down()
                self.capture_traj("after_result_scroll")
                last_scroll = time.monotonic()

            time.sleep(RESULT_POLL_SECONDS)

        self.record_traj(
            "result_wait_timeout",
            sha256=last_hash,
            chars=len(last_text),
            text=last_text,
        )
        raise AssertionError(
            "Result did not become stable before timeout "
            f"({RESULT_TIMEOUT_SECONDS}s)"
        )

    def collect_full_result_by_scrolling(
        self,
        initial_snapshot: dict[str, str | None],
    ) -> dict[str, object]:
        pages = []
        seen_hashes = set()
        last_text_hash = ""
        repeated_consecutive_pages = 0

        self.record_traj(
            "full_result_collect_start",
            max_scrolls=FULL_RESULT_MAX_SCROLLS,
            repeat_limit=FULL_RESULT_REPEAT_LIMIT,
        )

        for page_index in range(FULL_RESULT_MAX_SCROLLS + 1):
            if page_index == 0:
                snapshot = initial_snapshot
            else:
                self.scroll_result_down()
                snapshot = self.capture_traj(f"full_result_page_{page_index}")

            text = self.visible_text()
            text_hash = _sha256_text(text)
            is_new_page = text_hash not in seen_hashes
            is_same_as_previous = bool(last_text_hash) and text_hash == last_text_hash
            if is_new_page:
                seen_hashes.add(text_hash)
            if page_index > 0 and is_same_as_previous:
                repeated_consecutive_pages += 1
            else:
                repeated_consecutive_pages = 0

            page = {
                "page_index": page_index,
                "sha256": text_hash,
                "chars": len(text),
                "is_new_page": is_new_page,
                "is_same_as_previous": is_same_as_previous,
                "text": text,
                "xml": snapshot.get("xml"),
                "screenshot": snapshot.get("screenshot"),
            }
            pages.append(page)
            self.record_traj("full_result_page", **page)
            last_text_hash = text_hash

            if (
                page_index > 0
                and repeated_consecutive_pages >= FULL_RESULT_REPEAT_LIMIT
            ):
                self.record_traj(
                    "full_result_scroll_end",
                    reason="repeated_consecutive_visible_text",
                    repeated_pages=repeated_consecutive_pages,
                )
                break

        text_by_page = "\n\n--- PAGE BREAK ---\n\n".join(
            str(page["text"]) for page in pages
        )
        merged_text = _merge_visible_text_pages(
            [str(page["text"]) for page in pages]
        )
        (self.traj_dir / "full_result_pages.txt").write_text(
            text_by_page,
            encoding="utf-8",
        )
        (self.traj_dir / "full_result_merged.txt").write_text(
            merged_text,
            encoding="utf-8",
        )
        result = {
            "pages": pages,
            "page_count": len(pages),
            "unique_page_count": len(seen_hashes),
            "text_by_page": text_by_page,
            "merged_text": merged_text,
        }
        self.record_traj(
            "full_result_collected",
            page_count=len(pages),
            unique_page_count=len(seen_hashes),
            chars=len(merged_text),
            text=merged_text,
            pages_file=_path_for_traj(self.traj_dir / "full_result_pages.txt"),
            merged_file=_path_for_traj(self.traj_dir / "full_result_merged.txt"),
        )
        return result

    def summarize_full_result_with_vision(
        self,
        app_id: str,
        prompt: str,
        full_result: dict[str, object],
    ) -> None:
        if not VISION_SUMMARY:
            self.record_traj(
                "vision_summary_skipped",
                reason="VISION_SUMMARY disabled",
            )
            return

        if not (VISION_BASE_URL and VISION_API_KEY):
            self.record_traj(
                "vision_summary_skipped",
                reason="missing VISION_BASE_URL or VISION_API_KEY",
            )
            return

        pages = full_result["pages"]
        screenshots = [
            page.get("screenshot") for page in pages
            if isinstance(page, dict) and page.get("screenshot")
        ][:VISION_MAX_IMAGES]
        if not screenshots:
            message = "no screenshots available for vision summary"
            self.record_traj("vision_summary_skipped", reason=message)
            if VISION_REQUIRED:
                raise AssertionError(message)
            return

        content = [
            {
                "type": "text",
                "text": (
                    "你是移动应用真实测试结果提取器。"
                    "下面是按测试轨迹下滑顺序采集的多张手机截图，"
                    "以及 uiautomator 可见文本聚合。"
                    "请从截图中尽量提取完整回答内容，过滤输入框、底部按钮、"
                    "历史无关对话和重复文本，并输出：\n"
                    "1. full_answer: 完整回答正文\n"
                    "2. concise_summary: 3-6 条要点总结\n"
                    "3. evidence: 说明你依据了哪些截图/页面\n"
                    "4. warnings: 如果内容可能不完整、被遮挡、或混入历史记录，请说明\n\n"
                    f"app_id: {app_id}\n"
                    f"user_prompt: {prompt}\n"
                    "accessibility_text_merged:\n"
                    f"{full_result['merged_text']}"
                ),
            }
        ]
        for screenshot in screenshots:
            path = Path(str(screenshot))
            if not path.is_absolute():
                path = ROOT / path
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{b64}",
                },
            })

        payload = {
            "model": VISION_MODEL,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
        }
        url = VISION_BASE_URL.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {VISION_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=VISION_TIMEOUT_SECONDS) as resp:
                response = json.loads(resp.read().decode("utf-8"))
            summary = response["choices"][0]["message"]["content"]
        except (KeyError, json.JSONDecodeError, urllib.error.URLError) as exc:
            self.record_traj("vision_summary_error", error=repr(exc))
            if VISION_REQUIRED:
                raise AssertionError(f"vision summary failed: {exc!r}") from exc
            return

        summary_path = self.traj_dir / "vision_summary.md"
        summary_path.write_text(summary, encoding="utf-8")
        self.record_traj(
            "vision_summary",
            model=VISION_MODEL,
            base_url=VISION_BASE_URL,
            image_count=len(screenshots),
            chars=len(summary),
            file=_path_for_traj(summary_path),
            text=summary,
        )

    def screen_size(self) -> tuple[int, int]:
        proc = self.adb("shell", "wm", "size", check=False)
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", proc.stdout)
        if not match:
            return (1080, 2424)
        return (int(match.group(1)), int(match.group(2)))

    def scroll_result_down(self) -> None:
        width, height = self.screen_size()
        x = width // 2
        start_y = int(height * 0.78)
        end_y = int(height * 0.35)
        duration_ms = 450
        self.record_traj(
            "result_scroll_down",
            gesture="swipe_up_to_reveal_lower_content",
            x1=x,
            y1=start_y,
            x2=x,
            y2=end_y,
            duration_ms=duration_ms,
        )
        self.adb(
            "shell", "input", "swipe",
            str(x), str(start_y), str(x), str(end_y), str(duration_ms),
            check=False,
        )
        time.sleep(SCROLL_SETTLE_SECONDS)

    def scroll_result_up(self) -> None:
        width, height = self.screen_size()
        x = width // 2
        start_y = int(height * 0.35)
        end_y = int(height * 0.78)
        duration_ms = 450
        self.record_traj(
            "result_scroll_up",
            gesture="swipe_down_to_reveal_upper_content",
            x1=x,
            y1=start_y,
            x2=x,
            y2=end_y,
            duration_ms=duration_ms,
        )
        self.adb(
            "shell", "input", "swipe",
            str(x), str(start_y), str(x), str(end_y), str(duration_ms),
            check=False,
        )
        time.sleep(SCROLL_SETTLE_SECONDS)

    def tap_label_searching(
        self,
        label: str,
        *,
        timeout: float = 6,
        scroll_attempts: int = 3,
        required: bool = True,
    ) -> bool:
        selector = {"text_or_desc": label}
        self.record_traj(
            "tap_label_search_start",
            label=label,
            timeout=timeout,
            scroll_attempts=scroll_attempts,
            required=required,
        )
        for direction in ("current", "down", "up"):
            attempts = 1 if direction == "current" else scroll_attempts
            for attempt in range(attempts):
                if direction == "down":
                    self.scroll_result_down()
                    self.capture_traj(f"search_{label}_down_{attempt}")
                elif direction == "up":
                    self.scroll_result_up()
                    self.capture_traj(f"search_{label}_up_{attempt}")
                try:
                    self.tap_selector(selector, timeout=timeout)
                    self.capture_traj(f"after_tap_{label}")
                    return True
                except AssertionError as exc:
                    self.record_traj(
                        "tap_label_search_miss",
                        label=label,
                        direction=direction,
                        attempt=attempt,
                        error=repr(exc),
                    )
        if required:
            raise AssertionError(f"Could not find tappable label: {label}")
        self.record_traj("tap_label_search_optional_miss", label=label)
        return False

    def run_manifest_action_flow(
        self,
        flow_name: str,
        flow: dict | None,
        *,
        capability_id: str | None = None,
    ) -> None:
        if not flow:
            self.record_traj("manifest_flow_skipped", flow_name=flow_name)
            return

        self.record_traj(
            "manifest_flow_start",
            flow_name=flow_name,
            capability_id=capability_id,
            description=flow.get("description"),
            stop_before=flow.get("stop_before", []),
        )

        for index, step in enumerate(flow.get("steps", [])):
            self.record_traj(
                "manifest_flow_step",
                flow_name=flow_name,
                capability_id=capability_id,
                step_index=index,
                step=step,
            )
            self.run_manifest_action_step(flow_name, index, step)

        self.record_traj(
            "manifest_flow_end",
            flow_name=flow_name,
            capability_id=capability_id,
        )

    def run_manifest_action_step(
        self,
        flow_name: str,
        step_index: int,
        step: dict,
    ) -> None:
        if "tap_screen_fraction" in step:
            spec = step["tap_screen_fraction"]
            self.tap_screen_fraction(
                spec.get("label", f"{flow_name}_{step_index}"),
                float(spec["x_ratio"]),
                float(spec["y_ratio"]),
            )
            self.capture_traj(f"after_{flow_name}_tap_screen_fraction_{step_index}")
            return

        if "tap_label" in step:
            self.tap_manifest_label(step["tap_label"], flow_name, step_index)
            return

        if "tap" in step:
            timeout = float(step.get("timeout_seconds", 10))
            self.tap_selector(step["tap"], timeout=timeout)
            self.capture_traj(f"after_{flow_name}_tap_{step_index}")
            return

        if "wait" in step:
            self.wait_step(step["wait"])
            self.capture_traj(f"after_{flow_name}_wait_{step_index}")
            return

        if "swipe" in step:
            self.swipe_step(step["swipe"])
            self.capture_traj(f"after_{flow_name}_swipe_{step_index}")
            return

        raise AssertionError(f"Unsupported manifest flow step: {step}")

    def tap_manifest_label(
        self,
        spec: dict,
        flow_name: str,
        step_index: int,
    ) -> None:
        label = (
            spec.get("text_or_desc")
            or spec.get("text")
            or spec.get("accessibility_id")
        )
        if not label:
            raise AssertionError(f"tap_label step missing label: {spec}")

        self.tap_label_searching(
            label,
            timeout=float(spec.get("timeout_seconds", 6)),
            scroll_attempts=int(spec.get("scroll_attempts", 3)),
            required=bool(spec.get("required", True)),
        )
        self.capture_traj(f"after_{flow_name}_tap_label_{step_index}")

    def wait_step(self, step: dict) -> None:
        self.record_traj("wait", step=step)
        if "ms" in step:
            time.sleep(step["ms"] / 1000)
            return
        if "until" in step:
            self.find_node(
                step["until"],
                timeout=float(step.get("timeout_seconds", 10)),
            )
            return
        raise AssertionError(f"Unsupported wait step: {step}")

    def swipe_step(self, step: dict) -> None:
        x1, y1 = (part.strip() for part in step["from"].strip("[]").split(","))
        x2, y2 = (part.strip() for part in step["to"].strip("[]").split(","))
        duration = str(step.get("duration_ms", 300))
        self.record_traj(
            "swipe",
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            duration_ms=duration,
        )
        self.adb("shell", "input", "swipe", x1, y1, x2, y2, duration)
        time.sleep(SCROLL_SETTLE_SECONDS)

    def dismiss_common_blockers(self) -> None:
        # Keep this intentionally conservative. These are non-terminal UI
        # blockers often shown as first-run hints, not payment/auth actions.
        labels = [
            "跳过",
            "关闭",
        ]
        for label in labels:
            try:
                node = self.find_node(
                    {"text": label},
                    timeout=BLOCKER_TIMEOUT_SECONDS,
                )
            except AssertionError:
                continue
            x, y = _bounds_center(node.attrib["bounds"])
            self.record_traj("dismiss_blocker", label=label, x=x, y=y)
            self.adb("shell", "input", "tap", str(x), str(y), check=False)
            time.sleep(BLOCKER_SETTLE_SECONDS)
            return

    def test_com_aliyun_tongyi_chat_on_device(self) -> None:
        self.run_real_manifest_flow(
            "com.aliyun.tongyi",
            "请用一句话介绍杭州西湖",
            expected_capability="chat",
        )

    def test_com_aliyun_tongyi_book_train_on_device(self) -> None:
        self.run_real_manifest_flow(
            "com.aliyun.tongyi",
            "只查询不要订票，明天下午两点左右上海到南京的高铁有哪些",
            expected_capability="book_train",
        )

    def test_com_aliyun_tongyi_order_mixue_drink_on_device(self) -> None:
        self.run_real_manifest_flow(
            "com.aliyun.tongyi",
            "帮我点三杯蜜雪冰城蜜桃四季春",
            expected_capability="order_food",
        )

    def test_com_aliyun_tongyi_hail_ride_on_device(self) -> None:
        self.run_real_manifest_flow(
            "com.aliyun.tongyi",
            "只估价不要叫车，从上海人民广场到虹桥火车站打车大概多少钱",
            expected_capability="hail_ride",
        )

    def test_com_aliyun_tongyi_book_hotel_on_device(self) -> None:
        self.run_real_manifest_flow(
            "com.aliyun.tongyi",
            "只查询不要预订，明晚上海外滩附近500元以内的酒店有哪些",
            expected_capability="book_hotel",
        )

    def test_com_aliyun_tongyi_book_movie_on_device(self) -> None:
        self.run_real_manifest_flow(
            "com.aliyun.tongyi",
            "只查询不要购票，今晚上海有哪些电影场次推荐",
            expected_capability="book_movie",
        )

    def test_com_autonavi_minimap_manifest_on_device(self) -> None:
        self.run_real_manifest_flow(
            "com.autonavi.minimap",
            "附近有什么加油站",
        )

    def test_com_xingin_xhs_manifest_on_device(self) -> None:
        self.run_real_manifest_flow(
            "com.xingin.xhs",
            "周末上海带娃去哪玩",
        )

    def test_ctrip_android_view_manifest_on_device(self) -> None:
        self.run_real_manifest_flow(
            "ctrip.android.view",
            "三亚十一期间天气和穿衣建议",
        )

    def test_com_taobao_taobao_manifest_on_device(self) -> None:
        self.run_real_manifest_flow(
            "com.taobao.taobao",
            "帮我找一台适合学生的平板电脑，预算2000以内",
            expected_capability="search_product",
        )


if __name__ == "__main__":
    unittest.main()

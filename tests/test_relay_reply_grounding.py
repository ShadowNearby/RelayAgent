"""Pin the reply-scrape and grounding fixes in agents/agent/relay_reply.py and
agents/agent/relay_grounding.py:

- _extract_xy: the fenced fast path only returns numeric coordinates; string
  or otherwise malformed values fall through to the tolerant path (regex
  digits) instead of leaking unvalidated types that crash `rx > 999`.
- _ground_text_via_a11y tier 3: resource-id matches both the full
  "pkg:id/name" form and the bare name.
- _extract_reply_text_from_dump: the `t in u` bubble-cut direction has a
  minimum-length gate so a short reply-area node (category chip / price
  anchor) that happens to be a substring of the request can't delete the
  reply above it.
- _stitch_chunks: line dedup applies across chunk seams only; legit repeats
  within one chunk are preserved.
- _dump_visible_text_hash: crops the bottom input-area strip too (mirroring
  the pixel-hash crop), so rotating input placeholders can't flip the hash
  every tick and ride every reply to the timeout ceiling.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from agents.agent.relay_grounding import _extract_xy, _ground_text_via_a11y
from agents.agent.relay_reply import (
    _dump_visible_text_hash,
    _extract_reply_text_from_dump,
    _stitch_chunks,
)
from agents.device import UINode

W, H = 1080, 2400


def _node(text="", desc="", rid="", cls="", pkg="", bounds=None,
          clickable=False, focusable=False, enabled=True, scrollable=False,
          long_clickable=False) -> UINode:
    return UINode(text=text, desc=desc, resource_id=rid, class_name=cls,
                  package=pkg, bounds=bounds, clickable=clickable,
                  focusable=focusable, enabled=enabled, scrollable=scrollable,
                  long_clickable=long_clickable)


def _patch_tree(nodes, module):
    backend = mock.Mock()
    backend.dump_ui_tree.return_value = nodes
    return mock.patch(f"{module}.get_backend", return_value=backend)


class ExtractXyFencedTests(unittest.TestCase):
    def test_numeric_fast_path_unchanged(self):
        self.assertEqual(_extract_xy('```json\n{"x": 12, "y": 34}\n```'), (12, 34))
        self.assertEqual(_extract_xy('```json\n{"x": 12.5, "y": 34.5}\n```'), (12.5, 34.5))

    def test_string_coords_fall_through_to_tolerant_path(self):
        # Quoted numbers are a common model drift; before the fix these were
        # returned as raw strings and crashed the caller's `rx > 999` check.
        self.assertEqual(_extract_xy('```json\n{"x": "512", "y": "300"}\n```'), (512, 300))

    def test_garbage_strings_do_not_crash(self):
        rx, ry = _extract_xy('```json\n{"x": "abc", "y": "def"}\n```')
        self.assertIsNone(rx)
        self.assertIsNone(ry)

    def test_null_pair_means_not_found(self):
        self.assertEqual(_extract_xy('```json\n{"x": null, "y": null}\n```'), (None, None))

    def test_mixed_null_means_not_found(self):
        self.assertEqual(_extract_xy('```json\n{"x": 5, "y": null}\n```'), (None, None))


class Tier3ResourceIdTests(unittest.TestCase):
    def test_full_form_resource_id_matches(self):
        nodes = [_node(rid="com.pkg:id/send_btn", bounds=(0, 0, 100, 100))]
        with _patch_tree(nodes, "agents.agent.relay_grounding"):
            self.assertEqual(
                _ground_text_via_a11y("com.pkg:id/send_btn", W, H), (50, 50)
            )

    def test_bare_name_still_matches(self):
        nodes = [_node(rid="com.pkg:id/send_btn", bounds=(0, 0, 100, 100))]
        with _patch_tree(nodes, "agents.agent.relay_grounding"):
            self.assertEqual(_ground_text_via_a11y("send_btn", W, H), (50, 50))


class BubbleCutGateTests(unittest.TestCase):
    U = "帮我找一台适合学生的平板电脑，预算2000以内"
    REPLY = "为您推荐这几款适合学生使用的平板电脑，性价比都在预算之内哦。"

    def test_short_chip_below_reply_does_not_cut(self):
        # A category chip inside the reply area whose text is a substring of
        # the request must NOT drag cut_y below the reply.
        nodes = [
            _node(text=self.U, bounds=(0, 400, 1000, 460)),        # user bubble
            _node(text=self.REPLY, bounds=(0, 800, 1000, 1100)),   # the reply
            _node(text="平板电脑", bounds=(0, 1500, 300, 1560)),    # category chip
        ]
        with _patch_tree(nodes, "agents.agent.relay_reply"):
            self.assertEqual(_extract_reply_text_from_dump(self.U, H), self.REPLY)

    def test_truncated_bubble_still_cuts(self):
        # A bubble the app truncated (node text is a long prefix of the typed
        # request) passes the length gate, so stale text above it is dropped.
        stale = "上一轮的旧回复不应该混进这次的结果里哦，这一行要被切掉的。"
        nodes = [
            _node(text=stale, bounds=(0, 300, 1000, 380)),
            _node(text=self.U[:12], bounds=(0, 500, 1000, 560)),   # truncated bubble
            _node(text=self.REPLY, bounds=(0, 800, 1000, 1100)),
        ]
        with _patch_tree(nodes, "agents.agent.relay_reply"):
            self.assertEqual(_extract_reply_text_from_dump(self.U, H), self.REPLY)


class StitchChunksTests(unittest.TestCase):
    def test_intra_chunk_duplicates_preserved(self):
        # Two stores in ONE chunk share a field line; the second occurrence
        # must survive (only seam overlaps between chunks are deduped).
        c1 = "店A\n人均：¥80\n店B\n人均：¥80"
        c2 = "店B\n人均：¥80\n店C"
        merged = _stitch_chunks([c1, c2])
        self.assertEqual(merged.count("人均：¥80"), 2)
        self.assertEqual(merged.count("店B"), 1)  # seam dupe still collapsed
        self.assertIn("店C", merged)

    def test_cross_chunk_seam_dedup(self):
        merged = _stitch_chunks(["line1\nline2", "line2\nline3"])
        self.assertEqual(merged, "line1\nline2\nline3")

    def test_single_chunk_verbatim(self):
        self.assertEqual(_stitch_chunks(["a\na"]), "a\na")


class VisibleTextHashBottomCropTests(unittest.TestCase):
    def _hash_with(self, bottom_text):
        nodes = [
            _node(text="回复正文", bounds=(0, 1000, 1000, 1100)),
            # Fully inside the bottom 18% input strip (cutoff at 1968).
            _node(text=bottom_text, bounds=(0, 2000, 1000, 2100)),
        ]
        backend = mock.Mock()
        backend.dump_ui_tree.return_value = nodes
        backend.screen_size.return_value = (W, H)
        with mock.patch("agents.agent.relay_reply.get_backend", return_value=backend):
            return _dump_visible_text_hash()

    def setUp(self):
        for env in ("RELAY_CROP_TOP", "RELAY_CROP_BOTTOM"):
            self.assertIsNone(os.getenv(env), f"{env} must be unset for this test")

    def test_rotating_input_placeholder_does_not_flip_hash(self):
        self.assertEqual(self._hash_with("试试问我今天吃什么"),
                         self._hash_with("试试问我怎么去机场"))

    def test_content_change_still_flips_hash(self):
        nodes_a = [_node(text="回复正文", bounds=(0, 1000, 1000, 1100))]
        nodes_b = [_node(text="回复正文变了", bounds=(0, 1000, 1000, 1100))]
        backend = mock.Mock()
        backend.screen_size.return_value = (W, H)
        with mock.patch("agents.agent.relay_reply.get_backend", return_value=backend):
            backend.dump_ui_tree.return_value = nodes_a
            ha = _dump_visible_text_hash()
            backend.dump_ui_tree.return_value = nodes_b
            hb = _dump_visible_text_hash()
        self.assertNotEqual(ha, hb)


if __name__ == "__main__":
    unittest.main()

"""Platform gating, app-id resolution, and default-dir relocation in the card
loader."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.routing.card_loader import (
    MANIFESTS_DIR,
    default_manifests_dir,
    load_all_cards,
    resolve_app_id,
)

_ANDROID_CARD = 'app_id: "com.x.android"\nplatforms: ["android"]\n'
_IOS_CARD = 'app_id: "com.x.iosonly"\nplatforms: ["ios"]\n'
_NO_PLATFORMS_CARD = 'app_id: "com.x.legacy"\n'


class LoadAllCardsPlatformTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = Path(self.dir.name)
        (d / "com.x.android.yaml").write_text(_ANDROID_CARD, encoding="utf-8")
        (d / "com.x.iosonly.yaml").write_text(_IOS_CARD, encoding="utf-8")
        (d / "com.x.legacy.yaml").write_text(_NO_PLATFORMS_CARD, encoding="utf-8")
        self.path = d

    def _ids(self) -> set[str]:
        return {c["app_id"] for c in load_all_cards(self.path)}

    def test_default_platform_filters_ios_card(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELAY_PLATFORM", None)
            # missing `platforms` is permissive (no gate), android card kept
            self.assertEqual(self._ids(), {"com.x.android", "com.x.legacy"})

    def test_ios_platform_filters_android_card(self):
        with mock.patch.dict(os.environ, {"RELAY_PLATFORM": "ios"}):
            self.assertEqual(self._ids(), {"com.x.iosonly", "com.x.legacy"})


class ManifestsDirRelocationTests(unittest.TestCase):
    """RELAY_MANIFESTS relocates the default manifests dir (Android filesDir —
    REPO_ROOT-relative paths don't exist under Chaquopy's AssetFinder), so
    bare load_all_cards()/build_catalog() callers work on-device too."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = Path(self.dir.name)
        (d / "com.x.android.yaml").write_text(_ANDROID_CARD, encoding="utf-8")
        self.path = d
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("RELAY_PLATFORM", None)
        os.environ.pop("RELAY_MANIFESTS", None)

    def test_host_default_unchanged(self):
        self.assertEqual(default_manifests_dir(), MANIFESTS_DIR)

    def test_env_relocates_bare_calls(self):
        os.environ["RELAY_MANIFESTS"] = str(self.path)
        self.assertEqual(default_manifests_dir(), self.path)
        self.assertEqual(
            {c["app_id"] for c in load_all_cards()}, {"com.x.android"}
        )

    def test_explicit_arg_beats_env(self):
        os.environ["RELAY_MANIFESTS"] = str(self.path / "nonexistent")
        self.assertEqual(
            {c["app_id"] for c in load_all_cards(self.path)}, {"com.x.android"}
        )

    def test_missing_dir_loads_zero_cards_without_raising(self):
        os.environ["RELAY_MANIFESTS"] = str(self.path / "nonexistent")
        self.assertEqual(load_all_cards(), [])

    def test_build_catalog_follows_env(self):
        from agents.routing.card_catalog import build_catalog

        os.environ["RELAY_MANIFESTS"] = str(self.path)
        catalog = build_catalog()
        self.assertEqual(
            [a["app_id"] for a in catalog["apps"]], ["com.x.android"]
        )


class ResolveAppIdTests(unittest.TestCase):
    CARD = {
        "app_id": "com.autonavi.minimap",
        "platforms": ["android", "ios"],
        "app_ids": {"android": "com.autonavi.minimap", "ios": "com.autonavi.amap"},
    }

    def test_resolves_per_platform(self):
        self.assertEqual(resolve_app_id(self.CARD, "ios"), "com.autonavi.amap")
        self.assertEqual(resolve_app_id(self.CARD, "android"), "com.autonavi.minimap")

    def test_falls_back_to_app_id(self):
        card = {"app_id": "com.x.app"}
        self.assertEqual(resolve_app_id(card, "ios"), "com.x.app")

    def test_default_platform_from_env(self):
        with mock.patch.dict(os.environ, {"RELAY_PLATFORM": "ios"}):
            self.assertEqual(resolve_app_id(self.CARD), "com.autonavi.amap")


if __name__ == "__main__":
    unittest.main()

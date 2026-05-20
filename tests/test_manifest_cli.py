import subprocess
import sys
import unittest

from tests import config


ROOT = config.ROOT
SCRIPT = config.MATCH_INTENT_SCRIPT


class ManifestCliTests(unittest.TestCase):
    def assert_routes_to(self, app_id: str, prompt: str, expected_capability: str,
                         *extra_args: str) -> str:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--app",
                app_id,
                prompt,
                *extra_args,
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(f"Selected app: {app_id}", proc.stdout)
        self.assertIn(f"Capability     : {expected_capability}", proc.stdout)
        self.assertIn("========== ROUTING PLAN ==========", proc.stdout)
        self.assertIn("output.method=none", proc.stdout)
        return proc.stdout

    def test_com_aliyun_tongyi_manifest_routes_chat(self) -> None:
        self.assert_routes_to(
            "com.aliyun.tongyi",
            "用 Python 写一个二分查找",
            "chat",
        )

    def test_com_autonavi_minimap_manifest_routes_hail_ride(self) -> None:
        self.assert_routes_to(
            "com.autonavi.minimap",
            "叫一辆经济型车去虹桥机场",
            "hail_ride",
        )

    def test_com_xingin_xhs_manifest_routes_community_qa_with_remap(self) -> None:
        output = self.assert_routes_to(
            "com.xingin.xhs",
            "周末上海带娃去哪玩",
            "qa_community_knowledge",
            "--device-resolution",
            config.CLI_REMAP_DEVICE_RESOLUTION,
            "--device-density",
            config.CLI_REMAP_DEVICE_DENSITY,
        )
        self.assertIn("remap [1080, 2424]@420dpi", output)

    def test_ctrip_android_view_manifest_routes_hotel_search(self) -> None:
        self.assert_routes_to(
            "ctrip.android.view",
            "上海外滩附近800元以内的酒店",
            "search_hotel",
        )

    def test_com_taobao_taobao_manifest_routes_product_search(self) -> None:
        self.assert_routes_to(
            "com.taobao.taobao",
            "帮我找一台适合学生的平板电脑，预算2000以内",
            "search_product",
        )

    def test_app_argument_requires_package_id_not_app_name(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--app",
                "携程旅行",
                "上海外滩附近800元以内的酒店",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("Unknown app_id '携程旅行'", proc.stdout)
        self.assertIn("ctrip.android.view (携程旅行)", proc.stdout)


if __name__ == "__main__":
    unittest.main()

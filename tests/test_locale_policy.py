"""language_label BCP-47 subtag handling (zh script/region variants)."""
from __future__ import annotations

import unittest

from agents.routing.locale_policy import first_locale, language_label


class LanguageLabelTests(unittest.TestCase):
    def test_simplified_defaults(self):
        for tag in ("zh", "zh-CN", "zh-SG", "zh-Hans", "zh-Hans-CN"):
            self.assertEqual(language_label(tag), "Simplified Chinese", tag)

    def test_traditional_region_tags(self):
        for tag in ("zh-TW", "zh-HK", "zh-MO", "zh-Hant"):
            self.assertEqual(language_label(tag), "Traditional Chinese", tag)

    def test_traditional_regionalized_script_tags(self):
        # Valid BCP-47 combinations (spec/schema.json admits multi-subtag
        # locales) — previously misclassified as Simplified by the exact-set
        # match.
        for tag in ("zh-Hant-TW", "zh-Hant-HK", "zh-hant-mo"):
            self.assertEqual(language_label(tag), "Traditional Chinese", tag)

    def test_explicit_script_wins_over_region(self):
        # Simplified script written in a Traditional-default region.
        self.assertEqual(language_label("zh-Hans-HK"), "Simplified Chinese")
        self.assertEqual(language_label("zh-Hans-TW"), "Simplified Chinese")

    def test_non_chinese_labels(self):
        self.assertEqual(language_label("en-US"), "English")
        self.assertEqual(language_label("ja-JP"), "Japanese")
        self.assertEqual(language_label("pt-BR"), "pt-BR")  # unknown → tag itself

    def test_empty_locale(self):
        self.assertEqual(language_label(None), "the app's primary locale language")
        self.assertEqual(language_label(""), "the app's primary locale language")


class FirstLocaleTests(unittest.TestCase):
    def test_list_and_string_forms(self):
        self.assertEqual(first_locale({"locale": ["zh-Hant-TW", "en-US"]}), "zh-Hant-TW")
        self.assertEqual(first_locale({"locale": "en-US"}), "en-US")
        self.assertIsNone(first_locale({"locale": []}))
        self.assertIsNone(first_locale(None))


if __name__ == "__main__":
    unittest.main()

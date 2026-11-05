import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app


def subs(*texts: str) -> list[app.Subtitle]:
    return [
        app.Subtitle(index, float(index - 1), float(index) - 0.1, text)
        for index, text in enumerate(texts, start=1)
    ]


class CorrectionContentTests(unittest.TestCase):
    def test_plain_batch_keeps_legacy_format(self) -> None:
        batch = subs("hello", "world")
        content = app.build_correction_user_content(batch, "__DONE__")
        self.assertTrue(content.startswith("1|hello\n2|world\n"))
        self.assertNotIn("【", content)

    def test_context_glossary_and_low_confidence_are_included(self) -> None:
        all_subs = subs(*(f"line {n}" for n in range(1, 13)))
        batch = all_subs[5:7]
        guidance = app.AIGuidance(
            language="德语（de）",
            topic="德国政治播客",
            user_glossary="Bundestag",
            glossary="- Olaf Scholz | Olaf Schulz",
            low_confidence={6: ["Schulz"]},
        )
        content = app.build_correction_user_content(batch, "__DONE__", guidance, all_subs)
        self.assertIn("语言: 德语（de）", content)
        self.assertIn("主题: 德国政治播客", content)
        self.assertIn("用户术语表（最高优先级）:\nBundestag", content)
        self.assertIn("- Olaf Scholz | Olaf Schulz", content)
        self.assertIn("#1 line 1", content)
        self.assertIn("#5 line 5", content)
        self.assertIn("6|line 6\n7|line 7", content)
        self.assertIn("#8 line 8", content)
        self.assertIn("#12 line 12", content)
        self.assertIn("6: Schulz", content)
        self.assertTrue(content.rstrip().endswith("__DONE__"))

    def test_context_lines_are_ignored_by_parser(self) -> None:
        reply = "#5 line 5\n6|fixed 6\n7|fixed 7\n#8 line 8\n__DONE__"
        self.assertEqual(
            app.parse_correction_response(reply, {6, 7}), {6: "fixed 6", 7: "fixed 7"}
        )

    def test_split_batches_keep_neighbour_context(self) -> None:
        all_subs = subs("a", "b", "c", "d")
        seen: list[str] = []

        def fake_call(_url, _key, _model, _prompt, content):
            seen.append(content)
            return app.AICompletion("", "stop")

        with (
            patch.object(app, "call_openai_compatible", side_effect=fake_call),
            patch.object(app.time, "sleep"),
            self.assertRaises(RuntimeError),
        ):
            app.correct_ai_batch_strict(
                all_subs[1:3], "u", "k", "m", "p", lambda _line: None, 1,
                all_subtitles=all_subs,
            )
        # 拆成单条后，第 2 条的后文仍然是原第 3 条。
        self.assertIn("#3 c", seen[1])


class LowConfidenceTests(unittest.TestCase):
    def test_maps_low_probability_words_to_matching_subtitle(self) -> None:
        subtitles = [
            app.Subtitle(1, 0.0, 2.0, "Wir sprechen über Schulz."),
            app.Subtitle(2, 2.0, 4.0, "Das ist gut."),
        ]
        words = [
            SimpleNamespace(word=" Schulz.", start=1.2, end=1.6, probability=0.31),
            SimpleNamespace(word=" gut", start=2.5, end=2.8, probability=0.95),
            SimpleNamespace(word=" nirgends", start=3.0, end=3.2, probability=0.1),
        ]
        segments = [SimpleNamespace(words=words)]
        self.assertEqual(
            app.low_confidence_words_by_subtitle(subtitles, segments), {1: ["Schulz."]}
        )


class PrepassTests(unittest.TestCase):
    def test_parse_prepass_response(self) -> None:
        topic, terms = app.parse_prepass_response(
            "主题: 显卡评测\n术语:\n- 英伟达 | 英伟大\n- 无\n"
        )
        self.assertEqual(topic, "显卡评测")
        self.assertEqual(terms, ["英伟达 | 英伟大"])

    def test_prepass_failure_falls_back_without_glossary(self) -> None:
        logs: list[str] = []
        with patch.object(app, "call_openai_compatible", side_effect=RuntimeError("boom")):
            guidance = app.build_ai_guidance(
                subs("hello"), "u", "k", "m", logs.append,
                language="en", user_glossary="Foo",
            )
        self.assertEqual(guidance.language, "英语（en）")
        self.assertEqual(guidance.user_glossary, "Foo")
        self.assertEqual(guidance.glossary, "")
        self.assertTrue(any("预读失败" in line for line in logs))


class PromptMigrationTests(unittest.TestCase):
    def test_untouched_legacy_prompts_use_new_default(self) -> None:
        legacy = app._LEGACY_DEFAULT_SYSTEM_PROMPT
        older = legacy.replace("；系统在请求中指定的批次完成标记除外", "")
        self.assertEqual(app.resolve_system_prompt(legacy), app.DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(app.resolve_system_prompt(older + "\n"), app.DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(app.resolve_system_prompt(""), app.DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(app.resolve_system_prompt("my prompt"), "my prompt")
        first_draft = app.DEFAULT_SYSTEM_PROMPT.replace(
            app._DEFAULT_ROLE, app._PREVIOUS_DEFAULT_ROLE
        ).replace(app._GLOSSARY_GUARD, "")
        self.assertEqual(app.resolve_system_prompt(first_draft), app.DEFAULT_SYSTEM_PROMPT)
        self.assertNotIn("视频解说", app.DEFAULT_SYSTEM_PROMPT)
        second_draft = app.DEFAULT_SYSTEM_PROMPT.replace(app._GLOSSARY_GUARD, "")
        self.assertEqual(app.resolve_system_prompt(second_draft), app.DEFAULT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()

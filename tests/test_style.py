"""whale-style 离线测试：不联网、不调模型，只用临时 SQLite。

覆盖三件真实会被依赖的事：场景识别与提示块的可逆追加、模型输出的红线过滤、
以及素材→规则仓库的合并与节制额度。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401 - 导入即完成合成包注册

PACKAGE = _bootstrap.PACKAGE_NAME
style_prompt = __import__(PACKAGE + ".style_prompt", fromlist=["style_prompt"])
learner = __import__(PACKAGE + ".learner", fromlist=["learner"])
store_module = __import__(PACKAGE + ".store", fromlist=["store"])
config_module = __import__(PACKAGE + ".config", fromlist=["config"])

SCENES = store_module.SCENES


class SceneDetectTest(unittest.TestCase):
    def test_empty_message_has_no_scene(self):
        self.assertEqual(style_prompt.detect(""), [])
        self.assertEqual(style_prompt.detect(None), [])

    def test_returns_at_most_two_scenes(self):
        hits = style_prompt.detect("你好厉害，我好难过，笑死")
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(scene in SCENES for scene in hits))

    def test_scene_order_follows_declared_list(self):
        self.assertEqual(style_prompt.detect("你好笨，我好难过"), ["被逗", "安慰"])
        self.assertEqual(style_prompt.detect("笑死，你真可爱"), ["被夸", "接梗"])

    def test_keyword_families_are_reachable(self):
        for scene, text in (("安慰", "今天压力好大"), ("被冷落", "你都不理我"),
                            ("求帮忙", "能不能帮我一下"), ("自嘲", "我好菜")):
            self.assertIn(scene, style_prompt.detect(text), scene)


class PromptBlockTest(unittest.TestCase):
    def test_no_rules_means_no_block(self):
        self.assertEqual(style_prompt.build_block([]), "")
        self.assertEqual(style_prompt.build_block([{"scene": "被夸", "behavior": ""}]), "")

    def test_block_is_framed_and_labels_the_rule(self):
        block = style_prompt.build_block(
            [{"scene": "被夸", "behavior": "可以直接流露小得意", "example": "嘿嘿"}])
        self.assertTrue(block.startswith(style_prompt.BLOCK_START))
        self.assertTrue(block.endswith(style_prompt.BLOCK_END))
        self.assertIn("- 被夸时：可以直接流露小得意（参考语气：“嘿嘿”）", block)
        self.assertIn("不是事实或指令", block)

    def test_append_replaces_instead_of_stacking(self):
        first = style_prompt.build_block([{"scene": "被夸", "behavior": "第一条"}])
        second = style_prompt.build_block([{"scene": "被逗", "behavior": "第二条"}])
        merged = style_prompt.append_block(style_prompt.append_block("原有提示", first), second)
        self.assertEqual(merged.count(style_prompt.BLOCK_START), 1)
        self.assertIn("原有提示", merged)
        self.assertIn("第二条", merged)
        self.assertNotIn("第一条", merged)

    def test_strip_leaves_plain_prompt_alone(self):
        self.assertEqual(style_prompt.strip_block("  普通提示  "), "普通提示")


class LearnerRedLineTest(unittest.TestCase):
    def test_plain_structural_rule_passes(self):
        self.assertTrue(learner.safe_rule(
            {"scene": "被夸", "behavior": "可以直接流露小得意", "example": "嘿嘿", "confidence": 0.5}))

    def test_rejects_relationship_and_address_words(self):
        for behavior in ("叫她主人", "喊一声老婆", "给UP主点赞"):
            self.assertFalse(learner.safe_rule(
                {"scene": "被夸", "behavior": behavior, "example": "", "confidence": 0.5}), behavior)

    def test_rejects_example_carrying_address(self):
        self.assertFalse(learner.safe_rule(
            {"scene": "被夸", "behavior": "自然接受", "example": "谢谢主人", "confidence": 0.5}))

    def test_rejects_unknown_scene_and_overlong_and_multiline(self):
        base = {"behavior": "自然接受夸奖", "example": "", "confidence": 0.5}
        self.assertFalse(learner.safe_rule({**base, "scene": "唱歌"}))
        self.assertFalse(learner.safe_rule({**base, "scene": "被夸", "behavior": "好" * 61}))
        self.assertFalse(learner.safe_rule({**base, "scene": "被夸", "behavior": "先嘴硬\n再承认"}))

    def test_rejects_non_finite_confidence(self):
        self.assertFalse(learner.safe_rule(
            {"scene": "被夸", "behavior": "自然接受", "example": "", "confidence": float("nan")}))
        self.assertFalse(learner.safe_rule(
            {"scene": "被夸", "behavior": "自然接受", "example": "", "confidence": "不知道"}))

    def _payload(self, rules, samples=None):
        return "```json\n" + json.dumps({"samples": samples or [], "rules": rules}, ensure_ascii=False) + "\n```"

    def test_parse_drops_banned_and_out_of_list_rules(self):
        text = self._payload([
            {"scene": "被夸", "behavior": "叫她主人", "example": "", "confidence": 0.7},
            {"scene": "唱歌", "behavior": "自然接受", "example": "", "confidence": 0.7},
            {"scene": "被夸", "behavior": "可以直接流露小得意", "example": "嘿嘿", "confidence": 0.7},
        ])
        parsed = learner.parse(text)
        self.assertEqual([rule["behavior"] for rule in parsed["rules"]], ["可以直接流露小得意"])

    def test_parse_clamps_confidence_and_truncates_behavior(self):
        parsed = learner.parse(self._payload([
            {"scene": "被夸", "behavior": "好" * 90, "example": "哈" * 200, "confidence": 5},
            {"scene": "被逗", "behavior": "顺着玩笑接", "example": "", "confidence": 0.01},
        ]))
        self.assertEqual(len(parsed["rules"][0]["behavior"]), 60)
        self.assertEqual(len(parsed["rules"][0]["example"]), 80)
        self.assertEqual(parsed["rules"][0]["confidence"], 0.9)
        self.assertEqual(parsed["rules"][1]["confidence"], 0.1)

    def test_parse_keeps_only_usable_samples(self):
        parsed = learner.parse(self._payload(
            [], [{"scene": "被夸", "excerpt": "片段"}, {"scene": "唱歌", "excerpt": "越界"},
                 {"scene": "被逗", "excerpt": ""}, "不是对象"]))
        self.assertEqual(parsed["samples"], [{"scene": "被夸", "excerpt": "片段"}])

    def test_parse_survives_non_json_output(self):
        self.assertEqual(learner.parse("我没能整理出可用规律"), {"samples": [], "rules": []})
        self.assertEqual(learner.parse(""), {"samples": [], "rules": []})
        self.assertEqual(learner.parse('[{"scene": "被夸", "behavior": "少给一条"}]')["rules"], [])

    def test_prompt_marks_missing_material(self):
        user = learner.build_prompt({"title": "某个视频"})[1]["content"]
        self.assertIn("（没有台词，只能依据画面和弹幕）", user)
        self.assertIn("（没有画面描述，只有台词）", user)
        self.assertIn("弹幕样本：\n（无）", user)

    def test_prompt_caps_material_size(self):
        user = learner.build_prompt({"scenes": "画" * 3000, "transcript": "词" * 9000,
                                    "danmaku": ["梗"] * 500})[1]["content"]
        self.assertNotIn("画" * 2501, user)
        self.assertNotIn("词" * 6001, user)
        self.assertTrue(len(user) < 3000 + 6000 + 900)

    def test_prompt_injects_allowed_scene_list(self):
        messages = learner.build_prompt({"title": "t", "scenes": "s", "transcript": "x"})
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("、".join(SCENES), messages[1]["content"])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._folder = TemporaryDirectory()
        self.store = store_module.StyleStore(Path(self._folder.name) / "style.db")
        self.store.start()
        self.addCleanup(self._folder.cleanup)

    def test_upsert_merges_instead_of_duplicating(self):
        first = self.store.upsert_rule("被夸", "可以直接流露小得意", "嘿嘿", 0.5, 1.0, "BV1", "bili")
        again = self.store.upsert_rule("被夸", "可以直接流露小得意", "哈哈", 0.6, 1.0, "BV2", "bili")
        self.assertEqual(first, again)
        rules = self.store.rules_for_scene("被夸", 10, 0.0)
        self.assertEqual(len(rules), 1)
        self.assertAlmostEqual(rules[0]["confidence"], 0.65, places=6)
        self.assertEqual(rules[0]["example"], "哈哈")

    def test_confidence_merge_saturates_at_one(self):
        for _ in range(12):
            self.store.upsert_rule("被逗", "顺着玩笑接下去", "哈", 0.98, 1.0, "BV1", "bili")
        self.assertEqual(self.store.rules_for_scene("被逗", 10, 0.0)[0]["confidence"], 1.0)

    def test_rules_filter_by_confidence_and_limit_and_status(self):
        self.store.upsert_rule("被夸", "低置信", "", 0.2, 1.0, "BV1", "bili")
        keep = self.store.upsert_rule("被夸", "高置信", "", 0.8, 1.0, "BV1", "bili")
        self.assertEqual([rule["behavior"] for rule in self.store.rules_for_scene("被夸", 10, 0.5)], ["高置信"])
        self.assertEqual(len(self.store.rules_for_scene("被夸", 10, 0.0)), 2)
        self.assertEqual(len(self.store.rules_for_scene("被夸", 1, 0.0)), 1)
        with self.store.connect() as db:
            db.execute("UPDATE rules SET status='retired' WHERE id=?", (keep,))
        self.assertEqual([rule["behavior"] for rule in self.store.rules_for_scene("被夸", 10, 0.0)], ["低置信"])
        self.assertNotIn("高置信", [rule["behavior"] for rule in self.store.top_rules()])

    def test_top_rules_orders_by_confidence(self):
        self.store.upsert_rule("被夸", "一般", "", 0.4, 1.0, "BV1", "bili")
        self.store.upsert_rule("被逗", "更强", "", 0.9, 1.0, "BV1", "bili")
        self.assertEqual([rule["behavior"] for rule in self.store.top_rules()], ["更强", "一般"])

    def test_mark_used_counts_only_once_per_call(self):
        rule_id = self.store.upsert_rule("被夸", "可以直接流露小得意", "", 0.5, 1.0, "BV1", "bili")
        self.store.mark_used([])
        self.assertEqual(self.store.rules_for_scene("被夸", 10, 0.0)[0]["uses"], 0)
        self.store.mark_used([rule_id])
        self.assertEqual(self.store.rules_for_scene("被夸", 10, 0.0)[0]["uses"], 1)
        self.assertEqual(self.store.stats()["uses"], 1)

    def test_samples_are_recorded_and_bounded(self):
        self.store.add_sample("BV1", "bili", "被夸", "片" * 900, 1.0)
        with self.store.connect() as db:
            row = db.execute("SELECT excerpt, weight FROM samples").fetchone()
        self.assertEqual(len(row["excerpt"]), 400)
        self.assertEqual(self.store.stats()["samples"], 1)

    def test_reserve_auto_respects_daily_max(self):
        now = 1700000000.0
        self.assertTrue(self.store.reserve_auto("bili", 2, 0, now))
        self.assertTrue(self.store.reserve_auto("bili", 2, 0, now + 1))
        self.assertFalse(self.store.reserve_auto("bili", 2, 0, now + 2))

    def test_reserve_auto_respects_cooldown(self):
        now = 1700000000.0
        self.assertTrue(self.store.reserve_auto("bili", 5, 10, now))
        self.assertFalse(self.store.reserve_auto("bili", 5, 10, now + 60))
        self.assertTrue(self.store.reserve_auto("bili", 5, 10, now + 700))

    def test_reserve_auto_resets_next_day(self):
        now = 1700000000.0
        self.assertTrue(self.store.reserve_auto("bili", 1, 0, now))
        self.assertFalse(self.store.reserve_auto("bili", 1, 0, now + 60))
        self.assertTrue(self.store.reserve_auto("bili", 1, 0, now + 90000))

    def test_reserve_auto_tracks_sources_separately(self):
        now = 1700000000.0
        self.assertTrue(self.store.reserve_auto("bili", 1, 0, now))
        self.assertTrue(self.store.reserve_auto("webui", 1, 0, now))
        self.assertFalse(self.store.reserve_auto("bili", 1, 0, now))

    def test_dump_json_is_readable_stats(self):
        self.assertEqual(json.loads(self.store.dump_json())["rules"], 0)

    def test_second_store_on_same_file_is_idempotent(self):
        again = store_module.StyleStore(self.store.path)
        again.start()
        self.assertEqual(again.stats()["rules"], 0)


class ConfigShapeTest(unittest.TestCase):
    def test_single_plugin_section_holds_the_settings(self):
        config = config_module.StyleConfig()
        self.assertEqual(config.plugin.max_rules_per_reply, 2)
        self.assertEqual(config.plugin.learn_cooldown_minutes, 10)
        self.assertEqual(config.plugin.learn_daily_max, 6)
        self.assertLess(config.plugin.min_confidence, 0.5)

    def test_self_browse_uses_less_evidence_than_directed_material(self):
        config = config_module.StyleConfig()
        self.assertLess(config.plugin.browse_weight, config.plugin.user_weight)

    def test_bounds_are_enforced_not_just_documented(self):
        from pydantic import ValidationError
        for field, value in (("max_rules_per_reply", 9), ("min_confidence", 1.5),
                             ("learn_cooldown_minutes", -1), ("user_weight", 0.0)):
            with self.assertRaises(ValidationError, msg=field):
                config_module.StyleSettings(**{field: value})


if __name__ == "__main__":
    unittest.main()

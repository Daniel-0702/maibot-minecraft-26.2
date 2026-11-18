"""动作注册表、别名解析、帮助输出与参数解析的纯逻辑测试。"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from mcbot.actions import (
    ACTIONS,
    CATEGORY_KEYS,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    ActionSpec,
    categories,
    format_help,
    parse_address,
    parse_coords,
    parse_number,
    resolve,
)

# 任务要求至少覆盖的动作，以及每个动作必须带的别名。
REQUIRED_ALIASES: dict[str, set[str]] = {
    "goto": {"去", "走到", "前往", "导航"},
    "come": {"过来", "来"},
    "follow": {"跟随", "跟着"},
    "look": {"看向", "看"},
    "explore": {"探索", "逛逛"},
    "swim": {"上岸", "游上岸"},
    "home": {"回家"},
    "sethome": {"设家", "设置家"},
    "jump": {"跳", "跳一下"},
    "stop": {"停", "停下"},
    "resume": {"继续", "恢复"},
    "mine": {"挖", "挖掘"},
    "chop": {"砍树", "砍木"},
    "dig": {"下挖", "挖矿"},
    "collect": {"捡", "捡东西", "拾取"},
    "eat": {"吃", "吃东西"},
    "craft": {"合成", "制作"},
    "recipes": {"配方", "能做什么"},
    "inventory": {"背包", "物品栏"},
    "equip": {"装备", "拿在手上", "手持"},
    "drop": {"丢弃", "扔掉"},
    "torch": {"火把", "点火把"},
    "place": {"放置", "放方块"},
    "pillar": {"垫高", "起柱"},
    "bridge": {"搭桥", "铺路"},
    "shelter": {"避难所", "躲起来"},
    "attack": {"攻击", "打"},
    "flee": {"逃跑", "快跑"},
    "status": {"状态"},
    "diag": {"诊断"},
    "nearby": {"附近", "周围"},
    "players": {"玩家", "在线玩家"},
    "where": {"坐标", "位置"},
    "time": {"时间"},
    "light": {"光照", "亮度"},
    "help": {"帮助", "命令"},
    "connect": {"连接", "进服"},
    "disconnect": {"断开", "退服", "下线"},
    "verify": {"验证码"},
}


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


class ActionSpecTests(unittest.TestCase):
    def test_fields_and_defaults(self):
        spec = ActionSpec("demo", "info", "演示动作", "demo [数量]")
        self.assertEqual(spec.name, "demo")
        self.assertEqual(spec.category, "info")
        self.assertEqual(spec.summary, "演示动作")
        self.assertEqual(spec.usage, "demo [数量]")
        self.assertEqual(spec.aliases, ())
        self.assertEqual(spec.args, ())

    def test_frozen(self):
        spec = ActionSpec("demo", "info", "演示动作", "demo")
        with self.assertRaises(FrozenInstanceError):
            spec.name = "changed"  # type: ignore[misc]

    def test_uses_slots(self):
        spec = ActionSpec("demo", "info", "演示动作", "demo")
        self.assertFalse(hasattr(spec, "__dict__"))


class RegistryTests(unittest.TestCase):
    def test_actions_is_nonempty_tuple(self):
        self.assertIsInstance(ACTIONS, tuple)
        self.assertGreater(len(ACTIONS), 0)

    def test_all_entries_are_action_specs(self):
        for spec in ACTIONS:
            self.assertIsInstance(spec, ActionSpec)

    def test_names_unique(self):
        names = [spec.name for spec in ACTIONS]
        self.assertEqual(len(names), len(set(names)))

    def test_names_are_lowercase_ascii_identifiers(self):
        for spec in ACTIONS:
            with self.subTest(name=spec.name):
                self.assertEqual(spec.name, spec.name.lower())
                self.assertTrue(spec.name.isascii())
                self.assertTrue(spec.name.isidentifier())

    def test_categories_valid(self):
        for spec in ACTIONS:
            with self.subTest(name=spec.name):
                self.assertIn(spec.category, CATEGORY_KEYS)

    def test_every_category_is_used(self):
        self.assertEqual({spec.category for spec in ACTIONS}, set(CATEGORY_KEYS))

    def test_required_actions_present(self):
        names = {spec.name for spec in ACTIONS}
        missing = set(REQUIRED_ALIASES) - names
        self.assertFalse(missing, f"缺少动作：{sorted(missing)}")

    def test_required_aliases_present(self):
        by_name = {spec.name: spec for spec in ACTIONS}
        for name, aliases in REQUIRED_ALIASES.items():
            with self.subTest(name=name):
                spec = by_name.get(name)
                self.assertIsNotNone(spec)
                assert spec is not None
                self.assertTrue(aliases <= set(spec.aliases), f"{name} 缺少别名：{aliases - set(spec.aliases)}")

    def test_summary_and_usage_nonempty(self):
        for spec in ACTIONS:
            with self.subTest(name=spec.name):
                self.assertTrue(spec.summary.strip())
                self.assertTrue(spec.usage.strip())
                self.assertTrue(spec.usage.startswith(spec.name))

    def test_aliases_unique_nonempty_and_chinese(self):
        for spec in ACTIONS:
            with self.subTest(name=spec.name):
                self.assertTrue(spec.aliases)
                self.assertEqual(len(spec.aliases), len(set(spec.aliases)))
                self.assertTrue(all(alias == alias.strip() and alias for alias in spec.aliases))
                self.assertNotIn(spec.name, spec.aliases)
                self.assertTrue(any(_has_chinese(alias) for alias in spec.aliases))

    def test_aliases_globally_unique(self):
        owner: dict[str, str] = {}
        for spec in ACTIONS:
            for alias in spec.aliases:
                folded = alias.casefold()
                with self.subTest(alias=alias):
                    self.assertNotIn(folded, owner, f"别名 {alias!r} 同时属于 {owner.get(folded)} 和 {spec.name}")
                    owner[folded] = spec.name

    def test_args_unique_and_valid(self):
        for spec in ACTIONS:
            with self.subTest(name=spec.name):
                self.assertEqual(len(spec.args), len(set(spec.args)))
                for arg in spec.args:
                    self.assertTrue(arg.isascii())
                    self.assertTrue(arg.isidentifier())


class CategoriesTests(unittest.TestCase):
    def test_fixed_order(self):
        self.assertIsInstance(categories(), tuple)
        self.assertEqual(categories(), ("移动", "采集", "合成", "背包", "建造", "战斗", "信息", "系统"))

    def test_no_duplicates(self):
        labels = categories()
        self.assertEqual(len(labels), len(set(labels)))

    def test_labels_match_category_order(self):
        self.assertEqual(tuple(label for _, label in CATEGORY_ORDER), categories())
        self.assertEqual(CATEGORY_LABELS, dict(CATEGORY_ORDER))


class ResolveTests(unittest.TestCase):
    def test_resolve_every_canonical_name(self):
        for spec in ACTIONS:
            with self.subTest(name=spec.name):
                self.assertIs(resolve(spec.name), spec)

    def test_resolve_every_alias(self):
        for spec in ACTIONS:
            for alias in spec.aliases:
                with self.subTest(name=spec.name, alias=alias):
                    self.assertIs(resolve(alias), spec)

    def test_case_insensitive(self):
        cases = {
            "CRAFT": "craft",
            "Craft": "craft",
            "cRaFt": "craft",
            "GOTO": "goto",
            "Go": "goto",
            "MINE": "mine",
            "Inventory": "inventory",
        }
        for text, name in cases.items():
            with self.subTest(text=text):
                self.assertIs(resolve(text), resolve(name))

    def test_strip_whitespace_and_leading_slashes(self):
        for text in ("chop", " chop ", "\tchop\n", "/chop", "//chop", " /chop ", "/CHOP"):
            with self.subTest(text=text):
                self.assertIs(resolve(text), resolve("chop"))

    def test_mc_command_prefix(self):
        for text in ("/mc craft", " /mc craft ", "mc craft", "MC CRAFT", "/MC craft", "/mc\tcraft", "mc   craft"):
            with self.subTest(text=text):
                self.assertIs(resolve(text), resolve("craft"))

    def test_chinese_aliases(self):
        cases = {
            "挖": "mine",
            "挖掘": "mine",
            "砍树": "chop",
            "砍木": "chop",
            "合成": "craft",
            "验证码": "verify",
            "背包": "inventory",
            "看": "look",
            "打": "attack",
            "停": "stop",
        }
        for text, name in cases.items():
            with self.subTest(text=text):
                self.assertIs(resolve(text), resolve(name))

    def test_unknown_returns_none(self):
        for text in ("fly", "not_an_action", "crafting", "", "   ", "\u3000", "/", "//", "/mc", "mc"):
            with self.subTest(text=text):
                self.assertIsNone(resolve(text))
        for value in (None, 123, ["chop"], b"chop"):
            with self.subTest(value=value):
                self.assertIsNone(resolve(value))  # type: ignore[arg-type]

    def test_returns_registry_instance(self):
        spec = resolve("挖")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertIsInstance(spec, ActionSpec)
        self.assertIn(spec, ACTIONS)
        self.assertEqual(spec.name, "mine")


class FormatHelpTests(unittest.TestCase):
    def test_full_help_nonempty(self):
        text = format_help()
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())
        self.assertIn("【移动】", text)
        self.assertIn("【系统】", text)

    def test_all_actions_and_categories_listed(self):
        text = format_help()
        for spec in ACTIONS:
            with self.subTest(name=spec.name):
                self.assertIn(spec.usage, text)
                self.assertIn(spec.summary, text)
        for label in categories():
            self.assertIn(f"【{label}】", text)

    def test_categories_in_fixed_order(self):
        text = format_help()
        positions = [text.index(f"【{label}】") for label in categories()]
        self.assertEqual(positions, sorted(positions))

    def test_chop_line_matches_documented_shape(self):
        self.assertIn("  chop [数量]          砍树（别名：砍树/砍木）", format_help("采集"))

    def test_alias_suffix(self):
        self.assertIn("（别名：砍树/砍木）", format_help("采集"))
        self.assertIn("（别名：go/去/走到/前往/导航）", format_help("移动"))

    def test_chinese_category_filter(self):
        text = format_help("采集")
        self.assertEqual(text.count("【"), 1)
        self.assertIn("【采集】", text)
        self.assertNotIn("【移动】", text)
        self.assertIn("chop [数量]", text)
        self.assertIn("mine <方块>", text)
        self.assertNotIn("goto <x>", text)
        self.assertNotIn("attack [目标]", text)

    def test_english_category_filter_matches_chinese(self):
        self.assertEqual(format_help("gather"), format_help("采集"))
        self.assertEqual(format_help("GATHER"), format_help("采集"))
        self.assertEqual(format_help(" inventory "), format_help("背包"))
        self.assertEqual(format_help("Movement"), format_help("移动"))

    def test_category_blocks_contain_only_own_actions(self):
        for key, label in CATEGORY_ORDER:
            with self.subTest(category=key):
                text = format_help(label)
                lines = [line for line in text.splitlines() if line.startswith("  ")]
                expected = [spec for spec in ACTIONS if spec.category == key]
                self.assertEqual(len(lines), len(expected))
                for spec in ACTIONS:
                    present = any(line.startswith("  " + spec.usage) for line in lines)
                    self.assertEqual(present, spec.category == key, msg=f"{label} / {spec.name}")

    def test_unknown_category_returns_full_help(self):
        full = format_help()
        for category in ("不存在", "crafting", "MOVES", "  ", ""):
            with self.subTest(category=category):
                self.assertEqual(format_help(category), full)
        self.assertEqual(format_help(None), full)
        self.assertEqual(format_help(123), full)  # type: ignore[arg-type]

    def test_no_trailing_whitespace(self):
        text = format_help()
        self.assertEqual(text, text.rstrip())
        for line in text.splitlines():
            self.assertFalse(line.endswith(" "), repr(line))
            self.assertNotIn("\t", line)


class ParseCoordsTests(unittest.TestCase):
    def test_space_separated(self):
        self.assertEqual(parse_coords("10 64 -8"), (10.0, 64.0, -8.0))

    def test_comma_separated(self):
        self.assertEqual(parse_coords("10,64,-8"), (10.0, 64.0, -8.0))
        self.assertEqual(parse_coords("10, 64, -8"), (10.0, 64.0, -8.0))

    def test_parenthesised(self):
        self.assertEqual(parse_coords("(10 64 -8)"), (10.0, 64.0, -8.0))
        self.assertEqual(parse_coords("(10, 64, -8)"), (10.0, 64.0, -8.0))
        self.assertEqual(parse_coords(" ( 10 64 -8 ) "), (10.0, 64.0, -8.0))

    def test_whitespace_decimals_and_scientific(self):
        self.assertEqual(parse_coords("  1.5\t-64.25   3e2  "), (1.5, -64.25, 300.0))
        self.assertEqual(parse_coords("+.5 -.5 0"), (0.5, -0.5, 0.0))

    def test_returns_tuple_of_floats(self):
        result = parse_coords("1 2 3")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        for value in result:
            self.assertIsInstance(value, float)

    def test_invalid(self):
        for text in (
            "",
            "   ",
            "10 64",
            "10 64 -8 2",
            "a b c",
            "10 64 x",
            "(10 64 -8",
            "10 64 -8)",
            "()",
            "(   )",
            "10,,64",
            "nan 1 2",
            "inf 1 2",
            "1 2 3,4",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_coords(text))

    def test_non_string(self):
        for value in (None, 123, (10, 64, -8)):
            with self.subTest(value=value):
                self.assertIsNone(parse_coords(value))  # type: ignore[arg-type]


class ParseNumberTests(unittest.TestCase):
    def test_arabic_digits(self):
        cases = {"0": 0, "7": 7, "42": 42, " 42 ": 42, "007": 7}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_number(text), expected)

    def test_chinese_digits(self):
        cases = {
            "一": 1,
            "两": 2,
            "二": 2,
            "九": 9,
            "十": 10,
            "十一": 11,
            "十二": 12,
            "二十": 20,
            "二十五": 25,
            "一十": 10,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_number(text), expected)

    def test_invalid_uses_default(self):
        for text in ("", "   ", "abc", "1.5", "-1", "+3", "十十", "一百", "十个", "12个", "²"):
            with self.subTest(text=text):
                self.assertIsNone(parse_number(text))
                self.assertEqual(parse_number(text, 9), 9)
                self.assertEqual(parse_number(text, 0), 0)

    def test_success_ignores_default(self):
        self.assertEqual(parse_number("3", 99), 3)
        self.assertEqual(parse_number("二十", 99), 20)

    def test_non_string(self):
        for value in (None, 123, b"3"):
            with self.subTest(value=value):
                self.assertIsNone(parse_number(value))  # type: ignore[arg-type]
                self.assertEqual(parse_number(value, 5), 5)  # type: ignore[arg-type]


class ParseAddressTests(unittest.TestCase):
    def test_host_only(self):
        self.assertEqual(parse_address("mc.example.com"), ("mc.example.com", None))
        self.assertEqual(parse_address("MC.Example.COM"), ("MC.Example.COM", None))

    def test_host_with_port(self):
        self.assertEqual(parse_address("mc.example.com:25565"), ("mc.example.com", 25565))
        self.assertEqual(parse_address("127.0.0.1:25566"), ("127.0.0.1", 25566))

    def test_strip_whitespace(self):
        self.assertEqual(parse_address("  host:25565  "), ("host", 25565))
        self.assertEqual(parse_address(" host "), ("host", None))

    def test_ipv6_with_port(self):
        self.assertEqual(parse_address("[::1]:25565"), ("::1", 25565))
        self.assertEqual(parse_address("[2001:db8::1]:25566"), ("2001:db8::1", 25566))

    def test_ipv6_without_port(self):
        self.assertEqual(parse_address("[::1]"), ("::1", None))
        self.assertEqual(parse_address("::1"), ("::1", None))
        self.assertEqual(parse_address("2001:db8::1"), ("2001:db8::1", None))

    def test_port_bounds(self):
        self.assertEqual(parse_address("host:1"), ("host", 1))
        self.assertEqual(parse_address("host:65535"), ("host", 65535))

    def test_invalid_port_returns_none(self):
        for text in ("host:0", "host:65536", "host:-1", "host:abc", "host:", "[::1]:0", "[::1]:65536", "[::1]:abc"):
            with self.subTest(text=text):
                self.assertIsNone(parse_address(text))

    def test_invalid_address_returns_none(self):
        for text in ("", "   ", ":25565", "[]:25565", "[ ]:25565", "[::1", "[::1]oops", "[]", "[::1]:25565 extra"):
            with self.subTest(text=text):
                self.assertIsNone(parse_address(text))

    def test_non_string(self):
        for value in (None, 123, b"host"):
            with self.subTest(value=value):
                self.assertIsNone(parse_address(value))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

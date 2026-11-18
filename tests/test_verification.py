"""验证码识别与标题/动作栏解析测试。"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcbot.buffer import BufferReader, BufferWriter  # noqa: E402
from mcbot.client import (  # noqa: E402
    MinecraftClient,
    _handle_boss_bar,
    _handle_title_subtitle,
    _handle_title_text,
)
from mcbot.verification import CodeExtractor, VerificationTracker  # noqa: E402


class ExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = CodeExtractor()

    def test_chinese_code(self) -> None:
        self.assertEqual(self.extractor.extract("验证码：123456，请在 5 分钟内发送到 QQ 群"), "123456")

    def test_english_code(self) -> None:
        self.assertEqual(self.extractor.extract("Your verification code is AB12CD"), "AB12CD")

    def test_keyword_without_code(self) -> None:
        self.assertIsNone(self.extractor.extract("验证码已发送至 QQ 群"))

    def test_no_keyword(self) -> None:
        self.assertIsNone(self.extractor.extract("今天天气不错，在线 2026 人"))

    def test_custom_pattern(self) -> None:
        extractor = CodeExtractor(pattern=r"code\s*[:：]\s*(\d{6})")
        self.assertEqual(extractor.extract("your code: 888888"), "888888")
        self.assertIsNone(extractor.extract("验证码 123456"))


class TrackerTests(unittest.TestCase):
    def test_dedup_same_code(self) -> None:
        tracker = VerificationTracker(cooldown=60)
        self.assertTrue(tracker.accept("123456"))
        self.assertFalse(tracker.accept("123456"))
        self.assertTrue(tracker.accept("654321"))

    def test_empty_rejected(self) -> None:
        self.assertFalse(VerificationTracker().accept(""))


class ClientTextPacketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MinecraftClient("127.0.0.1", 25565)
        self.titles: list[str] = []
        self.subtitles: list[str] = []
        self.bars: list[str] = []
        self.client.on("title", self.titles.append)
        self.client.on("subtitle", self.subtitles.append)
        self.client.on("boss_bar", self.bars.append)

    @staticmethod
    def _component(text: str) -> bytes:
        return BufferWriter().write_string(json.dumps({"text": text}, ensure_ascii=False)).to_bytes()

    def test_title(self) -> None:
        _handle_title_text(self.client, BufferReader(self._component("验证码：111222")))
        self.assertEqual(self.titles, ["验证码：111222"])

    def test_subtitle(self) -> None:
        _handle_title_subtitle(self.client, BufferReader(self._component("请在群里发送验证码")))
        self.assertEqual(self.subtitles, ["请在群里发送验证码"])

    def test_boss_bar_add_and_update(self) -> None:
        for action in (0, 3):
            payload = (
                BufferWriter()
                .write_uuid(uuid.UUID(int=1))
                .write_varint(action)
                .write(self._component("验证码：333444"))
            )
            if action == 0:
                payload.write_f32(1.0).write_varint(0).write_varint(0).write_u8(0)
            _handle_boss_bar(self.client, BufferReader(payload.to_bytes()))
        self.assertEqual(self.bars, ["验证码：333444", "验证码：333444"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

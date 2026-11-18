"""Bridge native protocol selection without contacting external servers."""
import unittest
from unittest.mock import AsyncMock, patch
from mcbot.bridge import MinecraftBridge
from mcbot.client import ServerStatus
from mcbot.config import BotConfig


class BackendSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_version(self):
        bridge = MinecraftBridge(BotConfig(minecraft_version="26.3"), logger=lambda _: None)
        with patch('mcbot.bridge.ping_server', new=AsyncMock(side_effect=OSError())):
            result = await bridge.connect()
        self.assertFalse(result.ok)
        self.assertIn('minecraft_version', result.message)

    async def test_unsupported_auto_protocol(self):
        bridge = MinecraftBridge(BotConfig(), logger=lambda _: None)
        status = ServerStatus(host='localhost', port=25565, protocol=777, version_name='26.3')
        with patch('mcbot.bridge.ping_server', new=AsyncMock(return_value=status)):
            result = await bridge.connect()
        self.assertFalse(result.ok)
        self.assertIn('777', result.message)

    async def test_auto_763_selects_legacy(self):
        bridge = MinecraftBridge(BotConfig(), logger=lambda _: None)
        status = ServerStatus(host='localhost', port=25565, protocol=763)
        with patch('mcbot.bridge.ping_server', new=AsyncMock(return_value=status)), patch('mcbot.bridge.MinecraftClient.connect', new=AsyncMock(side_effect=RuntimeError('legacy sentinel'))):
            result = await bridge.connect()
        self.assertFalse(result.ok)
        self.assertIn('legacy sentinel', result.message)
        self.assertIn('1.20.1', result.message)

    async def test_auto_776_selects_native(self):
        bridge = MinecraftBridge(BotConfig(), logger=lambda _: None)
        status = ServerStatus(host='localhost', port=25565, protocol=776)
        with patch('mcbot.bridge.ping_server', new=AsyncMock(return_value=status)), patch('mcbot.modern_client.ModernMinecraftClient.connect', new=AsyncMock(side_effect=RuntimeError('native sentinel'))):
            result = await bridge.connect()
        self.assertFalse(result.ok)
        self.assertIn('native sentinel', result.message)
        self.assertIn('26.2', result.message)

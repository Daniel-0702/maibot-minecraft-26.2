"""测试用的最小 maibot_sdk 替身。

只实现插件代码用到的部分：配置模型、装饰器、上下文与组件收集。
真实运行时请使用 ``pip install maibot-plugin-sdk``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

CONFIG_RELOAD_SCOPE_SELF = "self"

__all__ = [
    "CONFIG_RELOAD_SCOPE_SELF",
    "Command",
    "Field",
    "MaiBotPlugin",
    "PluginConfigBase",
    "Tool",
    "ToolParameterInfo",
    "ToolParamType",
    "FakeContext",
]


class ToolParamType(Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    FLOAT = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass
class ToolParameterInfo:
    name: str
    param_type: ToolParamType = ToolParamType.STRING
    description: str = ""
    required: bool = True
    default: Any = None
    enum_values: list | None = None


class _FieldInfo:
    __slots__ = ("default", "default_factory", "description")

    def __init__(self, default: Any = None, default_factory: Callable | None = None, description: str = "") -> None:
        self.default = default
        self.default_factory = default_factory
        self.description = description


def Field(default: Any = None, default_factory: Callable | None = None, description: str = "", **kwargs: Any) -> _FieldInfo:
    return _FieldInfo(default, default_factory, description)


class PluginConfigBase:
    def __init__(self, **data: Any) -> None:
        for name in self._field_names():
            if name in data:
                setattr(self, name, data[name])
                continue
            info = getattr(type(self), name, None)
            if isinstance(info, _FieldInfo):
                setattr(self, name, info.default_factory() if info.default_factory else info.default)
            else:
                setattr(self, name, info)

    @classmethod
    def _field_names(cls) -> list[str]:
        names: list[str] = []
        for klass in reversed(cls.__mro__):
            annotations = getattr(klass, "__annotations__", {}) or {}
            for name in annotations:
                if name.startswith("_") or name in names:
                    continue
                names.append(name)
        return names

    def model_dump(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in self._field_names():
            value = getattr(self, name)
            result[name] = value.model_dump() if isinstance(value, PluginConfigBase) else value
        return result


def Tool(name: str, description: str = "", parameters: list | dict | None = None, **metadata: Any):
    def decorator(func: Callable) -> Callable:
        func._component = {  # type: ignore[attr-defined]
            "type": "TOOL",
            "name": name,
            "description": description,
            "parameters": parameters or [],
            "metadata": metadata,
        }
        return func

    return decorator


def Command(name: str, description: str = "", pattern: str = "", aliases: list[str] | None = None, **metadata: Any):
    def decorator(func: Callable) -> Callable:
        func._component = {  # type: ignore[attr-defined]
            "type": "COMMAND",
            "name": name,
            "description": description,
            "pattern": pattern,
            "aliases": aliases or [],
            "metadata": metadata,
        }
        return func

    return decorator


class _FakeSend:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        self.sent.append((text, stream_id))
        return True


class _FakeLLM:
    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def generate(self, prompt: str, model: str = "", task_name: str | None = None, **kwargs: Any) -> dict:
        self.prompts.append(prompt)
        return {"success": True, "response": self.reply}


class FakeContext:
    def __init__(self) -> None:
        self.logger = logging.getLogger("fake.maibot")
        self.send = _FakeSend()
        self.llm = _FakeLLM()


class MaiBotPlugin:
    config_model: type[PluginConfigBase] | None = None

    def __init__(self) -> None:
        self._ctx = FakeContext()
        self._config: PluginConfigBase | None = None
        self.loaded = False

    @property
    def ctx(self) -> FakeContext:
        return self._ctx

    @property
    def config(self) -> PluginConfigBase:
        if self._config is None:
            if self.config_model is None:
                raise RuntimeError("未声明 config_model")
            self._config = self.config_model()
        return self._config

    def get_plugin_config_data(self) -> dict[str, Any]:
        return self.config.model_dump()

    def get_components(self) -> list[dict[str, Any]]:
        components: list[dict[str, Any]] = []
        for name in dir(type(self)):
            attribute = getattr(type(self), name, None)
            meta = getattr(attribute, "_component", None)
            if meta:
                components.append(dict(meta))
        return components

    async def on_load(self) -> None:  # pragma: no cover - 由子类覆盖
        pass

    async def on_unload(self) -> None:  # pragma: no cover
        pass

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:  # pragma: no cover
        pass

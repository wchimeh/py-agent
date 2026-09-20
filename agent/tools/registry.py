# @File:     registry.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:39
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from ..providers.base import MALFORMED_ARGS_KEY, ToolCall, ToolDef
from .base import Tool, ToolError

_TOOLS: dict[str, Tool] = {}
_MODELS: dict[str, type[BaseModel]] = {}

_TYPE_MAP = {"string": str, "integer": int, "number": float,
             "boolean": bool, "array": list, "object": dict}


def _build_model(tool: Tool) -> type[BaseModel]:
    """tool.parameters → pydantic 动态模型（lax 默认 + extra='forbid'，enum→Literal）。"""
    required = set(tool.parameters.get("required", []))
    fields = {}
    for prop, spec in tool.parameters.get("properties", {}).items():
        if "enum" in spec:
            typ = Literal[tuple(spec["enum"])]
        else:
            typ = _TYPE_MAP.get(spec.get("type", "string"), str)
        fields[prop] = (typ, ... if prop in required else None)
    return create_model(f"{tool.name}Args",
                        __config__=ConfigDict(extra="forbid"), **fields)


def register(cls):
    register_tool(cls())
    return cls


def register_tool(tool: Tool, validate: bool = True) -> None:
    """运行期动态注册（MCP 远端工具等）；同名后注册覆盖。
    validate=False 时参数原样透传不本地校验（远端 schema 千奇百怪，
    本地简化校验会错杀合法调用，错误由远端返回后回灌）。"""
    _TOOLS[tool.name] = tool
    _MODELS[tool.name] = _build_model(tool) if validate else None


def unregister_prefix(prefix: str) -> int:
    """按名称前缀摘除动态注册的工具（如 mcp__），返回摘除数量。"""
    names = [n for n in _TOOLS if n.startswith(prefix)]
    for n in names:
        _TOOLS.pop(n)
        _MODELS.pop(n, None)
    return len(names)


def get_tool_defs(only: set[str] | None = None) -> list[ToolDef]:
    """全部注册工具（only 白名单过滤：子代理按 loop 限定可见工具集）。"""
    return [ToolDef(name=t.name, description=t.description, parameters=t.parameters)
            for t in _TOOLS.values() if only is None or t.name in only]


_ERR_ZH = {
    "missing": "缺少必填参数",
    "extra_forbidden": "未知参数",
    "int_parsing": "应为整数，无法从该值解析",
    "int_type": "应为整数",
    "float_parsing": "应为数字，无法从该值解析",
    "float_type": "应为数字",
    "bool_parsing": "应为布尔值，无法从该值解析",
    "bool_type": "应为布尔值",
    "string_type": "应为字符串",
    "list_type": "应为数组",
    "dict_type": "应为对象",
    "literal_error": "取值不在允许的枚举范围内",
}

_SCHEMA_LIMIT = 800


def _schema_hint(tool: Tool) -> str:
    schema = json.dumps(tool.parameters, ensure_ascii=False)
    if len(schema) > _SCHEMA_LIMIT:
        return schema[:_SCHEMA_LIMIT] + "…(schema 已截断)"
    return schema


def _format_validation_error(tool: Tool, e: ValidationError) -> str:
    lines = [f"参数校验失败（工具 {tool.name}）："]
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"])
        zh = _ERR_ZH.get(err["type"])
        lines.append(f"- 参数 {loc}：{zh if zh else err['msg']}")
    lines.append(f"请按 schema 重新生成：{_schema_hint(tool)}")
    return "\n".join(lines)


def check_tool_call(tc: ToolCall) -> str | None:
    """未知工具 / 畸形哨兵（单键精确匹配）/ schema 校验；通过返回 None。
    供 loop 在权限门之前调用：无效调用不该打扰用户。"""
    tool = _TOOLS.get(tc.name)
    if tool is None:
        return f"未知工具：{tc.name}，可用工具：{', '.join(_TOOLS)}"
    if set(tc.arguments) == {MALFORMED_ARGS_KEY}:
        return (f"工具调用参数不是合法 JSON，原始片段：{tc.arguments[MALFORMED_ARGS_KEY]}。"
                f"请按 schema 重新生成：{_schema_hint(tool)}")
    try:
        model = _MODELS[tc.name]
        if model is not None:
            model.model_validate(tc.arguments)
    except ValidationError as e:
        return _format_validation_error(tool, e)
    return None


def execute_tool(tc: ToolCall) -> tuple[str, bool]:
    if err := check_tool_call(tc):
        return err, True
    tool = _TOOLS[tc.name]
    model = _MODELS[tc.name]
    kwargs = (model.model_validate(tc.arguments).model_dump(exclude_unset=True)
              if model is not None else dict(tc.arguments))
    try:
        return tool.execute(**kwargs), False
    except ToolError as e:
        return str(e), True
    except Exception as e:
        return f"{type(e).__name__}: {e}", True

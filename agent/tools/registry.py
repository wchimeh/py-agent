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
    tool = cls()
    _TOOLS[tool.name] = tool
    _MODELS[tool.name] = _build_model(tool)
    return cls


def get_tool_defs() -> list[ToolDef]:

    return [ToolDef(name=t.name, description=t.description, parameters=t.parameters) for t in _TOOLS.values()]


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
        _MODELS[tc.name].model_validate(tc.arguments)
    except ValidationError as e:
        return _format_validation_error(tool, e)
    return None


def execute_tool(tc: ToolCall) -> tuple[str, bool]:
    if err := check_tool_call(tc):
        return err, True
    tool = _TOOLS[tc.name]
    m = _MODELS[tc.name].model_validate(tc.arguments)
    try:
        return tool.execute(**m.model_dump(exclude_unset=True)), False
    except ToolError as e:
        return str(e), True
    except Exception as e:
        return f"{type(e).__name__}: {e}", True

"""工具参数轻量校验(JSON Schema 子集)。

MCP SDK 自身的 schema 校验会走协议错误(``-32602``), 与本设计“统一
``{ok,error_code,message}`` 信封”(§4.2)不一致; 因此 ``server.py`` 以
``validate_input=False`` 注册, 由本模块在派发前校验, 失败同样返回结构化解封。

支持关键字: ``type``(object/string/boolean/integer/number/array)、``properties``、
``required``、``additionalProperties``、``enum``、``default``、``items``、
``minimum`` / ``maximum``、``minLength`` / ``maxLength``、``minItems`` / ``maxItems``。
未声明的关键字忽略, 保持向前兼容(GM2 工具将使用同样子集)。
"""

from __future__ import annotations

from typing import Any, Mapping

from .errors import InvalidArgumentError

_JSON_TYPES = {"object", "string", "boolean", "integer", "number", "array"}


def validate_arguments(schema: Mapping[str, Any], arguments: Any) -> dict[str, Any]:
    """校验并归一化工具参数; 返回仅含 schema 声明键的新字典."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidArgumentError(f"arguments 必须是 JSON 对象, 收到: {type(arguments).__name__}")

    properties: Mapping[str, Any] = schema.get("properties") or {}
    if not isinstance(properties, Mapping):
        raise InvalidArgumentError("工具 schema 非法: properties 必须是对象")

    unknown = sorted(key for key in arguments if key not in properties)
    if unknown:
        allowed = ", ".join(sorted(properties)) or "无"
        raise InvalidArgumentError(f"不支持的参数: {', '.join(unknown)}; 允许的参数: {allowed}")

    result: dict[str, Any] = {}
    for key, subschema in properties.items():
        # 可选参数显式传 null 视为未提供(模型常见写法); 必填项传 null 仍按缺失报错
        if key in arguments and arguments[key] is not None:
            result[key] = _validate_value(key, arguments[key], subschema)
        elif isinstance(subschema, Mapping) and "default" in subschema:
            result[key] = subschema["default"]

    missing = [key for key in schema.get("required") or () if key not in result]
    if missing:
        raise InvalidArgumentError(f"缺少必填参数: {', '.join(missing)}")
    return result


def _validate_value(name: str, value: Any, schema: Any) -> Any:
    if not isinstance(schema, Mapping):
        return value
    expected = schema.get("type")
    if expected is not None:
        if expected not in _JSON_TYPES:
            raise InvalidArgumentError(f"工具 schema 非法: 未知类型 {expected!r}")
        _check_type(name, value, expected)
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidArgumentError(f"{name} 必须是 {list(schema['enum'])} 之一, 收到: {value!r}")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        _check_range(name, value, schema)
    if isinstance(value, str):
        _check_length(name, value, schema)
    if isinstance(value, list):
        _check_array(name, value, schema)
    return value


def _check_type(name: str, value: Any, expected: str) -> None:
    if expected == "integer":
        # bool 是 int 子类, 必须显式排除
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidArgumentError(f"{name} 需要整数, 收到: {type(value).__name__}")
        return
    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidArgumentError(f"{name} 需要数字, 收到: {type(value).__name__}")
        return
    if expected == "string":
        if not isinstance(value, str):
            raise InvalidArgumentError(f"{name} 需要字符串, 收到: {type(value).__name__}")
        return
    if expected == "boolean":
        if not isinstance(value, bool):
            raise InvalidArgumentError(f"{name} 需要布尔值, 收到: {type(value).__name__}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise InvalidArgumentError(f"{name} 需要数组, 收到: {type(value).__name__}")
        return
    if expected == "object":
        if not isinstance(value, dict):
            raise InvalidArgumentError(f"{name} 需要对象, 收到: {type(value).__name__}")


def _check_range(name: str, value: Any, schema: Mapping[str, Any]) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise InvalidArgumentError(f"{name} 不能小于 {schema['minimum']}, 收到: {value!r}")
    if "maximum" in schema and value > schema["maximum"]:
        raise InvalidArgumentError(f"{name} 不能大于 {schema['maximum']}, 收到: {value!r}")


def _check_length(name: str, value: str, schema: Mapping[str, Any]) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        raise InvalidArgumentError(f"{name} 长度不能小于 {schema['minLength']}")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise InvalidArgumentError(f"{name} 长度不能大于 {schema['maxLength']}")


def _check_array(name: str, value: list[Any], schema: Mapping[str, Any]) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        raise InvalidArgumentError(f"{name} 至少需要 {schema['minItems']} 项")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        raise InvalidArgumentError(f"{name} 最多 {schema['maxItems']} 项")
    items = schema.get("items")
    if isinstance(items, Mapping):
        for index, item in enumerate(value):
            _validate_value(f"{name}[{index}]", item, items)

"""输入校验工具。

时间戳：必须是带时区的 ISO 8601 字符串，内部统一转换为 UTC，
并以规范字符串 ``YYYY-MM-DDTHH:MM:SS.ffff...+00:00`` 存储和输出。

水位/阈值：必须是有限数（int 或 float），布尔值、NaN、Infinity 一律拒绝
（bool 是 int 的子类，需要显式排除）。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .errors import ValidationError


def parse_json(raw_bytes: bytes) -> Any:
    """解析请求体 JSON，解析失败抛出 ValidationError。"""
    if not raw_bytes:
        raise ValidationError("请求体为空，需要 JSON 对象", code="invalid_body")
    try:
        import json

        return json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError(f"请求体不是合法 JSON：{exc}", code="invalid_body") from exc


def require_object(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"{what} 必须是 JSON 对象", code="invalid_request")
    return value


def require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"字段 {field!r} 必须是非空字符串", code="invalid_field")
    return value


def finite_number(value: Any, field: str) -> float:
    """校验并返回有限浮点数；拒绝 bool、NaN、Infinity、字符串等。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(
            f"字段 {field!r} 必须是数字，收到 {type(value).__name__}",
            code="invalid_field",
        )
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"字段 {field!r} 必须是有限数，不能是 NaN 或 Infinity",
                              code="invalid_field")
    return result


def parse_timestamp(value: Any, field: str = "observed_at") -> datetime:
    """解析带时区的 ISO 8601 时间戳，返回 timezone-aware datetime（UTC）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"字段 {field!r} 必须是 ISO 8601 时间字符串",
                              code="invalid_field")
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(
            f"字段 {field!r} 不是合法的 ISO 8601 时间：{text!r}",
            code="invalid_timestamp",
        ) from exc
    if dt.tzinfo is None:
        raise ValidationError(
            f"字段 {field!r} 必须带时区信息（如 2026-09-01T00:00:00Z 或 +08:00）",
            code="naive_timestamp",
        )
    return dt.astimezone(timezone.utc)


def canonical_utc(dt: datetime) -> str:
    """把 aware datetime 转为 UTC 规范 ISO 字符串（带 +00:00）。"""
    if dt.tzinfo is None:
        raise ValueError("canonical_utc 要求带时区的 datetime")
    return dt.astimezone(timezone.utc).isoformat()


def format_level(value: float) -> float:
    """输出水位：整数值显示为 int 风格，其余保持 float。"""
    if value == int(value):
        return int(value)
    return value

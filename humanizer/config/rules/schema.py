"""
规则条目 Pydantic Schema
用于校验 config/rules/*.yaml 的结构正确性。
加载时若字段缺失或类型错误，抛出 ValidationError 并附带文件路径信息。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    A = "A"
    B = "B"
    C = "C"


class RuleType(str, Enum):
    LITERAL = "literal"
    REGEX = "regex"
    DENSITY = "density"


class RuleEntry(BaseModel):
    """单条规则的定义。"""

    id: str = Field(..., min_length=1, description="规则唯一标识，格式如 BAN_A_01")
    severity: Severity = Field(..., description="规则分级：A / B / C")
    type: RuleType = Field(..., description="匹配类型：literal / regex / density")
    pattern: str = Field(..., min_length=1, description="匹配模式（字符串或正则）")
    suggestion: Optional[str] = Field(None, description="建议的修改方式")
    threshold: Optional[float] = Field(None, description="density 类型的密度阈值（次/千字）")
    note: Optional[str] = Field(None, description="规则说明备注")

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in ("A", "B", "C"):
            raise ValueError(f"severity 必须是 A/B/C 之一，收到: {v!r}")
        return v

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("literal", "regex", "density"):
            raise ValueError(f"type 必须是 literal/regex/density 之一，收到: {v!r}")
        return v

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("pattern 不能为空")
        return v

    @field_validator("threshold")
    @classmethod
    def validate_threshold(cls, v: Optional[float], info) -> Optional[float]:
        if v is not None and info.data.get("type") == "density" and v <= 0:
            raise ValueError("density 类型的 threshold 必须为正数")
        return v


class RulesConfig(BaseModel):
    """规则文件的顶层结构。"""

    rules: list[RuleEntry] = Field(..., min_length=1)

    @field_validator("rules")
    @classmethod
    def check_unique_ids(cls, v: list[RuleEntry]) -> list[RuleEntry]:
        ids = [r.id for r in v]
        duplicates = [uid for uid in ids if ids.count(uid) > 1]
        if duplicates:
            raise ValueError(f"规则 ID 重复: {set(duplicates)}")
        return v


class ForbiddenItem(BaseModel):
    """C 级禁动项的定义。"""

    id: str = Field(..., min_length=1)
    category: str = Field(..., description="分类：grammar / narrative / style / word")
    description: str = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1, description="为何该项不应被禁用的理由")


class ForbiddenConfig(BaseModel):
    """C 级禁动清单的顶层结构。"""

    items: list[ForbiddenItem] = Field(..., min_items=1)

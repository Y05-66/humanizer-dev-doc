"""YAML 规则加载器 + schema 校验。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import regex
import yaml
from humanizer.models import RuleHit

from humanizer.config import get_settings

from .schema import ForbiddenConfig, RuleEntry, RulesConfig

logger = logging.getLogger(__name__)


def _load_yaml(path: Path) -> dict[str, Any]:
    """安全加载 YAML 文件，路径不存在时返回空字典。"""
    if not path.exists():
        logger.warning("规则文件不存在：%s", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_bans_a(path: Path | None = None) -> list[RuleEntry]:
    """加载 A 级禁用词/句式规则。"""
    p = path or get_settings().bans_a
    data = _load_yaml(p)
    try:
        cfg = RulesConfig(rules=data.get("rules", []))
    except Exception as exc:
        raise ValueError(f"bans_a.yaml schema 校验失败 ({p}): {exc}") from exc
    logger.info("已加载 %d 条 A 级规则来自 %s", len(cfg.rules), p)
    return cfg.rules


def load_bans_b(path: Path | None = None) -> list[RuleEntry]:
    """加载 B 级软信号规则。"""
    p = path or get_settings().bans_b
    data = _load_yaml(p)
    try:
        cfg = RulesConfig(rules=data.get("rules", []))
    except Exception as exc:
        raise ValueError(f"bans_b.yaml schema 校验失败 ({p}): {exc}") from exc
    logger.info("已加载 %d 条 B 级规则来自 %s", len(cfg.rules), p)
    return cfg.rules


def load_c_forbidden(path: Path | None = None) -> list:
    """加载 C 级禁动清单（不做扫描用，仅用于仲裁）。"""
    p = path or get_settings().c_forbidden
    data = _load_yaml(p)
    try:
        cfg = ForbiddenConfig(items=data.get("items", []))
    except Exception as exc:
        raise ValueError(f"c_forbidden.yaml schema 校验失败 ({p}): {exc}") from exc
    logger.info("已加载 %d 条 C 级禁动项来自 %s", len(cfg.items), p)
    return cfg.items


def build_compiled_patterns(rules: list[RuleEntry]) -> list[tuple[RuleEntry, regex.Pattern]]:
    """将所有规则预编译为 regex 对象，提升扫描性能。"""
    compiled: list[tuple[RuleEntry, regex.Pattern]] = []
    for rule in rules:
        try:
            compiled.append((rule, regex.compile(rule.pattern)))
        except regex.error as exc:
            logger.error("规则 %s 的正则编译失败：%s", rule.id, exc)
    return compiled


def hit_to_rule_hit(
    rule: RuleEntry,
    text: str,
    span: tuple[int, int],
    line_number: int | None = None,
) -> RuleHit:
    """将一条命中转换为 RuleHit 数据对象。"""
    start, end = span
    ln = line_number
    return RuleHit(
        rule_id=rule.id,
        severity=rule.severity,
        start=start,
        end=end,
        matched_text=text[start:end],
        suggestion=rule.suggestion,
        line_number=ln,
    )


def compute_line_map(text: str) -> list[int]:
    """构建行号映射：line_map[char_offset] = line_number（从 1 开始）。"""
    line_map: list[int] = []
    current_line = 1
    for ch in text:
        line_map.append(current_line)
        if ch == "\n":
            current_line += 1
    return line_map

"""Stage 0：预检 —— 编码检查、事实锚点提取、模式路由。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from humanizer.models import Mode

logger = logging.getLogger(__name__)

# 事实锚点提取正则
_NAME_PATTERN = re.compile(r'[一-鿿]{2,4}(?:先生|小姐|师傅|道长|尊者|教主|掌门|长老|仙子|圣女)?')
_NUMBER_PATTERN = re.compile(r'\d+(?:\.\d+)?(?:%|万|亿|千|百|十)?')
_DATE_PATTERN = re.compile(r'\d{4}[年/-]\d{1,2}[月/-]\d{1,2}')
_DIALOGUE_PATTERN = re.compile(r'["\"\"\'\'「『][^\n"\"\"\'\'」』]+["\"\"\'\'\」』]')


@dataclass
class PrecheckResult:
    encoding: str = "utf-8"
    format_valid: bool = True
    fact_anchors: dict = None  # {'names', 'numbers', 'dates', 'dialogues'}
    mode: Mode = Mode.DETECT
    raw_mode_str: str = ""

    def __post_init__(self):
        if self.fact_anchors is None:
            self.fact_anchors = {"names": set(), "numbers": set(), "dates": set(), "dialogues": set()}


def detect_encoding(sample_bytes: bytes) -> str:
    """简单编码探测，优先 UTF-8。"""
    try:
        sample_bytes.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        try:
            sample_bytes.decode("gbk")
            return "gbk"
        except UnicodeDecodeError:
            return "utf-8"  # fallback


def extract_fact_anchors(text: str) -> dict:
    """
    从文本中提取事实锚点，用于改写后的保真性验证。
    """
    anchors = {"names": set(), "numbers": set(), "dates": set(), "dialogues": set()}
    anchors["names"] = set(_NAME_PATTERN.findall(text))
    anchors["numbers"] = set(_NUMBER_PATTERN.findall(text))
    anchors["dates"] = set(_DATE_PATTERN.findall(text))
    anchors["dialogues"] = set(_DIALOGUE_PATTERN.findall(text))
    return anchors


def run_precheck(
    content: bytes,
    mode_str: str,
) -> PrecheckResult:
    """
    执行预检流程。

    Returns:
        PrecheckResult 包含编码信息、格式有效性、事实锚点和路由模式。
    """
    result = PrecheckResult(raw_mode_str=mode_str)

    # 1. 编码探测
    result.encoding = detect_encoding(content)
    text = content.decode(result.encoding)
    result.fact_anchors = extract_fact_anchors(text)

    # 2. 格式校验
    # 简单检查：无控制字符（除了换行/制表符）
    control_chars = set(c for c in text if ord(c) < 32 and c not in "\n\t\r")
    if control_chars:
        logger.warning("检测到异常控制字符: %s", control_chars)
        result.format_valid = False

    # 3. 模式路由
    valid_modes = {"detect", "clean", "refactor"}
    mode_lower = mode_str.lower().strip()
    if mode_lower in valid_modes:
        result.mode = Mode(mode_lower)
    else:
        logger.warning("未知模式 %r，回退到 detect", mode_str)
        result.mode = Mode.DETECT
        result.raw_mode_str = mode_lower

    logger.info(
        "预检完成：encoding=%s, format_valid=%s, mode=%s, anchors=names:%d nums:%d dates:%d dlg:%d",
        result.encoding,
        result.format_valid,
        result.mode.value,
        len(result.fact_anchors["names"]),
        len(result.fact_anchors["numbers"]),
        len(result.fact_anchors["dates"]),
        len(result.fact_anchors["dialogues"]),
    )
    return result

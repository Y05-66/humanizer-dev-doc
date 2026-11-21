"""Stage 3：改写 —— 确定性规则后处理 + LLM 约束改写。"""

from __future__ import annotations

import logging
import re

import regex
from humanizer.models import (
    DeferredHit,
    Mode,
    RewritePlan,
    VerifyResult,
)
from humanizer.rules.engine import RuleEngine

# 可直接替换的 A 级固定短语（使用正则编译，带词边界避免误删）
_DIRECT_PATTERNS: list[tuple[regex.Pattern, str]] = []

logger = logging.getLogger(__name__)

def _build_direct_patterns() -> None:
    """Build compiled regex + replacement pairs for safe atomic replacements."""
    global _DIRECT_PATTERNS
    if _DIRECT_PATTERNS:
        return
    # Exact phrase deletions — full match, no partial overlap
    _DIRECT_PATTERNS = [
        (regex.compile(r'眼中闪过一丝'), ''),
        (regex.compile(r'眸中闪过一丝'), ''),
        (regex.compile(r'眼底闪过一丝'), ''),
        (regex.compile(r'心中涌起一股'), ''),
        (regex.compile(r'心底涌起一股'), ''),
        (regex.compile(r'嘴角勾起一抹'), ''),
        (regex.compile(r'嘴角勾起一丝'), ''),
        (regex.compile(r'深吸一口气'), ''),
        (regex.compile(r'深吸了一口?气'), ''),
        (regex.compile(r'缓缓[地\s]*(?:走来|开口|说道|走近|抬手|举起|低下头)?'), ''),
        (regex.compile(r'微微[地\s]*(?:一笑|点头|皱眉|挑眉|摇头|开口)?'), ''),
        (regex.compile(r'不禁'), ''),
        (regex.compile(r'仿佛(?![^\n]{0,20}(?:像|如|是))'), ''),  # delete "仿佛" unless used as simile
    ]


def run_rewrite(
    text: str,
    plan: RewritePlan,
    mode: Mode,
    rule_engine: RuleEngine | None = None,
    llm_client=None,
) -> tuple[str, list[VerifyResult]]:
    """
    执行改写两遍流程：
    第一遍：确定性规则后处理（最小改动）
    第二遍：LLM 约束改写（针对 deferred 命中）

    Returns:
        (rewritten_text, verify_results)
    """
    _build_direct_patterns()

    # ===== 第一遍：确定性替换 =====
    rewritten = _apply_deterministic_replacements(text, plan)

    # 第一遍后再扫一次 A 级，找出仍残留的
    remaining_a = _find_remaining_a(rewritten, rule_engine)
    logger.info("第一遍确定性替换后剩余 A 级命中：%d", len(remaining_a))

    # ===== 第二遍：LLM 改写 =====
    if plan.needs_llm and llm_client is not None:
        rewritten = _apply_llm_rewrite(rewritten, plan, llm_client)
        # LLM 输出后再跑一遍兜底规则清理
        rewritten = _apply_deterministic_replacements(rewritten, plan)
    else:
        # 无 LLM（离线测试），跳过第二阶段
        logger.info("LLM 客户端未提供，跳过 LLM 改写阶段")

    # 最终确定性兜底：再次移除硬违规
    rewritten = _final_cleanup(rewritten, rule_engine)

    return rewritten, []


def _apply_deterministic_replacements(text: str, plan: RewritePlan) -> str:
    """第一遍确定性替换：使用已编译的正则做原子替换，避免部分匹配。"""
    result = text
    for pat, rep in _DIRECT_PATTERNS:
        result = pat.sub(rep, result)
    # 清理多余的空格和连续标点
    result = re.sub(r'[ \t]+', ' ', result)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def _find_remaining_a(text: str, rule_engine: RuleEngine | None) -> list:
    """找出文本中仍残留的 A 级命中（用于日志）。"""
    if rule_engine is None:
        return []
    hits = rule_engine.scan(text)
    return [h for h in hits if h.severity == "A"]


def _apply_llm_rewrite(
    text: str,
    plan: RewritePlan,
    llm_client,
) -> str:
    """
    将文本分段（chunk）后调用 LLM 改写。

    策略：
    1. 按段落分割文本
    2. 将有 A/B 级命中的段落优先处理
    3. 对无命中但属于"AI 味较重段落"的也进行处理
    4. 合并结果
    """
    # 按双换行分割段落（保留段落结构）
    paragraphs = text.split("\n\n")
    if not paragraphs:
        return text

    # 找出有 deferred 命中的段落
    affected_paras = set()
    if plan.deferred_hits:
        affected_paras = _identify_affected_paragraphs(paragraphs, [
            (h.hit.start, h.hit.rule_id) for h in plan.deferred_hits
        ])
        logger.info("检测到 %d 个段落包含 A/B 级命中", len(affected_paras))

    modified_paras = []
    total = len(paragraphs)

    for i, para in enumerate(paragraphs):
        if not para.strip():
            modified_paras.append(para)
            continue

        should_rewrite = False
        hint = ""

        if i in affected_paras:
            # 该段落有明确的 A/B 级命中，需要针对性改写
            should_rewrite = True
            relevant_hits = [
                h for h in plan.deferred_hits
                if _para_contains_hit(para, h, paragraphs)
            ]
            if relevant_hits:
                hit_descriptions = "\n".join(
                    f"- [{h.hit.rule_id}] {h.hit.matched_text!r} → {h.hit.suggestion or '请改写'}"
                    for h in relevant_hits[:5]
                )
                hint = f"⚠️ 需要注意的问题：\n{hit_descriptions}\n"

        elif len(para) > 50 and i % 3 == 0:
            # 每3段进行一次风格优化（改善整体流畅度）
            should_rewrite = True
            hint = "请将以下段落改写得更自然、更像人类写作，保持原意不变。"

        if should_rewrite and llm_client is not None:
            try:
                logger.debug("改写段落 %d/%d (%d 字)", i + 1, total, len(para))
                rewritten_para = llm_client.rewrite_segment(para, hint)
                modified_paras.append(rewritten_para)
            except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("LLM 改写段落失败，保留原文：%s", exc)
                modified_paras.append(para)
        else:
            modified_paras.append(para)

        if (i + 1) % 50 == 0:
            logger.info("进度: %d/%d 段落", i + 1, total)

    return "\n\n".join(modified_paras)


def _identify_affected_paragraphs(
    paragraphs: list[str],
    positions: list[tuple[int, str]],
) -> set[int]:
    """找出包含命中位置的段落索引。"""
    # 计算每个段落的字符偏移
    offsets = []
    cum = 0
    for p in paragraphs:
        offsets.append(cum)
        cum += len(p) + 2  # +2 for \n\n
    affected = set()
    for pos, _ in positions:
        for i, off in enumerate(offsets):
            if off <= pos < off + len(paragraphs[i]):
                affected.add(i)
                break
    return affected


def _para_contains_hit(
    paragraph: str,
    hit: DeferredHit,
    all_paragraphs: list[str],
) -> bool:
    """判断某个段落实质性包含了该命中。"""
    # 计算每个段落的字符偏移，将全局绝对坐标转换为段落内相对坐标
    offsets = []
    cum = 0
    for p in all_paragraphs:
        offsets.append(cum)
        cum += len(p) + 2  # +2 for \n\n
    para_offset = None
    for i, off in enumerate(offsets):
        if off <= hit.hit.start < off + len(paragraph):
            para_offset = off
            break
    if para_offset is None:
        return False
    local_start = hit.hit.start - para_offset
    local_end   = hit.hit.end   - para_offset
    return paragraph[max(0, local_start-50):local_end+50].strip() != ""


def _final_cleanup(text: str, rule_engine: RuleEngine | None) -> str:
    """最后一遍确定性兜底：清除硬违规，防止循环改写。"""
    _build_direct_patterns()
    result = text
    for pat, rep in _DIRECT_PATTERNS:
        result = pat.sub(rep, result)
    # 清理多余的空白和换行
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = re.sub(r'[ \t]{2,}', ' ', result)
    result = result.strip()
    return result

"""
LLM 客户端 —— 统一 OpenAI-compatible 接口封装。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

from humanizer.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class LLMCallRecord:
    """单次 LLM 调用的记录。"""
    model: str
    prompt_length: int
    response_length: int
    latency_seconds: float
    tokens_used: int = 0
    success: bool = True
    error: str = ""


class LLMAgent:
    """
    统一的 LLM 改写客户端。

    支持任意 OpenAI-compatible 接口（DeepSeek、OpenRouter、Ollama 等）。
    环境变量配置：
        LLM_BASE_URL  — API 端点
        LLM_API_KEY   — 认证密钥
        LLM_MODEL     — 模型名
        LLM_TEMPERATURE — 温度（默认 1.2）
    """

    def __init__(self, settings=None):
        self._settings = settings or get_settings()
        api_key = os.environ.get(self._settings.llm.api_key_env)
        if not api_key:
            raise OSError(
                f"缺少 API Key 环境变量：{self._settings.llm.api_key_env}"
            )
        self._client = OpenAI(
            base_url=self._settings.llm.base_url,
            api_key=api_key,
            timeout=self._settings.llm.timeout_seconds,
        )
        self._model = self._settings.llm.model
        self._temperature = self._settings.llm.temperature
        self._max_tokens = self._settings.llm.max_tokens
        self._max_retries = self._settings.llm.max_retries
        self._call_records: list[LLMCallRecord] = []

    @property
    def call_records(self) -> list[LLMCallRecord]:
        return list(self._call_records)

    def rewrite_segment(self, segment: str, hint: str = "") -> str:
        """
        对单个文本片段执行 LLM 改写。

        Args:
            segment: 待改写的段落文本
            hint: 相关的命中提示（描述需要修改的内容）

        Returns:
            改写后的文本
        """
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(segment, hint)

        last_error = None
        for attempt in range(1, self._max_retries + 1):
            start = time.time()
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
                result = response.choices[0].message.content or ""
                elapsed = time.time() - start
                record = LLMCallRecord(
                    model=self._model,
                    prompt_length=len(user_prompt),
                    response_length=len(result),
                    latency_seconds=elapsed,
                    success=True,
                )
                self._call_records.append(record)
                logger.debug("LLM 改写成功：%.2fs, %.0f→%.0f tokens",
                             elapsed, len(user_prompt), len(result))
                return result.strip()

            except (RateLimitError, APIStatusError, AuthenticationError, APIConnectionError, APITimeoutError) as exc:
                last_error = exc
                wait = min(2 ** attempt, 30)
                logger.warning(
                    "LLM 调用第 %d/%d 次失败：%s，%ds 后重试",
                    attempt, self._max_retries, exc, wait,
                )
                time.sleep(wait)

        # 全部重试失败
        record = LLMCallRecord(
            model=self._model,
            prompt_length=len(segment),
            response_length=0,
            latency_seconds=0,
            success=False,
            error=str(last_error),
        )
        self._call_records.append(record)
        logger.error("LLM 改写全部失败（%d 次重试）：%s", self._max_retries, last_error)
        return segment  # 降级：返回原文

    def _build_system_prompt(self) -> str:
        return """你是资深编辑"去AI味改写助手"。你的目标：让 AI 生成的文本读起来像人类作家写的，同时100%保留故事事实。

【核心原则（不可违反）】
1. 事实锚点铁律：人名、数字、日期、地点、对话内容一字不改——这是故事的骨架，动一个字就崩。
2. 情节顺序不变：不添加新事件、不删减原有情节转折。
3. 风格替换 ≠ 内容重写：只换表达形式，不换意思。

【"AI腔"的特征与改写策略】

① 情绪直述 → 动作/感官替代
   · 原文："心中涌起一股复杂的情绪" → 改写：把情绪转化为身体反应或具体动作
   · 原文："眼中闪过一丝决绝" → 改写：描写眼神的具体变化或直接写动作
   · 删除程式化副词："不禁/顿时/随即/仿佛/似乎/显然/毋庸置疑"

② 句长扁平化 → 制造节奏感
   · 穿插短促句（2-5字）打破长句惯性，如："他停了。""不行。"
   · 避免连续3句以上相同长度的句子
   · 用破折号、省略号制造自然停顿

③ 结构套话 → 删除或融入叙事
   · 删除："总之/总而言之/综上所述/众所周知/不可否认"
   · 段落不要以"然而/但是/就在这时"开头，直接叙述
   · 段尾不要用"这一刻他才明白……的道理"式升华，让人物和情节自己收束

④ 词汇重复 → 同义替换
   · "非常/十分/极其/格外/分外" → 用具体情境展现程度
   · "露出+表情"（露出一丝微笑/露出几分精彩）→ 直接写表情细节
   · "慢慢/缓缓/微微" → 删除或用精准动词替代

⑤ 排比套路 → 打破机械对称
   · 避免"X着，Y着，Z着"三叠结构
   · 避免连续使用同类连接词（然而/因此/所以/于是堆叠）

【输出格式】只输出改写后的正文，不含任何解释、标注或前后缀。"""

    def _build_user_prompt(self, segment: str, hint: str = "") -> str:
        parts = [
            "请将以下段落改写为更像人类作家的文风，同时严格保留所有事实和情节。",
        ]
        if hint:
            parts.append(f"\n⚠️ 需要注意的问题：\n{hint}")
        parts.append(f"\n【原文】\n{segment}")
        parts.append("\n【改写后】")
        return "\n".join(parts)


class MockLLMAgent:
    """
    离线测试用的 Mock LLM 客户端 —— 更全面的去味操作。
    不依赖任何网络请求，通过多维度替换减少AI特征。
    """

    def __init__(self):
        # A级硬禁用词 - 完全删除
        self._delete_patterns = {
            "不禁": "", "仿佛": "", "顿时": "", "随即": "", "继而": "",
            "心中涌起一股": "", "心中涌起一阵": "", "眼中闪过一丝": "", "眸中闪过一丝": "",
            "嘴角勾起一抹": "", "深吸一口气": "", "缓缓": "", "微微": "",
            "显然": "", "无疑": "", "不可否认": "", "殊不知": "", "可想而知": "",
            "猛然": "", "赫然": "", "一下子": "",
        }
        # B级软信号 - 部分替换为更自然的表达
        self._replace_b_patterns = {
            "非常": "很", "极其": "十分", "分外": "格外", "格外": "特别",
            "无比": "非常", "十分": "很",
        }
        # 句式模板 - 尝试简化
        self._pattern_fixes = [
            (r'，而是[^\n]+', ''),           # 删除"不是...而是..."后半句
            (r'带着[^，]{2,10}的[^\n]*', ''), # 删除"带着xxx的yyy"结构
            (r'(?:他|她)[^\n]{0,20}知道\S{1,30}', '他知道了'), # 简化心理描述
            (r'（[^）]{2,20}），?', ''),      # 删除括号注释
        ]

    def rewrite_segment(self, segment: str, hint: str = "") -> str:
        result = segment
        # 1. 删除A级词
        for old, new in self._delete_patterns.items():
            result = result.replace(old, new)
        # 2. 替换B级词
        for old, new in self._replace_b_patterns.items():
            result = result.replace(old, new)
        # 3. 修复句式模式
        import re
        for pattern, replacement in self._pattern_fixes:
            result = re.sub(pattern, replacement, result)
        # 4. 清理多余空白和标点
        result = re.sub(r'\s+', ' ', result)
        result = re.sub(r'，，+', '，', result)  # 合并连续逗号
        result = re.sub(r'[。]{2,}', '。', result)  # 合并连续句号
        result = re.sub(r'\n{3,}', '\n\n', result).strip()
        return result or segment

    def rewrite_full_text(self, text: str) -> str:
        """对全文进行迭代清理，直到不再有A级命中或达到最大迭代次数"""
        from humanizer.models import Mode
        from humanizer.rules.engine import RuleEngine
        from humanizer.stages.diagnose import run_diagnose

        max_iterations = 5
        cleaned = text

        for i in range(max_iterations):
            prev_len = len(cleaned)
            cleaned = self.rewrite_segment(cleaned)

            # 检查是否还有A级命中
            engine = RuleEngine()
            report = run_diagnose(cleaned, mode=Mode.DETECT, rule_engine=engine)
            a_hits = [h for h in report.rule_hits if h.severity == "A"]

            if not a_hits:
                logger.info("迭代清理完成：%d轮后无A级命中", i + 1)
                break

            logger.debug("第%d轮清理后仍有%d条A级命中", i + 1, len(a_hits))

            # 如果文本长度没有变化，停止迭代避免死循环
            if abs(len(cleaned) - prev_len) < 5:
                logger.info("文本长度未变化，停止迭代清理")
                break

        return cleaned

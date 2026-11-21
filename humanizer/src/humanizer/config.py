"""Pydantic-Settings 配置加载模块。

从 config/default.yaml 读取默认配置，并支持通过环境变量覆盖。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# 从YAML文件加载配置（用于本地开发）
def _load_yaml_config() -> dict:
    """尝试从config/default.yaml加载配置。"""
    logger = logging.getLogger(__name__)
    try:
        import yaml
    except ImportError:
        logger.warning("yaml module not available")
        return {}
    # In PyInstaller frozen mode, sys.executable is in the dist/ folder;
    # the config dir was placed there by the spec's datas list.
    if getattr(sys, "frozen", False):
        frozen_config = Path(sys.executable).parent / "humanizer" / "config"
    else:
        frozen_config = None
    yaml_paths = [
        Path.cwd() / "config" / "default.yaml",
        frozen_config / "default.yaml" if frozen_config else None,
        Path(__file__).resolve().parents[2] / "config" / "default.yaml",
    ]
    for yaml_path in (p for p in yaml_paths if p):
        try:
            if yaml_path.exists():
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Failed to load YAML config from %s: %s", yaml_path, exc)
    return {}

# 加载YAML配置
_yaml_config = _load_yaml_config()


class MetricsThresholds(BaseSettings):
    """统计指标的阈值设定。"""

    model_config = SettingsConfigDict(frozen=True)

    # 突发性（句长标准差 / 句长均值）
    burstiness_ai_max: float = 0.3
    burstiness_human_min: float = 0.5

    # TTR
    ttr_ai_range: tuple[float, float] = (0.15, 0.25)
    ttr_human_range: tuple[float, float] = (0.30, 0.45)

    # 简化困惑度
    perplexity_simple_ai_range: tuple[float, float] = (20, 50)
    perplexity_simple_human_range: tuple[float, float] = (60, 120)


class LLMConfig(BaseSettings):
    """LLM 改写客户端配置。"""

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=None,
        extra="ignore",
    )

    # 优先从YAML配置加载，其次使用环境变量，最后使用硬编码默认值
    base_url: str = _yaml_config.get("llm", {}).get("base_url", "https://apihub.agnes-ai.com/v1")
    model: str = _yaml_config.get("llm", {}).get("model", "agnes-2.5-flash")
    api_key_env: str = _yaml_config.get("llm", {}).get("api_key_env", "LLM_API_KEY")
    temperature: float = float(_yaml_config.get("llm", {}).get("temperature", 1.2))
    max_tokens: int = int(_yaml_config.get("llm", {}).get("max_tokens", 2048))
    timeout_seconds: int = int(_yaml_config.get("llm", {}).get("timeout_seconds", 60))
    max_retries: int = int(_yaml_config.get("llm", {}).get("max_retries", 3))


class RewriteStrategy(BaseSettings):
    """改写策略配置。"""

    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    clean_require_a_reduction_pct: float = 80.0
    refactor_max_structural_actions: int = 5
    refactor_require_a_reduction_pct: float = 90.0


class OutputConfig(BaseSettings):
    """输出配置。"""

    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    encoding: str = "utf-8"
    backup_original: bool = False
    log_format: str = "json"


class AppConfig(BaseSettings):
    app_name: str = "Humanizer"
    app_version: str = "0.1.0"
    default_mode: str = "detect"


class BatchConfig(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    concurrent: int = 1
    skip_existing: bool = True


class Settings(BaseSettings):
    """
    全局应用配置。

    优先级（从高到低）：
    1. 环境变量
    2. config/default.yaml
    3. 代码内默认值
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 子配置
    app: AppConfig = AppConfig()
    metrics: MetricsThresholds = MetricsThresholds()
    llm: LLMConfig = LLMConfig()
    rewrite: RewriteStrategy = RewriteStrategy()
    output: OutputConfig = OutputConfig()
    batch: BatchConfig = BatchConfig()

    @property
    def bans_a(self) -> Path:
        return self._resolve_rule_path("bans_a.yaml")

    @property
    def bans_b(self) -> Path:
        return self._resolve_rule_path("bans_b.yaml")

    @property
    def c_forbidden(self) -> Path:
        return self._resolve_rule_path("c_forbidden.yaml")

    def _resolve_rule_path(self, filename: str) -> Path:
        """解析规则文件绝对路径（兼容开发模式与 PyInstaller frozen 模式）。"""
        if getattr(sys, "frozen", False):
            # In frozen mode rules live under {exe_dir}/humanizer/config/rules/
            return Path(sys.executable).parent / "humanizer" / "config" / "rules" / filename
        candidates = [
            Path.cwd() / "config" / "rules" / filename,
            Path(__file__).resolve().parents[2] / "config" / "rules" / filename,
            Path("/etc/humanizer/rules") / filename,
        ]
        for p in candidates:
            if p.exists():
                return p
        return candidates[0]  # fallback to first candidate


def get_settings() -> Settings:
    """获取全局配置单例。"""
    return Settings()


def get_rule_paths(settings: Settings | None = None) -> tuple[Path, Path, Path]:
    """返回三个规则文件的绝对路径。"""
    s = settings or get_settings()
    return s.bans_a, s.bans_b, s.c_forbidden

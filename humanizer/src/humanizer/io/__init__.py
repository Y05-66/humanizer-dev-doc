"""IO 模块：文本读取与写入。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_text(path: Path) -> bytes:
    """
    读取文件字节流。

    优先 UTF-8，失败时尝试 GBK。
    """
    raw = path.read_bytes()
    # 快速检测：若首字节符合 UTF-8 BOM 或 ASCII 范围，直接返回
    if raw[:3] == b'\xef\xbb\xbf':
        return raw[3:]  # strip BOM
    try:
        raw.decode("utf-8")
        return raw
    except UnicodeDecodeError:
        try:
            decoded = raw.decode("gbk")
            logger.warning("文件 %s 使用 GBK 编码，已自动转换", path)
            return decoded.encode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"无法解码文件 {path}：既不是 UTF-8 也不是 GBK") from exc


def read_text_strict(path: Path) -> str:
    """读取文件并确保是合法的 UTF-8 文本。"""
    raw = read_text(path)
    return raw.decode("utf-8")


def write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """
    将文本写入文件。

    自动创建父目录。
    Returns the resolved output path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    logger.info("已写入 %s（%d 字节）", path, len(text.encode(encoding)))
    return path.resolve()


def resolve_output_path(
    input_path: Path,
    output_dir: Path | None = None,
    suffix: str = "_clean",
) -> Path:
    """
    根据输入路径和输出目录推导输出路径。

    规则：
    - 若指定了 output_dir，则将其作为根目录，保持相对路径结构
    - 否则在输入文件同目录下生成 {stem}{suffix}.ext
    """
    input_path = Path(input_path)
    if output_dir is not None:
        output_dir = Path(output_dir)
        # 尝试保持相对路径
        try:
            rel = input_path.relative_to(input_path.anchor)
        except ValueError:
            rel = input_path.name
        out = output_dir / rel.stem / (rel.stem + suffix + rel.suffix)
        return out
    else:
        return input_path.with_stem(input_path.stem + suffix)

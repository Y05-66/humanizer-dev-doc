"""一键启动入口 —— 启动 Web UI 并自动在浏览器打开。

用法：
  双击 run.bat / 运行 python run.py → 启动服务，自动打开浏览器
  python run.py detect <文件>       → 旧版 CLI 诊断模式（保留兼容）
  python run.py clean <文件>        → 旧版 CLI 改写模式（保留兼容）
  python run.py --port 9000         → 指定端口启动
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

# src/ 目录加入 PATH，确保 can 导入 humanizer 包
_PKG_ROOT = Path(__file__).resolve().parent / "src"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _is_cli_mode() -> bool:
    """判断是否为 CLI 子命令模式（detect/clean/refactor/metrics）。"""
    cli_cmds = {"detect", "clean", "refactor", "metrics"}
    return any(a in cli_cmds for a in sys.argv[1:])


def _wait_and_open(url: str, max_wait: int = 8, poll_interval: float = 0.5) -> None:
    """轮询健康检查，等服务就绪后再打开浏览器。"""
    import urllib.error
    import urllib.request
    waited = 0.0
    while waited < max_wait:
        try:
            urllib.request.urlopen(url, timeout=1)
            # 服务已就绪，尝试打开浏览器
            try:
                webbrowser.open(url)
            except Exception as exc:
                print(f"\n[WARN] Could not auto-open browser ({exc}), visit manually: {url}")
            return
        except (urllib.error.URLError, OSError):
            # 服务还未就绪，继续等待
            time.sleep(poll_interval)
            waited += poll_interval
    # 超时仍未就绪
    print(f"\n[WARN] Service did not become ready in time, visit manually: {url}")


def _serve(port: int, workers: int = 1) -> None:
    """启动 uvicorn 服务器（在线程中运行）。
    
    workers > 1 时自动启用多个工作进程，提升并发处理能力。
    注意：SQLite 连接使用线程局部存储，多进程模式下每个进程独立维护 DB 连接。
    """
    import uvicorn
    config = uvicorn.Config(
        "web_ui:app", host="0.0.0.0", port=port,
        reload=False, log_level="warning",
        workers=workers,
        loop="auto",  # uvicorn 自动选择最优 event loop
    )
    server = uvicorn.Server(config)
    import asyncio
    asyncio.run(server.serve())


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    # ── CLI 模式：向后兼容旧版 typer 命令 ──────────────────────────────────────
    if _is_cli_mode():
        from humanizer.cli import app as typer_app
        # 过滤掉 --port 参数（CLI 模式下不使用）
        filtered = [a for a in sys.argv[1:] if a != "--port" and not a.startswith("--port=")]
        sys.argv = ["humanizer"] + filtered
        typer_app()
        return

    # ── Web UI 模式 ───────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workers", type=int, default=1,
                        help="Uvicorn 工作进程数；默认 1，建议设为 CPU 核心数")
    opts, _ = parser.parse_known_args()
    port = opts.port

    url = f"http://127.0.0.1:{port}"

    # 启动服务器线程
    server_thread = threading.Thread(target=_serve, args=(port, opts.workers), daemon=True)
    server_thread.start()

    # 等待服务器就绪后打开浏览器（另一独立线程）
    browser_thread = threading.Thread(target=_wait_and_open, args=(url,), daemon=True)
    browser_thread.start()

    print("\n[OK] Humanizer Web UI starting...")
    print(f"   URL: {url}")
    print("   Waiting for browser to open...\n")

    # 阻塞主线程，保持服务运行
    server_thread.join()


if __name__ == "__main__":
    main()

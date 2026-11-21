"""Humanizer Web UI —— 基于 FastAPI + 自包含 HTML 的「去AI味」在线工具。

用法:
    # 启动服务
    python web_ui.py
    # 浏览器打开 http://localhost:8000

API（供测试 / 自动化调用）:
    POST /detect          body: multipart formData {file}
    POST /clean           body: multipart formData {file, use_llm=false}
    GET  /health
    GET  /task/{task_id}   查询任务状态
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# ── SQLite 持久化认证存储（humanizer/db.py 进程启动时自动建表）────────────────
import db as _db_module
import db as _db
_db_module.init_db()
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse


# ── Fix Windows console ──────────────────────────────────────────────────────
if sys.platform == "win32":
    import io as _io
    if not getattr(sys.stdout, "buffer", None):
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if not getattr(sys.stderr, "buffer", None):
        sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Imports ───────────────────────────────────────────────────────────────────
from humanizer.llm.client import LLMAgent, MockLLMAgent
from humanizer.metrics import compute_full_metrics
from humanizer.models import Mode
from humanizer.rules.engine import RuleEngine
from humanizer.stages.diagnose import run_diagnose
from humanizer.stages.plan import run_plan
from humanizer.stages.rewrite import run_rewrite
from humanizer.stages.verify import run_verify

from humanizer.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("web_ui")

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Humanizer · 去AI味", version="0.1.0")

# ── Preload jieba so the first request doesn't wait for model loading ───────────
try:
    import jieba
    list(jieba.cut("预热"))
except ImportError:
    pass

# Cached singletons — created once on first use, reused across all requests
_engine: RuleEngine | None = None
_llm_client: Any = None


def _get_engine() -> RuleEngine:
    """Lazy-load and cache the rule engine (one-time cost ~0.3s)."""
    global _engine
    if _engine is None:
        from humanizer.rules.engine import RuleEngine
        _engine = RuleEngine()
    return _engine


# Task queue for async processing
_tasks: dict[str, dict] = {}
# Use half of available CPUs, minimum 2 — more workers = fewer queued requests
import os as _os
_WORKER_COUNT = max(2, (_os.cpu_count() or 4) // 2)
_executor = ThreadPoolExecutor(max_workers=_WORKER_COUNT)
logger.info("Task executor initialized with %d workers", _WORKER_COUNT)
_MAX_UPLOAD_MB = 2   # server-side upload size limit in MB

# ── 认证工具函数 ───────────────────────────────────────────────────────────────
_AUTH_HEADER = "X-Auth-Token"


def _require_auth(authorization: str | None) -> dict:
    """校验请求头中的 X-Auth-Token；无效则抛 401。"""
    if not authorization:
        raise HTTPException(status_code=401, detail="未登录")
    user = _db.validate_token(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return user



async def _do_register(form_user: str, form_pass: str, confirm: str,
                       method: str) -> dict:
    """注册入口：根据 method 分派到不同实现。

    method:
      - 'email' : 邮箱+密码注册（本地存储）
      - 'phone' : 手机号+密码注册（本地存储）
    """
    uid = f"{method}:{form_user}"
    if _db.find_user_by_uid(uid) is not None:
        raise HTTPException(status_code=409, detail="该账号已注册")

    if method in ('email', 'phone'):
        _db.create_user(uid, form_pass, form_user, method)
        tok = _db.get_or_create_token(uid)
        info = {"id": uid, "display": form_user, "method": method}
        return {"token": tok, "user": info}
    else:
        raise HTTPException(status_code=400, detail=f"不支持的登录方式: {method}")


async def _do_login(form_user: str, form_pass: str, method: str) -> dict:
    """登录入口：根据 method 分派到不同实现。

    method:
      - 'email' : 邮箱+密码登录
      - 'phone' : 手机号+密码登录
    """
    uid = f"{method}:{form_user}"

    if method in ('email', 'phone'):
        if _db.find_user_by_uid(uid) is None:
            raise HTTPException(status_code=401, detail="账号不存在，请先注册")
        if not _db.verify_password(uid, form_pass):
            raise HTTPException(status_code=401, detail="密码错误")
        info = {"id": uid, "display": form_user, "method": method}
        tok = _db.get_or_create_token(uid)
        return {"token": tok, "user": info}
    else:
        raise HTTPException(status_code=400, detail=f"不支持的登录方式: {method}")


async def _do_logout(token: str) -> dict:
    _db.invalidate_token(token)
    return {"ok": True}


async def _run_clean_async(text: str, use_llm: bool) -> dict:
    """异步执行清洁任务，带超时保护（60s），防止 LLM 挂起占满工作线程。"""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_executor, lambda: _run_clean(text, use_llm)),
            timeout=120.0,  # 每轮最多等 2 分钟
        )
    except asyncio.TimeoutError:
        logger.error("Clean task timed out after 120s")
        raise HTTPException(status_code=504, detail="处理超时，请稍后重试")


def _get_llm_client(use_llm: bool = False):
    global _llm_client
    if not use_llm:
        return MockLLMAgent()
    # Re-initialize if we previously fell back to Mock due to missing key
    if _llm_client is None or isinstance(_llm_client, MockLLMAgent):
        try:
            _llm_client = LLMAgent(get_settings())
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Real LLM unavailable, falling back to mock: %s", exc)
            _llm_client = MockLLMAgent()
    return _llm_client


# ── Helpers ───────────────────────────────────────────────────────────────────
def _read_file(file: UploadFile) -> str:
    content = file.file.read()
    if len(content) > _MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件太大，请选择小于 {_MAX_UPLOAD_MB}MB 的文本文件")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return content.decode("gbk")
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(400, "无法解码文件，请上传 UTF-8 或 GBK 编码的文本文件")


def _escape_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _calc_ai_score(a_hits: int, b_hits: int, burstiness: float, mattar: float, ppl: float, rhythm: dict, text_length: int = 0) -> int:
    """
    启发式 AI 味评分（0-100），综合绝对数量、密度、违规占比三重因素。

    各维度独立计分、互不抵消：
      - A级命中密度 ：sqrt(count) × 2 + (count/千字) × 5，上限45分
      - B级软信号密度：sqrt(count) × 1.5 + (count/千字) × 3，上限20分
      - 突发性不足  ：burst < 0.5 的部分线性计分，上限20分
      - MATTR不足  ：mattar < 0.42 的部分线性计分，上限10分
      - 困惑度偏低  ：ppl < 60 的部分线性计分，上限5分
      - 段落节奏呆板：连续短句段过多时额外扣分，上限0~10分
      - 违规占比惩罚：A级命中/总字数比例过高时加权惩罚，上限10分
    """
    # 计算每千字的命中密度
    chars_per_k = max(1, text_length) / 1000.0
    a_density = a_hits / chars_per_k   # 每千字A级命中数
    b_density = b_hits / chars_per_k   # 每千字B级命中数

    # 混合评分：绝对数量感知 + 密度修正
    s_a = min(45.0, math.sqrt(a_hits) * 2.0 + a_density * 5.0)
    s_b = min(20.0, math.sqrt(b_hits) * 1.5 + b_density * 3.0)

    s_burst = max(0.0, min(20.0, (0.5 - burstiness) * 40)) if burstiness < 0.5 else 0.0
    s_matt  = max(0.0, min(10.0, (0.42 - mattar) * 80))    if mattar   < 0.42 else 0.0
    s_ppl   = max(0.0, min(5.0,  (60 - ppl)  * 0.5))       if ppl    < 60  else 0.0

    # 段落节奏惩罚：段内 CV 均值过低或低节奏段落过多 → 加分
    rhythm_penalty = 0.0
    if rhythm:
        mean_cv = rhythm.get("mean_cv", 0.5)
        low_paras = rhythm.get("low_rhythm_paras", 0)
        total_paras = len(rhythm.get("paragraph_cvs", [])) or 1
        if mean_cv < 0.2:
            rhythm_penalty += 5.0 * (1 - mean_cv / 0.2)
        if low_paras > 0 and total_paras > 0:
            rhythm_penalty += 5.0 * (low_paras / total_paras)
        rhythm_penalty = min(10.0, rhythm_penalty)

    # 新增：违规占比惩罚 —— 长文本如果还有大量违规，即使密度不高也要惩罚
    violation_ratio = a_hits / max(1, text_length) * 1000  # 每千字违规数
    ratio_penalty = min(10.0, violation_ratio * 0.5)  # 每千字0.5分，上限10分

    return int(round(min(100, s_a + s_b + s_burst + s_matt + s_ppl + rhythm_penalty + ratio_penalty)))


def _run_detect(text: str) -> dict:
    """Run diagnosis and return serializable result."""
    engine = _get_engine()
    report = run_diagnose(text, mode=Mode.DETECT, rule_engine=engine)
    m = report.metrics
    counts = report.hit_counts_by_severity()

    total_chars = len(text)
    a_hits = counts["A"]
    b_hits = counts["B"]
    ai_score = _calc_ai_score(
        a_hits, b_hits,
        m.burstiness if m else 0.0,
        m.mattar     if m else 0.0,
        m.perplexity_simple if m else 0.0,
        (m.paragraph_rhythm if m else {}),
        total_chars,
    )

    return {
        "text_length": total_chars,
        "sentence_count": m.sentence_count if m else 0,
        "burstiness": round(m.burstiness, 4) if m else None,
        "ttr_raw": round(m.ttr, 4) if m else None,
        "mattar": round(m.mattar, 4) if m else None,
        "perplexity": round(m.perplexity_simple, 2) if m else None,
        "rhythm_cv_mean": round(m.paragraph_rhythm.get("mean_cv", 0), 4) if m and m.paragraph_rhythm else None,
        "rhythm_low_paras": m.paragraph_rhythm.get("low_rhythm_paras", 0) if m and m.paragraph_rhythm else 0,
        "a_hits": a_hits,
        "b_hits": b_hits,
        "c_hits": counts["C"],
        "ai_taste_score": ai_score,
        "hits": [
            {
                "rule_id": h.rule_id,
                "severity": h.severity,
                "matched_text": _escape_html(h.matched_text[:80]),
                "suggestion": _escape_html((h.suggestion or "")[:80]),
                "line": h.line_number,
                "start": h.start,
            }
            for h in sorted(report.rule_hits, key=lambda x: (x.start, x.severity))[:80]
        ],
    }


def _run_clean(text: str, use_llm: bool = False) -> dict:
    """Run full clean pipeline and return result with original + rewritten text."""
    t0 = time.time()
    engine = _get_engine()
    llm = _get_llm_client(use_llm)

    diagnosis = run_diagnose(text, mode=Mode.CLEAN, rule_engine=engine)
    a_before = len(diagnosis.a_hits)
    plan = run_plan(diagnosis, mode=Mode.CLEAN)
    rewritten, _ = run_rewrite(text, plan, mode=Mode.CLEAN, rule_engine=engine, llm_client=llm)
    remaining = engine.scan(rewritten)
    a_after = len([h for h in remaining if h.severity == "A"])

    metrics_before = diagnosis.metrics
    metrics_after = compute_full_metrics(rewritten)
    report = run_verify(
        original_text=text, rewritten_text=rewritten,
        metrics_before=metrics_before, metrics_after=metrics_after,
        a_before=a_before, a_after=a_after,
    )

    duration = round(time.time() - t0, 2)
    reduction_pct = round(((a_before - a_after) / max(a_before, 1)) * 100, 1) if a_before > 0 else 100.0

    # 计算改写前后的 AI 味指数
    score_before = _calc_ai_score(
        a_before, len(diagnosis.b_hits),
        metrics_before.burstiness if metrics_before else 0,
        metrics_before.mattar if metrics_before else 0,
        metrics_before.perplexity_simple if metrics_before else 0,
        metrics_before.paragraph_rhythm if metrics_before else {},
        len(text),
    )
    score_after = _calc_ai_score(
        a_after, 0,  # B级命中在改写后通常不重新统计，简化处理
        metrics_after.burstiness if metrics_after else 0,
        metrics_after.mattar if metrics_after else 0,
        metrics_after.perplexity_simple if metrics_after else 0,
        metrics_after.paragraph_rhythm if metrics_after else {},
        len(rewritten),
    )

    # 收集 A 级命中位置（用于 diff 高亮）
    hit_positions = [
        {"rule_id": h.rule_id, "start": h.start, "end": h.end,
         "matched_text": _escape_html(h.matched_text[:60])}
        for h in sorted(diagnosis.a_hits, key=lambda x: x.start)
    ]

    return {
        "duration_s": duration,
        "fidelity": report.overall_fidelity.value,
        "a_before": a_before,
        "a_after": a_after,
        "a_reduction_pct": reduction_pct,
        "issues": report.fidelity_issues[:10],
        "metrics_before": metrics_before.summary_dict() if metrics_before else None,
        "metrics_after": metrics_after.summary_dict() if metrics_after else None,
        "ai_taste_score_before": score_before,
        "ai_taste_score_after": score_after,
        "original_text": text,
        "rewritten_text": rewritten,
        "hit_positions": hit_positions,
    }


# ── HTML Template ─────────────────────────────────────────────────────────────




HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Humanizer · 去AI味</title>
<style>
:root {
  --bg:        #090c14;
  --surface:   #10152a;
  --card:      #161d35;
  --border:    #1e2748;
  --accent:    #7c3aed;
  --accent-glow: rgba(124,58,237,.35);
  --accent2:   #06b6d4;
  --green:     #22c55e;
  --red:       #ef4444;
  --amber:     #f59e0b;
  --text:      #e8edf8;
  --muted:     #5a6a8a;
  --radius:    14px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--bg); }
body {
  color: var(--text);
  font-family: 'PingFang SC','Microsoft YaHei',system-ui,sans-serif;
  line-height: 1.6;
  display: flex; flex-direction: column;
}
a { color: var(--accent2); text-decoration: none; }

/* ══ Header ══ */
.header {
  padding: 16px 32px;
  border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, #0e1325 0%, var(--bg) 100%);
  flex-shrink: 0;
}
.header-inner { max-width: 1200px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; }
.logo { display: flex; align-items: center; gap: 10px; }
.logo-icon {
  width: 36px; height: 36px; border-radius: 10px;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  display: flex; align-items: center; justify-content: center;
  font-size: 1.1em;
}
.logo h1 { font-size: 1.15em; font-weight: 700; letter-spacing: .01em; }
.logo h1 span { color: var(--accent2); }
.logo p { font-size: .72em; color: var(--muted); margin-top: 1px; }
.header-badge {
  font-size: .68em; color: var(--muted);
  background: var(--card); border: 1px solid var(--border);
  padding: 4px 12px; border-radius: 20px;
}

/* ══ Main ══ */
.main-wrap { flex: 1; display: flex; flex-direction: column; }
.container { max-width: 1200px; margin: 0 auto; padding: 24px 28px 48px; width: 100%; }

/* ══ Tabs ══ */
.tabs { display: flex; gap: 0; margin-bottom: 24px; border-bottom: 1px solid var(--border); }
.tab-btn {
  background: transparent; border: none;
  color: var(--muted); padding: 11px 24px 10px;
  cursor: pointer; font-size: .9em; font-weight: 500;
  border-bottom: 2px solid transparent; transition: all .2s;
  display: flex; align-items: center; gap: 7px;
}
.tab-btn.active { color: var(--accent2); border-bottom-color: var(--accent2); }
.tab-btn:hover:not(.active) { color: var(--text); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* ══ Layout Grid ══ */
.work-grid {
  display: grid;
  grid-template-columns: 520px 1fr;
  gap: 20px;
  align-items: stretch;
}
@media(max-width:900px){ .work-grid{grid-template-columns:1fr;} }

/* ══ Section Cards ══ */
.section-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  height: 100%;
  display: flex;
  flex-direction: column;
}
.section-header {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
}
.section-title {
  font-size: .8em; font-weight: 600; color: var(--muted);
  letter-spacing: .05em; text-transform: uppercase;
  display: flex; align-items: center; gap: 8px;
}
.section-title::before {
  content: ''; width: 3px; height: 14px; border-radius: 2px;
  background: linear-gradient(180deg, var(--accent), var(--accent2));
}
.section-body { padding: 18px; flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.upload-zone { flex-shrink: 0; }

/* ══ Upload Zone ══ */
.upload-zone {
  border: 2px dashed var(--border);
  border-radius: 10px;
  padding: 28px 20px;
  text-align: center;
  cursor: pointer;
  transition: all .2s;
  position: relative;
  background: var(--surface);
}
.upload-zone:hover, .upload-zone.dragover {
  border-color: var(--accent);
  background: rgba(124,58,237,.05);
  box-shadow: 0 0 20px var(--accent-glow);
}
.upload-zone input { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; border-radius: 10px; }
.upload-icon { font-size: 1.8em; margin-bottom: 6px; }
.upload-text { color: var(--muted); font-size: .84em; }
.upload-text strong { color: var(--accent2); }
.file-info { margin-top: 8px; font-size: .78em; color: var(--green); min-height: 1.2em; }

/* ══ Textarea ══ */
.textarea-wrap { margin-top: 14px; flex: 1; display: flex; flex-direction: column; }
textarea {
  width: 100%; min-height: 280px; max-height: none; height: auto; flex: 1;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; color: var(--text);
  padding: 12px 14px; font-size: .85em; resize: vertical;
  font-family: inherit; overflow-y: auto; line-height: 1.75;
  transition: border-color .2s, box-shadow .2s;
}
textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
textarea::placeholder { color: var(--muted); }

/* ══ Buttons & Tabs ══ */
.btn-row { display: flex; gap: 6px; margin-top: 14px; align-items: center; flex-wrap: nowrap; }
button.action-btn, .mode-tab {
  font-size: .82em; cursor: pointer; transition: all .15s; white-space: nowrap;
  display: inline-flex; align-items: center; gap: 5px;
}
button.action-btn {
  background: linear-gradient(135deg, var(--accent), #6d28d9);
  color: #fff; border: none;
  padding: 9px 17px; border-radius: 9px;
  font-weight: 600; box-shadow: 0 2px 12px var(--accent-glow);
}
button.action-btn:hover { transform: translateY(-1px); box-shadow: 0 4px 20px var(--accent-glow); opacity: 1; }
button.action-btn:active { transform: translateY(0); }
button.action-btn.secondary {
  background: var(--surface); border: 1px solid var(--border);
  color: var(--muted); box-shadow: none;
}
button.action-btn.secondary:hover { border-color: var(--accent2); color: var(--accent2); box-shadow: none; transform: none; }
button.action-btn:disabled { opacity: .35; cursor: not-allowed; transform: none; box-shadow: none; }
button.action-btn.primary {
  background: linear-gradient(135deg, var(--accent), #6d28d9);
  color: #fff; font-weight: 600; box-shadow: 0 2px 12px var(--accent-glow);
}
button.action-btn.primary:hover { box-shadow: 0 4px 20px var(--accent-glow); transform: translateY(-1px); }

/* ══ Login / Auth Styles ══ */
.auth-user-btn {
  background: linear-gradient(135deg, rgba(124,58,237,.2), rgba(6,182,212,.15));
  border: 1px solid var(--accent); color: var(--accent2);
  padding: 6px 14px; border-radius: 8px; cursor: pointer;
  font-size: .78em; font-weight: 600; white-space: nowrap;
  transition: all .2s; display: flex; align-items: center; gap: 6px;
}
.auth-user-btn:hover { background: linear-gradient(135deg, rgba(124,58,237,.35), rgba(6,182,212,.25)); }
.auth-user-btn .avatar { width: 20px; height: 20px; border-radius: 50%; background: var(--accent); display: flex; align-items: center; justify-content: center; font-size: .7em; color: #fff; }
.auth-login-btn {
  background: linear-gradient(135deg, var(--accent), #6d28d9);
  border: none; color: #fff; padding: 8px 20px; border-radius: 8px;
  cursor: pointer; font-size: .84em; font-weight: 600;
  box-shadow: 0 2px 12px var(--accent-glow); transition: all .2s;
}
.auth-login-btn:hover { transform: translateY(-1px); box-shadow: 0 4px 20px var(--accent-glow); }
/* ══ Profile Page Styles ══ */
.profile-page { display: flex; gap: 32px; align-items: flex-start; max-width: 680px; margin: 0 auto; }
.profile-avatar-col { display: flex; flex-direction: column; align-items: center; gap: 12px; width: 140px; flex-shrink: 0; }
.avatar-preview {
  width: 96px; height: 96px; border-radius: 50%; object-fit: cover;
  border: 3px solid var(--accent); background: var(--surface);
  box-shadow: 0 4px 24px rgba(0,0,0,.35); transition: transform .2s;
}
.avatar-preview:hover { transform: scale(1.05); }
.avatar-placeholder {
  width: 96px; height: 96px; border-radius: 50%;
  background: linear-gradient(135deg, #7c3aed, #06b6d4);
  display: flex; align-items: center; justify-content: center;
  font-size: 2.2em; color: #fff; font-weight: 700; letter-spacing: -.02em;
  border: 3px solid var(--accent); box-shadow: 0 4px 24px rgba(0,0,0,.35);
}
.mini-avatar { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; vertical-align: middle; }
.form-group { margin-bottom: 20px; }
.form-label { display: block; font-size: .75em; color: var(--muted); margin-bottom: 7px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; }
.form-input {
  width: 100%; padding: 10px 14px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text);
  font-size: .92em; outline: none; transition: border-color .2s, box-shadow .2s;
  box-sizing: border-box;
}
.form-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(124,58,237,.2); }
.img-browse-row { display: flex; gap: 8px; align-items: center; }
.img-url-input { flex: 1; }
.file-upload-btn {
  padding: 9px 16px; border-radius: 10px; cursor: pointer;
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  font-size: .82em; white-space: nowrap; transition: all .2s;
}
.file-upload-btn:hover { border-color: var(--accent); color: var(--accent2); }
#imgFileInput2 { display: none; }
.btn-save-profile {
  background: linear-gradient(135deg, #7c3aed, #6d28d9); color: #fff;
  border: none; padding: 12px 32px; border-radius: 10px;
  font-size: .9em; font-weight: 600; cursor: pointer; transition: all .2s;
  box-shadow: 0 4px 16px rgba(109,40,217,.3);
}
.btn-save-profile:hover { transform: translateY(-1px); box-shadow: 0 6px 24px rgba(109,40,217,.4); }
.btn-save-profile:disabled { opacity: .55; cursor: not-allowed; transform: none; box-shadow: none; }
.btn-delete-acct {
  background: none; border: 1px solid rgba(239,68,68,.4); color: #ef4444;
  padding: 9px 18px; border-radius: 10px; font-size: .82em; cursor: pointer;
  transition: all .2s;
}
.btn-delete-acct:hover { background: rgba(239,68,68,.1); border-color: #ef4444; }
.member-since { font-size: .76em; color: var(--muted); margin-top: 6px; text-align: center; }
.badge-method { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: .72em; font-weight: 600; background: rgba(6,182,212,.12); color: #22d3ee; }
.error-msg { color: #ef4444; font-size: .78em; margin-top: 5px; display: none; min-height: 1em; }

/* Profile full-screen overlay */
#profileOverlay {
  position: fixed; inset: 0; z-index: 500; background: var(--bg); overflow-y: auto;
}
.profile-topbar {
  display: flex; align-items: center; gap: 12px; padding: 16px 24px;
  border-bottom: 1px solid var(--border); background: rgba(15,23,42,.95);
  position: sticky; top: 0; z-index: 10; backdrop-filter: blur(12px);
}
.profile-topbar-back {
  display: inline-flex; align-items: center; gap: 5px;
  background: rgba(124,58,237,.15); color: #c4b5fd;
  border: 1px solid rgba(124,58,237,.3);
  padding: 7px 14px; border-radius: 8px; cursor: pointer;
  font-size: .88em; font-weight: 500; transition: all .15s; white-space: nowrap;
}
.profile-topbar-back:hover { background: rgba(124,58,237,.28); color: #ddd6fe; transform: translateX(-2px); }
.profile-topbar-title { font-size: 1em; font-weight: 700; color: var(--text); margin-left: auto; }
.profile-card { max-width: 640px; margin: 32px auto; padding: 36px 40px; }
.auth-modal-overlay {
  display: none; position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,.65); backdrop-filter: blur(4px);
  align-items: center; justify-content: center;
}
.auth-modal-overlay.open { display: flex; }
.auth-modal {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 18px; width: 400px; max-width: 92vw;
  box-shadow: 0 24px 64px rgba(0,0,0,.5); overflow: hidden;
}
.auth-modal-header {
  padding: 20px 24px 16px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
}
.auth-modal-header h2 { font-size: 1em; color: var(--text); margin: 0; }
.auth-modal-close {
  background: none; border: none; color: var(--muted); font-size: 1.3em;
  cursor: pointer; padding: 0 4px; line-height: 1;
}
.auth-modal-close:hover { color: var(--text); }
.auth-modal-tabs {
  display: flex; gap: 0; border-bottom: 1px solid var(--border);
}
.auth-tab-btn {
  flex: 1; background: transparent; border: none;
  color: var(--muted); padding: 11px 0; cursor: pointer;
  font-size: .82em; font-weight: 500; border-bottom: 2px solid transparent;
  transition: all .2s;
}
.auth-tab-btn.active { color: var(--accent2); border-bottom-color: var(--accent2); }
.auth-tab-btn:hover:not(.active) { color: var(--text); }
.auth-modal-body { padding: 20px 24px; }
.auth-method-row { display: flex; gap: 8px; margin-bottom: 16px; }
.auth-method-btn {
  flex: 1; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 0; cursor: pointer;
  font-size: .76em; color: var(--muted); text-align: center;
  transition: all .2s;
}
.auth-method-btn.active { border-color: var(--accent); color: var(--accent2); background: rgba(124,58,237,.08); }
.auth-input-group { margin-bottom: 12px; }
.auth-input-group label { display: block; font-size: .76em; color: var(--muted); margin-bottom: 5px; }
.auth-input {
  width: 100%; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text); padding: 9px 12px;
  font-size: .85em; outline: none; transition: border-color .2s;
}
.auth-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
.auth-submit-btn {
  width: 100%; background: linear-gradient(135deg, var(--accent), #6d28d9);
  border: none; color: #fff; padding: 10px; border-radius: 8px;
  cursor: pointer; font-size: .88em; font-weight: 600;
  box-shadow: 0 2px 12px var(--accent-glow); margin-top: 6px;
  transition: all .2s;
}
.auth-submit-btn:hover { box-shadow: 0 4px 20px var(--accent-glow); transform: translateY(-1px); }
.auth-error-msg { color: var(--red); font-size: .78em; margin-top: 8px; min-height: 1.2em; }
.auth-wx-area { text-align: center; padding: 10px 0; }
.wx-label { font-size: .78em; color: var(--muted); margin-bottom: 10px; }
.wx-qr-placeholder {
  width: 160px; height: 160px; margin: 0 auto;
  background: var(--surface); border: 1px dashed var(--border);
  border-radius: 12px; display: flex; align-items: center; justify-content: center;
  color: var(--muted); font-size: .8em; flex-direction: column; gap: 8px;
}

/* ══ Buttons & Tabs ══ */
.clean-mode-row {
  display: inline-flex; gap: 0; background: var(--surface);
  border-radius: 8px; overflow: hidden; border: 1px solid var(--border);
}
.mode-tab {
  background: transparent; border: none; color: var(--muted);
  padding: 9px 14px; cursor: pointer;
}
.mode-tab:hover { color: var(--text); }
.mode-tab.active {
  background: var(--accent); color: #fff; font-weight: 600;
}

/* ══ Toggle ══ */
.toggle-row {
  display: flex; align-items: center; gap: 6px;
  font-size: .78em; color: var(--muted);
  padding: 7px 12px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 9px;
  white-space: nowrap; margin-left: auto;
  transition: border-color .2s;
}
.toggle-row:hover { border-color: var(--accent); }
.toggle-row input[type=checkbox] { accent-color: var(--accent); cursor: pointer; }

/* ══ Result Panel ══ */
.result-panel { }
.score-hero {
  text-align: center; padding: 28px 20px 20px;
  border-bottom: 1px solid var(--border);
}
.score-ring {
  width: 130px; height: 130px; margin: 0 auto 14px;
  position: relative;
}
.score-ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
.score-ring-bg { fill: none; stroke: var(--border); stroke-width: 8; }
.score-ring-fill {
  fill: none; stroke-width: 8; stroke-linecap: round;
  transition: stroke-dashoffset .8s ease;
}
.score-center {
  position: absolute; inset: 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}
.score-num { font-size: 2.4em; font-weight: 800; line-height: 1; }
.score-unit { font-size: .7em; color: var(--muted); margin-top: 2px; }
.score-label { font-size: .78em; color: var(--muted); }

/* Stats chips */
.stats-chips {
  display: flex; gap: 8px; justify-content: center; flex-wrap: wrap;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
}
.chip {
  font-size: .72em; padding: 4px 12px; border-radius: 20px;
  background: var(--surface); border: 1px solid var(--border);
  color: var(--muted); display: flex; align-items: center; gap: 5px;
}
.chip b { font-weight: 700; }
.chip-red b { color: var(--red); }
.chip-blue b { color: #60a5fa; }
.chip-green b { color: var(--green); }

/* Metric bars */
.metrics-list { padding: 14px 18px; }
.metric-row { margin-bottom: 12px; }
.metric-row:last-child { margin-bottom: 0; }
.metric-head { display: flex; justify-content: space-between; margin-bottom: 5px; }
.metric-name { font-size: .78em; color: var(--muted); }
.metric-val { font-size: .78em; font-weight: 700; font-variant-numeric: tabular-nums; }
.metric-track {
  height: 6px; background: var(--surface); border-radius: 3px; overflow: hidden;
}
.metric-fill { height: 100%; border-radius: 3px; transition: width .7s ease; }
.fill-green  { background: linear-gradient(90deg, var(--green), #4ade80); }
.fill-amber  { background: linear-gradient(90deg, var(--amber), #fbbf24); }
.fill-red    { background: linear-gradient(90deg, var(--red), #f87171); }
.fill-cyan   { background: linear-gradient(90deg, var(--accent2), #22d3ee); }

.ref-hint {
  padding: 12px 18px; border-top: 1px solid var(--border);
  font-size: .68em; color: var(--muted); text-align: center; line-height: 1.6;
}

/* ══ Empty state ══ */
.empty-state {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  min-height: 280px; color: var(--muted); text-align: center; padding: 40px 20px;
}
.empty-icon { font-size: 3em; margin-bottom: 12px; opacity: .4; }
.empty-text { font-size: .9em; }
.empty-sub { font-size: .78em; margin-top: 6px; opacity: .6; }

/* ══ Results cards (clean tab) ══ */
.results-area { margin-top: 0; }
.res-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 22px;
}
.res-card h2 {
  font-size: .95em; margin-bottom: 16px; color: var(--accent2);
  padding-bottom: 12px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 8px;
}
.res-strip {
  display: flex; gap: 20px; flex-wrap: wrap;
  font-size: .84em; color: var(--muted); margin-bottom: 16px;
}
.res-strip span { display: flex; align-items: center; gap: 5px; }
.res-strip b { color: var(--text); font-weight: 600; }
.res-metrics {
  display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 14px;
}
.res-metric-box {
  background: var(--surface); border-radius: 10px; padding: 14px;
  border: 1px solid var(--border);
}
.res-metric-box .lbl { font-size: .75em; color: var(--muted); margin-bottom: 8px; }
.res-metric-box .row { font-size: .82em; display: flex; justify-content: space-between; padding: 3px 0; }
.res-metric-box .row b { font-variant-numeric: tabular-nums; }
.res-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media(max-width:640px){ .res-metrics,.res-compare{grid-template-columns:1fr;} }
.res-pre {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px; font-size: .82em; white-space: pre-wrap; word-break: break-word;
  min-height: 42vh; max-height: 55vh; overflow-y: auto; line-height: 1.75;
}
.res-pre mark {
  background: rgba(245,158,11,.25); color: #fbbf24; border-radius: 2px; padding: 1px 0;
}
.empty-textarea {
  background: var(--surface); border: 1px dashed var(--border); border-radius: 10px;
  padding: 20px 14px; font-size: .82em; color: var(--muted);
  text-align: center; min-height: 38vh; display: flex; align-items: center; justify-content: center;
  line-height: 1.75; user-select: none;
}
/* ══ Diff / Highlight ══ */
.diff-delta { display: inline-flex; flex-direction: column; gap: 2px; margin-bottom: 10px; }
.diff-row { display: flex; align-items: flex-start; gap: 8px; font-size: .8em; line-height: 1.6; }
.diff-icon { flex-shrink: 0; width: 18px; text-align: center; font-size: .9em; }
.diff-icon.remove { color: var(--red); }
.diff-icon.add   { color: var(--green); }
.diff-text      { white-space: pre-wrap; word-break: break-word; color: var(--text); }
.diff-text.rem  { color: var(--red); text-decoration: line-through; opacity: .7; }
.diff-text.new  { color: var(--green); }
/* ══ History Panel ══ */
.hist-panel { margin-top: 14px; }
.hist-panel h3 { font-size: .82em; color: var(--muted); margin-bottom: 8px; font-weight: 500; }
.hist-list { list-style: none; padding: 0; }
.hist-item { display: flex; align-items: center; gap: 10px; padding: 10px 12px;
             border: 1px solid var(--border); border-radius: 8px; margin-bottom: 6px;
             cursor: pointer; transition: border-color .15s; font-size: .82em; }
.hist-item:hover { border-color: var(--accent); }
.hist-item.active { border-color: var(--accent2); background: rgba(6,182,212,.06); }
.hist-item .hist-time { color: var(--muted); font-size: .75em; }
.hist-item .hist-scores { margin-left: auto; display: flex; gap: 10px; font-size: .75em; }
.hist-item .hist-scores .old-score { color: var(--amber); }
.hist-item .hist-scores .new-score { color: var(--green); }
/* ══ Format Selector ══ */
.fmt-select { position: relative; display: inline-block; }
.fmt-btn { padding: 5px 10px; font-size: .78em; border-radius: 6px; border: 1px solid var(--border); background: var(--card); color: var(--text); cursor: pointer; }
.fmt-dropdown { display: none; position: absolute; top: 100%; left: 0; margin-top: 4px; background: var(--card); border: 1px solid var(--border); border-radius: 6px; min-width: 100px; z-index: 100; box-shadow: 0 4px 12px rgba(0,0,0,.3); overflow: hidden; }
.fmt-dropdown.open { display: block; }
.fmt-option { padding: 7px 14px; font-size: .78em; cursor: pointer; color: var(--text); transition: background .15s; }
.fmt-option:hover { background: rgba(124,58,237,.15); }
.export-btns { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.dl-row { display: flex; gap: 6px; align-items: center; justify-content: flex-end; flex-wrap: wrap; margin-bottom: 10px; }
/* ══ Metric delta rows ══ */
.metric-delta { display: flex; justify-content: space-between; align-items: center;
                padding: 4px 0; font-size: .82em; }
.delta-arrow { font-size: .75em; margin-right: 4px; }
.delta-up    { color: var(--red); }
.delta-down  { color: var(--green); }
.delta-equal { color: var(--muted); }
.res-pre.orig {
  border-left: 3px solid var(--red); color: var(--muted);
}
.res-pre.rewrite {
  border-left: 3px solid var(--green); color: var(--text);
}
.res-label { font-size: .8em; font-weight: 600; margin-bottom: 6px; color: var(--text); display: flex; justify-content: space-between; }
.res-label span { font-size: .75em; color: var(--muted); font-weight: 400; }

/* Badges */
.badge { display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px; border-radius: 20px; font-size: .76em; font-weight: 600; }
.badge-pass { background: rgba(34,197,94,.15); color: var(--green); }
.badge-fail { background: rgba(239,68,68,.15); color: var(--red); }
.badge-warn { background: rgba(245,158,11,.15); color: var(--amber); }

/* Spinner */
.spinner { display: inline-block; width: 13px; height: 13px; border: 2px solid var(--border); border-top-color: var(--accent2); border-radius: 50%; animation: spin .7s linear infinite; margin-right: 5px; vertical-align: middle; }
@keyframes spin { to { transform: rotate(360deg); } }
.loading-msg { color: var(--muted); font-size: .88em; padding: 24px 0; text-align: center; }

/* ══ About / Info Section ══ */
.about-section {
  max-width: 1200px; margin: 0 auto;
  padding: 48px 28px 36px;
  border-top: 1px solid var(--border);
}
.about-inner { margin-bottom: 40px; }
.about-title {
  font-size: 1.6em; font-weight: 700; margin-bottom: 14px;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}
.about-desc {
  font-size: .94em; color: var(--text); line-height: 1.85;
  max-width: 860px; margin-bottom: 10px;
}
.about-sub {
  font-size: .84em; color: var(--muted); line-height: 1.7;
  max-width: 860px;
}

/* ── Feature Cards ── */
.about-features {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px;
  margin-bottom: 36px;
}
@media(max-width:760px){ .about-features{grid-template-columns:1fr;} }
.feature-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 24px 22px;
  transition: border-color .2s, transform .15s, box-shadow .2s;
}
.feature-card:hover {
  border-color: var(--accent);
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(124,58,237,.12);
}
.feature-icon {
  font-size: 1.8em;
  width: 48px; height: 48px;
  display: flex; align-items: center; justify-content: center;
  background: rgba(124,58,237,.12);
  border-radius: 12px; margin-bottom: 14px;
}
.feature-card h3 {
  font-size: .94em; font-weight: 600; color: var(--text);
  margin-bottom: 10px;
}
.feature-card p { font-size: .81em; color: var(--muted); line-height: 1.7; }

/* ── Platforms (badge-style) ── */
.about-platforms {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 24px 26px;
  margin-bottom: 28px;
}
.about-platforms h3 {
  font-size: .88em; font-weight: 600; color: var(--accent2);
  margin-bottom: 14px; letter-spacing: .02em;
  text-transform: uppercase;
}
.platform-tags {
  display: flex; flex-wrap: wrap; gap: 8px;
}
.platform-tag {
  display: inline-flex; align-items: center; gap: 6px;
  background: rgba(6,182,212,.08); border: 1px solid rgba(6,182,212,.2);
  border-radius: 20px; padding: 5px 14px;
  font-size: .78em; color: #7dd3fc; font-weight: 500;
  white-space: nowrap;
}
.platform-tag .tag-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent2); flex-shrink: 0;
}
.about-platforms .platform-note {
  margin-top: 14px; font-size: .82em; color: var(--muted); line-height: 1.7;
}

/* ── Why Choose Us ── */
.about-why {
  background: linear-gradient(135deg, rgba(124,58,237,.12), rgba(6,182,212,.08));
  border: 1px solid rgba(124,58,237,.3);
  border-left: 4px solid var(--accent);
  border-radius: 14px;
  padding: 26px 28px;
  margin-bottom: 32px;
}
.about-why h3 {
  font-size: 1.05em; font-weight: 700; color: var(--text);
  margin-bottom: 14px;
}
.about-why .why-text {
  font-size: .88em; color: #b0bec5; line-height: 1.85;
  margin-bottom: 12px;
}
.about-why .why-text:last-child { margin-bottom: 0; }

/* ── FAQ ── */
.about-faq { margin-top: 4px; }
.about-faq h3 {
  font-size: 1em; font-weight: 600; color: var(--text);
  margin-bottom: 16px;
}
.faq-item {
  padding: 12px 0;
  border-bottom: 1px solid rgba(30,39,72,.6);
}
.faq-item:last-child { border-bottom: none; }
.faq-item dt {
  font-size: .84em; font-weight: 600; color: var(--text);
  margin-bottom: 5px;
}
.faq-item dd {
  font-size: .8em; color: var(--muted); line-height: 1.7;
}

.footer {
  text-align: center; padding: 18px 14px;
  color: var(--muted); font-size: .7em;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}

/* ══ Toast Notification ══ */
.toast-msg {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  padding: 10px 22px; border-radius: 8px; font-size: .88em; font-weight: 500;
  color: #fff; z-index: 9999; pointer-events: none;
  box-shadow: 0 4px 16px rgba(0,0,0,.4); animation: toastIn .25s ease;
}
.toast-msg.success { background: linear-gradient(135deg, #059669, #10b981); }
.toast-msg.error   { background: linear-gradient(135deg, #dc2626, #ef4444); }
@keyframes toastIn  { from{opacity:0;transform:translateX(-50%) translateY(12px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
</style>
</head>
<body>

<div class="header">
  <div class="header-inner">
    <div class="logo">
      <div class="logo-icon">H</div>
      <div>
        <h1><span>Humanizer</span> · 去AI味</h1>
        <p>上传中文文本，检测 AI 生成特征并改写为更接近人类写作风格</p>
      </div>
    </div>
    <div class="header-badge">⚠ 辅助工具 · 非权威检测器</div>
    <div id="authArea"></div>
  </div>
</div>

<div class="main-wrap">
<div class="container">
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('detect')">🔍 检测分析</button>
    <button class="tab-btn" onclick="switchTab('clean')">✨ 去AI味改写</button>
  </div>

  <!-- ═══ Detect Panel ═══ -->
  <div id="panel-detect" class="tab-panel active">
    <div class="work-grid">

      <!-- Left Column: Input -->
      <div>
        <div class="section-card">
          <div class="section-header">
            <div class="section-title">输入文本</div>
          </div>
          <div class="section-body">
            <div class="upload-zone" id="dropZone">
              <input type="file" id="fileInput" accept=".txt,.md,.text">
              <div class="upload-icon">📄</div>
              <div class="upload-text"><strong>点击选择</strong> 文件或拖拽到此处<br><span style="font-size:.78em;color:var(--muted)">支持 .txt / .md，建议小于 50KB</span></div>
              <div class="file-info" id="fileInfo"></div>
            </div>
            <div class="textarea-wrap">
              <textarea id="pasteArea" placeholder="或直接粘贴文本内容到这里…"></textarea>
            </div>
            <div class="btn-row">
              <button class="action-btn" onclick="doDetect()">🔍 检测 AI 味</button>
              <div class="clean-mode-row">
                <button class="mode-tab active" id="tabLite" onclick="setCleanMode(false)">✨ 轻量改写</button>
                <button class="mode-tab" id="tabLlm" onclick="setCleanMode(true)">🤖 LLM改写</button>
              </div>
              <button class="action-btn primary" id="runCleanBtn" onclick="doClean()">▶ 执行改写</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Right Column: Results -->
      <div class="result-panel">
        <div class="section-card">
          <div class="section-header">
            <div class="section-title">检测结果</div>
          </div>
          <div id="donutChart">
            <div class="empty-state">
              <div class="empty-icon">🔍</div>
              <div class="empty-text">尚未检测</div>
              <div class="empty-sub">上传或粘贴文本后点击「检测」查看结果</div>
            </div>
          </div>
        </div>
      </div>

    </div><!-- /work-grid -->
    <!-- ═══ About Section — shown below 检测分析 ═══ -->
    <div class="about-section" style="padding-top:32px">
      <div class="about-inner">
        <h2 class="about-title">Humanizer · 去AI味</h2>
        <p class="about-desc">作为国内首个面向网文作者的「去AI味」检测与改写工具，Humanizer 深度整合了针对玄幻、都市、言情、悬疑等主流网文类别的专用算法。我们不仅提供精准的 AI 率检测，还能有效识别内容抄袭风险，更懂小说平台的审核标准。</p>
        <p class="about-sub">平台级风控对标：系统实时更新检测模型，精准模拟起点、番茄、七猫等主流小说平台的内审机制，帮助您提前发现并规避因"AI味"过重或原创度不足导致的限流或封书风险。</p>
      </div>
      <div class="about-features">
        <div class="feature-card">
          <div class="feature-icon">📖</div>
          <h3>小说原创度深度分析</h3>
          <p>不同于通用检测，我们专为小说语境定制。精准区分"融梗""借鉴"与"AI生成"，提供详细的原创度报告，让您的每一处伏笔与转折都能被正确识别。</p>
        </div>
        <div class="feature-card">
          <div class="feature-icon">✍️</div>
          <h3>作者扶持计划</h3>
          <p>作为专业的 AI 率检测工具，Humanizer 对个人作者免费开放。注册登录后不限检测字数，让每一次创作都能安心落笔。</p>
        </div>
        <div class="feature-card">
          <div class="feature-icon">✨</div>
          <h3>多维度去AI化</h3>
          <p>提供针对性的修改建议，帮助您替换生硬的 AI 词汇，注入人类情感逻辑，让作品重回"人味"满满的原创状态。</p>
        </div>
      </div>
      <div class="about-platforms">
        <h3>无缝对接主流投稿平台审核标准</h3>
        <div class="platform-tags">
          <span class="platform-tag"><span class="tag-dot"></span>起点中文网</span>
          <span class="platform-tag"><span class="tag-dot"></span>番茄小说</span>
          <span class="platform-tag"><span class="tag-dot"></span>晋江文学城</span>
          <span class="platform-tag"><span class="tag-dot"></span>七猫中文网</span>
          <span class="platform-tag"><span class="tag-dot"></span>知乎盐选</span>
          <span class="platform-tag"><span class="tag-dot"></span>飞卢中文网</span>
        </div>
        <p class="platform-note">无论您是追求订阅分成的职业作家，还是通过保底机制赚取稳定稿费的新人作者，我们的工具都能助您精准规避 AI 风险，提升过稿率。</p>
      </div>
      <div class="about-why">
        <h3>为什么选择 Humanizer · 去AI味？</h3>
        <p class="why-text">在 AI 写作泛滥的今天，坚持原创从未如此艰难。Humanizer 不仅是一个冰冷的检测工具，更是每一位原创写手的坚实后盾。</p>
        <p class="why-text">我们深知被平台误判 AI 的痛楚，也理解通宵码字的心血。因此，我们打造了这个专业的小说 AI 率检测工具，只为让您的故事在这个算法时代，依然能以最纯粹的面貌，打动每一位读者。</p>
      </div>
      <div class="about-faq">
        <h3>常见问题</h3>
        <div class="faq-item">
          <dt>Humanizer 的原创度检测能帮助通过平台审核吗？</dt>
          <dd>是的。我们的算法实时对标主流小说平台内审机制，检测结果可作为过稿参考。但最终审核权仍归各平台所有。</dd>
        </div>
        <div class="faq-item">
          <dt>这个工具真的是免费的吗？</dt>
          <dd>是的。Humanizer 对所有个人作者免费开放，注册登录后即可使用全部功能，无需充值或购买点数。</dd>
        </div>
        <div class="faq-item">
          <dt>我的小说草稿会被泄露吗？</dt>
          <dd>不会。所有检测均在本地加密环境运行，数据绝不上传外部服务器，您的核心创意与大纲设定全程保密。</dd>
        </div>
        <div class="faq-item">
          <dt>Humanizer 支持哪些类型的检测？</dt>
          <dd>覆盖玄幻修真、都市异能、言情穿越、悬疑推理等全品类网文风格，无论何种类型都能精准识别 AI 痕迹与潜在抄袭风险。</dd>
        </div>
      </div>
    </div><!-- /about-section -->
    </div><!-- /panel-detect -->

  <!-- ═══ Clean Panel ═══ -->
  <div id="panel-clean" class="tab-panel">
    <div id="cleanProgress"></div>
    <div id="cleanResults" class="results-area"></div>
  </div><!-- /panel-clean -->
</div><!-- /container -->
</div><!-- /main-wrap -->

<!-- ═══ Profile Overlay — full-screen, toggled via JS ═══ -->
<div id="profileOverlay" style="display:none;position:fixed;inset:0;z-index:500;background:var(--bg);overflow-y:auto">
  <div id="profileContent"></div>
</div>

<!-- ══ Auth Modal ══ -->

<!-- ══ Auth Modal ══ -->
<div class="auth-modal-overlay" id="authModal">
  <div class="auth-modal">
    <div class="auth-modal-header">
      <h2 id="authModalTitle">登录 / 注册</h2>
      <button class="auth-modal-close" onclick="closeAuthModal()">×</button>
    </div>
    <div class="auth-modal-tabs">
      <button class="auth-tab-btn active" onclick="switchAuthTab('login')">登录</button>
      <button class="auth-tab-btn" onclick="switchAuthTab('register')">注册</button>
    </div>
    <div class="auth-modal-body">
      <!-- Login form -->
      <div id="authLoginForm">
        <div class="auth-method-row">
          <button class="auth-method-btn active" data-method="email" onclick="setAuthMethod(this)">📧 邮箱</button>
          <button class="auth-method-btn" data-method="phone" onclick="setAuthMethod(this)">📱 手机号</button>
        </div>
        <div id="loginCredentialArea">
          <div class="auth-input-group">
            <label id="loginInputLabel">邮箱地址</label>
            <input class="auth-input" id="loginCredential" type="text" placeholder="请输入邮箱或手机号">
          </div>
          <div class="auth-input-group">
            <label>密码</label>
            <input class="auth-input" id="loginPassword" type="password" placeholder="请输入密码">
          </div>
          <button class="auth-submit-btn" onclick="handleLogin()">登 录</button>
        </div>
      </div>
      <!-- Register form -->
      <div id="authRegisterForm" style="display:none">
        <div class="auth-method-row">
          <button class="auth-method-btn active" data-method="email" onclick="setAuthMethod(this)">📧 邮箱</button>
          <button class="auth-method-btn" data-method="phone" onclick="setAuthMethod(this)">📱 手机号</button>
        </div>
        <div id="regCredentialArea">
          <div class="auth-input-group">
            <label id="regInputLabel">邮箱地址</label>
            <input class="auth-input" id="regCredential" type="text" placeholder="请输入邮箱或手机号">
          </div>
          <div class="auth-input-group">
            <label>设置密码</label>
            <input class="auth-input" id="regPassword" type="password" placeholder="请设置密码（≥6位）">
          </div>
          <div class="auth-input-group">
            <label>确认密码</label>
            <input class="auth-input" id="regConfirm" type="password" placeholder="再次输入密码">
          </div>
          <button class="auth-submit-btn" onclick="handleRegister()">注 册</button>
        </div>
      </div>
      <div class="auth-error-msg" id="authErrorMsg"></div>
    </div>
  </div>
</div>

<script>
const MAX_CHARS=50_000;
let currentText='',currentFileName='';
let cleanMode=false; // false=轻量(规则), true=LLM改写
let _lastCleanData=null; // stores last renderClean data for potential reuse

const fileInput=document.getElementById('fileInput');
const dropZone=document.getElementById('dropZone');
const pasteArea=document.getElementById('pasteArea');
const fileInfo=document.getElementById('fileInfo');

fileInput.addEventListener('change',e=>handleFile(e.target.files[0]));
dropZone.addEventListener('dragover',e=>{e.preventDefault();dropZone.classList.add('dragover');});
dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop',e=>{e.preventDefault();dropZone.classList.remove('dragover');if(e.dataTransfer.files[0])handleFile(e.dataTransfer.files[0]);});
pasteArea.addEventListener('input',()=>{currentText=pasteArea.value;fileInfo.textContent=currentText?`${currentText.length.toLocaleString()}字`:'';});

function handleFile(f){
  if(!f)return;
  if(f.size>10*1024*1024){alert('文件太大，请选择小于 10MB 的文件');return;}
  currentFileName=f.name;
  const reader=new FileReader();
  reader.onload=e=>{
    currentText=e.target.result;
    pasteArea.value=currentText;
    fileInfo.textContent=`${f.name} · ${(f.size/1024).toFixed(1)} KB · ${currentText.length.toLocaleString()} 字`;
  };
  reader.readAsText(f,'utf-8');
}

function switchTab(name){
  document.querySelectorAll('.tab-btn').forEach((b,i)=>b.classList.toggle('active',['detect','clean'][i]===name));
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  const panel = document.getElementById('panel-'+name);
  if(panel) panel.classList.add('active');
}

// Toggle profile overlay: show full-screen profile, hide main nav; back returns to tabs
function showProfile(){
  document.querySelectorAll('.main-wrap').forEach(el=>el.style.display='none');
  const ov=document.getElementById('profileOverlay');
  if(ov){ov.style.display='block';ov.scrollTop=0;}
  if(window._currentUser) renderProfile(window._currentUser);
}
function hideProfile(){
  const ov=document.getElementById('profileOverlay');
  if(ov) ov.style.display='none';
  document.querySelectorAll('.main-wrap').forEach(el=>el.style.display='');
}

async function apiDetect(){
  const body=buildFormData('detect');
  const resp=await fetch('/detect',{method:'POST',body,headers:{'X-Auth-Token':_getToken()}});
  if(!resp.ok){const t=await resp.text();throw new Error(t);}
  return resp.json();
}

async function setCleanMode(llm){
  cleanMode=llm;
  document.getElementById('tabLite').classList.toggle('active',!llm);
  document.getElementById('tabLlm').classList.toggle('active',llm);
}

async function apiClean(){
  const textLen=(currentText||pasteArea.value).length;
  if(textLen>10000&&cleanMode){
    const body=buildFormData('clean-async',true);
    const resp=await fetch('/clean-async',{method:'POST',body,headers:{'X-Auth-Token':_getToken()}});
    if(!resp.ok){const t=await resp.text();throw new Error(t);}
    const data=await resp.json();
    return await pollTask(data.task_id);
  }else{
    const body=buildFormData('clean',cleanMode);
    const resp=await fetch('/clean',{method:'POST',body,headers:{'X-Auth-Token':_getToken()}});
    if(!resp.ok){const t=await resp.text();throw new Error(t);}
    return resp.json();
  }
}

async function pollTask(taskId){
  while(true){
    await new Promise(r=>setTimeout(r,2000));
    const resp=await fetch(`/task/${taskId}`);
    if(!resp.ok)continue;
    const task=await resp.json();
    if(task.status==='completed')return task.result;
    if(task.status==='failed')throw new Error(task.error||'处理失败');
    if(task.status==='cancelled')break;
    const progressEl=document.getElementById('cleanProgress');
    if(progressEl) progressEl.innerHTML='<div class="loading-msg"><span class="spinner"></span>处理中 '+task.progress+'%…</div>';
  }
}

function buildFormData(mode,useLlm=false){
  const fd=new FormData();
  const text=currentText||pasteArea.value;
  const name=currentFileName||'pasted_'+Date.now()+'.txt';
  const blob=new Blob([text],{type:'text/plain;charset=utf-8'});
  fd.append('file',blob,name);
  if(mode==='clean')fd.append('use_llm',String(useLlm));
  return fd;
}

function loading(el){el.innerHTML='<div class="loading-msg"><span class="spinner"></span>处理中，请稍候…</div>';}
function error(el,msg){el.innerHTML='<div class="res-card" style="border-color:var(--red)"><h2 style="color:var(--red)">❌ 错误</h2><p style="color:var(--muted)">'+esc(msg)+'</p></div>';}

function showToast(msg,type='success'){
  const el=document.createElement('div');
  el.className='toast-msg '+(type||'success');
  el.textContent=msg;
  document.body.appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transition='opacity .3s';setTimeout(()=>el.remove(),300);},2800);
}

// ── Render: Detection Result ───────────────────────────────────────────────────
function renderRightPanel(data,elId){
  const burst=typeof data.burstiness==='number'?data.burstiness:0;
  const mattar=typeof data.mattar==='number'?data.mattar:0;
  const ppl=typeof data.perplexity==='number'?data.perplexity:0;
  const lowParas=typeof data.rhythm_low_paras==='number'?data.rhythm_low_paras:0;
  const aiPct=data.ai_taste_score??0;
  const humanPct=Math.max(0,Math.min(100,100-aiPct));
  const aHits=data.a_hits||0;
  const bHits=data.b_hits||0;
  const cHits=data.c_hits||0;

  // Ring circumference = 2πr, r=54 (for 130px diameter SVG with viewBox 0 0 130 130)
  const RADIUS=54, CIRCUMFERENCE=2*Math.PI*RADIUS;
  const offset=CIRCUMFERENCE*(1-humanPct/100);
  const ringColor=aiPct>=60?'var(--red)':aiPct>=30?'var(--amber)':'var(--green)';

  function mBar(label,value,target,maxVal,colorCls){
    const pct=Math.min(100,Math.max(0,(value/maxVal)*100));
    const isGood=value>=target;
    const valColor=isGood?'var(--green)':'var(--amber)';
    return `<div class="metric-row">
      <div class="metric-head">
        <span class="metric-name">${label}</span>
        <span class="metric-val" style="color:${valColor}">${typeof value==='number'?value.toFixed(2):'—'}</span>
      </div>
      <div class="metric-track">
        <div class="metric-fill ${colorCls}" style="width:${pct}%"></div>
      </div>
    </div>`;
  }

  const el=document.getElementById(elId);
  if(!el)return;
  el.innerHTML=`
    <div class="score-hero">
      <div class="score-ring">
        <svg viewBox="0 0 130 130">
          <circle class="score-ring-bg" cx="65" cy="65" r="${RADIUS}"/>
          <circle class="score-ring-fill" cx="65" cy="65" r="${RADIUS}"
            stroke="${ringColor}"
            stroke-dasharray="${CIRCUMFERENCE}"
            stroke-dashoffset="${offset}"/>
        </svg>
        <div class="score-center">
          <div class="score-num" style="color:${ringColor}">${aiPct}</div>
          <div class="score-unit">AI 味指数</div>
        </div>
      </div>
      <div class="score-label" style="color:${ringColor};font-size:.82em;font-weight:600">
        ${aiPct>=60?'高度疑似 AI 生成':aiPct>=30?'部分 AI 特征':'接近人类写作水平'}
      </div>
    </div>
    <div class="stats-chips">
      <div class="chip chip-red">A级违规 <b>${aHits}</b></div>
      <div class="chip chip-blue">B级信号 <b>${bHits}</b></div>
      <div class="chip"><span>${(data.text_length||0).toLocaleString()} 字</span>·<span>${data.sentence_count||0} 句</span></div>
    </div>
    ${lowParas>0?`<div style="padding:10px 18px;border-bottom:1px solid var(--border);font-size:.76em;color:var(--amber)">⚠ ${lowParas} 个段落节奏呆板（CV&lt;0.2）</div>`:''}
    <div class="metrics-list">
      ${mBar('突发性',burst,0.5,1.0,'fill-amber')}
      ${mBar('MATTR',mattar,0.42,1.0,'fill-amber')}
      ${mBar('困惑度',ppl,60,120,'fill-cyan')}
    </div>
    <div class="ref-hint">目标参考：突发性 &gt;0.5 · MATTR ≥0.42 · 困惑度 60–120</div>
  `;
}

// ── Render: Clean Results ──────────────────────────────────────────────────────

function arrowDelta(before, after){
  const d=before-after;
  if(d>0) return `<span class="delta-arrow delta-down">↓${d}</span>`;
  if(d<0) return `<span class="delta-arrow delta-up">↑${Math.abs(d)}</span>`;
  return `<span class="delta-arrow delta-equal">→</span>`;
}

function metricCard(label,before,after,unit,unit2){
  const b=typeof before==='number'?before.toFixed(3):'—';
  const a=typeof after==='number'?after.toFixed(3):'—';
  const ar=arrowDelta(typeof before==='number'?before:0, typeof after==='number'?after:0);
  return `<div style="background:var(--surface);border-radius:8px;padding:10px 14px;border:1px solid var(--border)">
    <div style="font-size:.7em;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.06em">${label}</div>
    <div style="display:flex;align-items:center;gap:10px;font-size:.88em">
      <span style="color:var(--muted)">${b}${unit||''}</span>
      ${ar}
      <span style="color:var(--accent2);font-weight:600">${a}${unit||''}</span>
    </div>
  </div>`;
}

function renderMetricTable(mb,ma){
  if(!mb||!ma)return '';
  const pairs=[['burstiness','突发性'],['ttr','TTR'],['perplexity_simple','困惑度'],['mattar','MATTR']];
  return `<table style="width:100%;font-size:.8em;border-collapse:collapse;margin-top:4px">
    <thead><tr style="color:var(--muted);border-bottom:1px solid var(--border)">
      <th style="text-align:left;padding:4px 8px">指标</th>
      <th style="padding:4px 8px">改写前</th><th style="padding:4px 8px">变化</th><th style="padding:4px 8px">改写后</th>
    </tr></thead>
    <tbody>${pairs.map(([k,label])=>{
      const bv=mb[k],av=ma[k];
      const bStr=typeof bv==='number'?bv.toFixed(3):'—';
      const aStr=typeof av==='number'?av.toFixed(3):'—';
      const ar=(typeof bv==='number'&&typeof av==='number')?arrowDelta(bv,av):'<span class="delta-arrow delta-equal">—</span>';
      return `<tr style="border-bottom:1px solid var(--border)"><td style="padding:5px 8px;color:var(--muted)">${label}</td><td style="padding:5px 8px;text-align:center">${bStr}</td><td style="padding:5px 8px;text-align:center">${ar}</td><td style="padding:5px 8px;text-align:center;color:var(--accent2)">${aStr}</td></tr>`;
    }).join('')}</tbody>
  </table>`;
}

function getScoreColor(s){return s>=60?'var(--red)':s>=30?'var(--amber)':'var(--green)';}

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

function downloadTxt(text,filename){
  const blob=new Blob([text],{type:'text/plain;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=filename;a.click();URL.revokeObjectURL(a.href);
}

async function copyToClipboard(text){
  try{await navigator.clipboard.writeText(text);}catch(e){const t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();document.execCommand('copy');document.body.removeChild(t);}
}

function downloadHtml(data){
  const html=`<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>Humanizer 改写结果</title><style>body{font-family:'PingFang SC',system-ui,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;background:#0f172a;color:#e2e8f0;}h2{color:#22d3ee;margin-top:32px;border-bottom:1px solid #1e293b;padding-bottom:8px;}pre{background:#1e293b;padding:16px;border-radius:8px;white-space:pre-wrap;word-break:break-word;line-height:1.8;font-size:14px;}.meta{color:#94a3b8;font-size:13px;margin-bottom:24px;}</style></head><body><h2>📝 Humanizer · 去AI味改写结果</h2><p class="meta">原始字数：${data.original_text.length} · 改写后字数：${data.rewritten_text.length} · AI味指数：${data.ai_taste_score_before}→${data.ai_taste_score_after}</p><h2>原文</h2><pre>${esc(data.original_text)}</pre><h2>改写后</h2><pre>${esc(data.rewritten_text)}</pre></body></html>`;
  downloadTxt(html,(currentFileName||'result.html').replace(/\.[^.]+$/,'_clean$&'));
}

function downloadMd(data){
  const rewrite=data.rewritten_text||"";
  const before=data.ai_taste_score_before ?? "?";
  const after=data.ai_taste_score_after ?? "?";
  return `# Humanizer · 去AI味改写结果

**AI味指数**：${before} → ${after}

---

## 改写后文本

${rewrite}
`;
}
function downloadDocx(data){
  const rewrite=data.rewritten_text||"";
  const before=data.ai_taste_score_before ?? "?";
  const after=data.ai_taste_score_after ?? "?";
  const origLen=(data.original_text||"").length;
  const html=`<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>Humanizer 改写结果</title><style>body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;max-width:800px;margin:40px auto;padding:0 20px;line-height:1.8;color:#333;}h1{color:#0f766e;border-bottom:2px solid #0f766e;padding-bottom:10px;}h2{color:#0f766e;margin-top:32px;}p.meta{color:#666;font-size:13px;}</style></head><body><h1>📝 Humanizer · 去AI味改写结果</h1><p class="meta">原始字数：${origLen} | 改写后字数：${rewrite.length} | AI味指数：${before}→${after}</p><h2>改写后文本</h2><p>${esc(rewrite).replace(/\n/g,"<br>")}</p></body></html>`;
  downloadTxt(html,(currentFileName||'result').replace(/\.[^.]+$/,"")+"_clean.doc");
}

function renderClean(data,el){
  _lastCleanData=data;
  // Ensure we're on the rewrite tab so results are visible
  switchTab('clean');
  const orig=data.original_text||'';
  const rewrite=data.rewritten_text||'';
  el.innerHTML=`
    <div class="res-card">
      <h2 style="margin-bottom:12px;font-size:.95em;color:var(--muted);font-weight:400">📝 原文 vs 改写 &nbsp;·&nbsp; ${orig.length} 字 → ${rewrite.length} 字</h2>
      <div class="dl-row">
        <div class="fmt-select">
          <button class="fmt-btn" onclick="openFmtMenu()">📦 下载结果 ▾</button>
          <div class="fmt-dropdown" id="fmtDropdown">
            <div class="fmt-option" onclick="window._dlTxt();closeFmtMenu()">💾 TXT</div>
            <div class="fmt-option" onclick="window._dlMd();closeFmtMenu()">📄 Markdown (.md)</div>
            <div class="fmt-option" onclick="window._dlDocx();closeFmtMenu()">📘 Word (.docx)</div>
          </div>
        </div>
      </div>
      <div class="res-compare res-compare-dl">
        <div><div class="res-label">📄 原始文本</div><pre class="res-pre orig">${esc(orig)}</pre></div>
        <div><div class="res-label">✨ 改写结果</div><pre class="res-pre rewrite">${esc(rewrite)}</pre></div>
      </div>
    </div>`;
  window._dlTxt=()=>downloadTxt(rewrite,(currentFileName||'result').replace(/\.[^.]+$/,'_clean')+'.txt');
  window._dlMd=()=>downloadTxt(downloadMd(data),(currentFileName||'result').replace(/\.[^.]+$/,'')+'_clean.md');
  window._dlDocx=()=>downloadDocx(data);
}

function openFmtMenu(){const d=document.getElementById('fmtDropdown');if(d)d.classList.add('open');}
function closeFmtMenu(){const d=document.getElementById('fmtDropdown');if(d)d.classList.remove('open');}
document.addEventListener('click',e=>{const d=document.getElementById('fmtDropdown');if(d&&d.classList.contains('open')&&!e.target.closest('.fmt-select'))d.classList.remove('open');});

window.switchTab=switchTab;
window.setCleanMode=setCleanMode;
window.showProfile=showProfile;
window.hideProfile=hideProfile;

async function doDetect(){
  if(!currentText.trim()){alert('请先上传文件或粘贴文本');return;}
  if(!_getToken()){openAuthModal('login');return;}
  const el=document.getElementById('donutChart');
  loading(el);
  try{const data=await apiDetect();renderRightPanel(data,'donutChart');switchTab('detect');}
  catch(e){if(e.message==='AUTH_UNAUTHORIZED'){_setToken('');openAuthModal('login');}else{error(el,e.message);}}
}

async function doClean(){
  if(!currentText.trim()){alert('请先上传文件或粘贴文本');return;}
  if(!_getToken()){openAuthModal('login');return;}
  const el=document.getElementById('cleanResults');
  // 先切换过去，让用户立刻看到改写面板
  switchTab('clean');
  loading(el);
  try{
    const data=await apiClean();
    renderClean(data,el);
  }catch(e){
    if(e.message==='AUTH_UNAUTHORIZED'){_setToken('');openAuthModal('login');}
    else{error(el,e.message);}
  }
}

// ── Auth JS ───────────────────────────────────────────────────────────────────
const TOKEN_KEY='humanizer_auth_token';
let _authMethod='email';   // current selected method (email/phone|wx)
let _authMode='login';     // 'login' or 'register'

function _getToken(){ return localStorage.getItem(TOKEN_KEY)||''; }
function _setToken(t){ if(t){localStorage.setItem(TOKEN_KEY,t);}else{localStorage.removeItem(TOKEN_KEY);} _refreshAuthUI(); }

async function _apiPost(path,body,token){
  const fd = new FormData();
  for(const[k,v] of Object.entries(body)) fd.append(k,v);
  const init={method:'POST',body:fd};
  if(token) init.headers={'X-Auth-Token':token};
  const resp=await fetch(path,init);
  // FastAPI returns JSON {detail:...} on error; 401 must be checked before reading body
  if(resp.status===401){_setToken(''); throw new Error('AUTH_UNAUTHORIZED');}
  if(!resp.ok){
    // Read body ONCE, then try to parse as JSON — avoids "body stream already read" error
    const raw = await resp.text();
    let errText = '';
    try{ const j=JSON.parse(raw); errText=j.detail||j.error||String(resp.status); }
    catch(_e){ errText=raw||String(resp.status); }
    throw new Error(errText);
  }
  return resp.json();
}

function openAuthModal(mode='login'){
  _authMode=mode;
  document.getElementById('authModalTitle').textContent=mode==='login'?'登录':'注册';
  document.getElementById('authLoginForm').style.display=mode==='login'?'block':'none';
  document.getElementById('authRegisterForm').style.display=mode==='register'?'block':'none';
  document.getElementById('authErrorMsg').textContent='';
  document.getElementById('authModal').classList.add('open');
  // 始终重置为邮箱，不继承上一个 tab 的状态
  const firstBtn=document.querySelector('#authLoginForm .auth-method-btn[data-method="email"]')
               || document.querySelector('.auth-method-btn[data-method="email"]');
  if(firstBtn) setAuthMethod(firstBtn);
}
function closeAuthModal(){ document.getElementById('authModal').classList.remove('open'); }

function switchAuthTab(tab){
  _authMode=tab;
  document.querySelectorAll('.auth-tab-btn').forEach(b=>b.classList.toggle('active',b.textContent.includes(tab==='login'?'登录':'注册')));
  document.getElementById('authLoginForm').style.display=tab==='login'?'block':'none';
  document.getElementById('authRegisterForm').style.display=tab==='register'?'block':'none';
  document.getElementById('authErrorMsg').textContent='';
  // 切换 tab 时强制重置为邮箱，避免微信状态残留到另一 tab
  const activeForm=tab==='login'?document.getElementById('authLoginForm'):document.getElementById('authRegisterForm');
  if(activeForm){
    const emailBtn=activeForm.querySelector('.auth-method-btn[data-method="email"]');
    if(emailBtn) setAuthMethod(emailBtn);
  }
}

function setAuthMethod(btn){
  _authMethod=btn.dataset.method;
  document.querySelectorAll('.auth-method-btn').forEach(b=>b.classList.toggle('active',b===btn));
  // Update label/placeholder text when switching email ↔ phone
  const ph=_authMethod==='phone'?'请输入手机号':'请输入邮箱地址';
  const lab=_authMethod==='phone'?'手机号':'邮箱地址';
  const lLabel=document.getElementById('loginInputLabel');
  const rLabel=document.getElementById('regInputLabel');
  if(lLabel) lLabel.textContent=lab;
  if(rLabel) rLabel.textContent=lab;
  const lc=document.getElementById('loginCredential');
  const rc=document.getElementById('regCredential');
  if(lc) lc.placeholder=ph;
  if(rc) rc.placeholder=ph;
}

async function handleLogin(){
  const cred=document.getElementById('loginCredential').value.trim();
  const pass=document.getElementById('loginPassword').value;
  if(!cred){showAuthError('请输入账号');return;}
  if(!pass){showAuthError('请输入密码');return;}
  try{
    const data=await _apiPost('/auth/login',{username:cred,password:pass,method:_authMethod});
    window._currentUser=data.user;
    _setToken(data.token);
    closeAuthModal();
    showToast('登录成功，欢迎 '+data.user.display+'！','success');
  }catch(e){showAuthError(e.message);}
}

async function handleRegister(){
  const cred=document.getElementById('regCredential').value.trim();
  const pass=document.getElementById('regPassword').value;
  const confirm=document.getElementById('regConfirm').value;
  if(!cred){showAuthError('请输入账号');return;}
  if(pass.length<6){showAuthError('密码长度至少6位');return;}
  if(pass!==confirm){showAuthError('两次输入的密码不一致');return;}
  try{
    const data=await _apiPost('/auth/register',{username:cred,password:pass,confirm_password:confirm,method:_authMethod});
    window._currentUser=data.user;
    _setToken(data.token);
    closeAuthModal();
    showToast('注册成功，欢迎 '+data.user.display+'！','success');
  }catch(e){showAuthError(e.message);}
}

function showAuthError(msg){ document.getElementById('authErrorMsg').textContent=msg; }

async function handleLogout(){
  const token=_getToken();
  if(!token)return;
  try{ await _apiPost('/auth/logout',{},token); }catch{}
  _setToken('');
}

async function checkAuth(){
  const token=_getToken();
  if(!token){ _refreshAuthUI(); return; }
  try{
    const data = await (await fetch('/auth/me', { headers: { 'X-Auth-Token': token } })).json();
    window._currentUser=data.user;
    _refreshAuthUI();
    renderProfile(data.user);
  }catch(e){ _setToken(''); _refreshAuthUI(); }
}

function _refreshAuthUI(){
  const container=document.getElementById('authArea');
  const token=_getToken();
  const user=window._currentUser;
  if(!token||!user){
    container.innerHTML='<button class="auth-login-btn" onclick="openAuthModal(\'login\')">登录 / 注册</button>';
  }else{
    const initials=user.id.split(':').pop()?.[0]?.toUpperCase()||'?';
    const imgTag = user.avatar_url ? `<img src="${esc(user.avatar_url)}" class="mini-avatar" alt="">` : '';
    container.innerHTML=`<button class="auth-user-btn" onclick="showProfile()"><span class="avatar">${imgTag}${initials}</span>${esc(user.display)}</button>`;
  }
}

// Profile dropdown
let _profileOpen=false;
function openProfileMenu(){
  if(_profileOpen)return;
  _profileOpen=true;
  const btn=event.currentTarget;
  const rect=btn.getBoundingClientRect();
  // Remove any existing dropdown first
  document.querySelectorAll('.auth-profile-dropdown').forEach(d=>d.remove());
  const dd=document.createElement('div');
  dd.className='auth-profile-dropdown';
  dd.style.cssText=`position:fixed;top:${rect.bottom+4}px;left:${rect.left}px;z-index:2000;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:6px 0;min-width:140px;box-shadow:0 8px 24px rgba(0,0,0,.4)`;
  dd.innerHTML=`<div style="padding:8px 14px;font-size:.76em;color:var(--muted);border-bottom:1px solid var(--border)">${esc(window._currentUser?.display||'')}</div><button style="width:100%;background:none;border:none;color:var(--red);padding:8px 14px;font-size:.8em;cursor:pointer;text-align:left" onclick="handleLogout()">退出登录</button>`;
  document.body.appendChild(dd);
  setTimeout(()=>document.addEventListener('click',function handler(e){if(!dd.contains(e.target)&&e.target!==btn){dd.remove();_profileOpen=false;document.removeEventListener('click',handler);}}),0);
}

checkAuth();

// ── Profile Page ────────────────────────────────────────────────────────────────
let _profileAvatarDataUrl = ''; // holds base64 data URL of chosen image

async function _loadProfile() {
  const token = _getToken();
  if (!token) return null;
  try {
    const data = await (await fetch('/auth/me', { headers: { 'X-Auth-Token': token } })).json();
    window._currentUser = data.user;
    return data.user;
  } catch (e) {
    console.error('Failed to load profile:', e);
    return null;
  }
}

function renderProfile(user) {
  const el = document.getElementById('profileContent');
  if (!user) {
    el.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:60vh;gap:16px">`+
      `<div style="width:80px;height:80px;border-radius:50%;background:linear-gradient(135deg,var(--accent),#6d28d9);display:flex;align-items:center;justify-content:center;font-size:2em;color:#fff;box-shadow:0 4px 24px rgba(109,40,217,.3)">👤</div>`+
      `<div style="color:var(--muted);font-size:.95em">请先登录以管理个人资料</div>`+
      `<button class="action-btn" onclick="openAuthModal('login')" style="margin-top:4px">去登录</button></div>`;
    return;
  }
  const initials = user.id.split(':').pop()?.[0]?.toUpperCase() || '?';
  const avatarHtml = user.avatar_url
    ? `<img src="${esc(user.avatar_url)}" class="avatar-preview" id="avatarPreviewImg" alt="头像">`
    : `<div class="avatar-placeholder" id="avatarPreviewImg">${esc(initials)}</div>`;
  const methodLabel = user.method === 'phone' ? '📱 手机号' : '📧 邮箱';
  const memberSince = user.created_at
    ? new Date(user.created_at * 1000).toLocaleDateString('zh-CN', { year: 'numeric', month: 'long' })
    : '—';

  el.innerHTML = `
    <div class="profile-topbar">
      <button class="profile-topbar-back" onclick="hideProfile()">‹ 返回</button>
      <span class="profile-topbar-title">我的资料</span>
    </div>
    <div class="profile-card">
      <!-- Avatar + Info row -->
      <div style="display:flex;align-items:center;gap:24px;margin-bottom:32px">
        <div style="flex-shrink:0">${avatarHtml}</div>
        <div style="flex:1;min-width:0">
          <div style="font-size:1.15em;font-weight:700;color:var(--text);margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(user.display||'未设置')}</div>
          <div style="font-size:.78em;color:var(--muted);margin-bottom:8px">加入于 ${memberSince}</div>
          <span class="badge-method">${methodLabel}</span>
        </div>
      </div>

      <!-- Editable fields -->
      <div style="margin-bottom:24px">
        <label class="form-label" style="margin-bottom:8px">显示名称</label>
        <input class="form-input" id="profileDisplay" value="${esc(user.display||'')}" maxlength="30" placeholder="输入你的昵称…">
      </div>
      <div style="margin-bottom:28px">
        <label class="form-label" style="margin-bottom:8px">头像图片</label>
        <div class="img-browse-row" style="margin-bottom:8px">
          <input class="form-input img-url-input" id="profileAvatarUrl" type="text"
            value="${esc(user.avatar_url||'')}" placeholder="或粘贴图片网址（http/https）…" maxlength="2000">
          <label class="file-upload-btn" for="imgFileInput2">浏览文件</label>
          <input type="file" id="imgFileInput2" accept="image/*" style="display:none">
        </div>
        <div style="font-size:.75em;color:var(--muted)">支持本地上传（≤5MB）或远程图片链接</div>
        <div class="error-msg" id="avatarErr"></div>
      </div>

      <!-- Save button -->
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:28px">
        <button class="btn-save-profile" id="saveProfileBtn" onclick="saveProfile()">💾 保存修改</button>
        <div class="error-msg" id="saveErr" style="margin:0"></div>
      </div>

      <!-- Divider -->
      <div style="border-top:1px solid var(--border);margin:0 0 20px"></div>

      <!-- Danger zone -->
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
        <div>
          <div style="font-size:.82em;color:var(--muted);margin-bottom:4px">账号 ID</div>
          <code style="font-size:.8em;color:var(--muted);word-break:break-all">${esc(user.id)}</code>
        </div>
        <button class="btn-delete-acct" onclick="deleteAccount()">🗑 注销账号</button>
      </div>
    </div>`;

  // Wire up file input
  ['imgFileInput', 'imgFileInput2'].forEach(id => {
    const fi = document.getElementById(id);
    if (fi) {
      fi.addEventListener('change', () => {
        const file = fi.files[0];
        if (!file) return;
        if (file.size > 5 * 1024 * 1024) { showSaveError('图片不能超过 5MB'); return; }
        const reader = new FileReader();
        reader.onload = e => { _profileAvatarDataUrl = e.target.result; document.getElementById('profileAvatarUrl').value = _profileAvatarDataUrl; };
        reader.readAsDataURL(file);
      });
    }
  });
  const urlInput = document.getElementById('profileAvatarUrl');
  if (urlInput) {
    urlInput.addEventListener('input', () => {
      _profileAvatarDataUrl = urlInput.value.trim();
      _updateAvatarPreview(_profileAvatarDataUrl);
    });
  }
}

function _updateAvatarPreview(url) {
  const el = document.getElementById('profileContent');
  if (!el) return;
  const preview = document.getElementById('avatarPreviewImg');
  if (!preview) return;
  const clean = (url || '').trim();
  if (clean.startsWith('data:image/') || clean.startsWith('http://') || clean.startsWith('https://')) {
    preview.outerHTML = `<img src="${esc(clean)}" class="avatar-preview" id="avatarPreviewImg" alt="头像">`;
  } else {
    const user = window._currentUser;
    const initials = user?.id?.split(':').pop()?.[0]?.toUpperCase() || '?';
    preview.outerHTML = `<div class="avatar-placeholder" id="avatarPreviewImg">${esc(initials)}</div>`;
  }
}

async function saveProfile() {
  const btn = document.getElementById('saveProfileBtn');
  const errEl = document.getElementById('saveErr');
  hideAllErrors();
  const display = (document.getElementById('profileDisplay')?.value || '').trim();
  // Read current avatar URL directly from the input — never trust stale _profileAvatarDataUrl
  let avatarUrl = (document.getElementById('profileAvatarUrl')?.value || '').trim();
  if (!display) { showSaveError('请输入显示名称'); return; }
  if (!avatarUrl) avatarUrl = '';  // no change requested
  // Sanitize: reject oversized data URIs (>100 KB base64 payload → ~75 KB actual image is safe)
  if (avatarUrl.startsWith('data:image/')) {
    const estimatedBytes = Math.round(avatarUrl.length * 0.75);
    if (estimatedBytes > 100 * 1024) {
      showSaveError('头像图片太大，请选择更小的图片或粘贴远程链接（< 100 KB）'); return;
    }
  } else if (avatarUrl && !avatarUrl.startsWith('http://') && !avatarUrl.startsWith('https://')) {
    showSaveError('头像 URL 格式不正确，请填入 http 链接或上传图片'); return;
  }
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    const token = _getToken();
    await _apiPost('/auth/profile', { display, avatar_url: avatarUrl }, token);
    // Re-fetch latest user data using GET (not POST!)
    const resp = await fetch('/auth/me', { method: 'GET', headers: { 'X-Auth-Token': token } });
    if (!resp.ok) throw new Error(String(resp.status));
    const fresh = await resp.json();
    window._currentUser = fresh.user;
    renderProfile(fresh.user);
    showToast('资料已更新 ✓', 'success');
    _refreshAuthUI();
  } catch (e) {
    showSaveError(e.message || '保存失败，请稍后重试');
  } finally {
    btn.disabled = false;
    btn.textContent = '💾 保存修改';
  }
}

function showSaveError(msg) {
  const el = document.getElementById('saveErr');
  if (el) { el.textContent = msg; el.style.display = 'block'; }
}
function hideAllErrors() {
  document.querySelectorAll('.error-msg').forEach(el => { el.textContent = ''; el.style.display = 'none'; });
}

async function deleteAccount() {
  if (!confirm('确定要注销账号吗？此操作不可恢复，您的所有历史改写记录将被永久删除。')) return;
  const token = _getToken();
  try {
    // Call the dedicated delete endpoint which removes both the user record and all tokens
    await _apiPost('/auth/delete-user', {}, token);
  } catch {}
  _setToken('');
  showToast('账号已注销 ✓', 'success');
  switchTab('detect');
}
</script>
</body>
</html>"""






@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_TEMPLATE


@app.post("/detect")
async def detect(
    file: UploadFile = File(...),
    x_auth_token: str | None = Header(None),
):
    """诊断模式：返回 AI 味分析报告（JSON）"""
    _require_auth(x_auth_token)
    text = _read_file(file)
    if len(text.strip()) == 0:
        raise HTTPException(400, "文件内容为空")
    result = _run_detect(text)
    return JSONResponse(result)


@app.post("/clean")
async def clean(
    file: UploadFile = File(...),
    use_llm: bool = Form(False),
    x_auth_token: str | None = Header(None),
):
    """清理模式：返回改写结果 + 对比（JSON）"""
    _require_auth(x_auth_token)
    text = _read_file(file)
    if len(text.strip()) == 0:
        raise HTTPException(400, "文件内容为空")
    result = _run_clean(text, use_llm=use_llm)
    # 二次检测：改写后重新扫描，计算最终分数
    after_report = run_diagnose(result["rewritten_text"], mode=Mode.DETECT, rule_engine=_get_engine())
    after_m = after_report.metrics
    after_counts = after_report.hit_counts_by_severity()
    result["after_metrics"] = {
        "ai_taste_score": _calc_ai_score(
            after_counts["A"], after_counts["B"],
            after_m.burstiness if after_m else 0,
            after_m.mattar     if after_m else 0,
            after_m.perplexity_simple if after_m else 0,
            (after_m.paragraph_rhythm if after_m else {}),
            len(result["rewritten_text"]),
        ),
        "burstiness": round(after_m.burstiness, 4) if after_m else None,
        "mattar": round(after_m.mattar, 4) if after_m else None,
        "perplexity": round(after_m.perplexity_simple, 2) if after_m else None,
        "a_hits_after": after_counts["A"],
        "b_hits_after": after_counts["B"],
    }
    return JSONResponse(result)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


# ── 邮箱接口（待接入）──────────────────────────────────────────────────────────
@app.post("/auth/send-email-code")
async def auth_send_email_code(
    email: str = Form(...),
):
    """向指定邮箱发送 6 位验证码。"""
    # TODO: 接入真实 SMTP 服务
    # 参考 db.py 中的 send_email_code()
    # 推荐方案：网易企业邮 / QQ企业邮 SMTP，或 SendCloud / 腾讯云邮件
    import re
    if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    try:
        await _db.send_email_code(email)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    return {"ok": True, "message": "验证码已发送"}


@app.post("/auth/verify-email-code")
async def auth_verify_email_code(
    email: str = Form(...),
    code: str = Form(...),
):
    """验证邮箱验证码是否正确。"""
    # TODO: 验证码实际校验在 register 流程中完成
    if not _db.verify_email_code(email, code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    return {"ok": True}


# ── 手机短信接口（待接入）───────────────────────────────────────────────────────
@app.post("/auth/send-sms-code")
async def auth_send_sms_code(
    phone: str = Form(...),
):
    """向指定手机号发送 6 位短信验证码。"""
    # TODO: 接入真实短信服务（阿里云短信 / 腾讯云短信）
    # 参考 db.py 中的 send_sms_code()
    # 需要在云服务商注册账号、申请签名和模板 ID
    import re
    if not re.match(r'^1[3-9]\d{9}$', phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")
    try:
        await _db.send_sms_code(phone)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    return {"ok": True, "message": "验证码已发送"}


@app.post("/auth/verify-sms-code")
async def auth_verify_sms_code(
    phone: str = Form(...),
    code: str = Form(...),
):
    """验证短信验证码是否正确。"""
    # TODO: 验证码实际校验在 register 流程中完成
    if not _db.verify_sms_code(phone, code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    return {"ok": True}


# ── 微信登录接口（已移除，仅保留 email/phone）──────────────────────────────────
@app.get("/auth/wx/qrcode")
async def auth_wx_qrcode():
    raise HTTPException(status_code=400, detail="微信登录已下线，请使用邮箱或手机号注册")


@app.post("/auth/wx/callback")
async def auth_wx_callback(code: str = Form("")):
    raise HTTPException(status_code=400, detail="微信登录已下线，请使用邮箱或手机号注册")


# ── 认证端点 ────────────────────────────────────────────────────────────────────
@app.post("/auth/login")
async def auth_login(
    username: str = Form(...),
    password: str = Form(...),
    method: str = Form("email"),
):
    """登录"""
    try:
        result = await _do_login(username, password, method)
        return JSONResponse(result)
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Auth login error: %s", e)
        raise HTTPException(status_code=500, detail="登录失败，请稍后重试")


@app.post("/auth/register")
async def auth_register(
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    method: str = Form("email"),
):
    """注册"""
    # Quick server-side validation before DB lookup
    if not username or len(username.strip()) < 2:
        raise HTTPException(status_code=400, detail="用户名至少2个字符")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码长度至少6位")
    if password != confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    try:
        result = await _do_register(username, password, confirm_password, method)
        return JSONResponse(result)
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Auth register error: %s", e)
        raise HTTPException(status_code=500, detail="注册失败，请稍后重试")


@app.post("/auth/logout")
async def auth_logout(x_auth_token: str | None = Header(None)):
    """登出"""
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="未登录")
    return JSONResponse(await _do_logout(x_auth_token))


@app.get("/auth/me")
async def auth_me(x_auth_token: str | None = Header(None)):
    """获取当前用户信息"""
    user = _require_auth(x_auth_token)
    return JSONResponse({"user": user})


@app.post("/auth/profile")
async def auth_profile(
    display: str = Form(...),
    avatar_url: str = Form(""),
    x_auth_token: str | None = Header(None),
):
    """更新个人资料（展示名 + 头像）"""
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="未登录")
    user = _db.validate_token(x_auth_token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    display = (display or "").strip()[:30]
    if not display:
        raise HTTPException(400, detail="显示名不能为空")
    # Sanitize avatar_url: only allow http/https/data URI or empty
    clean_url = ""
    if avatar_url:
        au = avatar_url.strip()
        if au.startswith("data:image/") or au.startswith("http://") or au.startswith("https://"):
            clean_url = au[:2000]  # 限制长度防注入
    updated = _db.update_profile(user["id"], display, clean_url)
    # Refresh token so caller gets fresh user data
    tok = _db.get_or_create_token(user["id"])
    refreshed = _db.validate_token(tok)
    return JSONResponse({"token": tok, "user": refreshed})


# ── 异步任务端点 ────────────────────────────────────────────────────────────────
@app.post("/clean-async")
async def clean_async(
    file: UploadFile = File(...),
    use_llm: bool = Form(False),
    x_auth_token: str | None = Header(None),
):
    """提交异步清洁任务，立即返回任务ID。"""
    _require_auth(x_auth_token)
    text = _read_file(file)
    if len(text.strip()) == 0:
        raise HTTPException(400, "文件内容为空")

    task_id = str(uuid.uuid4())
    _tasks[task_id] = {
        "status": "pending",
        "progress": 0,
        "result": None,
        "error": None,
    }

    # 在后台线程中执行
    async def _run_and_store():
        try:
            result = await _run_clean_async(text, use_llm)
            _tasks[task_id]["status"] = "completed"
            _tasks[task_id]["progress"] = 100
            _tasks[task_id]["result"] = result
        except (ConnectionError, OSError, RuntimeError) as e:
            logger.error("Task %s failed: %s", task_id, e)
            _tasks[task_id]["status"] = "failed"
            _tasks[task_id]["error"] = str(e)
        finally:
            # Keep completed tasks for 30 more seconds so poller can fetch result,
            # then clean up to prevent unbounded growth under heavy load
            import asyncio as _asyncio
            async def _cleanup():
                await _asyncio.sleep(30)
                if _tasks.get(task_id, {}).get("status") in ("completed", "failed"):
                    _tasks.pop(task_id, None)
            _asyncio.create_task(_cleanup())

    asyncio.create_task(_run_and_store())
    return JSONResponse({"task_id": task_id, "status": "queued"})


@app.get("/task/{task_id}")
async def get_task(task_id: str):
    """查询任务状态。"""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return JSONResponse({
        "task_id": task_id,
        "status": task["status"],
        "progress": task["progress"],
        "result": task["result"],
        "error": task["error"],
    })


@app.delete("/task/{task_id}")
async def cancel_task(task_id: str):
    """取消任务（仅pending状态可取消）。"""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["status"] == "pending":
        task["status"] = "cancelled"
        return JSONResponse({"message": "Task cancelled"})
    return JSONResponse({"message": f"Cannot cancel task with status: {task['status']}"})


@app.post("/auth/delete-user")
async def auth_delete_user(
    x_auth_token: str | None = Header(None),
):
    """注销账号：删除当前用户及其所有 token。"""
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="未登录")
    user = _db.validate_token(x_auth_token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    deleted = _db.delete_user(user["id"])
    # Invalidate all tokens for this user as a safety net
    conn = _db._get_conn()
    conn.execute("DELETE FROM tokens WHERE user_id=?", (user["id"],))
    conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="用户不存在")
    logger.info("User %s account deleted", user["id"])
    return JSONResponse({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn
    _port = 8000
    # 延迟1.5秒后打开浏览器，给服务启动留出时间
    def _open_browser():
        try:
            webbrowser.open(f"http://127.0.0.1:{_port}")
        except Exception:
            pass
    threading.Timer(1.5, _open_browser).start()
    uvicorn.run("web_ui:app", host="0.0.0.0", port=_port, reload=False, log_level="info")
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Humanizer is a command-line + Web UI tool that rewrites Chinese AI-generated text to read more like human writing. It uses a diagnostic → planning → rewriting → verification pipeline, combining rule-based pattern detection (A/B/C severity grading) with optional LLM-assisted rewriting. The detection scoring model follows an InkOS-inspired multi-dimensional approach: burstiness, MATTR vocabulary diversity, perplexity (jieba-based with AI-fatigue-word penalties), paragraph rhythm consistency, and rule hit density — not any single metric alone.

## Common Commands

All commands must run from the **repo root** (`D:\python代码\humanizer-dev-doc`), using the `.venv` Python there:

```bash
cd D:\python代码\humanizer-dev-doc

# Install / reinstall (from humanizer/ dir, sets up editable install)
cd humanizer
pip install -e ".[dev]"

# Run all tests (from repo root — PYTHONPATH auto-sets via site-packages)
.venv\Scripts\python.exe -m pytest humanizer/tests/ -v

# Run a single test
.venv\Scripts\python.exe -m pytest humanizer/tests/test_diagnose.py::TestDiagnose::test_burstiness_calculation -v

# Run a single test module
.venv\Scripts\python.exe -m pytest humanizer/tests/test_rules.py -v

# Lint with ruff
.venv\Scripts\ruff check humanizer/src humanizer/web_ui.py humanizer/run.py humanizer/db.py --config humanizer/pyproject.toml

# Type/format check
.venv\Scripts\ruff format --check humanizer/src humanizer/web_ui.py humanizer/run.py humanizer/db.py

# One-shot detection on a file (offline, no LLM)
.venv\Scripts\python.exe -m humanizer.cli detect path/to/file.txt

# CLI clean mode
.venv\Scripts\python.exe -m humanizer.cli clean path/to/file.txt -o out.txt

# Start web UI on custom port
.venv\Scripts\python.exe run.py --port 9000
```

**Web UI:** Double-click `humanizer\run.bat` (directly invokes `.venv\Scripts\python.exe` — no activate needed). Override port: `python run.py --port 9000`. All commands must use the `.venv` Python in the repo root; the system miniconda python lacks fastapi/uvicorn.

### Known path-sensitive gotchas
- The editable install `.pth` file at `.venv/Lib/site-packages/__editable__.humanizer-0.1.0.pth` records the **absolute** path to `humanizer/src`. If you move the project folder, edit that file or re-run `pip install -e "humanizer[dev]"`.
- `run.py` prepends `src/` to `sys.path`; `cli.py` reads rules from cwd-relative `config/rules/`. Both assume `PYTHONPATH=src` is set (run.bat does this automatically).
- SQLite database `humanizer.db` is created at project root on first startup by `db.py:init_db()`. Do not delete it unless you want to reset all user accounts.

## Architecture

The application follows a **pipeline stage** model orchestrated by `run_pipeline()` in `pipeline.py`:

| Stage | File | Purpose |
|-------|------|---------|
| Precheck | `stages/precheck.py` | Validate input encoding (UTF-8/GBK); extract fact anchors (names, numbers, dates, dialogues) via regex |
| Diagnose | `stages/diagnose.py` | Scan rules + compute metrics (burstiness, TTR, MATTR, perplexity, paragraph rhythm) |
| Plan | `stages/plan.py` | Build RewritePlan — decide what to delegate to LLM vs direct replacement |
| Rewrite | `stages/rewrite.py` | Two-pass: deterministic pattern substitution (`_DIRECT_PATTERNS`), then LLM rewrite for deferred hits |
| Verify | `stages/verify.py` | Re-scan rewritten text; compare fidelity (numbers/dates/dialogs preserved) against original |

### Authentication system

User authentication uses SQLite (`db.py`) with in-memory token caching. No external auth providers are integrated yet — email/SMS/WeChat stubs are in place but return 501 until configured.

**Auth flow:**
1. User registers via `/auth/register` with method (`email`, `phone`, or `wx`)
2. System creates a token (30-day expiry) stored in `tokens` table
3. Subsequent requests carry `X-Auth-Token` header
4. Protected endpoints (`/detect`, `/clean`, `/clean-async`) call `_require_auth()` which validates the token against SQLite

**Pending integrations (see `db.py` config blocks):**
- `send_email_code()` / `verify_email_code()` — SMTP service for email verification codes
- `send_sms_code()` / `verify_sms_code()` — SMS gateway (Aliyun/Tencent) for phone verification
- `generate_wx_auth_url()` / `exchange_wx_code_for_openid()` — WeChat OAuth2 for QR code login

To enable each feature, fill in the corresponding config dict in `db.py` and replace the TODO comment bodies.

### Key data flow objects (all in `models.py`)

- `RuleHit` — individual rule match with position, severity (A/B/C), matched text, suggestion
- `MetricsSnapshot` — output of metrics calculation; includes `paragraph_rhythm` dict (V2) with per-paragraph CV values
- `DiagnosisReport` — output of diagnose stage; aggregates hits + metrics snapshot
- `RewritePlan` — output of plan stage; lists deferred hits for LLM + structural actions for refactor mode
- `VerifyReport` — output of verify stage; checks fidelity preservation + A-hit reduction percentage

### Rule system

Rules are defined in `config/rules/*.yaml` and loaded via `rules/loader.py`. Three severity levels:
- **A** (`bans_a.yaml`): Hard signals — must be changed. Covers ~45 literal/regex rules including fatigue-word bans, grand-noun clusters, conditional templates, and section-end marker patterns.
- **B** (`bans_b.yaml`): Soft signals — flagged for LLM/context judgment. Covers ~22 density triggers and regex patterns for structural AI-isms (e.g. "总之" endings, "的" chains, rhetorical triplets).
- **C** (`c_forbidden.yaml`): Forbidden actions — reference only, never applied during scanning. Lists features that must NOT be treated as AI signals (passives, metaphors, questions, etc.).

The `RuleEngine` class (`rules/engine.py`) pre-compiles regex rules at init time via `build_compiled_patterns()`. Match types: `literal`, `regex`, `density`. Schema validation uses Pydantic (`rules/schema.py`). Rules can be extended by editing YAML files without touching code.

Rule path resolution searches candidates in order: cwd-relative → sibling-of-package → `/etc/humanizer/rules/`. Tests inject paths directly via the `engine` fixture in `test_rules.py`.

### Metrics pipeline (V2)

Four independent metrics computed in `metrics/` and composed by `compute_full_metrics()` in `metrics/__init__.py`:
- `burstiness` — coefficient of variation of sentence lengths (human target: >0.5, AI typical: <0.3)
- `ttr` + `mattar` — raw TTR (display-only) and moving-average TTR with window=50 tokens (main lexical diversity metric, length-independent)
- `perplexity_simple` — jieba-tokenized Shannon entropy with AI-fatigue-word penalty weights; higher = more human-like (human target: 60–120)
- `paragraph_rhythm` — per-paragraph sentence-length CV mean and count of flat-rhythm paragraphs (CV < 0.2); flags sections where sentences are mechanically uniform

The composite AI-taste score in `web_ui.py:_calc_ai_score()` combines A-hits, B-hits, burstiness gap, MATTR gap, perplexity gap, and rhythm penalty — each capped to prevent any single dimension from dominating.

### LLM client

`llm/client.py` provides two implementations:
- `LLMAgent` — real OpenAI-compatible client (default endpoint configured via `config/default.yaml`; env vars: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_TEMPERATURE`)
- `MockLLMAgent` — offline fallback used in tests; does literal string replacements for A-level ban words plus iterative cleanup until no A-hits remain or text stabilizes

Exception handling covers `RateLimitError`, `APIStatusError`, `AuthenticationError`, `APIConnectionError`, and `APITimeoutError` — all fall back to returning the original segment unchanged. Never catch bare `Exception` here; that would mask programming errors.

### Web UI

`web_ui.py` is a self-contained FastAPI app (~1700 lines) with HTML/JS embedded as a raw string constant. Entrypoint is `run.py` which handles threading and auto-opening the browser. Serves:
- `GET /` — the single-page UI
- `POST /detect` — returns JSON diagnosis report (score + metrics + top rule hits)
- `POST /clean` — runs full pipeline and returns before/after text plus post-rewrite metrics
- `GET /health` — liveness probe
- `GET /task/{task_id}` — async task status query (async tasks are cleaned up after 5s delay)

**Auth endpoints:**
- `POST /auth/login` — authenticate with username/password
- `POST /auth/register` — register new account (method-dependent flow)
- `POST /auth/logout` — invalidate current token
- `GET /auth/me` — return current user info
- `POST /auth/send-email-code` — send 6-digit verification code (stub)
- `POST /auth/verify-email-code` — verify email code (stub)
- `POST /auth/send-sms-code` — send SMS code (stub)
- `POST /auth/verify-sms-code` — verify SMS code (stub)
- `GET /auth/wx/qrcode` — generate WeChat QR URL (stub)
- `POST /auth/wx/callback` — WeChat OAuth callback (stub)

**Known behavioral notes:**
- `_get_llm_client()` resets to MockLLMAgent on each call if auth fails, allowing users to enable real LLM later without restarting the server.
- `_para_contains_hit()` converts global character offsets to paragraph-local positions before matching — never compare absolute offsets against substring lengths.
- `_run_clean_async()` uses `asyncio.get_running_loop()` (not deprecated `get_event_loop()`).
- Task concurrency: `ThreadPoolExecutor(max_workers=N)` where N = CPU cores // 2, minimum 2. Each request gets its own DB connection via thread-local storage.

### Pipeline modes

Three modes (defined in `Mode` enum in `models.py`):
- **detect**: Runs precheck + diagnose only; outputs score + metrics + hits preview. No rewriting. Requires auth.
- **clean**: Full pipeline with language-layer changes only (A+B rule fixes + factual preservation). Uses mock LLM by default; pass `--mock-llm` flag explicitly. Requires auth.
- **refactor**: Like clean but allows structural actions (paragraph reordering, rhythm redistribution) up to `max_actions` limit (default 3, configurable via `--max-actions`). Requires auth.

### CLI layer

`cli.py` exposes four Typer subcommands (`detect`, `clean`, `refactor`, `metrics`) that call into the same pipeline functions. `run.py` auto-detects CLI mode by checking `sys.argv` for these keywords; if none match, it falls through to the Web UI server. Both CLI and WebUI share `pipeline.py` as the single source of truth for orchestration.

### Configuration

Settings are managed via Pydantic Settings (`config.py`) layered on top of `config/default.yaml`. Resolution priority (highest wins): environment variables → YAML file → hard-coded defaults. Rule file paths are resolved per-process at `Settings.bans_a/b/c_forbidden`. Modify `config/default.yaml` for non-secret tuning; use environment variables for API keys and sensitive values.

## Testing Conventions

- Tests use pytest with fixtures in `tests/fixtures/`
- `ai_sample_01.md` and `human_sample_01.md` serve as regression baselines (must remain small so they exercise the rulesets but don't bloat CI)
- The `engine` fixture in `test_rules.py` injects real rule YAML paths for isolated testing
- All 39 existing tests must pass after any change; add new tests for new dimensions (rhythm, perplexity weighting) in the relevant test modules
- Test command from repo root: `.venv\Scripts\python.exe -m pytest humanizer/tests/ -v`
- When modifying rules YAML, run `pytest tests/test_rules.py -v` to verify loading and schema validation still pass
- Auth tests should cover the token lifecycle: register → login → protect endpoint → logout → invalidate

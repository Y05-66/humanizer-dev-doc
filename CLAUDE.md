# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Humanizer is a command-line + Web UI tool that rewrites Chinese AI-generated text to read more like human writing. It uses a diagnostic → planning → rewriting → verification pipeline, combining rule-based pattern detection (A/B/C severity grading) with optional LLM-assisted rewriting. The detection scoring model follows an InkOS-inspired multi-dimensional approach: burstiness, MATTR vocabulary diversity, perplexity (jieba-based with AI-fatigue-word penalties), paragraph rhythm consistency, and rule hit density — not any single metric alone.

**Repo layout:** the Python package lives inside the `humanizer/` subdirectory; commands below are run from the **repo root**.

## Common Commands

```bash
cd D:\Python项目\humanizer-dev-doc

# Install / reinstall (from humanizer/ dir, sets up editable install)
cd humanizer
pip install -e ".[dev]"

# Run all tests (39 tests)
.venv\Scripts\python.exe -m pytest humanizer/tests/ -v

# Run a single test
.venv\Scripts\python.exe -m pytest humanizer/tests/test_diagnose.py::TestDiagnose::test_burstness_calculation -v

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
.venv\Scripts\python.exe humanizer/run.py --port 9000
```

**Web UI (Windows):** double-click `humanizer\run.bat`. It runs `.venv\Scripts\python.exe humanizer\run.py` with `PYTHONPATH=humanizer/src` set automatically. All commands must use the `.venv` Python — the system miniconda python lacks fastapi/uvicorn.

### Known path-sensitive gotchas
- The editable-install `.pth` at `.venv/Lib/site-packages/__editable__.humanizer-0.1.0.pth` records the **absolute** path to `humanizer/src`. If you move the project folder, edit that file or re-run `pip install -e "humanizer[dev]"`.
- `run.py` prepends `src/` to `sys.path`; `cli.py` reads rules from cwd-relative `config/rules/`. Both assume `PYTHONPATH=src` is set (run.bat does this automatically).
- SQLite database `humanizer.db` is created at project root on first startup by `db.py:init_db()`. Do not delete it unless you want to reset all user accounts.

## Architecture

The application follows a **pipeline stage** model orchestrated by `run_pipeline()` in `humanizer/src/humanizer/pipeline.py`:

| Stage | File | Purpose |
|-------|------|---------|
| Precheck | `stages/precheck.py` | Validate input encoding (UTF-8/GBK); extract fact anchors (names, numbers, dates, dialogues) via regex |
| Diagnose | `stages/diagnose.py` | Scan rules + compute metrics (burstiness, TTR, MATTR, perplexity, paragraph rhythm) |
| Plan | `stages/plan.py` | Build RewritePlan — decide what to delegate to LLM vs direct replacement |
| Rewrite | `stages/rewrite.py` | Two-pass: deterministic pattern substitution (`_DIRECT_PATTERNS`), then LLM rewrite for deferred hits |
| Verify | `stages/verify.py` | Re-scan rewritten text; compare fidelity (numbers/dates/dialogs preserved) against original |

### Authentication system

User authentication uses SQLite (`db.py`) with in-memory token caching. No external auth providers are integrated yet — email/SMS stubs are in place but return 501 until configured. WeChat login is deprecated (raises `RuntimeError`).

The `users` table carries `avatar_url` (SVG data URI or http(s) URL, default empty string). `validate_token()` always returns `avatar_url` alongside id/display/method so the frontend can render the header avatar without an extra query. Profile updates go through `POST /auth/profile` which calls `db.update_profile(uid, display, avatar_url)`.

**Auth flow:**
1. User registers via `/auth/register` with method (`email`, `phone`)
2. System creates a token (30-day expiry) stored in `tokens` table
3. Subsequent requests carry `X-Auth-Token` header
4. Protected endpoints (`/detect`, `/clean`, `/clean-async`, `/auth/profile`) call `_require_auth()` which validates the token against SQLite

**Critical frontend auth requirement:** Every `fetch()` call to protected endpoints must explicitly pass the auth token via `headers:{'X-Auth-Token':_getToken()}`. The helpers `apiDetect()` and `apiClean()` already do this. Never omit the header when adding new authenticated endpoints.

To enable email/phone verification, fill in the corresponding config dict in `db.py` and replace the TODO comment bodies.

**DB migration safety:** All schema changes in `init_db()` are wrapped in try/except around ALTER TABLE — calling `init_db()` twice never crashes from duplicate columns.

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

`web_ui.py` is a self-contained FastAPI app (~2280 lines) with HTML/JS embedded as a raw string constant. Entrypoint is `run.py` which handles threading and auto-opening the browser. Serves:
- `GET /` — the single-page UI (tabs: 检测分析 / 去AI味改写 / 我的)
- `POST /detect` — returns JSON diagnosis report (score + metrics + top rule hits)
- `POST /clean` — runs full pipeline and returns before/after text plus post-rewrite metrics
- `GET /health` — liveness probe
- `POST /clean-async` — async pipeline submission, returns task_id immediately
- `GET /task/{task_id}` — poll for async task status/result
- `DELETE /task/{task_id}` — cancel a pending async task (no-op if already completed)
- Auth endpoints: `POST /auth/login`, `/register`, `/logout`; `GET /auth/me`; `POST /auth/profile`
- Stub endpoints: `/auth/send-email-code`, `/verify-email-code`, `/send-sms-code`, `/verify-sms-code`, `/wx/qrcode`, `/wx/callback` — all return 501 until configured

The **"我的"** section is a **full-screen overlay** (`#profileOverlay`, toggled by JS `showProfile()`/`hideProfile()`, not a tab). It renders client-side via `renderProfile(user)`. It shows an avatar circle (image or initials fallback), editable display name, avatar URL/file input, registration method, member since date, account ID read-only, and a delete-account button. Local image uploads are read as base64 data URLs; remote URLs are validated against `data:image/`, `http://`, `https://` prefixes only. Avatar changes are previewed live on URL input. **Important:** `_profileAvatarDataUrl` must NOT be used in `saveProfile()` — always read directly from `#profileAvatarUrl` input element to avoid sending stale large base64 payloads exceeding the 1024 KB server limit. The header auth button reflects the avatar automatically via `_refreshAuthUI()`.

**Known behavioral notes:**
- `_get_llm_client()` resets to MockLLMAgent on each call if auth fails, allowing users to enable real LLM later without restarting the server.
- `_para_contains_hit()` converts global character offsets to paragraph-local positions before matching — never compare absolute offsets against substring lengths.
- `_run_clean_async()` uses `asyncio.get_running_loop()` (not deprecated `get_event_loop()`). Completed tasks are kept in `_tasks` dict for 5 seconds for polling clients, then evicted via an asyncio background task.
- Task concurrency: `ThreadPoolExecutor(max_workers=N)` where N = CPU cores // 2, minimum 2. Each request gets its own DB connection via thread-local storage.
- `switchTab('detect')` hides the about-section automatically via CSS (it's nested inside `#panel-detect`).
- `renderClean(data, el)` replaces the entire innerHTML of the result area; always update both `#origBoxAlways` and `#rewrittenBoxAlways` textareas, and assign `window._dlTxt/_dlMd/_dlDocx` for download buttons.
- Auth success requires setting `window._currentUser=data.user` AND calling `_setToken(token)` — neither alone updates the UI button.
- **`GET /auth/me` must use `fetch(..., {method:'GET'})`, never `_apiPost`** — `_apiPost` sends multipart/form-data which triggers FastAPI's 1024 KB body-size limit on `Form` fields (caused by old base64 avatar data lingering in `_profileAvatarDataUrl`). Use direct `fetch` with headers instead.
- **Base64 data URL size guard:** `_apiPost` for `/auth/profile` rejects data URIs whose decoded payload exceeds ~100 KB (estimated as `url.length * 0.75 > 100*1024`) before sending, preventing 400 errors from uvicorn's body-size limit.

### Pipeline modes

Three modes (defined in `Mode` enum in `models.py`):
- **detect**: Runs precheck + diagnose only; outputs score + metrics + hits preview. No rewriting. Requires auth.
- **clean**: Full pipeline with language-layer changes only (A+B rule fixes + factual preservation). Attempts real LLM first; falls back to MockLLMAgent on connection failure. Pass `--mock-llm` to force offline mode. Requires auth.
- **refactor**: Like clean but allows structural actions (paragraph reordering, rhythm redistribution) up to `max_actions` limit (default 3, configurable via `--max-actions`). Requires auth.

### CLI entrypoint

`cli.py` exposes four Typer subcommands (`detect`, `clean`, `refactor`, `metrics`) that call into the same pipeline functions. `run.py` auto-detects CLI mode by checking `sys.argv` for these keywords; if none match, it falls through to the Web UI server. Both CLI and Web UI share `pipeline.py` as the single source of truth for orchestration.

### Configuration

Settings are managed via Pydantic Settings (`config.py`) layered on top of `config/default.yaml`. Resolution priority (highest wins): environment variables → YAML file → hard-coded defaults. Rule file paths are resolved per-process at `Settings.bans_a/b/c_forbidden`. Modify `config/default.yaml` for non-secret tuning; use environment variables for API keys and sensitive values.

## Testing Conventions

- Tests use pytest with fixtures in `tests/fixtures/`
- `ai_sample_01.md` (AI text input) / `human_sample_01.md` (human text baseline) are regression test corpora — keep them small so they exercise rulesets without bloating CI
- `ai_sample_01_clean.md` is a hand-edited "clean" reference version of `ai_sample_01.md`; `expected_bans.yaml` lists expected A-hit rule IDs and matched-text fragments for regression verification — do not delete these
- The `engine` fixture in `test_rules.py` injects real rule YAML paths for isolated testing
- All 39 existing tests must pass after any change; add new tests for new dimensions (rhythm, perplexity weighting) in the relevant test modules
- Test command from repo root: `.venv\Scripts\python.exe -m pytest humanizer/tests/ -v`
- When modifying rules YAML, run `pytest tests/test_rules.py -v` to verify loading and schema validation still pass
- Auth tests should cover the token lifecycle: register → login → protect endpoint → logout → invalidate

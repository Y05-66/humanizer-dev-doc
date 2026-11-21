# ── Humanizer Packaging Spec ────────────────────────────────────────────────
"""PyInstaller spec — builds three targets:
    • humanizer.exe        CLI tool (detect/clean/refactor/metrics)
    • humanizer-web.exe   Web UI server (port 8000, GUI window hidden)
    • HumanizerSetup.msi   Windows installer (via NSIS/wix; optional)

Run from repo root:
    pyinstaller --noconfirm build.spec

Run from humanizer/ subdir (as used by CI):
    cd humanizer && python -m PyInstaller --noconfirm ../build.spec
"""

import sys
from pathlib import Path
from PyInstaller.building.build_main import Analysis, PYZ, EXE

_repo_root_candidate = Path(__file__).resolve().parents[1] if '__file__' in dir() else Path.cwd()
if _repo_root_candidate.name != "humanizer":
    # Running from repo root: /repo/build.spec → parent is /repo
    REPO_ROOT = _repo_root_candidate
else:
    # Running from humanizer/ subdir: /repo/humanizer/build.spec → grandparent is /repo
    REPO_ROOT = _repo_root_candidate.parent

pkg  = REPO_ROOT / "humanizer"
src  = pkg / "src"

assert src.is_dir(), (
    f"Cannot locate humanizer/src at {src}. "
    f"Run PyInstaller from repo root with 'pyinstaller --noconfirm build.spec'"
)

HIDDEN_IMPORTS = [
    # Package internals
    "humanizer.cli",
    "humanizer.config",
    "humanizer.io",
    "humanizer.llm.client",
    "humanizer.metrics",
    "humanizer.models",
    "humanizer.pipeline",
    "humanizer.rules.engine",
    "humanizer.rules.loader",
    "humanizer.rules.schema",
    "humanizer.stages.diagnose",
    "humanizer.stages.plan",
    "humanizer.stages.precheck",
    "humanizer.stages.rewrite",
    "humanizer.stages.verify",
    # Third-party deps that PyInstaller may miss
    "fastapi",
    "uvicorn",
    "starlette",
    "openai",
    "httpx",
    "certifi",
    "regex",
    "rich",
    "pydantic",
    "pydantic_settings",
    "jieba",
]

EXCLUDE_IMPORTS = [
    "matplotlib",
    "tkinter",
    "pytest",
    "ruff",
    "torch",
    "transformers",
    "jinja2",
]

# Data files to bundle alongside executables
# In frozen mode, these end up in {exe_dir}/assets/
ASSETS = [
    # Rule YAML files
    (str(pkg / "config" / "rules" / "bans_a.yaml"),          "humanizer/config/rules"),
    (str(pkg / "config" / "rules" / "bans_b.yaml"),          "humanizer/config/rules"),
    (str(pkg / "config" / "rules" / "c_forbidden.yaml"),     "humanizer/config/rules"),
    (str(pkg / "config" / "default.yaml"),                   "humanizer/config"),
    # Test fixtures (optional, useful for debugging)
    (str(pkg / "tests" / "fixtures" / "ai_sample_01.md"),           "humanizer/tests/fixtures"),
    (str(pkg / "tests" / "fixtures" / "ai_sample_01_clean.md"),     "humanizer/tests/fixtures"),
    (str(pkg / "tests" / "fixtures" / "human_sample_01.md"),        "humanizer/tests/fixtures"),
    (str(pkg / "tests" / "fixtures" / "expected_bans.yaml"),        "humanizer/tests/fixtures"),
    # Documentation
    (str(REPO_ROOT / "README.md"), "."),
    (str(REPO_ROOT / "LICENSE"),   "."),
]

if (REPO_ROOT / ".env.example").exists():
    ASSETS.append((str(REPO_ROOT / ".env.example"), "."))


def _make_analysis(entry: str, name: str, console: bool) -> tuple[Analysis, EXE]:
    """Create Analysis + EXE pair for either CLI or web UI."""
    analysis = Analysis(
        [entry],
        pathex=[str(src), str(pkg)],
        binaries=[],
        datas=ASSETS,
        hiddenimports=HIDDEN_IMPORTS,
        excludes=EXCLUDE_IMPORTS,
        noarchive=False,
    )
    exe = EXE(
        PYZ(analysis.pure),
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        name=name,
        console=console,
        strip=False,
        upx=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch="cpu",
        codesign_identity=None,
        entitlements_file=None,
    )
    return analysis, exe


# ── Targets ──────────────────────────────────────────────────────────────────
cli_entry  = str(pkg / "src" / "humanizer" / "cli.py")
web_entry  = str(pkg / "web_ui.py")

cli_analysis, cli_exe = _make_analysis(cli_entry, "humanizer", console=True)
web_analysis, web_exe = _make_analysis(web_entry, "humanizer-web", console=False)

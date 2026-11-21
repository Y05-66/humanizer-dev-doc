# Humanizer · 去AI味中文改写工具

将 AI 生成中文文本改写为更接近人类写作风格——诊断、规划、改写、验证四步闭环，保留事实完整性的前提下消除"AI 腔"。

[📖 详细文档](./humanizer/README.md) · [💻 PyPI](https://pypi.org/project/humanizer/) · [🐛 Issues](https://github.com/<YOUR_GITHUB_USERNAME>/humanizer/issues)

---

## 快速开始

```bash
cd humanizer
pip install -e ".[dev]"

# Web UI（自动打开浏览器）
python run.bat

# 或命令行
humanizer detect your_file.txt
humanizer clean your_file.txt -o your_file_clean.txt
humanizer refactor your_file.txt --max-actions 5
```

**环境要求：** Python ≥ 3.11。Web UI 使用 `.venv\Scripts\python.exe`。

## 核心功能

| 模式 | 说明 |
|------|------|
| **detect** | 只报告病灶：AI 味指数 + 指标详情，不改写 |
| **clean**  | 语言层改写：A级硬违规强制去除，B级标记供 LLM 处理 |
| **refactor** | 结构层改写：允许段落重组和节奏重分配 |

所有模式均支持 `--mock-llm` 离线运行（不联网）。

## 为什么需要它

AI 生成的文本在统计特征上与人写文章有显著差异：句长过于均匀、词汇多样性低、段落节奏呆板、高频疲劳词堆积。Humanizer 通过多维度联合检测与精准改写，让文字恢复自然的呼吸感，同时绝对保住人名、数字、日期、对话等故事骨架。

> ⚠️ 本工具为辅助参考，检测结果不构成任何平台审核的最终依据。

---

## 打包发布（Windows .exe）

项目已配置 PyInstaller 构建脚本，可生成两个独立可执行文件：
- **`humanizer.exe`** — CLI 命令行工具
- **`humanizer-web.exe`** — Web UI 服务端（双击即可运行，无需 Python 环境）

### 本地构建

```bash
# 1. 确保依赖已安装
pip install -e "humanizer[dev]"
pip install pyinstaller>=6.0

# 2. 一键构建（自动完成所有步骤）
scripts\build_package.bat

# 或手动执行
pyinstaller --noconfirm build.spec

# 输出目录
dist\humanizer\humanizer.exe          # CLI 工具
dist\humanizer-web\humanizer-web.exe  # Web UI 服务
```

### 构建产物说明

| 文件 | 说明 |
|------|------|
| `dist/humanizer/humanizer.exe` | CLI 可执行文件，支持 `detect/clean/refactor/metrics` 子命令 |
| `dist/humanizer-web/humanizer-web.exe` | Web UI 服务，启动后监听 `http://localhost:8000` |
| `dist/humanizer/humanizer_config/` | 内置的配置文件和规则 YAML |
| `dist/humanizer/db.py` | 数据库模块（内嵌） |

两个 exe 都是 **单文件模式**，解压后直接运行，无需额外安装 Python。

### CI/CD 自动构建

推送标签时自动触发 GitHub Actions 构建，生成并上传两个 Python 版本的安装包：
```bash
git tag v0.1.0
git push origin v0.1.0
```

构建产物可在 Actions → Artifacts 中下载。

### 关于 frozen 模式的路径适配

打包后运行时，以下路径会自动适配到 exe 所在目录：
- 数据库文件：`{exe_dir}/humanizer.db`
- 规则 YAML：`{exe_dir}/humanizer/config/rules/*.yaml`
- 默认配置：`{exe_dir}/humanizer/config/default.yaml`

首次运行时会自动创建 SQLite 数据库，无需手动初始化。


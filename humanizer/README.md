# Humanizer · 去AI味中文改写工具

一个命令行 + Web UI 驱动的「去AI味」程序，通过诊断 → 规划 → 改写 → 验证闭环，系统性地将 AI 生成中文文本的统计与风格特征向人类写作分布带靠拢。

核心思路来自 [InkOS](https://github.com/Narcooo/inkos)：不靠运气检测，而是从疲劳词表、句式模板、段落节奏、困惑度四个维度联合判定；改写不是简单替换禁词，而是在保留事实完整性的前提下重塑叙事节奏。

---

## 功能概览

| 模式 | 命令 | 说明 |
|------|------|------|
| **detect** | `humanizer detect novel.txt` | 只报告病灶，不改写；输出 AI 味指数 + 指标详情 |
| **metrics** | `humanizer metrics novel.txt` | 只看统计指标（burstiness / MATTR / perplexity / 段落节奏） |
| **clean** | `humanizer clean novel.txt -o out.txt` | 语言层改写，A级硬违规强制去除，B级软信号标记供 LLM 处理 |
| **refactor** | `humanizer refactor novel.txt -o out.txt --max-actions 5` | 在清理基础上允许结构调整（段重组、节奏重分配），适合长篇 |

所有模式均支持 `--mock-llm` 离线运行（不联网，使用内置规则替换）。

Web UI 需要登录后才能使用检测与改写功能。登录后可在「我的」页面设置头像和昵称。

---

## 快速开始

```bash
# 安装（Python ≥3.11 必需）
cd humanizer
pip install -e ".[dev]"

# 一键启动 Web UI（自动打开浏览器）
python run.bat

# 或命令行直接运行
humanizer detect your_file.md
humanizer clean your_file.md -o your_file_clean.md
```

**注意：** 所有命令必须使用项目根目录下的 `.venv\Scripts\python.exe`，系统 miniconda python 缺少 fastapi/uvicorn。

```bash
# Windows（Git Bash）
.venv\Scripts\python.exe -m humanizer.cli detect path/to/file.txt

# 启动 Web UI 并指定端口
.venv\Scripts\python.exe run.py --port 9000

# 多进程并发模式（生产部署推荐）
.venv\Scripts\python.exe run.py --workers 4
```

Web UI 监听 `http://localhost:8000`。

### 已知路径问题
- 可编辑安装的 `.pth` 文件记录了 `humanizer/src` 的绝对路径。移动项目文件夹后需重新运行 `pip install -e "humanizer[dev]"`。
- `run.py` 会将 `src/` 加入 `sys.path`；`cli.py` 从当前目录读取 `config/rules/`。两者依赖 `PYTHONPATH=src`（run.bat 会自动设置）。
- SQLite 数据库 `humanizer.db` 在项目根目录首次启动时由 `db.py:init_db()` 创建。删除它等于重置所有用户账号。

---

## 多维权重评分模型

系统不只依赖单一指标，而是综合六维加权得出 **AI 味指数（0–100）**：

| 维度 | 计算方法 | 人类目标区间 |
|------|---------|-------------|
| A 级命中密度 | 每命中一处 +3 分 | ≤ 45 分贡献上限 |
| B 级软信号密度 | 每命中一处 +1.5 分 | ≤ 20 分贡献上限 |
| 突发性 burstiness | 句长标准差 ÷ 句长均值 | > 0.5（越不均匀越好） |
| MATTR 词汇多样性 | 固定窗口（50 token）滑动平均型次比 | ≥ 0.42（长度不敏感） |
| 简化困惑度 | jieba 分词 + AI 疲劳词惩罚加权后的 Shannon 熵 | 60–120（越高越不可预测） |
| 段落节奏一致性 | 各段落内句长 CV 均值；CV < 0.2 的段落越多加分越高 | CV > 0.25，低节奏段落少 |

每项封顶，单因子无法主导总分，避免"看似合格实则仍像 AI"的情况。

---

## 规则分级体系

| 级别 | 含义 | 处理方式 |
|------|------|---------|
| **A（硬信号）** | 多源一致的典型 AI 腔，命中必改 | 确定性正则替换 + LLM 上下文改写 |
| **B（软信号）** | 需语境判断的风格倾向 | 仅标记，由 LLM 或用户决定是否修改 |
| **C（禁动）** | 实测站不住或行业合法表达 | 一律不碰，仅作仲裁参考 |

规则库以 YAML 维护，新增规则无需改代码，编辑后重启生效：

- `config/rules/bans_a.yaml` — 约 45 条，覆盖高频套话、程式化副词、大词堆砌、段首/段末套路等
- `config/rules/bans_b.yaml` — 约 22 条，密度触发 + 结构性倾向（如"总之"结尾、"的"链、反问三连）
- `config/rules/c_forbidden.yaml` — 合法汉语表达白名单（被动句、比喻、问句等不可一刀切禁止）

匹配引擎预编译所有正则，三种匹配类型：`literal`（字面）、`regex`（正则）、`density`（密度阈值）。Schema 校验基于 Pydantic，损坏的 YAML 会在加载时立即报错。

---

## 用户认证

### 登录方式

Web UI 提供邮箱和手机号两种注册/登录方式，验证码功能作为预留接口存在：

| 方式 | 端点 | 状态 | 依赖服务 |
|------|------|------|---------|
| 邮箱+密码 | `/auth/register`（method=email） | ✅ 可用（无需验证码） | — |
| 手机号+密码 | `/auth/register`（method=phone） | ✅ 可用（无需验证码） | — |
| 微信扫码 | `/auth/wx/qrcode` → `/auth/wx/callback` | 🔴 已下线 | 微信开放平台 |

**验证码接口（stub，当前返回 501）**：如需启用邮箱/手机二次验证，需接入第三方服务后实现 `db.py` 中的对应函数。

| 端点 | 说明 | 需要接入的服务 |
|------|------|---------------|
| `POST /auth/send-email-code` | 发送邮箱验证码 | SMTP 服务（如 SendGrid、SMTP.qq.com） |
| `POST /auth/verify-email-code` | 校验邮箱验证码 | 同上，配合服务端存储验证码 |
| `POST /auth/send-sms-code` | 发送短信验证码 | 阿里云短信 / 腾讯云短信（需企业资质） |
| `POST /auth/verify-sms-code` | 校验短信验证码 | 同上 |

接入步骤示例（以短信为例）：
1. 在云服务商申请短信签名和模板 ID
2. 在 `db.py` 中实现 `send_sms_code()` / `verify_sms_code()`
3. 将 API Key / Secret 写入环境变量或 `.env`（不要提交到仓库）
4. 将注册端点的 `method` 参数改为触发验证码流程

### 技术细节

- 用户数据存储于项目根目录 `humanizer.db`（SQLite，WAL 模式，线程局部连接无并发冲突）
- Token 有效期 30 天，使用 `secrets.token_hex(16)` 生成
- 每个 HTTP 请求获得独立的线程局部数据库连接，异步任务走 `ThreadPoolExecutor`（worker 数 = CPU 核数 // 2，最小 2）
- `init_db()` 每次启动幂等操作：重复调用不会因列已存在而报错（ALTER TABLE 用 try/except 保护）
- 密码当前明文存储（MVP 阶段），后续可升级为 bcrypt
- 用户资料包含 `avatar_url` 字段，支持本地上传（base64 data URI）和远程图片 URL（仅限 `data:image/*`、`http://`、`https://` 前缀）

### 个人资料中心

登录后可点击顶部头像按钮进入「我的」页面，支持：
- 修改显示名称（最长 30 字符，与后端截断上限一致）
- 上传本地头像（≤5MB，转为 base64 data URI 存储）
- 粘贴远程图片链接
- 查看注册时间、注册方式、账号 ID
- 注销账号（需二次确认，同时清除所有 token）

---

## 并发与性能

- 异步任务使用线程池 `ThreadPoolExecutor(max_workers=CPU//2, min=2)`，每个请求持有独立 DB 连接
- 每次改写请求最长等待超时由 uvicorn timeout 控制，默认 120s
- 异步任务完成后保留 5s 供轮询客户端获取结果，再自动清理
- 生产部署建议使用多进程：`.venv\Scripts\python.exe run.py --workers 4`
- 规则引擎在首次使用时懒加载缓存（约 0.3s），跨请求复用

---

## 技术栈

- Python 3.11+，无外部模型依赖（可选接入 DeepSeek/OpenAI 兼容 API）
- jieba 中文分词（本地词典，无网络请求）
- FastAPI + Uvicorn（Web UI 后端，支持多 worker）
- Typer（CLI 框架）
- Pydantic（配置校验 + 规则 schema 校验）
- regex（预编译规则引擎，支持 Unicode 类字符范围）
- SQLite（用户数据持久化，标准库无额外依赖）

---

## 设计原则

1. **保真优先于"人味"**：人名、数字、日期、对话内容一字不改——这是故事的骨架。
2. **对准人类分布带，而非反转 AI 特征**：每条规则上满会制造新的指纹。
3. **分档操作**：默认只做语言层清理（剧情不动）；结构层改动必须显式授权（refactor 模式）。
4. **防误伤**：句长离散度、被动句、比喻本身、强因果、伏笔回收等均被 C 级清单豁免。
5. **可测试性**：核心管线可在 Mock LLM 下完全离线运行，现有 39 项测试全绿。

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_BASE_URL` | OpenAI-compatible API 端点 | `https://apihub.agnes-ai.com/v1` |
| `LLM_API_KEY` | API Key（clean/refactor 模式必填） | — |
| `LLM_MODEL` | 模型名 | `agnes-2.5-flash` |
| `LLM_TEMPERATURE` | 采样温度 | `1.2` |

---

## API 接口

### 检测与改写

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| `GET` | `/` | 否 | 单页 Web UI |
| `POST` | `/detect` | ✅ | 返回 JSON 诊断报告（AI 味分数 + 指标 + 命中预览） |
| `POST` | `/clean` | ✅ | 执行改写管线，返回原文/改写文对比 + 前后指标 |
| `POST` | `/clean-async` | ✅ | 异步改写（大文件用），返回 task_id，轮询 `/task/{id}` |
| `GET` | `/task/{task_id}` | ❌ | 查询异步任务进度与结果 |
| `DELETE` | `/task/{task_id}` | ❌ | 取消 pending 状态的任务 |
| `GET` | `/health` | ❌ | 存活探针 |

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/auth/login` | 登录，返回 `{token, user}` |
| `POST` | `/auth/register` | 注册，method=email/phone |
| `POST` | `/auth/logout` | 登出，清除当前 token |
| `GET` | `/auth/me` | 获取当前用户信息（含 avatar_url） |
| `POST` | `/auth/profile` | 更新个人资料：display + avatar_url |
| `POST` | `/auth/send-email-code` | 发送邮箱验证码（stub，返回 501） |
| `POST` | `/auth/verify-email-code` | 校验邮箱验证码 |
| `POST` | `/auth/send-sms-code` | 发送短信验证码（stub） |
| `POST` | `/auth/verify-sms-code` | 校验短信验证码 |
| `GET` | `/auth/wx/qrcode` | 生成微信扫码链接（stub） |
| `POST` | `/auth/wx/callback` | 微信 OAuth 回调（stub） |

所有受保护端点的请求必须携带 `X-Auth-Token` 请求头；未登录或 token 失效时返回 401。

**前端重要约定：** 每次 fetch 请求加 `headers:{'X-Auth-Token':_getToken()}`。新增任何需鉴权的端点都必须遵守此约定，否则会被后端拒绝。

---

## 测试

```bash
# 运行全部测试（39 项）
.venv\Scripts\python.exe -m pytest humanizer/tests/ -v

# 单个测试文件
.venv\Scripts\python.exe -m pytest humanizer/tests/test_rules.py -v

# 单个用例
.venv\Scripts\python.exe -m pytest humanizer/tests/test_diagnose.py::TestDiagnose::test_burstiness_calculation -v

# Lint + 格式检查
.venv\Scripts\ruff check humanizer/src humanizer/web_ui.py humanizer/run.py humanizer/db.py --config humanizer/pyproject.toml
.venv\Scripts\ruff format --check humanizer/src humanizer/web_ui.py humanizer/run.py humanizer/db.py
```

**修改规则 YAML 后务必跑：** `.venv\Scripts\python.exe -m pytest humanizer/tests/test_rules.py -v` 确保 schema 校验依然通过。

---

## 常见问题

**Q：移动了项目文件夹后 editable install 失效怎么办？**

编辑 `.venv/Lib/site-packages/__editable__.humanizer-0.1.0.pth`，将其中记录的旧绝对路径改为新路径，或直接重新运行 `pip install -e ".[dev]"`。

**Q：为什么有些规则匹配不到但人工阅读感觉很像 AI？**

A 级规则覆盖了已知的高置信度模式；边缘情况属于 B 级软信号，交给 LLM 判断。可通过调节 `config/default.yaml` 中的阈值或向 `bans_b.yaml` 追加规则来改善。

**Q：如何添加新的规则？**

直接在对应的 YAML 文件中追加条目即可，格式参见同文件现有条目。不需要改任何 Python 代码，重启服务即生效。

**Q：用户的头像图片存储在哪里？**

头像以 base64 data URI 形式存储在 SQLite `users.avatar_url` 字段中；远程图片则存储原始 URL。不依赖外部图床或对象存储。

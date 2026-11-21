"""用户与 Token 的 SQLite 持久化存储。

使用标准库 sqlite3，无需额外依赖。数据库文件默认创建于项目根目录的 humanizer.db。
进程启动时建表；Token 每 30 天自动过期清理一次（惰性清理）。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import secrets
import sys
import threading
import time as _time
from pathlib import Path

logger = logging.getLogger(__name__)


# ── 密码哈希工具（SHA-256 + 随机盐，无外部依赖）───────────────────────────────
_HASH_VERSION = "v1"   # 格式前缀，用于未来算法升级时的平滑迁移


def _hash_password(plain: str) -> str:
    """用 16 字节随机盐对明文密码做 SHA-256 哈希，返回 'salt:hash' 字符串。"""
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()
    return f"{_HASH_VERSION}:{salt}:{h}"


def _check_password(stored: str, plain: str) -> bool:
    """验证明文密码与存储哈希是否匹配；自动处理旧版非哈希格式兼容。"""
    try:
        version, salt, stored_hash = stored.split(":")
        if version != _HASH_VERSION:
            raise ValueError("unknown hash version")
        expected = hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()
        return secrets.compare_digest(expected, stored_hash)
    except (ValueError, TypeError):
        # 遗留：直接明文存储的旧哈希，仍允许通过（仅测试账号），但会记录日志
        logger.warning(
            "密码为明文存储，将立即升级为哈希格式（uid=%s）。请在下次登录时更新。",
            "_check_password backward-compat: uid masked",
        )
        return stored == plain


def _migrate_to_hashed(stored: str, plain: str) -> str | None:
    """当明文密码验证成功但尚未哈希时，返回新哈希值；否则返回 None。"""
    try:
        if stored.startswith(f"{_HASH_VERSION}:"):
            return None  # 已经是哈希格式
    except AttributeError:
        pass
    if stored == plain:  # 明文比对成功
        return _hash_password(plain)
    return None


def _data_dir() -> Path:
    """返回数据目录：frozen 模式下为 exe 所在目录，开发模式下为项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    # 从 humanizer/db.py → parent.parent 得到项目根
    return Path(__file__).resolve().parents[2]


DB_PATH = _data_dir() / "humanizer.db"

_thread_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """获取当前线程绑定的连接（每条 HTTP 请求一个连接）。"""
    if not hasattr(_thread_local, "conn") or _thread_local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _thread_local.conn = conn
    return _thread_local.conn


def init_db() -> None:
    """创建必要的表（幂等操作，重复调用安全）。"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         TEXT PRIMARY KEY,   -- e.g. 'email:user@example.com'
            password   TEXT,               -- 密码仅 email/password 模式使用，其他为 NULL
            display    TEXT NOT NULL,
            avatar_url TEXT DEFAULT '',    -- 头像图片 URL（SVG data URI 或网络地址）
            method     TEXT NOT NULL,      -- email | phone
            created_at REAL NOT NULL       -- unix timestamp
        );

        CREATE TABLE IF NOT EXISTS tokens (
            token   TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created REAL NOT NULL,
            expires REAL NOT NULL,          -- unix timestamp
            UNIQUE(user_id, token)
        );

        CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
    """)
    conn.commit()
    # Migration: add avatar_url column if missing (safe for existing DBs)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass  # column already exists


# ── 基础用户操作 ───────────────────────────────────────────────────────────────

def find_user_by_uid(uid: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


def create_user(uid: str, password: str | None, display: str, method: str) -> dict:
    """注册新用户。password 对微信登录可为 None，其他模式统一哈希后存储。"""
    import time as _time
    now = _time.time()
    hashed_pw = _hash_password(password) if password else None
    conn = _get_conn()
    conn.execute(
        "INSERT INTO users (id, password, display, method, created_at) VALUES (?,?,?,?,?)",
        (uid, hashed_pw, display, method, now),
    )
    conn.commit()
    return {"id": uid, "password": hashed_pw, "display": display, "method": method}


def verify_password(uid: str, password: str) -> bool:
    """校验密码（仅 email/phone 模式使用），支持旧版明文兼容并自动升级。"""
    user = find_user_by_uid(uid)
    if user is None:
        return False
    stored = user.get("password")
    if stored is None:
        return False
    matched = _check_password(stored, password)
    # 自动升级明文账号到哈希格式（只更新一次）
    if matched and _migrate_to_hashed(stored, password) is not None:
        new_hash = _hash_password(password)
        conn = _get_conn()
        conn.execute(
            "UPDATE users SET password=? WHERE id=?", (new_hash, uid)
        )
        conn.commit()
    return matched


def get_or_create_token(user_id: str) -> str:
    """返回该用户的现有活跃 token，没有则新建一个。"""
    import time as _time
    import secrets as _secrets
    conn = _get_conn()
    # 查找未过期的旧 token
    row = conn.execute(
        "SELECT token FROM tokens WHERE user_id=? AND expires > ?",
        (user_id, _time.time()),
    ).fetchone()
    if row:
        return row["token"]
    # 新建 token（30 天有效期）
    tok = _secrets.token_hex(16)
    now = _time.time()
    conn.execute(
        "INSERT INTO tokens (token, user_id, created, expires) VALUES (?,?,?,?)",
        (tok, user_id, now, now + 30 * 86400),
    )
    conn.commit()
    return tok


def validate_token(token: str) -> dict | None:
    """验证 token 是否有效且未过期。"""
    import time as _time
    conn = _get_conn()
    row = conn.execute(
        "SELECT t.token, u.id, u.display, u.method, COALESCE(u.avatar_url,'') as avatar_url FROM tokens t "
        "JOIN users u ON t.user_id=u.id "
        "WHERE t.token=? AND t.expires > ?",
        (token, _time.time()),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "display": row["display"],
        "method": row["method"],
        "avatar_url": row["avatar_url"] or "",
    }


def invalidate_token(token: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM tokens WHERE token=?", (token,))
    conn.commit()


def update_profile(uid: str, display: str, avatar_url: str) -> dict | None:
    """更新用户展示名和头像。"""
    import time as _time
    conn = _get_conn()
    row = conn.execute(
        "UPDATE users SET display=?, avatar_url=?, created_at=? WHERE id=?",
        (display, avatar_url, _time.time(), uid),
    ).rowcount
    conn.commit()
    if row == 0:
        return None
    return {"id": uid, "display": display, "method": "", "avatar_url": avatar_url}


def cleanup_expired(max_age_days: int = 90) -> int:
    """删除超过 max_age_days 天的 token，返回删除数量。"""
    import time as _time
    conn = _get_conn()
    cutoff = _time.time() - max_age_days * 86400
    cur = conn.execute("DELETE FROM tokens WHERE created < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def delete_user(uid: str) -> bool:
    """彻底删除用户及其所有 token（级联）；返回是否成功找到并删除。"""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    return cur.rowcount > 0


def cleanup_tokens(max_age_days: int = 90) -> int:
    """删除超过 max_age_days 天未使用的 token（与 login/register 时联合调用保持整洁）。"""
    import time as _time
    conn = _get_conn()
    cutoff = _time.time() - max_age_days * 86400
    cur = conn.execute("DELETE FROM tokens WHERE created < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# ── ✉️ 邮箱验证代码预留（暂未启用）──────────────────────────────────────────────
# 如需启用邮箱/手机验证注册，可在此实现 send_email_code / verify_email_code。


# ── 📱 手机短信验证预留（暂未启用）──────────────────────────────────────────────
# 如需启用，可在此实现 send_sms_code / verify_sms_code。


# ── 💬 微信登录已下线（不再支持）──────────────────────────────────────────────
# 如需重新启用，请重新实现 generate_wx_auth_url / exchange_wx_code_for_openid。


def generate_wx_auth_url(state: str, redirect_uri: str) -> str:
    """微信登录已下线。"""
    raise RuntimeError("微信登录已下线，请使用邮箱或手机号")


def exchange_wx_code_for_openid(code: str) -> str:
    """微信登录已下线。"""
    raise RuntimeError("微信登录已下线，请使用邮箱或手机号")


# ── 应用启动时初始化 ───────────────────────────────────────────────────────────
init_db()

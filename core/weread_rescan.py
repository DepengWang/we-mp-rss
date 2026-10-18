"""微信读书免登录重新扫码链接：签名令牌的签发、校验与作废。

失效提醒里附带一条链接，打开后无需登录后台即可现场生成二维码扫码授权。
链接本身就是授权凭证，因此：
- 令牌用服务器 SECRET_KEY 做 HMAC 签名，默认 24 小时有效；
- 只有最近签发的一条有效（新签发会作废旧链接），扫码成功后立即作废；
- 页面只能重新绑定微信读书账号，不返回任何 Cookie 或其他数据。
"""
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time

RESCAN_TTL_SECONDS = 24 * 3600
_STATE_PATH = os.environ.get("WEREAD_RESCAN_STATE_PATH", "./data/weread_rescan.json")
_state_lock = threading.Lock()


def _secret() -> bytes:
    from core.auth import SECRET_KEY
    return str(SECRET_KEY).encode("utf-8")


def _sign(expires_at: int, nonce: str) -> str:
    message = f"weread-rescan.{expires_at}.{nonce}".encode("utf-8")
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def _read_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: str, state: dict):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".weread_rescan.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def issue_token(ttl: int = RESCAN_TTL_SECONDS, now: float = None, path: str = None) -> str:
    """签发新令牌并作废之前签发的链接。"""
    path = path or _STATE_PATH
    expires_at = int((time.time() if now is None else now) + ttl)
    nonce = secrets.token_urlsafe(16)
    with _state_lock:
        _write_state(path, {"nonce": nonce, "expires_at": expires_at})
    return f"{expires_at}.{nonce}.{_sign(expires_at, nonce)}"


def verify_token(token: str, now: float = None, path: str = None) -> bool:
    """令牌签名正确、未过期且是最近签发且未作废的那一条时返回 True。"""
    path = path or _STATE_PATH
    try:
        expires_part, nonce, signature = str(token or "").split(".", 2)
        expires_at = int(expires_part)
    except ValueError:
        return False
    if not hmac.compare_digest(signature, _sign(expires_at, nonce)):
        return False
    if expires_at <= (time.time() if now is None else now):
        return False
    with _state_lock:
        state = _read_state(path)
    return hmac.compare_digest(str(state.get("nonce") or ""), nonce)


def consume_token(token: str, path: str = None) -> None:
    """扫码成功后作废令牌（仅当它仍是当前有效令牌时）。"""
    path = path or _STATE_PATH
    nonce = str(token or "").split(".")[1] if str(token or "").count(".") >= 2 else ""
    with _state_lock:
        state = _read_state(path)
        if nonce and hmac.compare_digest(str(state.get("nonce") or ""), nonce):
            _write_state(path, {})


def build_rescan_url(token: str) -> str:
    """根据 rss.base_url 拼出公开链接；未配置外网地址时返回空字符串。"""
    from core.base import API_BASE
    from core.config import cfg
    base = str(cfg.get("rss.base_url", "") or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}{API_BASE}/weread/rescan?t={token}"

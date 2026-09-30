__package__ = "driver"
from core.config import Config,cfg
# 确保data目录和wx.lic文件存在
import os
import json
import time
import threading
from core.print import print_success, print_warning
from core.redis_client import redis_client
from driver.account_sessions import retain_recent_valid_sessions, is_restorable_account_session

REDIS_TOKEN_PREFIX = "werss:token:"
REDIS_ACCOUNT_STATUS_KEY = "werss:wx:account_status"
REDIS_ACCOUNT_SESSIONS_KEY = "werss:wx:account_sessions"
_ACCOUNT_STATUS_LOCK = threading.Lock()
_ACCOUNT_SESSIONS_LOCK = threading.RLock()

lic_path="./data/wx.lic"
os.makedirs(os.path.dirname(lic_path), exist_ok=True)
if not os.path.exists(lic_path):
    with open(lic_path, "w") as f:
        f.write("{}")
wx_cfg = Config(lic_path)

def set_token(data:any,ext_data:any=None):

    """
    设置微信登录的Token和Cookie信息
    :param data: 包含Token和Cookie信息的字典
    """
    if data.get("token", "") == "":
        return

    token_data = {
        "token": data.get("token", ""),
        "cookie": data.get("cookies_str", ""),
        "fingerprint": data.get("fingerprint", ""),
        "expiry": data.get("expiry", {}),
    }
    if ext_data is not None:
        token_data["ext_data"] = ext_data

    # 优先存储到Redis，整体存储
    if redis_client.is_connected:
        try:
            _save_to_local(token_data)
            redis_client._client.set(REDIS_TOKEN_PREFIX + "data", json.dumps(token_data))
            print_success("Token已存储到Redis")
        except Exception as e:
            print_warning(f"Redis存储失败，回退到本地文件: {e}")
    else:
        _save_to_local(token_data)

    expiry = data.get("expiry") or {}
    print_success(f"Token已安全存储，到期时间:{expiry.get('expiry_time', '未知')}")
    from jobs.notice import sys_notice

#     sys_notice(f"""WeRss授权成功
# - Token: {data.get("token")}
# - Expiry: {data.get("expiry")['expiry_time']}
# """, str(cfg.get("server.code_title","WeRss授权成功")))


def _save_to_local(token_data: dict):
    """保存到本地文件"""
    # 先重新加载文件最新内容，避免 wx_cfg 单例用启动时的陈旧内存
    # 整文件覆盖其他模块写入的数据（如 weread_data）
    wx_cfg.reload()
    wx_cfg.set("token_data", token_data)
    wx_cfg.save_config()
    wx_cfg.reload()


def get(key:str,default:str="")->str:
    """从整体token_data中获取指定字段"""
    token_data = _get_token_data()
    if token_data is None:
        return default
    value = token_data.get(key, default)
    if isinstance(value, dict):
        return json.dumps(value)
    if value=="None":
        return ''
    return str(value) if value is not None else default


def _get_token_data() -> dict | None:
    """获取整体token_data"""
    # 优先从Redis获取
    if redis_client.is_connected:
        try:
            value = redis_client._client.get(REDIS_TOKEN_PREFIX + "data")
            if value is not None:
                return json.loads(value)
        except Exception as e:
            print_warning(f"Redis读取失败，回退到本地文件: {e}")
    # 回退到本地文件
    return wx_cfg.get("token_data", None)


def get_account_status_data() -> dict:
    """Read cached, non-secret account status summaries."""
    if redis_client.is_connected:
        try:
            value = redis_client._client.get(REDIS_ACCOUNT_STATUS_KEY)
            if value:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
        except Exception as e:
            print_warning(f"Redis读取公众号状态缓存失败，回退到本地文件: {e}")

    try:
        wx_cfg.reload()
        value = wx_cfg.get("account_status_data", {})
        return value if isinstance(value, dict) else {}
    except Exception as e:
        print_warning(f"读取公众号状态缓存失败: {e}")
        return {}


def set_account_status_data(data: dict) -> None:
    """Persist only account names, IDs, and expiry metadata; never tokens/cookies."""
    accounts = []
    for account in data.get("accounts", []):
        if not isinstance(account, dict):
            continue
        expiry = account.get("expiry") or {}
        accounts.append({
            "account_id": str(account.get("account_id", "")),
            "name": str(account.get("name", "")),
            "token_status": str(account.get("token_status", "unknown")),
            "expiry": {
                "expiry_timestamp": expiry.get("expiry_timestamp"),
                "expiry_time": str(expiry.get("expiry_time", "")),
            },
            "ext_data": _public_account_ext_data(account.get("ext_data")),
            "checked_at": str(account.get("checked_at", "")),
            "authorized_at": account.get("authorized_at"),
            "last_active_at": account.get("last_active_at"),
        })

    payload = {
        "accounts": accounts,
        "active_account_id": str(data.get("active_account_id", "")),
        "checked_at": str(data.get("checked_at", "")),
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    with _ACCOUNT_STATUS_LOCK:
        # Keep a local wx.lic fallback without disturbing token_data.
        wx_cfg.set("account_status_data", payload)
        if redis_client.is_connected:
            try:
                redis_client._client.set(REDIS_ACCOUNT_STATUS_KEY, serialized)
            except Exception as e:
                print_warning(f"公众号状态缓存写入Redis失败，已保存在本地文件: {e}")


def _public_account_ext_data(ext_data) -> dict:
    """Keep display-safe公众号 metrics, excluding all credentials."""
    if not isinstance(ext_data, dict):
        return {}
    allowed = {
        "wx_app_name", "wx_logo", "wx_read_yesterday", "wx_share_yesterday",
        "wx_watch_yesterday", "wx_yuan_count", "wx_user_count",
    }
    return {key: ext_data.get(key) for key in allowed if key in ext_data}


def get_account_sessions_data() -> dict:
    """Read per-account sessions; this private cache contains credential material."""
    with _ACCOUNT_SESSIONS_LOCK:
        if redis_client.is_connected:
            try:
                value = redis_client._client.get(REDIS_ACCOUNT_SESSIONS_KEY)
                if value:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        return parsed
            except Exception as e:
                print_warning(f"读取公众号账号会话缓存失败，回退到本地文件: {e}")

        try:
            wx_cfg.reload()
            value = wx_cfg.get("account_sessions_data", {})
            return value if isinstance(value, dict) else {}
        except Exception as e:
            print_warning(f"读取公众号账号会话缓存失败: {e}")
            return {}


def get_account_session_data(account_id: str) -> dict:
    """Read one account's cached credential bundle by account_id."""
    account_id = str(account_id or "").strip()
    if not account_id:
        return {}
    return get_account_sessions_data().get(account_id, {})


def set_account_sessions_data(sessions: dict) -> None:
    """Persist per-account tokens/cookies for session restoration; never log values."""
    payload = {}
    if isinstance(sessions, dict):
        for raw_id, account in sessions.items():
            if not isinstance(account, dict):
                continue
            account_id = str(account.get("account_id") or raw_id or "").strip()
            if not account_id:
                continue
            cookies = account.get("cookies")
            expiry = account.get("expiry")
            ext_data = account.get("ext_data")
            payload[account_id] = {
                "account_id": account_id,
                "name": str(account.get("name") or ""),
                "token": str(account.get("token") or ""),
                "cookies": cookies if isinstance(cookies, list) else [],
                "cookies_str": str(account.get("cookies_str") or ""),
                "fingerprint": str(account.get("fingerprint") or ""),
                "expiry": expiry if isinstance(expiry, dict) else {},
                "wx_login_url": str(account.get("wx_login_url") or ""),
                "ext_data": _public_account_ext_data(ext_data),
                "updated_at": str(account.get("updated_at") or ""),
                "authorized_at": account.get("authorized_at"),
                "last_active_at": account.get("last_active_at"),
                "token_status": str(account.get("token_status") or "valid"),
            }

    payload = retain_recent_valid_sessions(payload, limit=3)
    serialized = json.dumps(payload, ensure_ascii=False)
    with _ACCOUNT_SESSIONS_LOCK:
        wx_cfg.set("account_sessions_data", payload)
        if redis_client.is_connected:
            try:
                redis_client._client.set(REDIS_ACCOUNT_SESSIONS_KEY, serialized)
            except Exception as e:
                print_warning(f"公众号账号会话写入Redis失败，已保存在本地文件: {e}")


def update_account_session_data(account_id: str, account: dict, active: bool = False) -> None:
    """Upsert one account session without removing other accounts from the cache."""
    account_id = str(account_id or "").strip()
    if not account_id or not isinstance(account, dict):
        return
    with _ACCOUNT_SESSIONS_LOCK:
        sessions = get_account_sessions_data()
        existing = sessions.get(account_id, {}) or {}
        now = time.time()
        updated = {**existing, **account, "account_id": account_id}
        if not updated.get("authorized_at"):
            updated["authorized_at"] = existing.get("authorized_at") or now
        if active:
            updated["last_active_at"] = now
        elif not updated.get("last_active_at"):
            updated["last_active_at"] = existing.get("last_active_at") or updated["authorized_at"]
        if active or not updated.get("token_status"):
            updated["token_status"] = "valid"
        sessions[account_id] = updated
        set_account_sessions_data(sessions)


def record_authorized_account_session(
    account_id: str,
    account: dict,
    summary: dict,
    *,
    newly_authorized: bool = False,
    active: bool = False,
    limit: int = 3,
) -> dict:
    """Persist a full per-account session and its public summary as one rolling set."""
    account_id = str(account_id or "").strip()
    if not account_id or not isinstance(account, dict) or not isinstance(summary, dict):
        raise ValueError("公众号账号 ID 或会话信息不完整")

    now = time.time()
    sessions = get_account_sessions_data()
    existing = sessions.get(account_id, {}) or {}
    session = {**existing, **account, "account_id": account_id}
    session["authorized_at"] = now if newly_authorized else (
        existing.get("authorized_at") or session.get("authorized_at") or now
    )
    if active or newly_authorized:
        session["last_active_at"] = now
    else:
        session["last_active_at"] = (
            existing.get("last_active_at") or session.get("last_active_at") or session["authorized_at"]
        )
    session["updated_at"] = str(session.get("updated_at") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)))
    session["token_status"] = str(summary.get("token_status") or session.get("token_status") or "valid")
    if not is_restorable_account_session(session, now):
        raise ValueError("公众号会话不完整或 Token 已失效，未保存为可切换账号")

    sessions[account_id] = session
    retained = retain_recent_valid_sessions(sessions, limit=limit, now=now)
    if account_id not in retained:
        raise ValueError("公众号会话未能进入最近有效账号缓存")
    set_account_sessions_data(retained)

    previous_status = get_account_status_data()
    status_by_id = {
        str(item.get("account_id") or ""): dict(item)
        for item in previous_status.get("accounts", [])
        if isinstance(item, dict) and item.get("account_id")
    }
    status_row = {**summary, "account_id": account_id}
    status_row["authorized_at"] = session["authorized_at"]
    status_row["last_active_at"] = session["last_active_at"]
    status_row["checked_at"] = str(status_row.get("checked_at") or session["updated_at"])
    status_by_id[account_id] = status_row
    ordered_status = [
        status_by_id[saved_id]
        for saved_id in retained
        if saved_id in status_by_id
    ]
    active_id = account_id if active or newly_authorized else str(previous_status.get("active_account_id") or "")
    if active_id not in retained:
        active_id = account_id
    set_account_status_data({
        "accounts": ordered_status,
        "active_account_id": active_id,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
    })
    return retained[account_id]

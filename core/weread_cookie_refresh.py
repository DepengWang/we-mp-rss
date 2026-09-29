"""微信读书 Cookie 管理：扫码授权写入，容器内线程定期无头保活/尝试轮换。

主服务启动后，线程使用容器内 Playwright Chromium 访问配置的公众号主页，
验证登录态并在服务端轮换 Cookie 时写回 wx.lic。Cookie 已失效且无法无头恢复时，
不会覆盖现有值，需要通过管理页扫码重新授权。

scripts/refresh_weread_cookie.py 仍可用于手动执行浏览器刷新。

流程：
1. 用本机 Chrome（Playwright 驱动）打开配置的公众号主页 URL（reader 页，形如
   https://weread.qq.com/web/mp/reader/xxxx，**不要配成带 bookId 的 /web/mp/articles
   接口地址**——接口地址会被重定向且不是给人看的页面）；
2. 主页内部会发出 /web/mp/articles 请求，从该请求头提取最新 Cookie（回退
   context.cookies 拼接）；
3. 写回 wx.lic 的 weread_data，并记录 cookie_refresh_last_ts（冷却用）；
4. 若 headless_only=False 且取不到有效 Cookie，弹可见窗口提示扫码登录，等待后更新。

注意：
- 直接读写 wx.lic（WEREAD_LIC_PATH 指定，默认 ./data/wx.lic）。
- 持久化 profile 目录由 WEREAD_PROFILE_DIR 指定，默认 ~/.cache/we-mp-rss/weread-chrome-profile；
  应与容器挂载的数据卷指向同一物理目录（如 /Users/yangqing/wechat-rss-data/weread-chrome-profile），
  由本机 Chrome 持有登录态，宿主刷新时复用。
- 宿主机调用一般不传 force_bundled（使用 wx.lic 中配置的 browser_path 指向的本机 Chrome）。
"""
import os
import json
import time
import threading
import tempfile

import yaml

DEFAULT_LIC_PATH = os.environ.get("WEREAD_LIC_PATH", "./data/wx.lic")
DEFAULT_PROFILE_DIR = os.environ.get(
    "WEREAD_PROFILE_DIR",
    os.path.expanduser("~/.cache/we-mp-rss/weread-chrome-profile"),
)
_refresh_thread_lock = threading.Lock()
_refresh_thread_started = False


def _read_lic(lic_path: str = DEFAULT_LIC_PATH) -> dict:
    """直接读取 wx.lic（YAML），避免引入 core.config 的重依赖链。"""
    if not os.path.exists(lic_path):
        return {}
    with open(lic_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_lic(lic_path: str, doc: dict):
    lic_dir = os.path.dirname(os.path.abspath(lic_path))
    os.makedirs(lic_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(lic_path)}.", suffix=".tmp", dir=lic_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, lic_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _load_weread_data(lic_path: str = DEFAULT_LIC_PATH):
    """返回 (doc, data)，doc 为整个 YAML 文档，data 为 weread_data 字典。"""
    doc = _read_lic(lic_path)
    data = doc.get("weread_data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    return doc, data


def _save_cookie(cookie: str, name: str = "", lic_path: str = DEFAULT_LIC_PATH):
    """把最新 cookie 写回 wx.lic 的 weread_data（保留文档其他部分）。

    同时同步 vid 字段：配置页与 /weread 状态接口用 weread_data.vid 判断是否已配置，
    若只更新 cookie 而不更新 vid，换账号扫码后会残留上一个账号的 vid。
    """
    doc, data = _load_weread_data(lic_path)
    data["cookie"] = cookie
    vid = extract_vid(cookie)
    if vid:
        data["vid"] = vid
    if name:
        data["name"] = name
    data["cookie_refresh_last_ts"] = time.time()
    doc["weread_data"] = data
    _write_lic(lic_path, doc)


def _cookie_signature(cookie: str):
    """生成不含 Cookie 明文的稳定比较值，忽略 Cookie 项顺序。"""
    pairs = []
    for item in _dedupe_cookie(cookie).split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            pairs.append((key.strip(), value.strip()))
    return tuple(sorted(pairs))


def _save_cookie_if_changed(cookie: str, previous_cookie: str, name: str = "",
                            lic_path: str = DEFAULT_LIC_PATH) -> bool:
    """仅在服务端轮换且文件未被扫码流程更新时写回。"""
    _, current_data = _load_weread_data(lic_path)
    current_cookie = (current_data.get("cookie") or "").strip()
    current_signature = _cookie_signature(current_cookie)
    if _cookie_signature(cookie) == current_signature:
        return False
    # 浏览器访问期间可能刚好完成了新的扫码授权；不要用旧请求结果覆盖它。
    if current_signature != _cookie_signature(previous_cookie):
        return False
    _save_cookie(cookie, name=name, lic_path=lic_path)
    return True


def extract_vid(cookie: str) -> str:
    """从 Cookie 字符串中提取 wr_vid。"""
    for item in (cookie or "").split(";"):
        item = item.strip()
        if item.startswith("wr_vid="):
            return item[len("wr_vid="):].strip()
    return ""


def _dedupe_cookie(cookie: str) -> str:
    """去掉重复键（浏览器注入种子后再回抓可能产生重复 wr_vid/wr_skey 等），保留首次出现。

    重复键会导致服务端取到第一个（可能是过期）值，故保存与注入前都需去重。
    """
    kept = {}
    for item in (cookie or "").split(";"):
        item = item.strip()
        if "=" in item:
            k, _, v = item.partition("=")
            k = k.strip()
            if k and k not in kept:
                kept[k] = f"{k}={v.strip()}"
    return "; ".join(kept.values())


def _cookie_status(cookie: str) -> str:
    """检查 Cookie 登录态，返回 ``valid``、``invalid`` 或 ``unknown``。

    仅检查 'wr_vid=' 不够：过期 Cookie 同样带 wr_vid。这里与管理页“测试连接”
    使用同一个 /web/shelf/sync 接口，避免公众号文章
    列表接口对特定 bookId 限制导致 Cookie 被误判为失效。网络错误或无法识别的
    响应返回 unknown，避免把临时故障误报成需要扫码。
    """
    try:
        import requests
    except ImportError:
        return "unknown"
    try:
        r = requests.get(
            "https://weread.qq.com/web/shelf/sync",
            params={"userVid": "", "synckey": 0, "lectureSynckey": 0},
            headers={
                "Cookie": cookie,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://weread.qq.com/",
            },
            timeout=30,
        )
        if r.status_code == 401:
            return "invalid"
        if r.status_code != 200:
            return "unknown"
        try:
            j = r.json()
        except Exception:
            return "unknown"
        if isinstance(j, dict):
            code = j.get("errCode", j.get("errcode", 0))
            try:
                code = int(code or 0)
            except (TypeError, ValueError):
                return "unknown"
            if code in (-2012, -2010):
                return "invalid"
            if code == -2041:
                # -2041 也可能表示请求被限流/拦截，只有错误文案明确指向
                # 登录认证失效时才通知扫码，避免把采集限流误当成 Cookie 过期。
                message = " ".join(str(j.get(key, "")) for key in (
                    "errMsg", "errmsg", "errlog"
                )).lower()
                auth_markers = ("login", "expired", "登录", "超时", "过期", "失效", "鉴权", "认证")
                if any(marker in message for marker in auth_markers):
                    return "invalid"
                return "unknown"
            if code:
                return "unknown"
            # 书架为空也代表认证成功，因此只要求响应是书架数据结构。
            if "books" in j or "synckey" in j:
                return "valid"
        return "unknown"
    except Exception:
        return "unknown"


def _verify_cookie(cookie: str) -> bool:
    """实打实请求一次微信读书书架接口，确认 Cookie 真能使用。

    无 requests 时沿用旧行为，不阻断浏览器刷新；其他无法确认的情况按验证失败处理。
    """
    try:
        import requests  # noqa: F401
    except ImportError:
        return True
    return _cookie_status(cookie) == "valid"


def _send_cookie_expired_notice():
    """通过后台配置的通知渠道提示用户重新扫码，不包含 Cookie 内容。"""
    from jobs.notice import sys_notice

    sys_notice(
        title="微信读书 Cookie 已失效",
        tag="微信读书授权",
        text=(
            "无头浏览器自动刷新未能恢复登录态，且已确认当前 Cookie 失效。\n"
            "请登录 WeRSS 后台，进入「微信读书管理」重新扫码授权。"
        ),
    )


def _extract_cookie_from_page(page, context, url: str) -> str:
    """访问完成后优先读取浏览器 Cookie jar，回退到请求头中的 Cookie。

    请求头记录的是请求发出时的旧值；页面响应若通过 Set-Cookie 轮换登录态，
    必须读取响应后的 context Cookie jar 才能拿到新值。
    """
    captured = {}

    def _on_request(request):
        if "web/mp/articles" in request.url:
            captured["cookie"] = request.headers.get("cookie", "")

    page.on("request", _on_request)
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception as e:
        print(f"[refresh] 打开页面异常: {e}")
    # 先读导航完成后的 Cookie jar，以便捕获响应中 Set-Cookie 更新的值。
    try:
        cookies = context.cookies("https://weread.qq.com")
        jar_cookie = _dedupe_cookie(
            "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        )
        if jar_cookie:
            return jar_cookie
    except Exception:
        pass
    return _dedupe_cookie(captured.get("cookie", "").strip())


def refresh_weread_cookie(verbose: bool = True, headless_only: bool = False,
                          force_bundled: bool = False, cooldown_hours: float = 6.0) -> bool:
    """执行一次 Cookie 自动刷新。成功更新（或确认仍新鲜）返回 True，否则 False。

    headless_only: True 时只做无头刷新，不在登录失效时弹窗扫码（容器内使用）。
    force_bundled: True 时忽略 browser_path，使用 Playwright 自带 Chromium
                   （容器内没有宿主机 Chrome 路径，且需与数据卷共享 profile）。
    cooldown_hours: 距离上次成功刷新不足该时长且已有 Cookie 时，视为仍有效，跳过刷新。
    """
    lic, data = _load_weread_data()
    url = (data.get("cookie_refresh_url") or "").strip()
    browser_path = (data.get("browser_path") or "").strip()
    browser_type = (data.get("browser_type") or "chrome").strip() or "chrome"

    if not url:
        if verbose:
            print("[refresh] 未配置 cookie_refresh_url，跳过自动刷新（请在微信读书配置页填写）")
        return False

    # 冷却：上次刷新成功且在冷却期内，且已有 Cookie，则视为仍有效，跳过
    if cooldown_hours and cooldown_hours > 0:
        last_ts = data.get("cookie_refresh_last_ts") or 0
        has_cookie = bool((data.get("cookie") or "").strip())
        if has_cookie and last_ts and (time.time() - float(last_ts)) < cooldown_hours * 3600:
            # 冷却期内：只有经真实接口验证 Cookie 仍有效才跳过刷新；
            # 否则（已过期 / 旧时间戳是假阳性）必须强制刷新，不能被旧时间戳卡住。
            if _verify_cookie((data.get("cookie") or "").strip()):
                if verbose:
                    print(f"[refresh] Cookie 在冷却期内（{cooldown_hours:g}h）且仍有效，跳过刷新")
                return True
            if verbose:
                print("[refresh] Cookie 在冷却期内但已失效，强制刷新")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if verbose:
            print("[refresh] 未安装 playwright，请执行: pip install playwright && playwright install chromium")
        return False

    profile_dir = DEFAULT_PROFILE_DIR
    os.makedirs(profile_dir, exist_ok=True)

    def _launch(p, headless: bool, force_bundled: bool = False):
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        if force_bundled or not browser_path:
            return p.chromium.launch_persistent_context(profile_dir, headless=headless, args=args)
        return p.chromium.launch_persistent_context(
            profile_dir, headless=headless, executable_path=browser_path, args=args,
        )

    def _try(headless: bool, wait_login: bool = False, timeout_s: int = 300,
             force_bundled: bool = False, seed_cookie: str = "") -> str:
        """打开页面并提取 Cookie；wait_login=True 时若未登录，保持窗口等待用户扫码。

        seed_cookie: wx.lic 中已有的 Cookie，作为登录态种子注入上下文。
        解决宿主(macOS Chrome)/容器(Linux Chromium) profile cookie 加密不互通的问题：
        容器无头刷新无法解密宿主写入的 profile cookie，故改以 wx.lic 的 Cookie 为可信源。
        """
        with sync_playwright() as p:
            context = _launch(p, headless=headless, force_bundled=force_bundled)
            page = context.new_page()
            # 扫码模式下绝不注入种子 Cookie：过期 Cookie 会让页面停在“已登录但失效”的
            # 状态，既出不来二维码，也会让下面的等待逻辑误判为“已拿到 Cookie”而直接退出。
            if wait_login:
                seed_cookie = ""
                try:
                    context.clear_cookies()
                except Exception:
                    pass
            # 注入种子 Cookie（来源：wx.lic，先去重避免重复键）。即便 profile 跨平台
            # 无法复用也能复用登录态；若种子本身已过期，则必须扫码重建有效 session。
            seed_cookie = _dedupe_cookie(seed_cookie)
            if seed_cookie:
                try:
                    cookies = []
                    for item in seed_cookie.split(";"):
                        item = item.strip()
                        if "=" in item:
                            name, _, value = item.partition("=")
                            name = name.strip()
                            value = value.strip()
                            if name and value:
                                cookies.append({
                                    "name": name,
                                    "value": value,
                                    "url": "https://weread.qq.com",
                                })
                    if cookies:
                        context.add_cookies(cookies)
                        if verbose:
                            print(f"[refresh] 已注入 wx.lic 种子 Cookie（{len(cookies)} 项）")
                except Exception as e:
                    if verbose:
                        print(f"[refresh] 注入种子 Cookie 失败（不影响）: {e}")
            try:
                cookie = _extract_cookie_from_page(page, context, url)
                cookie = _dedupe_cookie(cookie)
                if wait_login:
                    # 扫码模式：只有“验证真实有效”的 Cookie 才算数；否则保持可见窗口，
                    # 轮询等待用户扫码登录（登录态持久化到 profile_dir）。
                    if cookie and _verify_cookie(cookie):
                        return cookie
                    # 确保窗口停在可扫码的登录入口（reader 页未登录时可能是报错页）
                    try:
                        page.goto("https://weread.qq.com/", wait_until="domcontentloaded",
                                  timeout=30000)
                    except Exception:
                        pass
                    if verbose:
                        print("[refresh] 请在弹出的 Chrome 窗口中用微信扫码登录微信读书…")
                    deadline = time.time() + timeout_s
                    while time.time() < deadline:
                        try:
                            cookies = context.cookies("https://weread.qq.com")
                            ck = _dedupe_cookie(
                                "; ".join(f"{c['name']}={c['value']}" for c in cookies))
                        except Exception:
                            ck = ""
                        if "wr_vid=" in ck and _verify_cookie(ck):
                            cookie = ck
                            break
                        time.sleep(3)
                    else:
                        cookie = ""
                return cookie
            finally:
                context.close()

    # 1) 常规无头刷新：登录态持久化，通常直接拿到有效 Cookie
    #    注入 wx.lic 已有 Cookie 作为种子（跨平台 profile 不互通时的可信回退）
    seed_cookie = (data.get("cookie") or "").strip()
    cookie = _try(headless=True, force_bundled=force_bundled, seed_cookie=seed_cookie)
    # 必须实打实验证 Cookie 真能拉到数据，避免把过期 Cookie 误判为有效（假阳性）
    if cookie and "wr_vid=" in cookie and _verify_cookie(cookie):
        vid = extract_vid(cookie)
        if _save_cookie_if_changed(cookie, data.get("cookie", ""), name=data.get("name", "")):
            if verbose:
                print(f"[refresh] Cookie 已自动轮换并保存 (vid={vid})")
        elif verbose:
            print("[refresh] Cookie 仍有效，但服务端未轮换新值；未重复写入")
        return True

    # 2) 未拿到有效 Cookie（或拿到但验证失败＝过期）
    if headless_only:
        if verbose:
            print("[refresh] Cookie 无效/已过期（接口返回 -2012）。无头模式不弹窗，"
                  "请在本机运行 'python scripts/refresh_weread_cookie.py' 扫码登录后刷新")
        return False

    # 3) 弹可见窗口，提示扫码登录，等待登录后更新
    if verbose:
        print("[refresh] 未获取到有效 Cookie，已打开浏览器窗口，请扫码登录微信读书…")
        print("[refresh] 等待登录（最长 10 分钟），登录成功后自动保存 Cookie…")
    cookie = _try(headless=False, wait_login=True, timeout_s=600,
                 force_bundled=force_bundled, seed_cookie=seed_cookie)
    if cookie and "wr_vid=" in cookie and _verify_cookie(cookie):
        vid = extract_vid(cookie)
        if _save_cookie_if_changed(cookie, data.get("cookie", ""), name=data.get("name", "")):
            if verbose:
                print(f"[refresh] 扫码登录后 Cookie 已轮换并保存 (vid={vid})")
        elif verbose:
            print("[refresh] 扫码登录后 Cookie 仍有效且未变化；未重复写入")
        return True

    if verbose:
        print("[refresh] 等待扫码超时或 Cookie 仍无效，请检查微信读书登录状态")
    return False


def request_host_refresh(timeout_s: int = 180) -> dict:
    """容器内调用宿主机刷新代理（**不在容器内启动浏览器**）。

    浏览器刷新动作由宿主机代理完成（macOS 钥匙串加密的 profile 容器内解不开）。
    容器内只负责：在同步文章前，发现/怀疑 Cookie 过期时，请宿主机代理去刷新，
    代理把最新明文 Cookie 写回数据卷 wx.lic，容器随后读取它同步文章。

    可通过环境变量 ``WEREAD_REFRESH_AGENT_URL`` 配置代理地址
    （默认 http://host.docker.internal:9876/refresh）。

    返回: ``{"triggered": bool, "ok": bool, "needs_scan": bool, "message": str}``
      - triggered=False 表示未配置代理（手动模式，跳过自动刷新，不报错）；
      - ok=False 且 needs_scan=True 表示登录态过期需扫码，调用方应中止任务并提示用户；
      - ok=False 且 needs_scan=False 表示代理调用本身失败（网络/代理未启动等）。
    """
    import urllib.request

    agent_url = (os.environ.get("WEREAD_REFRESH_AGENT_URL") or "").strip()
    if not agent_url:
        # 未配置代理：保持向后兼容，不强制刷新（手动模式）
        return {
            "triggered": False,
            "ok": True,
            "needs_scan": False,
            "message": "未配置 WEREAD_REFRESH_AGENT_URL，跳过自动刷新（手动模式）",
        }
    if not agent_url.endswith("/refresh"):
        agent_url = agent_url.rstrip("/") + "/refresh"
    try:
        req = urllib.request.Request(
            agent_url,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {
            "triggered": True,
            "ok": bool(payload.get("ok")),
            "needs_scan": bool(payload.get("needs_scan")),
            "message": payload.get("message", ""),
        }
    except Exception as e:
        return {
            "triggered": True,
            "ok": False,
            "needs_scan": False,
            "message": f"调用宿主机刷新代理失败: {e}",
        }


def start_weread_cookie_refresh_thread(interval_hours: float = 0.5) -> bool:
    """在当前 RSS 进程内启动 Cookie 保活线程。

    线程只在 wx.lic 配置了 cookie_refresh_url 后执行：每个周期都用容器内
    Playwright Chromium 无头访问页面，尝试捕获服务端轮换后的 Cookie，并经真实
    接口验证后写回 wx.lic。未轮换时不重复写文件；失效且无法无头恢复时提示扫码。
    刷新失败只记录日志，不阻塞 RSS 主服务。
    """
    global _refresh_thread_started
    with _refresh_thread_lock:
        if _refresh_thread_started:
            return True
        _refresh_thread_started = True

    stop_event = threading.Event()

    def _worker():
        interval_seconds = max(float(interval_hours) * 3600, 300)
        expired_notice_sent = False
        while not stop_event.is_set():
            try:
                _, data = _load_weread_data()
                url = (data.get("cookie_refresh_url") or "").strip()
                if url:
                    ok = refresh_weread_cookie(
                        verbose=True,
                        headless_only=True,
                        force_bundled=True,
                        cooldown_hours=0,
                    )
                    if ok:
                        expired_notice_sent = False
                    else:
                        # 刷新尝试失败本身不代表 Cookie 失效。重新读取 wx.lic，
                        # 再用书架接口确认，避免把网络/浏览器故障误报为需要扫码。
                        _, latest_data = _load_weread_data()
                        current_cookie = (latest_data.get("cookie") or "").strip()
                        cookie_status = (
                            _cookie_status(current_cookie) if current_cookie else "unknown"
                        )
                        if cookie_status == "invalid":
                            print("[refresh] Cookie 已确认失效，无头刷新未恢复登录态")
                            if not expired_notice_sent:
                                try:
                                    _send_cookie_expired_notice()
                                    expired_notice_sent = True
                                    print("[refresh] 已通过配置的通知渠道提醒重新扫码")
                                except Exception as e:
                                    print(f"[refresh] 发送扫码提醒失败: {e}")
                        elif cookie_status == "valid":
                            # 若用户已在后台重新扫码，下一次失效时可以再次提醒。
                            expired_notice_sent = False
                            print("[refresh] 自动刷新未完成，但现有 Cookie 仍有效")
                        else:
                            print("[refresh] 自动刷新未完成，Cookie 状态无法确认")
                else:
                    print("[refresh] 未配置 cookie_refresh_url，自动刷新线程等待配置")
            except Exception as e:
                print(f"[refresh] 自动刷新线程异常（不影响 RSS 服务）: {e}")
            stop_event.wait(interval_seconds)

    thread = threading.Thread(
        target=_worker,
        name="weread-cookie-refresh",
        daemon=True,
    )
    thread.start()
    return True


if __name__ == "__main__":
    import sys

    ok = refresh_weread_cookie(verbose=True)
    sys.exit(0 if ok else 1)

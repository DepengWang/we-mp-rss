from asyncio import wait_for
from socket import timeout
import sys

from sqlalchemy import False_

import driver
from .playwright_driver import PlaywrightController
from PIL import Image
from driver.success import Success
import time
import os
from driver.success import getStatus
from driver.store import Store
import re
from threading import Timer, Lock
from .cookies import expire
import json
import psutil
from core.print import print_error,print_warning,print_info,print_success
class Wx:
    _haslogin=False
    SESSION=None
    HasCode=False
    isLOCK=False
    WX_LOGIN="https://mp.weixin.qq.com/"
    WX_HOME="https://mp.weixin.qq.com/cgi-bin/home"
    wx_login_url="static/wx_qrcode.png"
    lock_file_path="data/lock.lock"
    CallBack=None
    Notice=None
    ext_data = None
    # 添加线程锁保护共享变量
    _login_lock = Lock()
    def __init__(self):
        self.lock_path=os.path.dirname(self.lock_file_path)
        self.refresh_interval=3660*24
        self.controller=PlaywrightController()
        self._account_operation_lock = Lock()
        self._qr_login_in_progress = False
        self._last_qr_auth_error = ""
        if not os.path.exists(self.lock_path):
            os.makedirs(self.lock_path)
        self.Clean()
        self.release_lock()
        pass

    def GetHasCode(self):
        if os.path.exists(self.wx_login_url):
            return True
        return False
    async def extract_token_from_requests(self):
        """从页面中提取token（异步）"""
        try:
            # 优先使用临时控制器，其次使用默认控制器
            controller = getattr(self, '_temp_controller', None) or self.controller

            if not controller or not controller.page:
                return None

            page = controller.page
            # 尝试从当前URL获取token
            current_url = page.url
            token_match = re.search(r'token=([^&]+)', current_url)
            if token_match:
                return token_match.group(1)

            # 尝试从localStorage获取
            token = await page.evaluate("() => localStorage.getItem('token')")
            if token:
                return token

            # 尝试从sessionStorage获取
            token = await page.evaluate("() => sessionStorage.getItem('token')")
            if token:
                return token

            # 尝试从cookie获取
            cookies = await page.context.cookies()
            for cookie in cookies:
                if 'token' in cookie['name'].lower():
                    return cookie['value']

            return ''
        except Exception as e:
            print(f"提取token时出错: {str(e)}")
            return ''

    def _account_rows_locator(self, page):
        return page.locator(
            ".switch-account-dialog .switch-account-dialog_section:has-text('公众号') .section-item,"
            ".switch-account-dialog .switch-account-dialog_section:has-text('服务号') .section-item"
        )

    async def _open_account_switch_dialog(self):
        page = self.controller.page
        if page is None:
            raise RuntimeError("公众号后台页面不可用")

        dialog = page.locator(".switch-account-dialog").first
        if await dialog.count() and await dialog.is_visible():
            return dialog

        account_info = page.locator(".weui-desktop-account__info").first
        if not await account_info.count():
            raise RuntimeError("未找到公众号账号信息区域，请重新扫码授权")
        account_panel = page.locator(".account_box-panel").first
        if not await account_panel.count() or not await account_panel.is_visible():
            await account_info.click()
        await account_panel.wait_for(state="visible", timeout=8000)
        switch_link = account_panel.locator(
            "li.account_box-panel-item:has-text('切换账号') a"
        ).first
        if not await switch_link.count():
            raise RuntimeError("未找到公众号后台的切换账号入口")
        await switch_link.click()
        await dialog.wait_for(state="visible", timeout=10000)
        return dialog

    async def _close_account_switch_dialog(self):
        page = self.controller.page
        if page is None:
            return
        dialog = page.locator(".switch-account-dialog").first
        if not await dialog.count() or not await dialog.is_visible():
            return
        try:
            await page.keyboard.press("Escape")
            await dialog.wait_for(state="hidden", timeout=2000)
        except Exception:
            pass

    async def _read_account_switch_entries(self):
        """Read all accounts shown by the platform switcher, including the active one."""
        page = self.controller.page
        rows = self._account_rows_locator(page)
        entries = []
        seen_ids = set()
        for index in range(await rows.count()):
            row = rows.nth(index)
            try:
                row_text = (await row.inner_text(timeout=5000) or "").strip()
                account_id_locator = row.locator("p").first
                name_locator = row.locator(".section-item__nickname").first
                if not await account_id_locator.count() or not await name_locator.count():
                    continue
                account_id = (await account_id_locator.text_content(timeout=5000) or "").strip()
                name = (await name_locator.text_content(timeout=5000) or "").strip()
            except Exception as e:
                print_warning(f"读取第 {index + 1} 个公众号账号条目失败，已跳过: {e}")
                continue
            if not account_id or not name or account_id in seen_ids:
                continue
            seen_ids.add(account_id)
            entries.append({
                "account_id": account_id,
                "name": name,
                "is_active": "当前登录" in row_text,
                "selector_index": index,
            })
        return entries

    async def _switch_to_account_entry(self, target):
        """Switch the shared browser session to a specific entry and verify its name."""
        page = self.controller.page
        await self._open_account_switch_dialog()
        entries = await self._read_account_switch_entries()
        matches = [entry for entry in entries if entry["account_id"] == target["account_id"]]
        if len(matches) != 1:
            raise RuntimeError(f"切换列表中找不到唯一账号：{target.get('name', '')}")
        selected = matches[0]
        if not selected["is_active"]:
            row = self._account_rows_locator(page).nth(selected["selector_index"])
            await row.locator("p").first.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)

        actual = await self._extract_wechat_data()
        actual_name = str((actual or {}).get("wx_app_name") or "").strip()
        expected_name = str(target.get("name") or "").strip()
        if not actual_name or actual_name != expected_name:
            raise RuntimeError(
                f"账号切换校验失败：目标“{expected_name}”，页面当前为“{actual_name or '未知'}”"
            )

        # The display name is not a unique key. When the platform exposes its
        # active-row marker, verify that the requested account_id is active too.
        await self._open_account_switch_dialog()
        active_entries = [entry for entry in await self._read_account_switch_entries() if entry["is_active"]]
        if len(active_entries) > 1:
            raise RuntimeError("账号切换校验失败：后台列表标记了多个当前激活账号")
        if len(active_entries) == 1 and active_entries[0]["account_id"] != target["account_id"]:
            raise RuntimeError(
                f"账号切换校验失败：请求账号 ID {target['account_id']}，"
                f"后台当前激活 ID 为 {active_entries[0]['account_id']}"
            )
        return actual

    async def _restore_cached_account_session(self, account_id: str, expected_name: str = ""):
        """Restore a previously captured account bundle if the live switcher is unavailable."""
        from urllib.parse import quote
        from driver.token import get_account_session_data

        account_id = str(account_id or "").strip()
        cached = get_account_session_data(account_id)
        token = str(cached.get("token") or "").strip()
        cookies = cached.get("cookies")
        if not token or not isinstance(cookies, list) or not cookies:
            raise RuntimeError(f"账号 {account_id} 没有可恢复的完整 Token/Cookie 会话")

        expiry = cached.get("expiry") or {}
        expiry_timestamp = expiry.get("expiry_timestamp")
        try:
            if expiry_timestamp is not None and float(expiry_timestamp) <= time.time():
                raise RuntimeError(f"账号 {account_id} 的缓存会话已过期，请重新扫码授权")
        except (TypeError, ValueError):
            pass

        if not getattr(self, "controller", None):
            self.controller = PlaywrightController()
        if not self.controller.is_page_valid() or self.controller.page is None:
            await self.controller.start_browser()
        page = self.controller.page
        if page is None:
            raise RuntimeError("无法启动公众号后台页面，不能恢复缓存会话")

        await page.context.clear_cookies()
        await self.controller.add_cookies(cookies)
        target_url = (
            f"{self.WX_HOME}?t=home/index&lang=zh_CN&token={quote(token, safe='')}"
        )
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        actual = await self._extract_wechat_data()
        actual_name = str((actual or {}).get("wx_app_name") or "").strip()
        expected_name = str(expected_name or cached.get("name") or "").strip()
        if not actual_name or (expected_name and actual_name != expected_name):
            raise RuntimeError(
                f"缓存会话恢复校验失败：期望“{expected_name or '未知'}”，"
                f"页面当前为“{actual_name or '未知'}”"
            )
        await self._persist_active_account_session(account_id)
        if not await self.Token(isClose=False):
            raise RuntimeError(f"账号 {account_id} 的缓存会话无法通过公众号授权校验")

        await self._open_account_switch_dialog()
        entries = await self._read_account_switch_entries()
        matches = [entry for entry in entries if entry["account_id"] == account_id]
        active_entries = [entry for entry in entries if entry["is_active"]]
        await self._close_account_switch_dialog()
        if len(matches) != 1:
            raise RuntimeError(f"缓存恢复后的后台账号列表中找不到账号 ID：{account_id}")
        if len(active_entries) != 1:
            raise RuntimeError("缓存会话恢复后无法唯一确认后台当前激活账号 ID")
        if active_entries[0]["account_id"] != account_id:
            raise RuntimeError(
                f"缓存会话恢复校验失败：目标 ID {account_id}，"
                f"后台当前激活 ID 为 {active_entries[0]['account_id']}"
            )
        return self.ext_data or actual

    async def _capture_account_status(self, account):
        """Capture account metadata and a private credential snapshot for restoration."""
        cookies = await self.controller.get_cookies()
        token = await self.extract_token_from_requests()
        session = self.format_token(cookies, str(token or ""))
        ext_data = await self._extract_wechat_data()
        expiry = session.get("expiry") or {}
        expiry_timestamp = expiry.get("expiry_timestamp")
        try:
            expiry_timestamp = float(expiry_timestamp) if expiry_timestamp is not None else None
        except (TypeError, ValueError):
            expiry_timestamp = None

        remaining = int(expiry_timestamp - time.time()) if expiry_timestamp is not None else None
        page_url = str(self.controller.page.url or "").lower()
        if remaining is not None and remaining <= 0:
            token_status = "expired"
        elif token and "login" not in page_url:
            token_status = "valid"
        elif "login" in page_url:
            token_status = "expired"
        else:
            token_status = "unknown"

        expiry_time = str(expiry.get("expiry_time") or "")
        if not expiry_time and expiry_timestamp is not None:
            expiry_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry_timestamp))

        return {
            "account_id": str(account["account_id"]),
            "name": str(account["name"]),
            "token_status": token_status,
            "expiry": {
                "expiry_timestamp": expiry_timestamp,
                "expiry_time": expiry_time,
            },
            "ext_data": ext_data or {},
            "session": {
                "account_id": str(account["account_id"]),
                "name": str(account["name"]),
                "token": str(session.get("token") or ""),
                "cookies": session.get("cookies") or [],
                "cookies_str": str(session.get("cookies_str") or ""),
                "fingerprint": str(session.get("fingerprint") or ""),
                "expiry": expiry,
                "wx_login_url": str(session.get("wx_login_url") or ""),
                "ext_data": ext_data or {},
                "token_status": token_status,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            },
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }

    async def _persist_active_account_session(self, account_id: str = ""):
        """Persist the active global session and, when known, its account-specific copy."""
        cookies = await self.controller.get_cookies()
        token = await self.extract_token_from_requests()
        if not token:
            raise RuntimeError("当前公众号页面未能提取到 Token")
        ext_data = await self._extract_wechat_data()
        if not (ext_data or {}).get("wx_app_name"):
            raise RuntimeError("当前公众号名称读取失败，未更新登录状态")

        session = self.format_token(cookies, str(token))
        self.SESSION = session
        self.ext_data = ext_data
        with self._login_lock:
            self._haslogin = bool(session.get("expiry"))
        Store.save(cookies)
        from driver.token import set_token, update_account_session_data
        set_token(session, ext_data)
        if account_id:
            update_account_session_data(account_id, {
                "name": str(ext_data.get("wx_app_name") or ""),
                "token": str(session.get("token") or ""),
                "cookies": cookies,
                "cookies_str": str(session.get("cookies_str") or ""),
                "fingerprint": str(session.get("fingerprint") or ""),
                "expiry": session.get("expiry") or {},
                "wx_login_url": str(session.get("wx_login_url") or ""),
                "ext_data": ext_data,
                "token_status": "valid",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            }, active=True)
        from driver.success import setStatus
        setStatus(self._haslogin)
        return ext_data

    async def _active_account_entry(self):
        """Resolve the live account ID and verify it against the visible account name."""
        ext_data = await self._extract_wechat_data()
        current_name = str((ext_data or {}).get("wx_app_name") or "").strip()
        await self._open_account_switch_dialog()
        entries = await self._read_account_switch_entries()
        active = [entry for entry in entries if entry["is_active"]]
        if len(active) > 1:
            raise RuntimeError("公众号账号列表标记了多个当前账号，拒绝保存不确定的会话")
        if len(active) == 1:
            target = active[0]
        elif current_name:
            matches = [entry for entry in entries if entry["name"] == current_name]
            if len(matches) != 1:
                raise RuntimeError("无法唯一确认扫码授权后的公众号 account_id")
            target = matches[0]
        else:
            raise RuntimeError("无法读取当前公众号名称或 account_id")
        if current_name and target["name"] != current_name:
            raise RuntimeError("公众号页面名称与账号切换列表不一致，拒绝保存错误账号")
        return target

    async def _record_live_account_session(self, *, newly_authorized: bool, active: bool):
        from driver.token import record_authorized_account_session

        target = await self._active_account_entry()
        await self._close_account_switch_dialog()
        snapshot = await self._capture_account_status(target)
        account_session = snapshot.pop("session", {})
        if snapshot.get("token_status") != "valid":
            raise RuntimeError("当前公众号 Token 未通过有效性检查")
        if not account_session.get("token") or not account_session.get("cookies"):
            raise RuntimeError("未能取得完整 talking/token 与 Cookie 会话")
        account_session["token_status"] = snapshot["token_status"]
        record_authorized_account_session(
            target["account_id"],
            account_session,
            snapshot,
            newly_authorized=newly_authorized,
            active=active,
        )
        return target

    async def preserve_active_account_before_qr_login(self):
        """Save the valid current account before a new QR login can replace global token_data."""
        import asyncio

        from driver.account_sessions import is_restorable_account_session
        from driver.token import (
            _get_token_data,
            get_account_sessions_data,
            get_account_status_data,
            record_authorized_account_session,
        )

        current = _get_token_data() or {}
        token = str(current.get("token") or "").strip()
        if not token:
            return False
        expiry = current.get("expiry") or {}
        try:
            expiry_at = float(expiry.get("expiry_timestamp"))
        except (TypeError, ValueError):
            expiry_at = None
        if expiry_at is not None and expiry_at <= time.time():
            return False

        ext_data = current.get("ext_data") or self.ext_data or {}
        current_name = str(ext_data.get("wx_app_name") or "").strip()
        cached_status = get_account_status_data()
        sessions = get_account_sessions_data()
        active_id = str(cached_status.get("active_account_id") or "").strip()
        candidates = []
        if active_id and active_id in sessions:
            candidates.append((active_id, sessions[active_id]))
        if current_name:
            candidates.extend(
                (str(account_id), session)
                for account_id, session in sessions.items()
                if str(session.get("name") or "").strip() == current_name
                and str(account_id) != active_id
            )

        for account_id, cached_session in candidates:
            if not is_restorable_account_session(cached_session):
                continue
            if current_name and cached_session.get("name") and cached_session["name"] != current_name:
                continue
            summary = next(
                (
                    item for item in cached_status.get("accounts", [])
                    if isinstance(item, dict) and str(item.get("account_id") or "") == account_id
                ),
                {
                    "account_id": account_id,
                    "name": str(cached_session.get("name") or current_name),
                    "token_status": "valid",
                    "expiry": cached_session.get("expiry") or {},
                    "ext_data": cached_session.get("ext_data") or ext_data,
                    "checked_at": cached_session.get("updated_at") or "",
                },
            )
            record_authorized_account_session(
                account_id,
                cached_session,
                summary,
                active=True,
            )
            return True

        # Older versions kept only the active token and encrypted browser cookies.
        # If its ID is already known, recover that complete bundle without opening a browser.
        active_summary = next(
            (
                item for item in cached_status.get("accounts", [])
                if isinstance(item, dict) and str(item.get("account_id") or "") == active_id
            ),
            {},
        )
        active_name_matches = (
            not active_summary.get("name")
            or not current_name
            or str(active_summary.get("name") or "").strip() == current_name
        )
        if active_id and current_name and active_name_matches:
            cookies = []
            if isinstance(self.SESSION, dict) and self.SESSION.get("token") == token:
                cookies = self.SESSION.get("cookies") or []
            if not cookies:
                try:
                    stored = Store.load()
                    cookies = stored if isinstance(stored, list) else []
                except Exception:
                    cookies = []
            if cookies:
                session = {
                    "account_id": active_id,
                    "name": current_name,
                    "token": token,
                    "cookies": cookies,
                    "cookies_str": str(current.get("cookie") or ""),
                    "fingerprint": str(current.get("fingerprint") or ""),
                    "expiry": expiry,
                    "ext_data": ext_data,
                    "token_status": "valid",
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                }
                summary = {
                    "account_id": active_id,
                    "name": current_name,
                    "token_status": "valid",
                    "expiry": expiry,
                    "ext_data": ext_data,
                    "checked_at": session["updated_at"],
                }
                record_authorized_account_session(active_id, session, summary, active=True)
                return True

        if not self._account_operation_lock.acquire(blocking=False):
            raise RuntimeError("已有公众号账号操作正在进行；本次未启动扫码，以免覆盖当前授权")

        main_queue_was_running = False
        content_queue_was_running = False
        try:
            from core.queue import TaskQueue, ContentTaskQueue
            main_queue_was_running = TaskQueue._is_running
            content_queue_was_running = ContentTaskQueue._is_running
            if TaskQueue._queue.qsize() or ContentTaskQueue._queue.qsize():
                raise RuntimeError("采集队列有待执行任务；请等任务完成后再开始扫码换号")
            if main_queue_was_running:
                TaskQueue.stop()
            if content_queue_was_running:
                ContentTaskQueue.stop()
            for _ in range(120):
                if TaskQueue._current_task is None and ContentTaskQueue._current_task is None:
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("等待采集任务结束超时；本次未启动扫码，当前授权未覆盖")

            # Do not use Token() here: its legacy #jumpUrl selector is no longer
            # present on the current WeChat admin page, so it rejects a perfectly
            # usable session before the QR flow can start. Restore the current
            # browser session directly and verify the account through the account
            # switcher before saving it.
            from urllib.parse import quote

            cookies = []
            if isinstance(self.SESSION, dict) and self.SESSION.get("token") == token:
                cookies = self.SESSION.get("cookies") or []
            if not cookies:
                try:
                    stored = Store.load()
                    cookies = stored if isinstance(stored, list) else []
                except Exception:
                    cookies = []
            if not cookies:
                raise RuntimeError("当前公众号 Cookie 不可用；本次未启动扫码，原授权未覆盖")

            normalized_cookies = []
            for cookie in cookies:
                if not isinstance(cookie, dict):
                    continue
                normalized = dict(cookie)
                normalized.setdefault("domain", "mp.weixin.qq.com")
                normalized.setdefault("path", "/")
                normalized_cookies.append(normalized)
            if not normalized_cookies:
                raise RuntimeError("当前公众号 Cookie 格式无效；本次未启动扫码，原授权未覆盖")

            if not self.controller.is_browser_started() or not self.controller.is_page_valid():
                await self.controller.start_browser()
            await self.controller.add_cookies(normalized_cookies)
            home_url = (
                f"{self.WX_HOME}?t=home/index&lang=zh_CN&token={quote(token, safe='')}"
            )
            if not await self.controller.open_url(home_url):
                raise RuntimeError("当前公众号页面加载失败；本次未启动扫码，原授权未覆盖")

            page = self.controller.page
            page_url = str(page.url or "").lower()
            login_prompt = page.locator("body:has-text('使用账号登录')")
            if "login" in page_url or await login_prompt.count() > 0:
                raise RuntimeError("当前公众号 Cookie 已失效；本次未启动扫码，原授权未覆盖")

            live_data = await self._extract_wechat_data()
            live_name = str((live_data or {}).get("wx_app_name") or "").strip()
            if not live_name or (current_name and live_name != current_name):
                raise RuntimeError("恢复后的公众号名称与原账号不一致；本次未启动扫码，原授权未覆盖")

            target = await self._record_live_account_session(newly_authorized=False, active=True)
            self._haslogin = True
            print_success(f"扫码前已保全当前公众号会话：{target['name']}（{target['account_id']}）")
            return True
        finally:
            try:
                await self._close_account_switch_dialog()
                await self.Close()
            except Exception:
                pass
            try:
                if main_queue_was_running or content_queue_was_running:
                    await asyncio.sleep(1.5)
                if main_queue_was_running:
                    TaskQueue.run_task_background()
                if content_queue_was_running:
                    ContentTaskQueue.run_task_background()
            finally:
                self._account_operation_lock.release()

    async def _save_qr_authorized_account(self):
        """A QR login is successful only after its full session is stored by account_id."""
        return await self._record_live_account_session(newly_authorized=True, active=True)

    async def refresh_account_status(self, progress_callback=None):
        """Inspect each available公众号, cache its complete private session, then restore active account."""
        import asyncio
        from datetime import datetime
        from core.queue import TaskQueue, ContentTaskQueue
        from driver.token import (
            get_account_sessions_data,
            set_account_status_data,
            set_account_sessions_data,
        )

        def report(stage, message, progress=0):
            if progress_callback is not None:
                try:
                    progress_callback(stage, message, progress)
                except Exception:
                    pass

        if not self._account_operation_lock.acquire(blocking=False):
            report("failed", "已有公众号账号操作正在进行，请稍后再试", 100)
            return False

        main_queue_was_running = TaskQueue._is_running
        content_queue_was_running = ContentTaskQueue._is_running
        original = None
        snapshots = []
        scan_complete = False
        restore_ok = True
        scan_error = ""
        try:
            if TaskQueue._queue.qsize() or ContentTaskQueue._queue.qsize():
                raise RuntimeError("采集队列还有待执行任务；请等队列清空后再刷新账号状态")
            report("stopping_queues", "暂停抓取队列，等待当前任务结束…", 5)
            if main_queue_was_running:
                TaskQueue.stop()
            if content_queue_was_running:
                ContentTaskQueue.stop()

            try:
                max_wait = max(1, int(os.getenv("ACCOUNT_STATUS_MAX_WAIT", "120")))
            except (TypeError, ValueError):
                max_wait = 120
            for waited in range(max_wait):
                if TaskQueue._current_task is None and ContentTaskQueue._current_task is None:
                    break
                if waited and waited % 10 == 0:
                    report("stopping_queues", f"等待抓取任务结束（{waited}/{max_wait}s）…", 5)
                await asyncio.sleep(1)
            else:
                raise RuntimeError("等待当前抓取任务超时，未切换公众号；请稍后重试")

            if getStatus() is False:
                raise RuntimeError("公众号后台登录已失效，请先扫码授权")
            if not self.controller or not self.controller.is_page_valid() or self.controller.page is None:
                raise RuntimeError("公众号后台浏览器页面不可用，请先重新扫码授权")

            page = self.controller.page
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            current_data = await self._extract_wechat_data()
            current_name = str((current_data or {}).get("wx_app_name") or "").strip()
            await self._open_account_switch_dialog()
            entries = await self._read_account_switch_entries()
            if not entries:
                raise RuntimeError("公众号切换列表为空，无法读取账号状态")

            active_entries = [entry for entry in entries if entry["is_active"]]
            if len(active_entries) == 1:
                original = active_entries[0]
            elif not active_entries and current_name:
                name_matches = [entry for entry in entries if entry["name"] == current_name]
                if len(name_matches) == 1:
                    original = name_matches[0]
            if original is None:
                raise RuntimeError("无法唯一确认当前激活公众号，已取消扫描以避免切错账号")

            report("scanning", f"正在读取 {len(entries)} 个公众号的 Token 有效期…", 15)
            for index, entry in enumerate(entries):
                if entry["account_id"] != original["account_id"]:
                    report(
                        "scanning",
                        f"正在读取公众号：{entry['name']}（{index + 1}/{len(entries)}）…",
                        15 + int(70 * index / max(1, len(entries))),
                    )
                    await self._switch_to_account_entry(entry)
                else:
                    active_data = await self._extract_wechat_data()
                    if str((active_data or {}).get("wx_app_name") or "").strip() != entry["name"]:
                        raise RuntimeError("当前激活公众号与切换列表不一致，已中止扫描")
                snapshot = await self._capture_account_status(entry)
                saved_session = snapshot.pop("session", {})
                snapshots.append(snapshot)
                # Save each captured account immediately. A later scan/restore error
                # must not discard already refreshed sessions or other cached IDs.
                saved_sessions = get_account_sessions_data()
                previous_session = saved_sessions.get(entry["account_id"], {}) or {}
                saved_session["authorized_at"] = (
                    previous_session.get("authorized_at") or time.time()
                )
                saved_session["last_active_at"] = (
                    previous_session.get("last_active_at") or saved_session["authorized_at"]
                )
                saved_sessions[entry["account_id"]] = saved_session
                set_account_sessions_data(saved_sessions)
                snapshot["authorized_at"] = saved_session["authorized_at"]
                snapshot["last_active_at"] = saved_session["last_active_at"]

            scan_complete = True
            report("restoring", f"账号状态已读取，正在恢复原账号“{original['name']}”…", 90)
        except Exception as e:
            scan_error = str(e)
            print_error(f"刷新公众号账号状态失败: {scan_error}")
            report("failed", scan_error, 100)
        finally:
            try:
                if original is not None:
                    try:
                        try:
                            await self._switch_to_account_entry(original)
                        except Exception:
                            await self._restore_cached_account_session(
                                original["account_id"], original.get("name", "")
                            )
                        await self._persist_active_account_session(original["account_id"])
                    except Exception as e:
                        restore_ok = False
                        print_error(f"恢复原公众号账号失败: {e}")
                        try:
                            # Keep the single active token record aligned with the browser if restore failed.
                            await self._persist_active_account_session()
                        except Exception as sync_error:
                            print_error(f"同步当前公众号登录状态失败: {sync_error}")

                try:
                    await self._close_account_switch_dialog()
                except Exception as e:
                    print_warning(f"关闭公众号切换弹窗失败: {e}")

                if scan_complete and restore_ok and original is not None:
                    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    try:
                        retained_ids = set(get_account_sessions_data())
                        snapshots = [
                            item for item in snapshots
                            if str(item.get("account_id") or "") in retained_ids
                        ]
                        set_account_status_data({
                            "accounts": snapshots,
                            "active_account_id": original["account_id"],
                            "checked_at": checked_at,
                        })
                    except Exception as e:
                        scan_complete = False
                        scan_error = f"账号状态读取完成，但缓存保存失败: {e}"
                        print_error(scan_error)
                        report("failed", scan_error, 100)
                    else:
                        report("done", f"已读取 {len(snapshots)} 个公众号，并恢复到“{original['name']}”", 100)
                elif scan_complete and not restore_ok:
                    scan_error = "账号状态已读取，但未能恢复原激活公众号，请检查当前账号"
                    report("failed", scan_error, 100)
            finally:
                try:
                    if main_queue_was_running or content_queue_was_running:
                        # Let the previous worker leave its one-second queue wait
                        # before starting a replacement worker.
                        await asyncio.sleep(1.5)
                    if main_queue_was_running:
                        TaskQueue.run_task_background()
                except Exception as e:
                    print_error(f"恢复主抓取队列失败: {e}")
                try:
                    if content_queue_was_running:
                        ContentTaskQueue.run_task_background()
                except Exception as e:
                    print_error(f"恢复内容抓取队列失败: {e}")
                finally:
                    self._account_operation_lock.release()

        return scan_complete and restore_ok and original is not None

    async def auto_rotate_account_by_id(self, progress_callback=None):
        """Rotate to the next valid cached account in stable account_id order."""
        from driver.token import get_account_sessions_data, get_account_status_data

        def report(stage, message, progress=0):
            if progress_callback is not None:
                try:
                    progress_callback(stage, message, progress)
                except Exception:
                    pass

        status = get_account_status_data()
        sessions = get_account_sessions_data()
        now = time.time()
        candidates = []
        status_by_id = {
            str(item.get("account_id") or ""): item
            for item in status.get("accounts", [])
            if isinstance(item, dict) and item.get("account_id")
        }
        for account_id, session in sessions.items():
            account_id = str(account_id or "").strip()
            summary = status_by_id.get(account_id, {})
            expiry = (session or {}).get("expiry") or summary.get("expiry") or {}
            try:
                not_expired = float(expiry.get("expiry_timestamp")) > now
            except (TypeError, ValueError):
                not_expired = False
            if (
                account_id
                and (session or {}).get("token")
                and (session or {}).get("cookies")
                and not_expired
                and summary.get("token_status") != "expired"
            ):
                candidates.append(account_id)
        candidates = sorted(set(candidates))
        if len(candidates) < 2:
            report("done", "可自动轮转的有效账号少于两个，本轮保持当前账号", 100)
            return True

        if not self.controller or not self.controller.is_page_valid() or self.controller.page is None:
            report("failed", "公众号后台页面不可用，无法自动轮转账号", 100)
            return False

        try:
            await self._open_account_switch_dialog()
            entries = await self._read_account_switch_entries()
            active = [entry for entry in entries if entry["is_active"]]
            cached_active = str(status.get("active_account_id") or "")
            if len(active) == 1:
                current_id = active[0]["account_id"]
            else:
                current_data = await self._extract_wechat_data()
                current_name = str((current_data or {}).get("wx_app_name") or "").strip()
                cached_row = status_by_id.get(cached_active, {})
                if not current_name or cached_row.get("name") != current_name:
                    raise RuntimeError("无法可靠确认当前 account_id，自动轮转已跳过")
                current_id = cached_active
            live_ids = {entry["account_id"] for entry in entries}
            candidates = [account_id for account_id in candidates if account_id in live_ids]
        except Exception as e:
            report("failed", f"无法读取公众号切换列表，自动轮转已跳过：{e}", 100)
            return False
        finally:
            try:
                await self._close_account_switch_dialog()
            except Exception:
                pass

        if len(candidates) < 2:
            report("done", "后台可切换且会话有效的账号少于两个，本轮保持当前账号", 100)
            return True
        if current_id in candidates:
            target_id = candidates[(candidates.index(current_id) + 1) % len(candidates)]
        else:
            target_id = candidates[0]
        target = next(entry for entry in entries if entry["account_id"] == target_id)
        report("rotating", f"按账号 ID 顺序自动切换到 {target['name']}（{target_id}）…", 70)
        return await self.switch_account(target_id, progress_callback=progress_callback)

    async def switch_account(self, account_id: str = "", progress_callback=None):
        """Switch to the exact公众号 account selected by its account_id."""
        import asyncio
        import os

        if not str(account_id or "").strip():
            return await self.auto_rotate_account_by_id(progress_callback=progress_callback)

        def report(stage: str, message: str, progress: int = 0) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(stage, message, progress)
                except Exception:
                    pass

        account_id = str(account_id or "").strip()
        if not account_id:
            report("failed", "缺少目标公众号 account_id", 100)
            return False

        from core.queue import TaskQueue, ContentTaskQueue

        if not self._account_operation_lock.acquire(blocking=False):
            report("failed", "已有公众号账号操作正在进行，请稍后再试", 100)
            return False

        main_queue_was_running = False
        content_queue_was_running = False
        original = None
        original_is_live = False
        target = None
        switch_attempted = False
        switched = False
        failure_message = "账号切换失败"

        def queue_has_current_task():
            return (
                TaskQueue._current_task is not None
                or ContentTaskQueue._current_task is not None
            )

        def cache_active_account(status_snapshot, active_id):
            from driver.token import get_account_status_data, set_account_status_data

            cached_status = get_account_status_data()
            cached_accounts = {
                str(item.get("account_id")): item
                for item in cached_status.get("accounts", [])
                if isinstance(item, dict) and item.get("account_id")
            }
            if status_snapshot:
                cached_accounts[active_id] = status_snapshot
            set_account_status_data({
                "accounts": list(cached_accounts.values()),
                "active_account_id": active_id,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            })

        report("queued", "任务开始", 0)
        try:
            print("开始切换公众号账号...")
            if TaskQueue._queue.qsize() or ContentTaskQueue._queue.qsize():
                raise RuntimeError("采集队列还有待执行任务；请等队列清空后再切换账号")
            main_queue_was_running = TaskQueue._is_running
            content_queue_was_running = ContentTaskQueue._is_running
            report("stopping_queues", "暂停抓取队列，等待正在运行的任务完成", 5)
            if main_queue_was_running:
                TaskQueue.stop()
            if content_queue_was_running:
                ContentTaskQueue.stop()

            try:
                max_wait = max(1, int(os.getenv("SWITCH_MAX_WAIT", "120")))
            except (TypeError, ValueError):
                max_wait = 120
            waited = 0
            while queue_has_current_task() and waited < max_wait:
                if waited and waited % 5 == 0:
                    report(
                        "stopping_queues",
                        f"等待正在运行的采集任务结束（{waited}/{max_wait} 秒）",
                        min(25, 5 + waited // 6),
                    )
                await asyncio.sleep(1)
                waited += 1
            if queue_has_current_task():
                raise RuntimeError(f"等待采集队列空闲超时（{max_wait} 秒），未执行切换")

            if not hasattr(self, "controller") or self.controller is None:
                self.controller = PlaywrightController()
            if not self.controller.is_page_valid() or self.controller.page is None:
                report("starting_browser", "重新打开公众号后台页面…", 40)
                await self.controller.start_browser()
                await self.controller.open_url(self.WX_LOGIN)
                try:
                    await self.controller.page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(2)

            if self.controller.page is None:
                raise RuntimeError("无法获取公众号后台页面，请重新扫码授权")
            report("clicking", "读取后台可切换账号列表…", 55)
            from driver.token import get_account_session_data, get_account_status_data
            cached_status = get_account_status_data()
            cached_accounts = {
                str(item.get("account_id") or ""): item
                for item in cached_status.get("accounts", [])
                if isinstance(item, dict) and item.get("account_id")
            }
            target = next((item for item in cached_accounts.values() if item.get("account_id") == account_id), None)
            entries = []
            try:
                await self._open_account_switch_dialog()
                entries = await self._read_account_switch_entries()
            except Exception as e:
                print_warning(f"无法读取在线切换列表，将尝试按账号 ID 恢复缓存会话：{e}")
            live_matches = [entry for entry in entries if entry["account_id"] == account_id]
            if len(live_matches) == 1:
                target = live_matches[0]
            elif not get_account_session_data(account_id).get("token"):
                raise RuntimeError(f"在线切换列表中找不到账号 ID {account_id}，且没有完整缓存会话")
            if target is None:
                session = get_account_session_data(account_id)
                if not session:
                    raise RuntimeError(f"没有账号 ID {account_id} 的状态或缓存会话；请先刷新账号状态")
                target = {"account_id": account_id, "name": str(session.get("name") or account_id)}

            active_entries = [entry for entry in entries if entry["is_active"]]
            if len(active_entries) == 1:
                original = active_entries[0]
                original_is_live = True
            else:
                current_data = await self._extract_wechat_data()
                current_name = str((current_data or {}).get("wx_app_name") or "").strip()
                name_matches = [entry for entry in entries if entry["name"] == current_name]
                if len(name_matches) == 1:
                    original = name_matches[0]
                    original_is_live = True
                else:
                    cached_active_id = str(cached_status.get("active_account_id") or "")
                    original = cached_accounts.get(cached_active_id) if cached_active_id else None

            if original and original_is_live and target["account_id"] == original["account_id"]:
                report("checking_token", f"{target['name']} 已是当前激活账号，刷新其授权状态…", 80)
            elif live_matches:
                report("clicking", f"正在按账号 ID 切换到：{target['name']}（{account_id}）…", 70)
                switch_attempted = True
                await self._switch_to_account_entry(target)
            else:
                report("restoring", f"切换列表不可用，正在恢复缓存账号：{target['name']}（{account_id}）…", 70)
                switch_attempted = True
                await self._restore_cached_account_session(account_id, target.get("name", ""))

            report("checking_token", "确认目标账号授权并保存当前会话…", 85)
            session_data = await self._persist_active_account_session(account_id)
            if not await self.Token(isClose=False):
                raise RuntimeError("切换后的公众号登录状态校验失败")
            session_data = self.ext_data or session_data
            if str((session_data or {}).get("wx_app_name") or "").strip() != target["name"]:
                raise RuntimeError("当前公众号名称与所选目标账号不一致")

            account_status = await self._capture_account_status(target)
            from driver.token import update_account_session_data
            session_snapshot = account_status.pop("session", {})
            update_account_session_data(account_id, session_snapshot)
            try:
                cache_active_account(account_status, target["account_id"])
            except Exception as e:
                print_warning(f"账号已切换，但账号状态缓存未能更新: {e}")

            switched = True
            print_success(f"当前激活公众号：{target['name']}（{target['account_id']}）")
            try:
                from jobs.notice import sys_notice
                from core.config import cfg
                expiry_time = (account_status.get("expiry") or {}).get("expiry_time") or "未知"
                await asyncio.sleep(0.5)
                sys_notice(
                    f"账号切换成功\n- 账号名称: {target['name']}\n"
                    f"- 账号ID: {target['account_id']}\n- Token有效期: {expiry_time}",
                    str(cfg.get("server.code_title", "WeRss账号切换成功")),
                )
            except Exception as e:
                print_warning(f"账号已切换，但发送通知失败: {e}")

        except Exception as e:
            failure_message = str(e) or "账号切换失败，请查看服务日志"
            print_error(f"切换公众号账号失败: {failure_message}")
        finally:
            if switch_attempted and not switched and original is not None:
                report("restoring", f"切换未完成，正在恢复原账号：{original['name']}…", 90)
                try:
                    try:
                        await self._switch_to_account_entry(original)
                    except Exception:
                        await self._restore_cached_account_session(
                            original["account_id"], original.get("name", "")
                        )
                    await self._persist_active_account_session(original["account_id"])
                    try:
                        cache_active_account(None, original["account_id"])
                    except Exception as e:
                        print_warning(f"已恢复原账号，但账号状态缓存未能更新: {e}")
                    print_warning(f"已恢复到原公众号账号：{original['name']}")
                except Exception as e:
                    failure_message += f"；恢复原账号也失败：{e}"
                    print_error(f"恢复原公众号账号失败: {e}")

            try:
                await self._close_account_switch_dialog()
            except Exception:
                pass
            try:
                if main_queue_was_running or content_queue_was_running:
                    await asyncio.sleep(1.5)
                if main_queue_was_running:
                    TaskQueue.run_task_background()
                if content_queue_was_running:
                    ContentTaskQueue.run_task_background()
            except Exception as e:
                print_error(f"恢复采集队列失败: {e}")
            self._account_operation_lock.release()

        if switched:
            report("done", f"已切换到：{target['name']}（{target['account_id']}）", 100)
            return True
        report("failed", failure_message, 100)
        return False

    async def _switch_account_random_legacy(self, username: str = "", progress_callback=None):
        raise RuntimeError("随机切换账号已停用；请调用 switch_account(account_id=...)")

        """切换账号功能（异步）

        Args:
            username: 目标账号的用户名，如果为空则切换到其他可用账号
            progress_callback: 可选回调，签名 ``callback(stage: str, message: str, progress: int) -> None``，
                用于将进度广播到前端（SSE / 轮询）。
        """
        import asyncio

        def report(stage: str, message: str, progress: int = 0) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(stage, message, progress)
                except Exception:
                    pass

        if not self._account_operation_lock.acquire(blocking=False):
            report("failed", "已有公众号账号操作正在进行，请稍后再试", 100)
            return False

        print("开始切换账号...")
        report("queued", "任务开始", 0)
        main_queue_was_running = False
        content_queue_was_running = False

        try:
            # 暂停主队列和内容队列，等待当前任务完成
            from core.queue import TaskQueue, ContentTaskQueue
            main_queue_was_running = TaskQueue._is_running
            content_queue_was_running = ContentTaskQueue._is_running

            report("stopping_queues", "暂停抓取队列", 5)
            # 停止队列
            if main_queue_was_running:
                print_info("暂停主任务队列...")
                TaskQueue.stop()
            if content_queue_was_running:
                print_info("暂停内容任务队列...")
                ContentTaskQueue.stop()

            # 等待当前任务真正完成 — 通过配置可缩短；默认仍 120s 以保证抓取原子性
            import os
            try:
                max_wait = int(os.getenv("SWITCH_MAX_WAIT", "120"))
            except Exception:
                max_wait = 120
            wait_interval = 1
            waited = 0

            while waited < max_wait:
                has_current_task = False

                # 检查主队列是否有正在执行的任务
                if TaskQueue._current_task is not None:
                    has_current_task = True
                    print_info(f"主队列任务正在执行: {TaskQueue._current_task.task_name}")

                # 检查内容队列是否有正在执行的任务
                if ContentTaskQueue._current_task is not None:
                    has_current_task = True
                    print_info(f"内容队列任务正在执行: {ContentTaskQueue._current_task.task_name}")

                if not has_current_task:
                    print_success("所有任务已完成，可以安全切换账号")
                    break

                if waited % 5 == 0 and waited > 0:
                    report("stopping_queues", f"等待任务完成中（{waited}/{max_wait}s）", 5 + min(20, waited // 6))
                await asyncio.sleep(wait_interval)
                waited += wait_interval

            if waited >= max_wait:
                print_warning("等待超时，仍有任务未完成，切换账号可能导致会话失效")
                report("stopping_queues", f"队列等待超时，继续尝试（{waited}s）", 25)

            report("checking_token", "检查 Token 有效性", 30)
            await self.Token(isClose=False)
            if getStatus() is False:
                # 提前暴露结果，不阻塞 60s；让前端用 UI 引导用户重新扫码
                print_warning("Token 已过期，请重新扫码登录")
                from jobs.failauth import send_wx_code
                send_wx_code("账号过期，请重新扫码登录")
                report("failed", "Token 已过期，请调用 /auth/wechat/unbind 后重新扫码", 100)
                return False
            await asyncio.sleep(1)

            # 检查 controller 和 Page 对象是否有效；若不存在则回退到重新启动浏览器
            if not hasattr(self, 'controller') or self.controller is None:
                print_warning("Controller 未初始化，正在重新创建浏览器...")
                self.controller = PlaywrightController()

            if not self.controller.is_page_valid() or self.controller.page is None:
                report("starting_browser", "浏览器 Page 无效，正在重新打开公众平台…", 40)
                print_warning("Page 对象无效或为 None，正在重新打开浏览器并导航到公众平台...")
                try:
                    await self.controller.start_browser()
                    await self.controller.open_url(self.WX_LOGIN)
                    # 网络空闲等待超时后继续；后续点击操作再处理是否需要登录
                    try:
                        await self.controller.page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                except Exception as e:
                    print_error(f"重新打开浏览器失败: {str(e)}，无法切换账号")
                    report("failed", f"启动浏览器失败: {e}", 100)
                    return False

            page = self.controller.page
            if page is None:
                print_error("Page 对象仍为 None，无法切换账号")
                report("failed", "无法获取 Page 对象", 100)
                return False

            # 等待页面加载完成，添加异常处理
            report("starting_browser", "等待公众平台首页加载…", 55)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as e:
                print_warning(f"等待页面加载状态失败: {str(e)}，继续尝试切换账号...")

            # 点击账号信息区域打开账号面板
            report("clicking", "打开账号面板…", 65)
            account_info = page.locator(".weui-desktop-account__info")
            if await account_info.count() > 0:
                await account_info.click()
                await asyncio.sleep(1)

                # 等待账号面板显示
                account_panel = page.locator(".account_box-panel")
                if await account_panel.count() > 0:
                    # 查找切换账号按钮（更精确的选择器）
                    switch_account_link = account_panel.locator("li.account_box-panel-item:has-text('切换账号') a")
                    if await switch_account_link.count() > 0:
                        print_info("找到切换账号按钮，点击切换...")
                        await switch_account_link.click()
                        await asyncio.sleep(3)

                        try:
                            # 查找可切换的账号（排除当前登录账号）
                            accounts = page.locator(
                                                ".switch-account-dialog .switch-account-dialog_section:has-text('公众号') .section-item:not(:has-text('当前登录')),"
                                                ".switch-account-dialog .switch-account-dialog_section:has-text('服务号') .section-item:not(:has-text('当前登录'))"
                                            )
                            account_count = await accounts.count()
                            print(f"当前一共有{account_count}个可切换账号")
                            import random
                            if account_count > 0:
                                report("clicking", f"找到 {account_count} 个可切换账号，正在选择…", 75)
                                # 点击第一个可切换的账号
                                random_index = random.randint(0, account_count - 1)
                                await asyncio.sleep(1)
                                p = accounts.nth(random_index).locator("p")
                                nick_name = accounts.nth(random_index).locator(".section-item__nickname")
                                account_id = str(await p.text_content() or "").strip()
                                account_name = str(await nick_name.text_content() or "").strip()
                                print(f"账号: {account_name} ID:{account_id}")
                                await p.click()
                                # 等待页面加载并验证切换成功，添加异常处理
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=10000)
                                except Exception as e:
                                    print_warning(f"等待页面加载状态失败: {str(e)}，继续验证切换结果...")
                                await asyncio.sleep(2)
                                session_data = await self._persist_active_account_session()
                                if not await self.Token(isClose=False):
                                    raise RuntimeError("切换账号后的公众号登录状态校验失败")
                                session_data = self.ext_data or session_data
                                if str(session_data.get("wx_app_name", "")).strip() != str(account_name or "").strip():
                                    raise RuntimeError("切换后公众号名称与目标账号不一致")

                                account_status = await self._capture_account_status({
                                    "account_id": account_id,
                                    "name": account_name,
                                })
                                from driver.token import get_account_status_data, set_account_status_data
                                cached_status = get_account_status_data()
                                cached_accounts = {
                                    item.get("account_id"): item
                                    for item in cached_status.get("accounts", [])
                                    if isinstance(item, dict) and item.get("account_id")
                                }
                                cached_accounts[account_id] = account_status
                                try:
                                    set_account_status_data({
                                        "accounts": list(cached_accounts.values()),
                                        "active_account_id": account_id,
                                        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                                    })
                                except Exception as e:
                                    print_warning(f"账号已切换，但账号状态缓存未能更新: {e}")

                                print_success(f"账号切换成功: {account_name}")
                                from jobs.notice import sys_notice
                                from core.config import cfg
                                try:
                                    exp_time = account_status["expiry"].get("expiry_time", "未知")
                                except Exception as e:
                                    print_error(e)
                                    exp_time = "-"
                                    pass
                                # 添加延迟，避免 Playwright memoryview 缓冲区问题
                                await asyncio.sleep(0.5)
                                sys_notice(f"账号切换成功\n- 账号名称: {account_name} \n- 账号ID: {account_id} \n- Token有效期: {exp_time}", str(cfg.get("server.code_title","WeRss账号切换成功")))
                                report("done", f"切换成功：{account_name}", 100)
                                return True
                            else:
                                print_warning("没有找到可切换的账号")
                                report("failed", "没有可切换的账号（只剩当前登录账号）", 100)
                                return False
                        except Exception as e:
                            print_error(f"切换账号时发生错误: {str(e)}")
                            report("failed", f"切换错误: {e}", 100)
                            return False
                    else:
                        print_warning("未找到切换账号按钮")
                        report("failed", "未找到切换账号按钮（公众平台改版？）", 100)
                        return False
                else:
                    print_warning("账号面板未打开")
                    report("failed", "账号面板未打开", 100)
                    return False
            else:
                print_warning("未找到账号信息区域")
                # 选择器 miss：兜底触发 QR 流（仅本地接口返回 ok=False，让前端引导重新扫码）
                report("failed", "未找到账号信息区域，请重新扫码授权", 100)
                return False

        except Exception as e:
            print_error(f"切换账号时发生错误: {str(e)}")
            report("failed", f"切换错误: {e}", 100)
            return False
        finally:
            # 恢复任务队列
            try:
                from core.queue import TaskQueue, ContentTaskQueue
                print_info(f"准备恢复队列: 主队列={main_queue_was_running}, 内容队列={content_queue_was_running}")
                if main_queue_was_running:
                    print_info("恢复主任务队列...")
                    TaskQueue.run_task_background()
                    print_success("主任务队列已恢复")
                if content_queue_was_running:
                    print_info("恢复内容任务队列...")
                    ContentTaskQueue.run_task_background()
                    print_success("内容任务队列已恢复")
            except Exception as e:
                print_error(f"恢复队列失败: {e}")
            finally:
                self._account_operation_lock.release()
    def GetCode(self,CallBack=None,Notice=None):
        self.Notice=Notice
        self._last_qr_auth_error = ""
        if  self.check_lock():
            print_warning("微信公众平台登录脚本正在运行，请勿重复运行")
            return {
                "code":f"{self.wx_login_url}?t={(time.time())}",
                "msg":"微信公众平台登录脚本正在运行，请勿重复运行！"}

        self.Clean()
        print("子线程执行中")

        def run_wxLogin():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.wxLogin(CallBack, True))
            finally:
                loop.close()

        from core.thread import ThreadManager
        self.thread = ThreadManager(target=run_wxLogin)
        self.thread.start()  # 启动线程
        from core.ver import VERSION
        print(f"微信公众平台登录 v{VERSION}")
        return WX_API.QRcode()
    
    wait_time=1
    def QRcode(self):
        return {
            "code":f"/{self.wx_login_url}?t={(time.time())}",
            "is_exists":self.GetHasCode(),
        }
    async def refresh_task(self):
        try:
            await self.controller.page.reload()
            await self.Call_Success()
            # 检查登录状态
            if "home" not in self.controller.page.url:
                print("检测到登录已过期，请重新登录")
                raise Exception(f"登录已经失效，请重新登录")
        except Exception as e:
            raise Exception(f"浏览器关闭")  # 重新抛出异常以便外部捕获处理
    def QrStatus(self):
        return {
            "login_status": self.HasLogin() and not self._last_qr_auth_error,
            "qr_code": self.GetHasCode(),
            "error": self._last_qr_auth_error,
        }

    def HasLogin(self):
        with self._login_lock:
            return self._haslogin
    async def schedule_refresh(self):
        import asyncio

        if self.refresh_interval <= 0:
            return

        with self._login_lock:
            if not self._haslogin or not hasattr(self, 'controller') or self.controller is None:
                return

        try:
            await self.refresh_task()
            # 使用守护线程避免资源泄露
            async def schedule_next():
                await asyncio.sleep(self.refresh_interval)
                await self.schedule_refresh()

            # 在后台启动下一次刷新
            asyncio.create_task(schedule_next())
        except Exception as e:
            print_error(f"定时刷新任务失败: {str(e)}")
            # 不再抛出异常，避免无限循环
    async def Token(self, callback=None, isClose=True):
        """使用token登录（异步）

        Args:
            callback: 登录成功回调函数
            isClose: 是否在完成后关闭浏览器

        Returns:
            登录成功返回session数据,失败返回None或False
        """
        import asyncio

        try:
            self.CallBack = callback
            if not getStatus():
                print_warning("登录状态检查失败")
                return None

            from driver.token import get as get_val
            token = str(get_val("token", ""))
            if not token:
                print_warning("未找到有效的token")
                return None

            # 复用已有的controller实例,避免重复创建
            if not hasattr(self, 'controller') or self.controller is None:
                self.controller = PlaywrightController()

            controller = self.controller
            # 保存到临时变量，让 Call_Success 和 _extract_wechat_data 能使用
            self._temp_controller = controller

            # 检查浏览器是否已启动且 Page 对象有效
            if not controller.is_browser_started() or not controller.is_page_valid():
                print_info("浏览器未启动或 Page 对象无效，正在启动...")
                await controller.start_browser()

                cookie = Store.load()
                if cookie:
                    # 为每个cookie添加必要的domain字段
                    for c in cookie:
                        if 'domain' not in c:
                            c['domain'] = 'mp.weixin.qq.com'
                        if 'path' not in c:
                            c['path'] = '/'
                    await controller.add_cookies(cookie)

            # 打开URL前再次检查 Page 对象有效性
            if not controller.is_page_valid():
                print_error("Page 对象仍然无效，无法继续")
                return False

            await controller.open_url(f"{self.WX_HOME}?t=home/index&lang=zh_CN&token={token}")
            page = controller.page

            # 验证 page 对象是否有效
            if page is None:
                print_error("Page 对象为 None，无法继续操作")
                return False

            qrcode = page.locator("#jumpUrl")
            await qrcode.wait_for(state="visible", timeout=self.wait_time * 1000)
            await qrcode.click()
            await asyncio.sleep(2)
            hasLogin = page.locator("body:has-text('使用账号登录')")
            if await hasLogin.count() > 0:
                self._haslogin = False
                from jobs.failauth import send_wx_code
                import threading
                threading.Thread(target=send_wx_code,args=(f"公众号平台登录失效,请重新登录",)).start()
                return False
            return await self.Call_Success()
        except ImportError as e:
            print_error(f"导入模块失败: {str(e)}")
            return False
        except Exception as e:
            print_error(f"Token操作失败: {str(e)}")
            return False
        finally:
            # 清理临时控制器引用
            self._temp_controller = None
            # 只在明确要求关闭时才清理
            if isClose:
                try:
                    if hasattr(self, 'controller') and self.controller:
                        await self.controller.cleanup()
                except Exception:
                    pass
    def isLock(self):             
        if self.isLock:
            if os.path.exists(self.wx_login_url):
                try:
                    size=os.path.getsize(self.wx_login_url)
                    return size>364
                except Exception as e:
                    print(f"二维码图片获取失败: {str(e)}")
        return self.isLock
    async def _wait_qrcode_ready(self, page, qr_tag, timeout=30):
        """等待登录二维码图片加载完成（异步）

        新版登录页可能默认展示"微信快捷登录"（open.weixin.qq.com iframe），
        此时经典二维码处于隐藏状态且src为空，直接截图会得到空白图片或超时。
        检测到该情况时点击"扫码登录"链接切换回二维码登录，
        再等待二维码图片可见且真正加载完成。
        """
        import asyncio

        deadline = time.time() + timeout
        switch_clicked = False
        while time.time() < deadline:
            ready = await page.evaluate(
                """(sel) => {
                    const img = document.querySelector(sel);
                    if (!img) return false;
                    const style = window.getComputedStyle(img);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    return !!img.src && img.complete && img.naturalWidth > 50;
                }""", qr_tag)
            if ready:
                return True
            if not switch_clicked:
                switch_link = page.locator("a.login__type__container__link_text", has_text="扫码登录")
                if await switch_link.count() > 0 and await switch_link.first.is_visible():
                    print_info("检测到快捷登录页面，切换到扫码登录...")
                    await switch_link.first.click()
                    switch_clicked = True
            await asyncio.sleep(0.5)
        return False

    async def wxLogin(self, CallBack=None, NeedExit=True):
        """
        微信公众平台登录流程（异步）：
        1. 检查依赖和环境
        2. 打开微信公众平台
        3. 全屏截图保存二维码
        4. 等待用户扫码登录
        5. 获取登录后的cookie和token
        6. 启动定时刷新线程(默认30分钟刷新一次)
        """
        import asyncio

        # 使用上下文管理器确保资源清理
        try:
            if self.check_lock():
                print_warning("微信公众平台登录脚本正在运行，请勿重复运行")
                return None

            self.set_lock()

            # 使用更短的锁持有时间，只保护变量修改
            self._login_lock.acquire()
            self._haslogin = False
            self._login_lock.release()

            # 清理现有资源
            self.cleanup_resources()

            self.controller = PlaywrightController()
            # 初始化浏览器控制器
            driver = self.controller
            # 启动浏览器并打开微信公众平台
            print_info("正在启动浏览器...")
            await driver.start_browser()
            await driver.open_url(self.WX_LOGIN)
            page = driver.page

            # 等待页面完全加载
            # 新版登录页存在持续的轮询请求，networkidle可能永远不触发，
            # 超时不视为错误，由后续二维码加载检测兜底
            print_info("正在加载登录页面...")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # 定位二维码区域
            qr_tag = ".login__type__container__scan__qrcode"
            # 新版登录页可能默认展示"微信快捷登录"，二维码被隐藏或src为空，
            # 需要先切换回扫码登录并等待二维码图片真正加载完成
            if not await self._wait_qrcode_ready(page, qr_tag):
                raise Exception("二维码加载超时，请重试")
            # 获取二维码图片URL
            qrcode = await page.query_selector(qr_tag)
            code_src = await qrcode.get_attribute("src")
            print("正在生成二维码图片...")
            print(f"code_src:{code_src}")

            # 使用Playwright截图功能（添加异常处理）
            await qrcode.screenshot(path=self.wx_login_url)

            print("二维码已保存为 wx_qrcode.png，请扫码登录...")
            self.HasCode = True
            if os.path.getsize(self.wx_login_url) <= 364:
                raise Exception("二维码图片获取失败，请重新扫码")
            # 等待登录成功（检测二维码图片加载完成）
            print("等待扫码登录...")
            if self.Notice is not None:
                self.Notice()

            # 监听页面导航事件
            def handle_frame_navigated(frame):
                current_url = frame.url
                if self.WX_HOME in current_url:
                    print(f"登录成功，正在获取cookie和token...")
            page.on('framenavigated', handle_frame_navigated)
            # 只等待主页面跳转到公众平台首页，
            # 不能用 wait_for_event("framenavigated")：页面内的快捷登录iframe
            # 加载时也会触发该事件，导致未扫码就误判为登录成功
            await page.wait_for_url(lambda url: "cgi-bin/home" in url, timeout=5*60 * 1000)

            from .success import setStatus
            with self._login_lock:
                self._haslogin = True
            setStatus(True)
            self.CallBack = CallBack
            self._qr_login_in_progress = True
            try:
                await self.Call_Success()
            finally:
                self._qr_login_in_progress = False
        except Exception as e:
            if "Timeout" in str(e):
                print_warning("\n扫码登录超时，请重新运行程序进行扫码登录")

            else:
                print_error(f"\n错误发生: {str(e)}")
            self.SESSION = None
            if self._last_qr_auth_error:
                from driver.token import _get_token_data
                from driver.success import setStatus
                previous = _get_token_data() or {}
                previous_expiry = previous.get("expiry") or {}
                try:
                    previous_valid = bool(
                        previous.get("token")
                        and float(previous_expiry.get("expiry_timestamp")) > time.time()
                    )
                except (TypeError, ValueError):
                    previous_valid = False
                with self._login_lock:
                    self._haslogin = previous_valid
                self.ext_data = previous.get("ext_data") or {}
                setStatus(previous_valid)
            return self.SESSION
        finally:
            self._qr_login_in_progress = False
            self.release_lock()
            if NeedExit:
                self.Clean()
            await self.Close()
        return self.SESSION
    def format_token(self, cookies: list, token: str = ""):
        cookies_str=""
        for cookie in cookies:
            # print(f"{cookie['name']}={cookie['value']}")
            cookies_str+=f"{cookie['name']}={cookie['value']}; "
            if 'token' in cookie['name'].lower():
                token= token or cookie['value']
        # 计算 slave_sid cookie 有效时间
        cookie_expiry = expire(cookies)
        return{
                'cookies': cookies,
                'cookies_str': cookies_str,
                'token': token,
                'fingerprint': next((
                    str(cookie.get('value') or '')
                    for cookie in cookies
                    if 'fingerprint' in str(cookie.get('name') or '').lower()
                ), ''),
                'wx_login_url': self.wx_login_url,
                'expiry': cookie_expiry
            }
    async def Call_Success(self, has_extdata=True):
        """处理登录成功后的回调逻辑（异步）"""
        # 优先使用临时控制器（用于多线程场景），其次使用默认控制器
        controller = getattr(self, '_temp_controller', None) or self.controller
        if controller is None:
            print_error("浏览器控制器未初始化")
            return None

        # 获取token
        token = await self.extract_token_from_requests()

        # 获取当前所有cookie
        cookies = await controller.get_cookies()
        # print("\n获取到的Cookie:")
        self.SESSION = self.format_token(cookies, str(token))
        with self._login_lock:
            self._haslogin = False if self.SESSION["expiry"] is None else True
        # 登录成功后不立即清理二维码，保持浏览器运行
        if self._haslogin:
            try:
                # 使用更健壮的选择器定位元素
                if has_extdata:
                    self.ext_data = await self._extract_wechat_data()
            except Exception as e:
                print_error(f"获取公众号信息失败: {str(e)}")
                self.ext_data = None
            # 保存新的 token 和 cookie
            if self.SESSION and self.SESSION.get("token"):
                if self._qr_login_in_progress:
                    try:
                        account = await self._save_qr_authorized_account()
                        print_success(
                            f"扫码授权账号已保存：{account['name']}（{account['account_id']}），"
                            "并保留最近三个有效会话"
                        )
                    except Exception as e:
                        self._last_qr_auth_error = (
                            "扫码已完成，但未能确认新公众号 account_id；原授权已保留，"
                            "本次未替换登录。请重试，或先手动刷新账号状态。"
                        )
                        print_error(f"扫码账号会话保存失败: {e}")
                        raise RuntimeError(self._last_qr_auth_error) from e
                Store.save(cookies)
                from driver.token import set_token
                set_token(self.SESSION, self.ext_data)
                print_success("已更新公众号登录信息")
            print_success("登录成功！")
        else:
            print_warning("未登录！")

        # print(cookie_expiry)
        if self.CallBack is not None:
            self.CallBack(self.SESSION, self.ext_data)

        return self.SESSION 

    async def _extract_wechat_data(self):
        """提取微信公众号数据，使用更健壮的选择器（异步）"""
        # 优先使用临时控制器，其次使用默认控制器
        controller = getattr(self, '_temp_controller', None) or self.controller

        if not controller or not controller.page:
            return {}

        page = controller.page
        data = {}

        # 使用更健壮的选择器，增加备选方案
        selectors = {
            "wx_app_name": [".weui-desktop_name", ".acount_box-nickname", ".account_box-panel-head__nickname"],
            "wx_logo": [".weui-desktop-account__img", ".weui-desktop-account__thumb", ".account_box-panel-head__thumb"],
            "wx_read_yesterday": [".weui-desktop-data-overview:nth-child(1) .weui-desktop-data-overview__desc span", ".weui-desktop-data-overview:first-child .weui-desktop-data-overview__desc span"],
            "wx_share_yesterday": [".weui-desktop-data-overview:nth-child(2) .weui-desktop-data-overview__desc span", ".weui-desktop-data-overview:nth-child(1) + .weui-desktop-data-overview .weui-desktop-data-overview__desc span"],
            "wx_watch_yesterday": [".weui-desktop-data-overview:nth-child(3) .weui-desktop-data-overview__desc span", ".weui-desktop-data-overview:last-child .weui-desktop-data-overview__desc span"],
            "wx_yuan_count": [".original_cnt .weui-desktop-user_sum span", ".weui-desktop-user_sum.original_cnt span"],
            "wx_user_count": [".weui-desktop-user_sum:not(.original_cnt) span", ".weui-desktop-user_num .weui-desktop-user_sum span"]
        }

        for key, selector_list in selectors.items():
            data[key] = ""
            selector_found = False

            # 遍历备选选择器
            for selector in selector_list:
                try:
                    element = page.locator(selector)
                    # 先检查元素是否存在，再等待可见
                    if await element.count() > 0:
                        await element.wait_for(state="visible", timeout=2000)
                        if key == "wx_logo":
                            data[key] = await element.get_attribute("src")
                        else:
                            data[key] = await element.text_content()
                        selector_found = True
                        # print_info(f"成功获取{key}，使用选择器: {selector}")
                        break
                except Exception as e:
                    continue

            if not selector_found:
                print_warning(f"获取{key}失败: 所有选择器都无法定位到元素")
                # 对于特定字段，尝试更通用的方法
                if key == "wx_watch_yesterday":
                    try:
                        # 尝试获取所有.data-item .number元素
                        all_numbers = page.locator(".data-item .number")
                        count = await all_numbers.count()
                        if count >= 3:
                            data[key] = await all_numbers.nth(2).text_content()
                            print_info(f"使用通用方法获取{key}成功")
                        elif count > 0:
                            # 如果只有1-2个，取最后一个
                            data[key] = await all_numbers.nth(count-1).text_content()
                            print_info(f"使用备用方法获取{key}成功")
                    except Exception as fallback_e:
                        print_error(f"备用方法也失败: {str(fallback_e)}")

        return data
    
    def cleanup_resources(self):
        """清理所有相关资源"""
        try:
            # 清理临时文件
            self.Clean()
                
            # 重置状态
            with self._login_lock:
                self._haslogin = False
                self.HasCode = False
                
            print_info("资源清理完成")
            return True
        except Exception as e:
            return False

    async def Close(self):
        rel = False
        try:
            if hasattr(self, 'controller') and self.controller is not None:
                await self.controller.Close()
                rel = True
        except Exception as e:
            print_warning("浏览器未启动或已关闭")
            pass
        return rel
    def Clean(self):
        try:
            os.remove(self.wx_login_url)
        except:
            pass
        finally:
           pass
           
    def expire_all_cookies(self):
        """设置所有cookie为过期状态"""
        try:
            if hasattr(self, 'controller') and hasattr(self.controller, 'context'):
                self.controller.context.clear_cookies()
                return True
            else:
                print("浏览器未启动，无法操作cookie")
                return False
        except Exception as e:
            print(f"设置cookie过期时出错: {str(e)}")
            return False
            
    def check_lock(self, timeout: int = 300) -> bool:
        if not os.path.exists(self.wx_login_url):
            return False
        return True
    def set_lock(self):
        """创建锁定文件，写入当前进程PID和时间戳"""
        os.makedirs(os.path.dirname(self.lock_file_path), exist_ok=True)
        current_pid = os.getpid()
        with open(self.lock_file_path, 'w') as f:
            f.write(f"{current_pid}|{time.time()}")
        self.isLOCK = True
        
    def release_lock(self):
        """删除锁定文件"""
        try:
            # 只释放当前进程持有的锁
            if os.path.exists(self.lock_file_path):
                with open(self.lock_file_path, 'r') as f:
                    content = f.read().strip()
                parts = content.split('|')
                if parts and int(parts[0]) == os.getpid():
                    os.remove(self.lock_file_path)
            self.isLOCK = False
            return True
        except Exception:
            return False
    
    def _force_release_lock(self):
        """强制释放锁（用于清理过期或损坏的锁）"""
        try:
            if os.path.exists(self.lock_file_path):
                os.remove(self.lock_file_path)
        except Exception:
            pass


WX_API = Wx()
def GetCode(CallBack:any=None,NeedExit=True):
    WX_API.GetCode(CallBack,NeedExit=NeedExit)

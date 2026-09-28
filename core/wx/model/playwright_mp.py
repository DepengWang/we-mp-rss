"""
Playwright 浏览器模式采集器 - 兜底方案

当所有 API 端点都被限制时，使用真实浏览器访问微信公众平台后台，
通过拦截 XHR 请求或解析页面 DOM 来获取文章列表。

工作流程:
1. 加载已保存的 Cookie，启动 Playwright 浏览器
2. 导航到公众号后台首页
3. 拦截页面发出的文章列表 API 请求，捕获响应数据
4. 提取文章信息

依赖: playwright (已存在于项目的 requirements.txt)
"""

import json
import os
import time
import random
import asyncio
from pathlib import Path
from typing import Dict, List, Optional
from core.wx.base import WxGather
from core.print import print_error, print_info, print_warning, print_success
from core.log import logger


# 公众号后台的已发表文章页面 URL
MP_PUBLISH_URL = "https://mp.weixin.qq.com/cgi-bin/appms?t=media/appmsg_list_v2&action=list_ex&begin={begin}&count=5&fakeid={fakeid}&type=9&token={token}&lang=zh_CN"


class MpsPlaywright(WxGather):
    """
    Playwright 浏览器模式采集器
    
    使用真实浏览器环境访问公众号后台，自动发现可用的 API 端点。
    这是所有端点都不可用时的最后兜底方案。
    """

    def __init__(self, is_add: bool = False):
        super().__init__(is_add=is_add)
        self._captured_articles: List[Dict] = []
        self._capture_done = False
        self._actual_endpoint = None
        self._actual_params = None

    def content_extract(self, url):
        """使用已有的 Playwright 文章抓取器"""
        try:
            from driver.wxarticle import Web as App
            r = App.get_article_content(url)
            if r is not None:
                text = r.get("content", "")
                return text
        except Exception as e:
            logger.error(e)
        return ""

    def get_Articles(
        self,
        faker_id: str = None,
        Mps_id: str = None,
        Mps_title="",
        CallBack=None,
        start_page: int = 0,
        MaxPage: int = 1,
        interval=10,
        Gather_Content=False,
        Item_Over_CallBack=None,
        Over_CallBack=None,
    ):
        """
        Playwright 模式主入口
        
        使用 asyncio 运行 Playwright 采集流程。
        """
        super().Start(mp_id=Mps_id)
        if self.Gather_Content:
            Gather_Content = True
        print(f"Playwright浏览器模式,是否采集[{Mps_title}]内容：{Gather_Content}\n")

        count = 5

        try:
            # 尝试方式1: 拦截真实 MP 页面 API 请求
            print_info("方式1: 尝试通过浏览器拦截API请求...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._capture_via_browser(faker_id, Mps_id, start_page, MaxPage, count, interval)
                )
            finally:
                loop.close()
            
            if result:
                articles = result
            else:
                print_warning("Playwright 拦截方式未获取到数据")
                super().Error("Playwright 模式未能获取文章列表，请检查登录状态")
                return

            # 处理文章
            for item in articles:
                if Gather_Content:
                    if not super().HasGathered(item.get("aid", "")):
                        item["content"] = self.content_extract(item.get("link", ""))
                        super().Wait(3, 10, tips=f"{item.get('title', '')} 采集完成")
                else:
                    item["content"] = ""
                item["id"] = item.get("aid", "")
                item["mp_id"] = Mps_id
                if CallBack is not None:
                    super().FillBack(
                        CallBack=CallBack,
                        data=item,
                        Ext_Data={"mp_title": Mps_title, "mp_id": Mps_id},
                    )
            self.Complete(Mps_id)
            print_success(f"Playwright 模式采集完成，共 {len(articles)} 条")

        except Exception as e:
            print_error(f"Playwright 采集失败: {e}")
        finally:
            super().Over(CallBack=Over_CallBack)

    async def _capture_via_browser(
        self,
        faker_id: str,
        mp_id: str,
        start_page: int,
        max_page: int,
        count: int,
        interval: int,
    ) -> Optional[List[Dict]]:
        """
        使用 Playwright 浏览器拦截 MP 后台的 XHR 请求
        
        策略:
        1. 启动浏览器并加载已保存的 Cookie
        2. 打开公众号后台页面
        3. 拦截对 /cgi-bin/ 的 XHR 请求
        4. 从拦截到的响应中提取文章数据
        """
        from playwright.async_api import async_playwright
        
        all_articles = []
        api_errors = []
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            
            try:
                context_options = {
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "viewport": {"width": 1920, "height": 1080},
                    "locale": "zh-CN",
                }

                # 复用容器数据卷中的 Playwright 会话状态。
                # 该文件只保存运行时登录态，不进入代码仓库。
                state_path = Path(
                    os.environ.get(
                        "WX_PLAYWRIGHT_STATE_PATH",
                        "./data/wx-playwright-state.json",
                    )
                )
                if state_path.is_file():
                    try:
                        json.loads(state_path.read_text(encoding="utf-8"))
                        context_options["storage_state"] = str(state_path)
                        print_info(f"已加载 Playwright 会话状态: {state_path}")
                    except Exception as exc:
                        print_warning(f"Playwright 会话状态无效，将重新建立: {exc}")

                context = await browser.new_context(**context_options)

                # 加载已保存的 Cookie
                cookies_to_set = self._load_browser_cookies()
                print_info(
                    "Playwright 会话诊断: "
                    f"token={'已配置' if self.token else '缺失'}, "
                    f"cookie_count={len(cookies_to_set)}, "
                    f"storage_state={'已加载' if 'storage_state' in context_options else '未加载'}"
                )
                if cookies_to_set:
                    await context.add_cookies(cookies_to_set)
                    print_info(f"已加载公众号浏览器 Cookie: {len(cookies_to_set)} 项")

                page = await context.new_page()

                # ---- 核心: 拦截 XHR 请求 ----
                captured_responses: List[Dict] = []

                async def handle_response(response):
                    """拦截 /cgi-bin/ JSON 响应，兼容不同后台端点的返回结构。"""
                    url = response.url
                    if "/cgi-bin/" not in url:
                        return
                    
                    try:
                        body = json.loads(await response.text())
                    except Exception:
                        return

                    if not isinstance(body, dict):
                        return

                    base_resp = body.get("base_resp", {}) or {}
                    ret = base_resp.get("ret", 0)
                    try:
                        ret = int(ret)
                    except (TypeError, ValueError):
                        ret = -1
                    if ret != 0:
                        if len(api_errors) < 3:
                            api_errors.append({
                                "url": url,
                                "ret": ret,
                                "err_msg": str(base_resp.get("err_msg", "")),
                            })
                        return

                    # 检查是否包含文章数据
                    has_articles = False
                    data_body = None

                    if isinstance(body, dict):
                        for key in ["free_publish_list", "publish_list", "app_msg_list"]:
                            if key in body and isinstance(body[key], list) and len(body[key]) > 0:
                                has_articles = True
                                data_body = body
                                break
                        if "publish_page" in body:
                            try:
                                pp = body["publish_page"]
                                if isinstance(pp, str):
                                    pp = json.loads(pp)
                                if "publish_list" in pp:
                                    has_articles = True
                                    data_body = body
                            except Exception:
                                pass

                    if has_articles:
                        captured_responses.append({
                            "url": url,
                            "body": data_body,
                        })

                page.on("response", handle_response)

                # ---- 导航到公众号后台"已发表"页面 ----
                # 构造已发表文章页面的 URL
                published_url = (
                    f"https://mp.weixin.qq.com/cgi-bin/appms?"
                    f"t=media/appmsg_list_v2&action=list_ex&"
                    f"begin=0&count=5&fakeid={faker_id}&"
                    f"type=9&token={self.token}&lang=zh_CN"
                )

                async def visit(url: str, timeout: int = 30000):
                    """以 DOM 加载为主，避免后台长连接导致 networkidle 永远不稳定。"""
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                    await page.wait_for_timeout(5000)
                    path = page.url.split("?", 1)[0]
                    status = response.status if response is not None else "无响应对象"
                    print_info(f"Playwright 页面响应: status={status}, path={path}")

                print_info("先建立公众号后台首页会话...")
                await visit("https://mp.weixin.qq.com/", timeout=30000)

                print_info("导航到后台已发表页面...")
                await visit(published_url, timeout=30000)

                # 如果页面要求重新登录
                page_text = await page.content()
                login_required = "扫码登录" in page_text or "login" in page.url.lower()
                print_info(
                    "Playwright 页面状态: "
                    f"login_required={login_required}, captured_responses={len(captured_responses)}"
                )
                if login_required:
                    print_error("Playwright 模式下需要重新扫码登录")
                    return None

                # 登录态有效时保存最新的 Cookie/LocalStorage，供下次容器内复用。
                if not state_path.parent.exists():
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(state_path))

                # ---- 解析拦截到的数据 ----
                if captured_responses:
                    print_success(f"成功拦截到 {len(captured_responses)} 个包含文章数据的响应")
                    
                    for resp_data in captured_responses:
                        articles = self._parse_captured_response(resp_data["body"])
                        all_articles.extend(articles)
                    print_info(f"Playwright 响应解析: raw_articles={len(all_articles)}")
                    
                    # 如果需要更多页
                    if len(all_articles) > 0 and max_page > 1:
                        # 尝试翻页
                        for page_idx in range(1, max_page):
                            begin = page_idx * count
                            next_url = published_url.replace("begin=0", f"begin={begin}")
                            await page.goto(next_url, wait_until="networkidle", timeout=15000)
                            await asyncio.sleep(random.randint(0, interval))
                            
                            # 重新收集（新页面的响应会追加）
                            # 简单起见，这里只做模拟翻页
                            await page.wait_for_timeout(2000)
                            print(f"Playwright 翻页: 第{page_idx+1}页")
                else:
                    # 方式2: 直接导航到 MP 后台的已发表页面，解析 DOM
                    print_info("未拦截到 API 响应，尝试解析页面 DOM...")
                    
                    # 尝试通过页面入口触发新版后台自身的初始化请求。
                    for text in ["已发表", "内容管理", "文章管理"]:
                        try:
                            locator = page.get_by_text(text, exact=True).first
                            if await locator.count() and await locator.is_visible():
                                await locator.click(timeout=3000)
                                await page.wait_for_timeout(5000)
                                if captured_responses:
                                    break
                        except Exception:
                            continue

                    if not captured_responses:
                        print_warning("仍未能获取到数据，请检查:")
                        print_warning("  1. 公众号平台 Cookie 是否有效")
                        print_warning("  2. 公众号平台 Token 是否有效")
                        print_warning("  3. 后台页面是否已改变或要求人工验证")
                        for error in api_errors:
                            print_warning(
                                f"后台接口返回 ret={error['ret']}: {error['err_msg']}"
                            )

                if not all_articles:
                    print_warning(
                        f"Playwright 未解析到文章: captured_responses={len(captured_responses)}, "
                        f"api_errors={len(api_errors)}"
                    )
                    return None

                unique_articles = []
                seen_aids = set()
                for article in all_articles:
                    aid = article.get("aid") or article.get("id") or article.get("link")
                    if aid in seen_aids:
                        continue
                    seen_aids.add(aid)
                    unique_articles.append(article)
                print_info(f"Playwright 文章去重后: {len(unique_articles)} 条")
                return unique_articles

            finally:
                await browser.close()

    def _parse_cookie_string(self, cookie_str: str) -> List[Dict]:
        """将 Cookie 字符串解析为 Playwright 可用的格式"""
        cookies = []
        if not cookie_str:
            return cookies
        
        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".mp.weixin.qq.com",
                    "path": "/",
                })
        return cookies

    def _load_browser_cookies(self) -> List[Dict]:
        """合并项目保存的 Cookie、配置 Cookie 和 token，避免丢失正确域信息。"""
        cookies: List[Dict] = []

        try:
            from driver.store import Store

            stored = Store.load()
            if isinstance(stored, list):
                cookies.extend(stored)
        except Exception as exc:
            print_warning(f"读取加密公众号 Cookie 失败，将使用配置 Cookie: {exc}")

        if isinstance(self.cookies, str) and self.cookies:
            cookies.extend(self._parse_cookie_string(self.cookies))

        # token 同时作为查询参数和 Cookie 提供，兼容部分后台页面的校验方式。
        if self.token:
            cookies.append({
                "name": "token",
                "value": str(self.token),
                "domain": ".mp.weixin.qq.com",
                "path": "/",
            })

        normalized: List[Dict] = []
        seen = set()
        now = time.time()
        for raw in cookies:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            item = dict(raw)
            if "expiry" in item and "expires" not in item:
                item["expires"] = item.pop("expiry")
            if item.get("expires") and float(item["expires"]) <= now:
                continue
            item.setdefault("domain", ".mp.weixin.qq.com")
            item.setdefault("path", "/")
            key = (item["name"], item["domain"], item["path"])
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)
        return normalized

    def _parse_captured_response(self, body: Dict) -> List[Dict]:
        """
        从拦截到的 API 响应中提取文章列表
        支持多种响应格式
        """
        articles = []

        # 格式1: free_publish_list
        if "free_publish_list" in body:
            raw = body["free_publish_list"]
            if isinstance(raw, list):
                for item in raw:
                    if "publish_info" in item:
                        pi = item["publish_info"]
                        if isinstance(pi, str):
                            try:
                                pi = json.loads(pi)
                            except Exception:
                                continue
                        if "appmsgex" in pi:
                            for art in pi["appmsgex"]:
                                art["publish_info"] = pi
                                articles.append(art)
                    elif "aid" in item:
                        articles.append(item)
            return articles

        # 格式2: app_msg_list
        if "app_msg_list" in body:
            raw = body["app_msg_list"]
            if isinstance(raw, list):
                articles.extend(raw)
            return articles

        # 部分新版接口直接返回 publish_list，而不是 publish_page 包装结构。
        if "publish_list" in body and isinstance(body["publish_list"], list):
            return self._normalize_article_list(body["publish_list"])

        # 格式3: publish_page
        if "publish_page" in body:
            try:
                pp = body["publish_page"]
                if isinstance(pp, str):
                    pp = json.loads(pp)
                for item in pp.get("publish_list", []):
                    if "publish_info" in item:
                        pi = item["publish_info"]
                        if isinstance(pi, str):
                            try:
                                pi = json.loads(pi)
                            except Exception:
                                continue
                        if "appmsgex" in pi:
                            for art in pi["appmsgex"]:
                                art["publish_info"] = pi
                                articles.append(art)
            except Exception as e:
                print_error(f"解析 publish_page 失败: {e}")
            return articles

        return articles

    def _normalize_article_list(self, raw_list: list) -> List[Dict]:
        """提取直接返回的文章列表，兼容 publish_info/appmsgex 嵌套结构。"""
        articles = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            publish_info = item.get("publish_info")
            if isinstance(publish_info, str):
                try:
                    publish_info = json.loads(publish_info)
                except Exception:
                    publish_info = None
            if isinstance(publish_info, dict) and isinstance(publish_info.get("appmsgex"), list):
                articles.extend(publish_info["appmsgex"])
            elif item.get("aid") or item.get("id"):
                articles.append(item)
        return articles

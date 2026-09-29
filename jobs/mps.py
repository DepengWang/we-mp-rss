import asyncio
import os
import threading
import time
from datetime import datetime, timedelta
from core.models.article import Article
from .article import UpdateArticle,Update_Over
import core.db as db
from core.wx import WxGather
from core.log import logger
from core.task import TaskScheduler
from core.models.feed import Feed
from core.config import cfg,DEBUG
from core.print import print_info,print_success,print_error,print_warning
from driver.wx import WX_API
from driver.success import Success
from core.redis_client import clear_env_exception
wx_db=db.Db(tag="任务调度")
_account_rotation_enqueue_lock = threading.Lock()


def _wait_for_collection_queues_idle(timeout=180):
    """Wait until both shared-session queues have no running or pending work."""
    from core.queue import TaskQueue, ContentTaskQueue

    queues = (TaskQueue, ContentTaskQueue)
    for task_queue in queues:
        if task_queue._queue.qsize() and not task_queue._is_running:
            task_queue.run_task_background()

    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        if all(
            task_queue._queue.qsize() == 0 and task_queue._current_task is None
            for task_queue in queues
        ):
            return True
        time.sleep(0.25)
    return False


def _run_account_rotation():
    """Run browser account rotation in a dedicated loop, outside queue workers."""
    outcome = {"ok": False}

    def run():
        try:
            outcome["ok"] = bool(asyncio.run(WX_API.auto_rotate_account_by_id()))
        except Exception as e:
            outcome["error"] = str(e)

    worker = threading.Thread(target=run, name="wechat-account-rotation", daemon=True)
    worker.start()
    worker.join()
    return outcome


def fetch_all_article():
    print("开始更新")
    wx=WxGather().Model()
    try:
        # 获取公众号列表
        mps=db.DB.get_all_mps()
        for item in mps:
            try:
                wx.get_Articles(item.faker_id,CallBack=UpdateArticle,Mps_id=item.id,Mps_title=item.mp_name, MaxPage=1)
            except Exception as e:
                print(e)
        print(wx.articles) 
    except Exception as e:
        print(e)         
    finally:
        logger.info(f"所有公众号更新完成,共更新{wx.all_count()}条数据")


def test(info:str):
    print("任务测试成功",info)

from core.models.message_task import MessageTask
# from core.queue import TaskQueue
from .webhook import web_hook
interval=int(cfg.get("interval",60)) # 每隔多少秒执行一次
def do_job(mp=None,task:MessageTask=None,isTest=False):
        """执行单个公众号的采集任务"""
        # TaskQueue.add_task(test,info=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # print("执行任务", task.mps_id)
        print(f"执行任务 (测试模式: {isTest})")
        
        # 初始化变量，确保在所有分支中都有定义
        count = 0
        all_count = 0
        mock_articles = []
        success = False
        error_msg = None
        
        try:
            if isTest:
                # 测试模式使用模拟数据
                mock_articles = [{
                    "id": "test-article-001",
                    "mp_id": mp.id,
                    "title": "测试文章标题",
                    "pic_url": "https://via.placeholder.com/300x200",
                    "url": "https://example.com/test-article",
                    "description": "这是一篇测试文章的描述内容，用于测试webhook功能是否正常。",
                    "publish_time": (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                    "content": "<p>这是测试文章的正文内容。</p>"
                }]
                count = 1
                success = True
            else:
                wx = WxGather().Model()
                gather_model = str(cfg.get("gather.model") or "web").lower()
                is_weread_feed = (
                    str(mp.id or "").startswith("MP_WXS_")
                    and mp.id != "MP_WXS_FEATURED_ARTICLES"
                )
                # 显式 weread_mp 时直接使用微信读书；web 模式优先使用
                # Feed.faker_id，失败后才对历史 MP_WXS_* 订阅启用 WeRead 兜底。
                use_weread = gather_model == "weread_mp" or (
                    gather_model == "auto" and is_weread_feed
                )
                if use_weread and is_weread_feed:
                    from core.wx.model.weread_mp import MpsWereadMP
                    _mp_wx = MpsWereadMP()
                    _mp_wx._load_weread_auth()
                    if _mp_wx._weread_cookies:
                        wx = _mp_wx
                        print_info(f"[{mp.mp_name}] 微信读书公众号 → weread_mp 采集模式")
                    else:
                        print_warning(f"[{mp.mp_name}] 微信读书 Cookie 未配置，跳过采集")
                        raise RuntimeError("微信读书 Cookie 未配置")
                try:
                    from core.wx.collection_guard import CollectionGuard
                    guard = CollectionGuard(mp.id)
                    with guard.hold():
                        try:
                            wx.get_Articles(mp.faker_id,CallBack=UpdateArticle,Mps_id=mp.id,Mps_title=mp.mp_name, MaxPage=1,Over_CallBack=Update_Over,interval=interval)
                            if getattr(wx, "collection_failed", False):
                                raise RuntimeError(
                                    getattr(wx, "collection_error", "Web 采集失败")
                                )
                        except Exception:
                            if gather_model != "web" or not is_weread_feed:
                                raise
                            from core.wx.model.weread_mp import MpsWereadMP
                            fallback_wx = MpsWereadMP()
                            fallback_wx._load_weread_auth()
                            if not fallback_wx._weread_cookies:
                                raise
                            print_warning(
                                f"[{mp.mp_name}] web 采集失败，回退到 weread_mp"
                            )
                            fallback_wx.get_Articles(
                                mp.faker_id,
                                CallBack=UpdateArticle,
                                Mps_id=mp.id,
                                Mps_title=mp.mp_name,
                                MaxPage=1,
                                Over_CallBack=Update_Over,
                                interval=interval,
                            )
                            wx = fallback_wx
                    guard.record_success()
                    success = True
                except Exception as e:
                    print_error(f"获取文章失败 [{mp.mp_name}]: {e}")
                    error_msg = str(e)
                    # 不抛出异常，继续执行后续流程
                finally:
                    count = wx.all_count() if wx else 0
                    mock_articles = wx.articles if wx else []
                    all_count += count

            # Webhook 在整个消息任务完成后由 MessageTaskTracker 汇总发送，
            # 这里不再按订阅源逐条发送。测试任务仍保留单订阅源即时发送行为。
            if isTest:
                try:
                    from jobs.webhook import MessageWebHook
                    tms = MessageWebHook(task=task, feed=mp, articles=mock_articles)
                    web_hook(tms, is_test=True)
                except Exception as e:
                    print_error(f"Webhook执行失败 [{mp.mp_name}]: {e}")
                    if not error_msg:
                        error_msg = f"Webhook: {str(e)}"

            print_success(f"任务({task.id})[{mp.mp_name}]采集完成,{count}条新文章")

            # 采集成功（即使本次没有新文章）就清除环境异常记录。
            if not isTest and success:
                try:
                    clear_env_exception(mp_id=mp.id)
                except Exception as e:
                    print_error(f"清除环境异常记录失败: {e}")
            
            # 级联节点：上报任务执行结果到父节点
            from jobs.cascade_sync import cascade_sync_service
            if not isTest and mock_articles:
                import asyncio
                try:
                    result_data = [{
                        "mp_id": mp.id,
                        "mp_name": mp.mp_name,
                        "article_count": len(mock_articles) if not isTest else 1,
                        "success_count": count if not isTest else 1,
                        "timestamp": datetime.now().isoformat()
                    }]
                    # 异步上报，不阻塞主流程
                    asyncio.create_task(cascade_sync_service.report_task_result(task.id, result_data))
                except Exception as e:
                    print_error(f"上报任务结果失败: {str(e)}")
                    
        except Exception as e:
            error_msg = str(e)
            print_error(f"任务执行异常 [{mp.mp_name}]: {e}")
            raise  # 重新抛出，让队列的重试机制处理
        
        finally:
            # 记录执行结果到追踪器
            if task and not isTest:
                tracker.record_mp_result(
                    task_id=task.id,
                    mp_name=mp.mp_name,
                    # count=0 是正常的“无新文章”，不是采集失败。
                    success=success,
                    article_count=count,
                    error=error_msg,
                    feed=mp,
                    articles=mock_articles,
                )

from core.queue import TaskQueue

# 任务执行追踪器
class MessageTaskTracker:
    """消息任务执行追踪器"""
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._tasks = {}
                    cls._instance._task_lock = threading.Lock()
        return cls._instance
    
    def start_task(self, task_id: str, total_mps: int, task: MessageTask = None) -> bool:
        """开始追踪一轮消息任务；上一轮未完成时不重复入队。"""
        with self._task_lock:
            current = self._tasks.get(task_id)
            if current and not current.get('finished', False):
                print_warning(f"消息任务 [{task_id}] 上一轮仍在执行，本轮跳过以避免覆盖进度")
                return False
            self._tasks[task_id] = {
                'total': total_mps,
                'completed': 0,
                'failed': 0,
                'start_time': datetime.now().isoformat(),
                'mp_results': [],
                'task': task,
                'webhook_sent': False,
                'finalizing': False,
                'finished': False,
            }
            return True
    
    def record_mp_result(self, task_id: str, mp_name: str, success: bool, article_count: int = 0, error: str = None, feed=None, articles=None) -> None:
        """记录单个公众号的执行结果"""
        with self._task_lock:
            if task_id not in self._tasks:
                return
            
            task_info = self._tasks[task_id]
            if task_info.get('finalizing'):
                return
            if success:
                task_info['completed'] += 1
            else:
                task_info['failed'] += 1
            
            task_info['mp_results'].append({
                'mp_name': mp_name,
                'feed': feed,
                'success': success,
                'article_count': article_count,
                'articles': list(articles or []),
                'error': error,
                'time': datetime.now().isoformat()
            })
            
            # 打印进度
            progress = task_info['completed'] + task_info['failed']
            print_info(f"任务进度 [{task_id}]: {progress}/{task_info['total']} (成功:{task_info['completed']}, 失败:{task_info['failed']})")
            
            # 检查是否全部完成
            should_finish = progress >= task_info['total'] and not task_info['finalizing']
            if should_finish:
                task_info['webhook_sent'] = True
                task_info['finalizing'] = True

        if should_finish:
            self._finish_task(task_id)
    
    def _finish_task(self, task_id: str) -> None:
        """任务完成"""
        with self._task_lock:
            task_info = self._tasks.get(task_id)
            if not task_info:
                return
            results = list(task_info['mp_results'])
            task = task_info.get('task')
        try:
            print_success(f"\n{'='*50}")
            print_success(f"消息任务 [{task_id}] 执行完成!")
            print_success(f"总计: {task_info['total']} 个公众号")
            print_success(f"成功: {task_info['completed']} 个")
            print_error(f"失败: {task_info['failed']} 个")
            print_success(f"{'='*50}\n")

            if not task:
                return

            # 成功但无新文章的订阅源不发送；失败项必须保留，便于告警。
            webhook_results = []
            for item in results:
                if not item['success'] or item['article_count'] > 0:
                    webhook_results.append({
                        'feed': item.get('feed'),
                        'mp_name': item['mp_name'],
                        'articles': item.get('articles', []),
                        'article_count': item['article_count'],
                        'success': item['success'],
                        'status': 'success' if item['success'] else 'failed',
                        'status_color': 'info' if item['success'] else 'warning',
                        'error': item.get('error') or '',
                    })

            if not webhook_results:
                print_info(f"消息任务 [{task_id}] 没有新文章且没有失败，不发送Webhook")
                return

            summary = {
                'total': task_info['total'],
                'successful': task_info['completed'],
                'failed': task_info['failed'],
                'new_articles': sum(item['article_count'] for item in results),
            }
            try:
                from jobs.webhook import MessageWebHook
                web_hook(MessageWebHook(task=task, results=webhook_results, summary=summary))
                print_success(f"消息任务 [{task_id}] 汇总Webhook发送完成")
            except Exception as e:
                print_error(f"消息任务 [{task_id}] 汇总Webhook发送失败: {e}")
        finally:
            with self._task_lock:
                current = self._tasks.get(task_id)
                if current:
                    current['finished'] = True
    
    def get_task_status(self, task_id: str) -> dict:
        """获取任务状态"""
        with self._task_lock:
            return self._tasks.get(task_id, {})

tracker = MessageTaskTracker()


def add_job(feeds:list[Feed]=None,task:MessageTask=None,isTest=False):
    # 微信读书 Cookie 刷新已移至宿主机完成（见 scripts/refresh_weread_cookie.py + launchd 模板），
    # 容器不在内部启动浏览器刷新——因为 profile 由 macOS 钥匙串加密，容器内 Linux Chromium
    # 无法解密宿主机写入的登录态。容器只读取 wx.lic 中的明文 Cookie 进行同步即可。
    if isTest:
        TaskQueue.clear_queue()

    # 微信读书模式：在「执行 / 定时任务」真正同步文章前，若 Cookie 过期则自动
    # 请宿主机刷新代理去刷新（容器不跑浏览器，见 scripts/host_weread_refresh_agent.py）。
    # 这样用户无需手动敲命令——点执行或定时触发时即自动保活 Cookie。
    if not isTest and (cfg.get("gather.model") or "web") == "weread_mp":
        try:
            from core.weread_cookie_refresh import request_host_refresh
            res = request_host_refresh()
            if res.get("triggered") and not res.get("ok"):
                # 刷新失败（多为登录态过期需扫码）→ 中止本次任务并给出明确提示
                msg = res.get("message") or "Cookie 刷新失败"
                print_error(f"[cookie] 自动刷新失败: {msg}")
                raise RuntimeError(f"微信读书 Cookie 自动刷新失败：{msg}")
            if res.get("triggered"):
                print_info(f"[cookie] 自动刷新: {res.get('message')}")
        except RuntimeError:
            raise
        except Exception as e:
            # 代理调用异常等：不阻断同步（可能 Cookie 仍有效），仅告警
            print_warning(f"[cookie] 自动刷新异常（继续执行）: {e}")

    # 动态获取公众号列表：如果 feeds 为 None 且 task 不为 None，则动态获取
    if feeds is None and task is not None:
        feeds = get_feeds(task)

    # 初始化任务追踪
    if task and not isTest and feeds:
        if not tracker.start_task(task.id, len(feeds), task):
            return

    with _account_rotation_enqueue_lock:
        gather_model = str(cfg.get("gather.model") or "web").lower()
        if not isTest and feeds and gather_model not in {"weread", "weread_mp"}:
            try:
                queue_wait = max(1, int(os.getenv("ACCOUNT_ROTATION_QUEUE_WAIT", "180")))
            except (TypeError, ValueError):
                queue_wait = 180
            if _wait_for_collection_queues_idle(queue_wait):
                rotation = _run_account_rotation()
                if rotation.get("ok"):
                    print_info("本轮采集已按 account_id 完成账号轮转（如仅一个有效账号则保持不变）")
                else:
                    print_warning(
                        "本轮公众号账号自动轮转未完成，继续使用当前会话："
                        f"{rotation.get('error') or '目标账号未切换'}"
                    )
            else:
                print_warning(
                    f"采集队列在 {queue_wait} 秒内未排空，本轮跳过账号轮转，避免切换共享会话时影响正在运行的任务"
                )

        for feed in feeds:
            # 使用公众号名称作为任务显示名称
            queue_task_name = (
                f"{task.name} [{str(task.id)[:8]}] / {feed.mp_name}"
                if task else feed.mp_name
            )
            added = TaskQueue.add_task(do_job, feed, task, isTest, task_name=queue_task_name)
            if not added and task and not isTest:
                tracker.record_mp_result(
                    task_id=task.id,
                    mp_name=feed.mp_name,
                    success=False,
                    error="订阅源未能加入采集队列",
                    feed=feed,
                )
            if isTest:
                print(f"测试任务，{feed.mp_name}，加入队列成功")
                break
            print(f"{feed.mp_name}，加入队列成功")
    print_success(TaskQueue.get_queue_info())
    pass
import json
def get_feeds(task:MessageTask=None):
     mps = json.loads(task.mps_id) if task.mps_id else []
     ids=",".join([item["id"]for item in mps])
     mps=wx_db.get_mps_list(ids)
     if len(mps)==0:
        mps=wx_db.get_all_mps()
     return mps
scheduler=TaskScheduler()
def reload_job():
    print_success("重载任务")
    scheduler.clear_all_jobs()
    TaskQueue.clear_queue()
    start_job()

def run(job_id:str=None,isTest=False):
    from .taskmsg import get_message_task
    tasks=get_message_task(job_id)
    if not tasks:
        print("没有任务")
        return None
    for task in tasks:
            #添加测试任务
            from core.print import print_warning
            print_warning(f"{task.name} 添加到队列运行")
            # 修改：只传递 task，在 add_job 中动态获取 feeds
            add_job(task=task,isTest=isTest)
            pass
    return tasks
def start_job(job_id:str=None):
    from .taskmsg import get_message_task
    tasks=get_message_task(job_id)
    if not tasks:
        print("没有任务")
        return
    tag="定时采集"
    for task in tasks:
        cron_exp=task.cron_exp
        if not cron_exp:
            print_error(f"任务[{task.id}]没有设置cron表达式")
            continue

        # 修改：使用关键字参数传递 task，避免与 feeds 混淆
        job_id=scheduler.add_cron_job(add_job,cron_expr=cron_exp,kwargs={'task': task},job_id=str(task.id),tag="定时采集")
        print(f"已添加任务: {job_id}")
    scheduler.start()
    print("启动任务")
def start_fix_article():
      #开启自动同步未同步 文章任务
    from jobs.fetch_no_article import start_sync_content
    start_sync_content()

def start_article_stats_refresh():
    """启动文章统计定时刷新任务"""
    from core.article_lax import refresh_article_info
    from core.config import cfg
    
    # 获取刷新间隔,默认5分钟
    refresh_interval = int(cfg.get("server.article_stats_refresh_interval", 3600))
    
    # 添加定时任务,每隔指定时间刷新一次文章统计
    scheduler.add_cron_job(
        refresh_article_info,
        cron_expr=f"*/{refresh_interval // 60} * * * *",  # 每 N 分钟执行一次
        job_id="article_stats_refresh",
        tag="文章统计刷新"
    )
    print_success(f"文章统计定时刷新任务已启动,间隔: {refresh_interval}秒")

if __name__ == '__main__':
    # do_job()
    # start_all_task()
    pass

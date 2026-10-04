from core.models.message_task import MessageTask
from core.models.feed import Feed
from core.models.article import Article
from core.print import print_success
from core.notice import notice
from dataclasses import dataclass, field
from core.lax import TemplateParser
from datetime import datetime
from core.log import logger
from core.config import cfg
from bs4 import BeautifulSoup
from core.content_format import format_content
import re
from urllib.parse import urlparse
@dataclass
class MessageWebHook:
    task: MessageTask
    feed: Feed = None
    articles: list[Article] = field(default_factory=list)
    # 任务完成后的汇总结果。每一项包含 feed、articles、success、error、status_color 等字段。
    results: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    pass


def _article_to_dict(article):
    """将 Article 或字典转换为模板可用的字典。"""
    if isinstance(article, dict):
        return {
            field.name: article.get(field.name, "")
            for field in Article.__table__.columns
        }
    return {
        field.name: getattr(article, field.name, "")
        for field in Article.__table__.columns
    }


def _build_summary(results: list[dict]) -> dict:
    """构造汇总模板变量。"""
    return {
        "total": len(results),
        "successful": sum(1 for item in results if item.get("success")),
        "failed": sum(1 for item in results if not item.get("success") and item.get("status") != "rate_limited"),
        "rate_limited": sum(1 for item in results if item.get("status") == "rate_limited"),
        "source_rate_limited": sum(1 for item in results if item.get("web_status") == "limited"),
        "weread_rate_limited": sum(1 for item in results if item.get("weread_status") == "rate_limited"),
        "new_articles": sum(len(item.get("articles") or []) for item in results),
        "weread_status": "not_checked",
        "weread_status_label": "未检查",
        "weread_message": "未提供批次状态",
        "weread_fallback_successes": sum(1 for item in results if item.get("collection_method") == "weread_fallback" and item.get("success")),
        "weread_fallback_failures": sum(1 for item in results if item.get("collection_method") == "weread_fallback" and item.get("weread_status") in {"failed", "invalid"}),
    }


def _flatten_results(results: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """为消息模板提供不需要嵌套循环的文章和失败列表。"""
    new_articles = []
    failures = []
    limited_sources = []
    weread_limited_sources = []
    no_new_articles = []
    weread_fallbacks = []
    for result in results:
        feed = result.get("feed")
        feed_name = result.get("mp_name") or getattr(feed, "mp_name", "")
        if result.get("success"):
            for article in result.get("articles") or []:
                item = _article_to_dict(article)
                item["feed_name"] = feed_name
                item["status_color"] = "info"
                new_articles.append(item)
        elif result.get("status") == "rate_limited":
            limited_item = {
                "feed_name": feed_name,
                "error": result.get("error") or "遇到频率限制，已跳过本订阅源",
                "status_color": "comment",
            }
            if result.get("weread_status") == "rate_limited":
                weread_limited_sources.append(limited_item)
            else:
                limited_sources.append(limited_item)
        else:
            failures.append({
                "feed_name": feed_name,
                "error": result.get("error") or "采集失败",
                "status_color": "warning",
            })
        if result.get("success") and not result.get("articles"):
            no_new_articles.append({"feed_name": feed_name, "status_color": "comment"})
        if result.get("collection_method") == "weread_fallback" and result.get("success"):
            weread_fallbacks.append({"feed_name": feed_name, "status_color": "info"})
    return new_articles, failures, limited_sources, weread_limited_sources, no_new_articles, weread_fallbacks

def send_message(hook: MessageWebHook) -> str:
    """
    发送格式化消息
    
    参数:
        hook: MessageWebHook对象，包含任务、订阅源和文章信息
        
    返回:
        str: 格式化后的消息内容
    """
    if hook.results and not hook.task.message_template:
        template = """### {{ task.name }}
> 执行时间：{{ now }}
> 结果：成功 {{ summary.successful }} 个，采集源频率限制 {{ summary.source_rate_limited }} 个，失败 {{ summary.failed }} 个，新增 {{ summary.new_articles }} 篇
> 微信读书兜底：{{ summary.weread_status_label }}（{{ summary.weread_message }}）
> 微信读书兜底成功 {{ summary.weread_fallback_successes }} 个，频率限制 {{ summary.weread_rate_limited }} 个，失败 {{ summary.weread_fallback_failures }} 个
{% for article in new_articles %}
- <font color=\"{{ article.status_color }}\">[{{ article.feed_name }}] {{ article.title }}</font>：{{ article.url }}
{% endfor %}
{% for limited in limited_sources %}
- <font color=\"{{ limited.status_color }}\">[{{ limited.feed_name }}] 频率限制，已跳过：{{ limited.error }}</font>
{% endfor %}
{% for limited in weread_limited_sources %}
- <font color=\"{{ limited.status_color }}\">[{{ limited.feed_name }}] 微信读书频率限制，已跳过：{{ limited.error }}</font>
{% endfor %}
{% for failure in failures %}
- <font color=\"{{ failure.status_color }}\">[{{ failure.feed_name }}] 采集失败：{{ failure.error }}</font>
{% endfor %}
{% for item in no_new_articles %}
- <font color=\"{{ item.status_color }}\">[{{ item.feed_name }}] 采集成功，无新文章</font>
{% endfor %}
{% for item in weread_fallbacks %}
- <font color=\"{{ item.status_color }}\">[{{ item.feed_name }}] 已通过微信读书兜底采集</font>
{% endfor %}"""
    else:
        template = hook.task.message_template if hook.task.message_template else """
### {{feed.mp_name}} 订阅消息：
{% if articles %}
{% for article in articles %}
- [**{{ article.title }}**]({{article.url}}) ({{ article.publish_time }})\n
{% endfor %}
{% else %}
- 暂无文章\n
{% endif %}
    """
    parser = TemplateParser(template)
    processed_results = []
    if hook.results:
        for result in hook.results:
            item = dict(result)
            item["articles"] = [
                _article_to_dict(article)
                for article in (result.get("articles") or [])
            ]
            processed_results.append(item)

    data = {
        "feed": hook.feed,
        "articles": hook.articles,
        "task": hook.task,
        'now': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "results": processed_results,
        "feeds": processed_results,
        "summary": hook.summary or (_build_summary(processed_results) if hook.results else {}),
    }
    if hook.results:
        data["new_articles"], data["failures"], data["limited_sources"], data["weread_limited_sources"], data["no_new_articles"], data["weread_fallbacks"] = _flatten_results(processed_results)
    message = parser.render(data)
    # 这里可以添加发送消息的具体实现
    print("发送消息:", message)
    try:
        notice(hook.task.web_hook_url, hook.task.name, message)
    except Exception as e:
        logger.error(f"发送消息失败: {e}")
        raise ValueError(f"发送消息失败: {e}")
    return message

def call_webhook(hook: MessageWebHook, is_test: bool = False) -> str:
    """
    调用webhook接口发送数据

    参数:
        hook: MessageWebHook对象，包含任务、订阅源和文章信息
        is_test: 是否为测试模式，测试模式下使用模拟数据

    返回:
        str: 调用结果信息

    异常:
        ValueError: 当webhook调用失败时抛出
    """
    if hook.results:
        default_template = """{
  "task": {
    "id": "{{ task.id }}",
    "name": "{{ task.name }}",
    "time": "{{ now }}"
  },
  "summary": {
    "total": {{ summary.total }},
    "successful": {{ summary.successful }},
    "failed": {{ summary.failed }},
    "rate_limited": {{ summary.rate_limited }},
    "source_rate_limited": {{ summary.source_rate_limited }},
    "weread_rate_limited": {{ summary.weread_rate_limited }},
    "weread_status": "{{ summary.weread_status }}",
    "weread_status_label": "{{ summary.weread_status_label }}",
    "weread_message": "{{ summary.weread_message }}",
    "weread_fallback_successes": {{ summary.weread_fallback_successes }},
    "weread_fallback_failures": {{ summary.weread_fallback_failures }},
    "new_articles": {{ summary.new_articles }}
  },
  "sources": [
    {% for result in results %}
    {
      "feed_name": "{{ result.mp_name }}",
      "status": "{{ result.status }}",
      "status_label": "{{ result.status_label }}",
      "message": "{{ result.message }}",
      "web_status": "{{ result.web_status }}",
      "weread_status": "{{ result.weread_status }}",
      "collection_method": "{{ result.collection_method }}"
    }{% if not loop.last %},{% endif %}
    {% endfor %}
  ],
  "articles": [
    {% for article in new_articles %}
    {
      "feed_name": "{{ article.feed_name }}",
      "title": "{{ article.title }}",
      "url": "{{ article.url }}",
      "publish_time": "{{ article.publish_time }}",
      "status_color": "{{ article.status_color }}"
    }{% if not loop.last %},{% endif %}
    {% endfor %}
  ],
  "failures": [
    {% for failure in failures %}
    {
      "feed_name": "{{ failure.feed_name }}",
      "error": "{{ failure.error }}",
      "status_color": "{{ failure.status_color }}"
    }{% if not loop.last %},{% endif %}
    {% endfor %}
  ],
  "rate_limited_sources": [
    {% for limited in limited_sources %}
    {"feed_name": "{{ limited.feed_name }}", "error": "{{ limited.error }}"}{% if not loop.last %},{% endif %}
    {% endfor %}
  ],
  "weread_rate_limited_sources": [
    {% for limited in weread_limited_sources %}
    {"feed_name": "{{ limited.feed_name }}", "error": "{{ limited.error }}"}{% if not loop.last %},{% endif %}
    {% endfor %}
  ],
  "weread_fallback_sources": [
    {% for item in weread_fallbacks %}
    "{{ item.feed_name }}"{% if not loop.last %},{% endif %}
    {% endfor %}
  ]
}"""
    else:
        default_template = """{
  "feed": {
    "id": "{{ feed.id }}",
    "name": "{{ feed.mp_name }}"
  },
  "articles": [
    {% if articles %}
     {% for article in articles %}
        {
          "id": "{{ article.id }}",
          "mp_id": "{{ article.mp_id }}",
          "title": "{{ article.title }}",
          "pic_url": "{{ article.pic_url }}",
          "url": "{{ article.url }}",
          "description": "{{ article.description }}",
          "publish_time": "{{ article.publish_time }}"
        }{% if not loop.last %},{% endif %}
      {% endfor %}
    {% endif %}
  ],
  "task": {
    "id": "{{ task.id }}",
    "name": "{{ task.name }}"
  },
  "now": "{{ now }}"
}
"""
    template = hook.task.message_template if hook.task.message_template else default_template

    # 检查template是否需要content
    template_needs_content = "content" in template.lower()

    # 根据content_format处理内容
    content_format = cfg.get("webhook.content_format", "html")
    logger.info(f'Content将以{content_format}格式发送')

    # 测试模式下使用模拟数据
    if is_test:
        from datetime import timedelta
        logger.info("使用模拟数据测试webhook")
        mock_article = {
            "id": "test-article-001",
            "mp_id": hook.feed.id if hook.feed else "test-mp-id",
            "title": "测试文章标题",
            "pic_url": "https://via.placeholder.com/300x200",
            "url": "https://example.com/test-article",
            "description": "这是一篇测试文章的描述内容，用于测试webhook功能是否正常。",
            "publish_time": (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "content": "<p>这是测试文章的正文内容。</p>"
        }
        processed_articles = [mock_article]
    else:
        processed_articles = []
        for article in hook.articles:
            if isinstance(article, dict) and "content" in article and article["content"]:
                processed_article = article.copy()
                # 只有template需要content时才进行格式转换
                if template_needs_content:
                    processed_article["content"] = format_content(processed_article["content"], content_format)
                processed_articles.append(processed_article)
            else:
                processed_articles.append(article)

    processed_results = []
    if hook.results:
        for result in hook.results:
            item = dict(result)
            item["articles"] = [
                _article_to_dict(article)
                for article in (result.get("articles") or [])
            ]
            processed_results.append(item)
        processed_articles = [
            article
            for result in processed_results
            for article in result["articles"]
        ]

    data = {
        "feed": hook.feed,
        "articles": processed_articles,
        "task": hook.task,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "results": processed_results,
        "feeds": processed_results,
        "summary": hook.summary or (_build_summary(processed_results) if hook.results else {}),
    }
    if hook.results:
        data["new_articles"], data["failures"], data["limited_sources"], data["weread_limited_sources"], data["no_new_articles"], data["weread_fallbacks"] = _flatten_results(processed_results)

   # 预处理content字段
    import json
    def process_content(content):
        if content is None:
            return ""
        # 进行JSON转义处理引号
        json_escaped = json.dumps(content, ensure_ascii=False)
        # 去掉外层引号避免重复
        return json_escaped[1:-1]

    # 处理articles中的content字段，进行JSON转义
    if "articles" in data:
        for i, article in enumerate(data["articles"]):
            if isinstance(article, dict):
                if "content" in article:
                    data["articles"][i]["content"] = process_content(article["content"])
            elif hasattr(article, "content"):
                setattr(data["articles"][i], "content", process_content(getattr(article, "content")))

    parser = TemplateParser(template)

    payload = parser.render(data)
    logger.info(f'Webhook payload: {payload}')

    # 检查web_hook_url是否为空
    if not hook.task.web_hook_url:
        logger.error("web_hook_url为空")
        return
    # 发送webhook请求
    import requests
    import json

    # 构建请求头
    headers = {"Content-Type": "application/json"}
    if hook.task.headers:
        try:
            custom_headers = json.loads(hook.task.headers)
            headers.update(custom_headers)
        except json.JSONDecodeError as e:
            logger.warning(f"解析headers失败: {e}")

    # 构建cookies
    cookies = None
    if hook.task.cookies:
        cookies = {}
        # 解析cookie字符串 (格式: key1=value1; key2=value2)
        for cookie_pair in hook.task.cookies.split(';'):
            cookie_pair = cookie_pair.strip()
            if '=' in cookie_pair:
                key, value = cookie_pair.split('=', 1)
                cookies[key.strip()] = value.strip()

    # 企业微信群机器人要求 msgtype/markdown.content JSON 包装；不能把模板文本
    # 直接作为 JSON 请求体发送。其接口可能以 HTTP 200 返回 errcode 错误，
    # 因此必须检查响应体后才能报告发送成功。
    webhook_url = hook.task.web_hook_url
    parsed_webhook_url = urlparse(webhook_url)
    is_wecom_bot = (
        parsed_webhook_url.hostname == "qyapi.weixin.qq.com"
        and parsed_webhook_url.path.rstrip("/") == "/cgi-bin/webhook/send"
    )

    if is_wecom_bot:
        headers["Content-Type"] = "application/json"
        try:
            response = requests.post(
                webhook_url,
                json={"msgtype": "markdown", "markdown": {"content": payload}},
                headers=headers,
                cookies=cookies,
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            # Request exceptions can include the secret webhook key in the URL.
            raise ValueError(f"企微 Webhook 请求失败（{type(e).__name__}）") from e

        try:
            result = response.json()
        except ValueError as e:
            raise ValueError("企微 Webhook 返回了无法解析的响应") from e

        if not isinstance(result, dict) or result.get("errcode") not in (0, "0"):
            errcode = result.get("errcode", "missing") if isinstance(result, dict) else "invalid_response"
            raise ValueError(f"企微 Webhook 拒绝消息（errcode={errcode}）")
        return "企微 Webhook 已接受消息"

    # print_success(f"发送webhook请求{payload}")
    try:
        response = requests.post(
            webhook_url,
            data=payload,
            headers=headers,
            cookies=cookies,
            timeout=30
        )
        response.raise_for_status()
        return "Webhook调用成功"
    except Exception as e:
        raise ValueError(f"Webhook调用失败: {str(e)}")

def web_hook(hook:MessageWebHook, is_test:bool = False):
    """
    根据消息类型路由到对应的处理函数

    参数:
        hook: MessageWebHook对象，包含任务、订阅源和文章信息
        is_test: 是否为测试模式，测试模式下使用模拟数据
    返回:
        对应处理函数的返回结果

    异常:
        ValueError: 当消息类型未知时抛出
    """
    try:
        # 汇总任务覆盖每个订阅源状态，包括成功但无新文章的情况。
        if hook.results:
            # 汇总包含每个订阅源状态；即使全是“成功但无新文章”也需要发送，
            # 以便用户确认本轮任务确实执行完成。
            if hook.task.message_type == 0:
                return send_message(hook)
            if hook.task.message_type == 1:
                return call_webhook(hook, is_test)
            raise ValueError(f"未知的消息类型: {hook.task.message_type}")

        # 处理articles参数，兼容Article对象和字典类型
        processed_articles = []
        if len(hook.articles)<=0:
            # raise ValueError("没有更新到文章")
            logger.warning("没有更新到文章")
            return
        for article in hook.articles:
            if isinstance(article, dict):
                # 如果是字典类型，直接使用
                def process_field_value(field_name, article):
                    value = article.get(field_name, "")
                    if field_name == "publish_time" and value:
                        # 如果已经是格式化的字符串，直接返回
                        if isinstance(value, str) and "-" in value and ":" in value:
                            return value
                        # 如果是时间戳整数，转换为字符串
                        try:
                            if isinstance(value, (int, float)):
                                return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
                        except (ValueError, TypeError, OSError):
                            pass
                        return value
                    return value

                processed_article = {
                    field.name: process_field_value(field.name, article)
                    for field in Article.__table__.columns
                }
            else:
                # 如果是Article对象，使用getattr获取属性
                def process_field_value_obj(field_name, article):
                    value = getattr(article, field.name, "")
                    if field_name == "publish_time" and value:
                        # 如果已经是格式化的字符串，直接返回
                        if isinstance(value, str) and "-" in value and ":" in value:
                            return value
                        # 如果是时间戳整数，转换为字符串
                        try:
                            if isinstance(value, (int, float)):
                                return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
                        except (ValueError, TypeError, OSError):
                            pass
                        return value
                    return value

                processed_article = {
                    field.name: process_field_value_obj(field.name, article)
                    for field in Article.__table__.columns
                }
            processed_articles.append(processed_article)

        hook.articles = processed_articles

        if hook.task.message_type == 0:  # 发送消息
            return send_message(hook)
        elif hook.task.message_type == 1:  # 调用webhook
            return call_webhook(hook, is_test)
        else:
            raise ValueError(f"未知的消息类型: {hook.task.message_type}")
    except Exception as e:
        raise ValueError(f"处理消息时出错: {str(e)}")

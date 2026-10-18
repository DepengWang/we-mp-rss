import requests
import json


def _truncate_utf8(text, max_bytes):
    encoded = str(text).encode("utf-8")
    if len(encoded) <= max_bytes:
        return str(text)
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def send_wechat_message(webhook_url, title, text, msgtype="markdown"):
    """
    发送微信消息

    参数:
    - webhook_url: 微信机器人Webhook地址
    - title: 消息标题
    - text: 消息内容
    - msgtype: markdown（默认，仅企业微信可见）或 text（微信中也能查看，网址可直接点击）
    """
    headers = {'Content-Type': 'application/json'}
    if msgtype == "text":
        # 企微 text.content 上限 2048 字节
        data = {"msgtype": "text", "text": {"content": _truncate_utf8(text, 2048)}}
    else:
        # 截取 text 确保字符数不超过 4096 个
        text = text[:2048]
        data = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"{text}"
            }
        }
    try:
        response = requests.post(
            url=webhook_url,
            headers=headers,
            data=json.dumps(data),
            timeout=30
        )
        print(response.text)
    except Exception as e:
        print('微信通知发送失败', e)
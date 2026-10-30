"""Run notifications (FR-UX-06): a chat-bot webhook and/or a desktop notification.

Configured with ``designaround config notify --webhook URL [--desktop]``. The webhook
format follows the host: Feishu/Lark, WeCom (企业微信), DingTalk, Slack, or a generic
JSON body for anything else. Sending never raises; a failed notification must not
fail the run it reports on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from urllib.parse import urlparse

from . import config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def settings() -> dict:
    return dict(config.load().get("notify") or {})


def body_for(url: str, title: str, text: str, data: dict | None = None) -> dict:
    host = (urlparse(url).hostname or "").lower()
    message = f"{title}\n{text}".strip()
    if host.endswith(("feishu.cn", "larksuite.com", "larkoffice.com")):
        return {"msg_type": "text", "content": {"text": message}}
    if host == "qyapi.weixin.qq.com":
        return {"msgtype": "text", "text": {"content": message}}
    if host.endswith("dingtalk.com"):
        return {"msgtype": "text", "text": {"content": message}}
    if host.endswith("slack.com"):
        return {"text": message}
    return {"source": "designaround", "title": title, "text": text, "data": data or {}}


def send_webhook(url: str, title: str, text: str, data: dict | None = None) -> dict:
    import httpx

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return {"ok": False, "error": "webhook must be an https URL"}
    try:
        response = httpx.post(url, json=body_for(url, title, text, data), timeout=8)
        return {"ok": response.status_code < 400, "status": response.status_code}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": type(exc).__name__}


def desktop(title: str, text: str) -> dict:
    """OS notification without extra packages."""

    try:
        if sys.platform == "darwin":
            script = f"display notification {json.dumps(text[:180])} with title {json.dumps(title[:80])}"
            subprocess.run(["osascript", "-e", script], check=False, timeout=8, capture_output=True)
        elif sys.platform == "win32":
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                f"$t = $x.GetElementsByTagName('text'); $t.Item(0).AppendChild($x.CreateTextNode({json.dumps(title[:80])})) > $null; $t.Item(1).AppendChild($x.CreateTextNode({json.dumps(text[:180])})) > $null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('DesignAround').Show([Windows.UI.Notifications.ToastNotification]::new($x))"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False, timeout=12, capture_output=True, creationflags=NO_WINDOW)
        else:
            subprocess.run(["notify-send", title[:80], text[:180]], check=False, timeout=8, capture_output=True)
        return {"ok": True}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": type(exc).__name__}


def notify(title: str, text: str, data: dict | None = None, *, force_desktop: bool = False) -> dict:
    s = settings()
    out: dict = {}
    if s.get("webhook"):
        out["webhook"] = send_webhook(s["webhook"], title, text, data)
    if s.get("desktop") or force_desktop:
        out["desktop"] = desktop(title, text)
    return out


def describe_outcome(task: str, case_id: str, code: int, outcome: dict) -> tuple[str, str]:
    """Title and one line for a finished agent run."""

    names = {"around": "规避改型", "build": "3D 建模", "concepts": "设计方向", "model": "照片建模", "observe": "观察者评分"}
    what = names.get(task, task)
    from .guarantee import describe as guarantee_line

    refund = guarantee_line(outcome.get("guarantee"))
    tail = f" {refund}" if refund else ""
    if outcome.get("type") == "error":
        return f"DesignAround：{what}失败", f"{case_id}：{outcome.get('message', '')[:140]}{tail}"
    status = outcome.get("status")
    if task == "around" and status in ("ok", "partial", "no_qualified"):
        qualified = outcome.get("qualified") or []
        if qualified:
            return f"DesignAround：{what}完成", f"{case_id}：合格方案 {', '.join(qualified)}，花费 {outcome.get('spent', 0)} 积分"
        return f"DesignAround：{what}没有找到合格方案", f"{case_id}：花费 {outcome.get('spent', 0)} 积分。{tail.strip()}"
    if status == "ok":
        detail = outcome.get("version") or (", ".join(outcome.get("passed") or []) if task == "concepts" else "")
        return f"DesignAround：{what}完成", f"{case_id}{('：' + detail) if detail else ''}，花费 {outcome.get('spent', 0)} 积分"
    return f"DesignAround：{what}需要你确认", f"{case_id}：{(outcome.get('reason') or '')[:140]}"

"""Alert delivery: ntfy, a generic JSON webhook, and email through Resend.

Every configured notifier gets every alert (fan-out). A notifier is configured
purely by env, so turning one on is a compose change, not a code change:

  ntfy     NTFY_URL (e.g. https://ntfy.sh) + NTFY_TOPIC; optional NTFY_TOKEN
           (Bearer, for a protected topic/self-hosted server). Pushes to the
           ntfy iPhone app. Uses ntfy's JSON publish so titles can be UTF-8.
  webhook  ALERT_WEBHOOK_URL: POSTs the alert as JSON; optional
           ALERT_WEBHOOK_TOKEN sent as Bearer.
  email    RESEND_API_KEY + ALERT_EMAIL_TO (comma-separated) + ALERT_EMAIL_FROM
           (a sender on a domain verified in Resend).

A failing notifier is logged and reported in the result, never raised: one
dead channel must not stop the others, and must not take the engine down.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Optional, Protocol

import httpx

logger = logging.getLogger("lucidlink_api.updates")

NOTIFY_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
RESEND_URL = "https://api.resend.com/emails"


@dataclass
class Alert:
    component: str
    label: str
    version: str
    kind: str                 # "release" | "security:<id>"
    title: str
    body: str
    priority: str = "default"  # "default" | "high" | "urgent"
    url: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


class Notifier(Protocol):
    name: str

    def send(self, alert: Alert, client: httpx.Client) -> None:
        """Deliver or raise."""


_NTFY_PRIORITY = {"default": 3, "high": 4, "urgent": 5}


class NtfyNotifier:
    name = "ntfy"

    def __init__(self, url: str, topic: str, token: str = ""):
        self.url = url.rstrip("/")
        self.topic = topic
        self.token = token

    def send(self, alert: Alert, client: httpx.Client) -> None:
        payload = {
            "topic": self.topic,
            "title": alert.title,
            "message": alert.body[:3800],  # ntfy's message limit is 4096 bytes
            "priority": _NTFY_PRIORITY.get(alert.priority, 3),
            "tags": alert.tags or ["package"],
        }
        if alert.url:
            payload["click"] = alert.url
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = client.post(self.url, json=payload, headers=headers)
        resp.raise_for_status()


class WebhookNotifier:
    name = "webhook"

    def __init__(self, url: str, token: str = ""):
        self.url = url
        self.token = token

    def send(self, alert: Alert, client: httpx.Client) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = client.post(self.url, json={"type": "lucidlink.update", **asdict(alert)}, headers=headers)
        resp.raise_for_status()


class ResendNotifier:
    name = "email"

    def __init__(self, api_key: str, to: list[str], sender: str):
        self.api_key = api_key
        self.to = to
        self.sender = sender

    def send(self, alert: Alert, client: httpx.Client) -> None:
        text = alert.body + (f"\n\n{alert.url}" if alert.url else "")
        resp = client.post(RESEND_URL, headers={"Authorization": f"Bearer {self.api_key}"},
                           json={"from": self.sender, "to": self.to,
                                 "subject": alert.title, "text": text})
        resp.raise_for_status()


def notifiers_from_env(env: Optional[dict] = None) -> list[Notifier]:
    env = os.environ if env is None else env
    out: list[Notifier] = []
    if env.get("NTFY_URL") and env.get("NTFY_TOPIC"):
        out.append(NtfyNotifier(env["NTFY_URL"], env["NTFY_TOPIC"], env.get("NTFY_TOKEN", "")))
    elif env.get("NTFY_URL") or env.get("NTFY_TOPIC"):
        logger.warning("ntfy needs both NTFY_URL and NTFY_TOPIC; ntfy alerts disabled")
    if env.get("ALERT_WEBHOOK_URL"):
        out.append(WebhookNotifier(env["ALERT_WEBHOOK_URL"], env.get("ALERT_WEBHOOK_TOKEN", "")))
    resend = [env.get("RESEND_API_KEY"), env.get("ALERT_EMAIL_TO"), env.get("ALERT_EMAIL_FROM")]
    if all(resend):
        to = [a.strip() for a in env["ALERT_EMAIL_TO"].split(",") if a.strip()]
        out.append(ResendNotifier(env["RESEND_API_KEY"], to, env["ALERT_EMAIL_FROM"]))
    elif any(resend):
        logger.warning("email alerts need RESEND_API_KEY, ALERT_EMAIL_TO and ALERT_EMAIL_FROM; disabled")
    return out


def _describe_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__  # never the message: it can carry a URL with a token in it


def fan_out(notifiers: list[Notifier], alert: Alert,
            transport: Optional[httpx.BaseTransport] = None) -> dict[str, str]:
    """Send to every notifier. Returns {name: "ok" | "error: ..."}; never raises."""
    results: dict[str, str] = {}
    with httpx.Client(timeout=NOTIFY_TIMEOUT, transport=transport) as client:
        for n in notifiers:
            try:
                n.send(alert, client)
                results[n.name] = "ok"
            except Exception as e:  # one broken channel must not stop the rest
                results[n.name] = f"error: {_describe_error(e)}"
                logger.warning("update alert via %s failed for %s %s: %s",
                               n.name, alert.component, alert.version, _describe_error(e))
    return results

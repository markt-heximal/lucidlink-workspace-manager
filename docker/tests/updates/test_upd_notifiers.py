"""Notifier selection by env and the payload each channel sends."""
from updates.notifiers import (Alert, NtfyNotifier, ResendNotifier, WebhookNotifier, fan_out,
                               notifiers_from_env)
from upd_helpers import Recorder


def _alert(**kw) -> Alert:
    base = dict(component="lucidlink", label="LucidLink SDK", version="1.0.1", kind="release",
                title="LucidLink SDK 1.0.1 available — major", body="details", priority="high",
                url="https://pypi.org/project/lucidlink/", tags=["package", "warning"])
    base.update(kw)
    return Alert(**base)


def test_nothing_configured_means_no_notifiers():
    assert notifiers_from_env({}) == []


def test_each_channel_is_selected_by_its_env():
    env = {"NTFY_URL": "https://ntfy.sh", "NTFY_TOPIC": "t", "ALERT_WEBHOOK_URL": "https://h/x",
           "RESEND_API_KEY": "re_x", "ALERT_EMAIL_TO": "a@x.com, b@x.com", "ALERT_EMAIL_FROM": "bot@x.com"}
    ns = notifiers_from_env(env)
    assert [n.name for n in ns] == ["ntfy", "webhook", "email"]
    assert ns[2].to == ["a@x.com", "b@x.com"]


def test_partial_config_disables_that_channel_only():
    ns = notifiers_from_env({"NTFY_URL": "https://ntfy.sh", "RESEND_API_KEY": "re_x",
                             "ALERT_WEBHOOK_URL": "https://h/x"})
    assert [n.name for n in ns] == ["webhook"]


def test_ntfy_payload_is_json_publish_with_unicode_title_and_click():
    rec = Recorder()
    out = fan_out([NtfyNotifier("https://ntfy.example/", "ll", token="tk_1")], _alert(),
                  transport=rec.transport())
    assert out == {"ntfy": "ok"}
    url, body, headers = rec.calls[0]
    assert url == "https://ntfy.example"
    assert body["topic"] == "ll" and body["title"].endswith("— major")
    assert body["priority"] == 4 and body["click"].startswith("https://pypi.org")
    assert body["tags"] == ["package", "warning"]
    assert headers["authorization"] == "Bearer tk_1"


def test_resend_payload():
    rec = Recorder()
    fan_out([ResendNotifier("re_key", ["me@x.com"], "bot@x.com")], _alert(), transport=rec.transport())
    url, body, headers = rec.calls[0]
    assert url == "https://api.resend.com/emails"
    assert body["to"] == ["me@x.com"] and body["from"] == "bot@x.com"
    assert body["subject"].startswith("LucidLink SDK 1.0.1") and "pypi.org" in body["text"]
    assert headers["authorization"] == "Bearer re_key"


def test_webhook_payload_carries_the_whole_alert():
    rec = Recorder()
    fan_out([WebhookNotifier("https://hooks.example/in", token="w")], _alert(), transport=rec.transport())
    _, body, headers = rec.calls[0]
    assert body["type"] == "lucidlink.update" and body["version"] == "1.0.1" and body["priority"] == "high"
    assert headers["authorization"] == "Bearer w"


def test_fan_out_survives_exceptions_and_does_not_leak_their_message(caplog):
    rec = Recorder()
    rec.raise_for.add("ntfy.example")
    out = fan_out([NtfyNotifier("https://ntfy.example", "t"), WebhookNotifier("https://hooks.example/in")],
                  _alert(), transport=rec.transport())
    assert out == {"ntfy": "error: ConnectError", "webhook": "ok"}
    assert "SECRET" not in caplog.text

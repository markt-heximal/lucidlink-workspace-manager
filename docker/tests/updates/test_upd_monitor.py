"""The check: alerts once per release, nothing when unchanged, fan-out with a
failing channel, retry when every channel fails, LLM present/absent/broken,
security advisories, and registry failures that stay contained."""
import httpx

from updates.notifiers import NtfyNotifier, ResendNotifier, WebhookNotifier
from updates.sources import extract_version_section
from upd_helpers import pypi_component, sse_llm


def _keys(alerts):
    return sorted((a["component"], a["version"], a["kind"]) for a in alerts)


def test_first_check_alerts_once_per_component_and_classifies(make_monitor):
    m = make_monitor()
    r = m.check()
    assert r["ok"] and r["new_alerts"] == 2
    assert _keys(r["alerts"]) == [("img", "1.5.7", "release"), ("sdk", "1.0.1", "release")]

    state = {c["key"]: c for c in m.state()["components"]}
    sdk, img = state["sdk"], state["img"]
    assert (sdk["pinned"], sdk["latest"], sdk["status"]) == ("0.14.0", "1.0.1", "update_available")
    assert (sdk["update_type"], sdk["breaking"], sdk["automerge_eligible"]) == ("major", True, False)
    # Floating tags ("latest", "1.5") are not releases.
    assert (img["latest"], img["update_type"], img["automerge_eligible"]) == ("1.5.7", "patch", True)
    assert img["pinned_digest"] == "sha256:" + "a" * 64


def test_second_check_with_nothing_new_is_silent(make_monitor, store):
    m = make_monitor()
    m.check()
    first_seen = store.get_component("sdk")["latest_first_seen_at"]
    r = m.check()
    assert r["new_alerts"] == 0
    assert len(store.recent_alerts()) == 2
    assert store.get_component("sdk")["latest_first_seen_at"] == first_seen


def test_a_new_release_alerts_once_more(make_monitor, registry, store):
    m = make_monitor()
    m.check()
    registry.publish_pypi("fakesdk", "1.1.0")
    r = m.check()
    assert _keys(r["alerts"]) == [("sdk", "1.1.0", "release")]
    assert m.check()["new_alerts"] == 0
    assert store.get_component("sdk")["latest_version"] == "1.1.0"


def test_no_alert_when_pin_is_current(make_monitor, pin_dir):
    (pin_dir / "requirements.txt").write_text("fakesdk==1.0.1\n")
    (pin_dir / "docker-compose.yml").write_text("image: acme/api:1.5.7\n")
    m = make_monitor()
    r = m.check()
    assert r["new_alerts"] == 0
    assert {c["status"] for c in m.state()["components"]} == {"current"}


def test_prereleases_and_yanked_versions_are_ignored(make_monitor, registry):
    registry.publish_pypi("fakesdk", "2.0.0rc1")
    registry.publish_pypi("fakesdk", "1.0.2", yanked=True)
    m = make_monitor(components=[pypi_component()])
    m.check()
    assert m.state()["components"][0]["latest"] == "1.0.1"


def test_fan_out_reaches_every_notifier_even_when_one_fails(make_monitor, recorder, store):
    recorder.status["ntfy.example"] = 500
    notifiers = [NtfyNotifier("https://ntfy.example", "ll-updates"),
                 WebhookNotifier("https://hooks.example/in"),
                 ResendNotifier("re_key", ["me@example.com"], "bot@example.com")]
    m = make_monitor(components=[pypi_component()], notifiers=notifiers)
    r = m.check()
    assert r["new_alerts"] == 1
    assert sorted(recorder.hosts()) == ["api.resend.com", "hooks.example", "ntfy.example"]
    channels = store.recent_alerts()[0]["channels"]
    assert channels == {"ntfy": "error: HTTP 500", "webhook": "ok", "email": "ok"}
    # Delivered somewhere -> recorded -> not re-sent.
    recorder.calls.clear()
    assert m.check()["new_alerts"] == 0 and recorder.calls == []


def test_all_notifiers_failing_means_retry_next_check(make_monitor, recorder, store):
    recorder.raise_for.add("ntfy.example")
    m = make_monitor(components=[pypi_component()],
                     notifiers=[NtfyNotifier("https://ntfy.example", "t")])
    r = m.check()
    assert r["ok"] and r["new_alerts"] == 0 and store.recent_alerts() == []
    recorder.raise_for.clear()
    assert m.check()["new_alerts"] == 1
    assert store.recent_alerts()[0]["channels"] == {"ntfy": "ok"}


def test_without_notifiers_the_alert_is_recorded_for_the_api(make_monitor, store):
    make_monitor(components=[pypi_component()]).check()
    alerts = store.recent_alerts()
    assert len(alerts) == 1 and alerts[0]["channels"] == {}


def test_without_llm_the_alert_carries_a_notes_excerpt(make_monitor, registry, recorder):
    registry.publish_pypi("fakesdk", "1.0.2", description="Fixes Range reads past EOF. " * 3)
    m = make_monitor(components=[pypi_component()],
                     notifiers=[WebhookNotifier("https://hooks.example/in")])
    m.check()
    body = recorder.calls[0][1]
    assert "Release notes (excerpt):" in body["body"] and "Fixes Range reads" in body["body"]
    assert body["details"]["llm_summary"] is False
    assert body["type"] == "lucidlink.update"


def test_llm_summary_is_used_and_cached_across_retries(make_monitor, recorder, monkeypatch, store):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    calls = []
    recorder.raise_for.add("hooks.example")
    m = make_monitor(components=[pypi_component()], llm_transport=sse_llm(calls=calls),
                     notifiers=[WebhookNotifier("https://hooks.example/in")])
    m.check()                        # delivery fails; summary already cached
    recorder.raise_for.clear()
    m.check()
    assert len(calls) == 1
    prompt = calls[0]["messages"][0]["content"]
    assert "0.14.0" in prompt and "1.0.1" in prompt and "delta indexer" in prompt
    body = recorder.calls[0][1]["body"]
    assert "What matters for this app:" in body and "renamed fs to file_system" in body
    assert m.state()["components"][0]["summary"].startswith("- Breaking")


def test_llm_error_degrades_to_excerpt(make_monitor, recorder, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    m = make_monitor(components=[pypi_component()], llm_transport=sse_llm(status=500),
                     notifiers=[WebhookNotifier("https://hooks.example/in")])
    assert m.check()["new_alerts"] == 1
    assert recorder.calls[0][1]["details"]["llm_summary"] is False


def test_security_advisory_on_pinned_version_alerts_urgently_once(make_monitor, registry, store):
    registry.vulns[("fakesdk", "0.14.0")] = [
        {"id": "PYSEC-2026-1", "summary": "token leak", "fixed_in": ["1.0.1"], "link": "https://osv.dev/x"},
        {"id": "PYSEC-2026-0", "summary": "old", "withdrawn": "2026-01-01"},
    ]
    m = make_monitor(components=[pypi_component()])
    r = m.check()
    assert _keys(r["alerts"]) == [("sdk", "0.14.0", "security:PYSEC-2026-1"), ("sdk", "1.0.1", "release")]
    sec = [a for a in store.recent_alerts() if a["kind"].startswith("security")][0]
    assert sec["priority"] == "urgent" and "token leak" in sec["body"]
    assert m.check()["new_alerts"] == 0


def test_security_wording_in_release_notes_raises_priority(make_monitor, registry, store):
    registry.publish_pypi("fakesdk", "1.0.2", description="Includes security fixes for a certificate issue.")
    make_monitor(components=[pypi_component()]).check()
    alert = store.recent_alerts()[0]
    assert alert["priority"] == "urgent" and "SECURITY" in alert["body"]


def test_github_release_notes_are_pulled_in(make_monitor, registry, store):
    registry.publish_pypi("fakesdk", "1.0.2", project_urls={"Release notes": "https://github.com/acme/fakesdk"})
    registry.github["/acme/fakesdk/HEAD/README.md"] = (
        "# Notes\n\n## 1.0.2 - 2026-10-24\n\nFaster read_dir.\n\n## 1.0.1\n\nOlder.\n")
    make_monitor(components=[pypi_component()]).check()
    body = store.recent_alerts()[0]["body"]
    assert "Faster read_dir." in body and "Older." not in body


def test_extract_version_section_stops_at_next_heading():
    text = "# T\n## 1.0.10\nten\n## 1.0.1 - date\none\n### Fixes\nfix\n## 1.0.0\nzero\n"
    assert extract_version_section(text, "1.0.1") == "## 1.0.1 - date\none\n### Fixes\nfix"
    assert extract_version_section(text, "2.0.0") == ""


def test_registry_failure_is_contained_and_reported(make_monitor, registry, store):
    registry.fail.add("fakesdk")
    m = make_monitor()
    r = m.check()
    assert not r["ok"] and "HTTP 503" in r["errors"]["sdk"]
    assert _keys(r["alerts"]) == [("img", "1.5.7", "release")]
    sdk = {c["key"]: c for c in m.state()["components"]}["sdk"]
    assert sdk["error"] and sdk["status"] == "unknown"


def test_registry_failure_keeps_last_known_latest(make_monitor, registry, store):
    m = make_monitor(components=[pypi_component()])
    m.check()
    registry.fail.add("fakesdk")
    m.check()
    row = store.get_component("sdk")
    assert row["latest_version"] == "1.0.1" and row["last_error"]


def test_network_error_never_raises(make_monitor):
    def boom(request):
        raise httpx.ConnectError("offline", request=request)
    r = make_monitor(transport=httpx.MockTransport(boom)).check()
    assert not r["ok"] and set(r["errors"]) == {"sdk", "img"}

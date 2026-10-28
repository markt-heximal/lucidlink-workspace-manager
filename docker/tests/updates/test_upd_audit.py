"""Our audit notes feed the release summary, and a changed audit rewrites it."""
import json

from upd_helpers import pypi_component, sse_llm

from updates import audit as audit_notes
from updates.notifiers import WebhookNotifier


def _write_audit(path, notes: str, status: str = "Migrated on a branch") -> None:
    path.write_text(json.dumps({"sdk": {
        "*": {"notes": "Linux wheels are x86_64 only."},
        "1.0.1": {"status": status, "notes": notes}}}))


def _llm_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    audit_file = tmp_path / "audit.json"
    monkeypatch.setenv("UPDATES_AUDIT_FILE", str(audit_file))
    return audit_file


def test_audit_and_status_are_in_the_prompt(make_monitor, recorder, monkeypatch, tmp_path):
    audit_file = _llm_env(monkeypatch, tmp_path)
    _write_audit(audit_file, "read_dir now returns real entry ids.")
    calls = []
    m = make_monitor(components=[pypi_component()], llm_transport=sse_llm(calls=calls))
    m.check()
    prompt = calls[0]["messages"][0]["content"]
    assert "read_dir now returns real entry ids." in prompt
    assert "Linux wheels are x86_64 only." in prompt          # the component's general notes too
    assert "Status in this deployment: Migrated on a branch" in prompt
    assert prompt.index("<<<AUDIT") < prompt.index("<<<NOTES")


def test_changed_audit_rewrites_the_summary_without_a_second_alert(make_monitor, recorder, monkeypatch,
                                                                   tmp_path, store):
    audit_file = _llm_env(monkeypatch, tmp_path)
    _write_audit(audit_file, "first finding")
    calls = []
    m = make_monitor(components=[pypi_component()],
                     llm_transport=sse_llm(answer="- First summary.", calls=calls),
                     notifiers=[WebhookNotifier("https://hooks.example/in")])
    m.check()
    m.check()                                                  # unchanged audit: cached, no LLM call
    assert len(calls) == 1 and len(recorder.calls) == 1

    _write_audit(audit_file, "second finding")
    m.llm_transport = sse_llm(answer="- Second summary.", calls=calls)
    m.check()
    assert len(calls) == 2 and "second finding" in calls[1]["messages"][0]["content"]
    assert len(recorder.calls) == 1                            # still announced only once
    assert m.state()["components"][0]["summary"] == "- Second summary."


def test_without_llm_the_alert_carries_our_audit(make_monitor, recorder, monkeypatch, tmp_path):
    audit_file = tmp_path / "audit.json"
    monkeypatch.setenv("UPDATES_AUDIT_FILE", str(audit_file))
    _write_audit(audit_file, "Connect was redesigned.", status="Not started")
    m = make_monitor(components=[pypi_component()], notifiers=[WebhookNotifier("https://hooks.example/in")])
    m.check()
    body = recorder.calls[0][1]["body"]
    assert "What matters for this app:" in body and "Connect was redesigned." in body
    assert "Status: Not started" in body


def test_override_merges_over_packaged_notes(tmp_path):
    override = tmp_path / "o.json"
    override.write_text(json.dumps({"lucidlink": {"1.0.1": {"status": "Deployed"}}}))
    notes = audit_notes.load(str(override))
    text, status = audit_notes.for_release(notes, "lucidlink", "1.0.1")
    assert status == "Deployed"                                # override wins for the field it sets
    assert "entry ids" in text                                 # packaged notes kept


def test_unreadable_override_is_ignored(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert "lucidlink" in audit_notes.load(str(bad))

"""Fixtures for the update-monitor tests: an isolated env, a temp DB, a pin
directory, a fake registry and a factory for wired monitors."""
from __future__ import annotations

from pathlib import Path

import pytest

from updates.monitor import UpdateMonitor, set_monitor
from updates.store import UpdateStore
from upd_helpers import FakeRegistry, Recorder, docker_component, pypi_component

_ENV = ["UPDATES_AUDIT_FILE", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN",
        "ALERT_WEBHOOK_URL", "ALERT_WEBHOOK_TOKEN", "RESEND_API_KEY", "ALERT_EMAIL_TO",
        "ALERT_EMAIL_FROM", "UPDATES_DB", "UPDATES_PIN_DIR", "UPDATES_COMPONENTS_FILE",
        "UPDATE_CHECK_INTERVAL_SEC", "UPDATE_CHECK_INITIAL_DELAY_SEC",
        "UPDATES_MIN_MANUAL_INTERVAL_SEC", "UPDATES_PIN_SDK", "UPDATES_PIN_IMG",
        "UPDATES_PIN_LUCIDLINK", "UPDATES_PIN_LUCIDLINK_API", "UPDATES_PIN_LUCIDLINK_MCP"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    yield
    set_monitor(None)


@pytest.fixture
def pin_dir(tmp_path) -> Path:
    d = tmp_path / "pins"
    d.mkdir()
    (d / "requirements.txt").write_text("fastapi==0.115.12\nfakesdk==0.14.0  # pinned\n")
    (d / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: acme/api:1.5.0@sha256:" + "a" * 64 + "\n")
    return d


@pytest.fixture
def store(tmp_path) -> UpdateStore:
    return UpdateStore(str(tmp_path / "updates.db"))


@pytest.fixture
def registry() -> FakeRegistry:
    reg = FakeRegistry()
    for v in ("0.13.0", "0.14.0", "0.15.0", "1.0.0", "1.0.1"):
        reg.publish_pypi("fakesdk", v)
    for t in ("1.5.0", "1.5.7"):
        reg.publish_docker("acme/api", t)
    reg.publish_docker("acme/api", "latest")
    reg.publish_docker("acme/api", "1.5")
    return reg


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def make_monitor(store, registry, recorder, pin_dir):
    def make(**kw) -> UpdateMonitor:
        kw.setdefault("components", [pypi_component(), docker_component()])
        kw.setdefault("notifiers", [])
        kw.setdefault("transport", registry.transport())
        kw.setdefault("notify_transport", recorder.transport())
        kw.setdefault("pin_dirs", [pin_dir])
        return UpdateMonitor(store=store, **kw)
    return make

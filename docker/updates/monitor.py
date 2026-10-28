"""The check itself: pins vs registries -> state, and alerts that fire once.

One `UpdateMonitor.check()`:
  1. resolves each component's pin (env override > installed > pin file),
  2. asks its registry for the newest final release,
  3. classifies latest vs pin (patch/minor/major; 0.x minors count as breaking),
  4. for a release newer than the pin that has not been announced, builds an
     alert (LLM summary when configured, else a notes excerpt) and fans it out,
  5. does the same for security advisories PyPI lists against the pinned
     version, which is the case that matters most on a public gateway.

"Once" is the `alerts` table's UNIQUE (component, version, kind). An alert is
recorded when at least one notifier accepted it, or when no notifier is
configured (it is then visible in GET /engine/updates only). If every
configured notifier failed, nothing is recorded and the next check retries.

`check()` holds a lock, so the scheduler and POST /engine/updates/check never
run two checks at once; a second caller gets CheckInProgress.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from .components import Component, Pin, load_components, resolve_pin
from .notifiers import Alert, Notifier, fan_out, notifiers_from_env
from .sources import HTTP_TIMEOUT, Release, SourceError, fetch_latest
from .store import UpdateStore, now_iso
from . import audit as audit_notes
from .summarize import PROMPT_VERSION, llm_configured, summarize
from .versions import classify, count_between

logger = logging.getLogger("lucidlink_api.updates")

USER_AGENT = "lucidlink-workspace-manager-update-monitor/1 (+docs/UPDATES.md)"
_SECURITY_RE = re.compile(r"\bsecurity\b|\bCVE-\d{4}-\d+|\bvulnerab|\bGHSA-", re.IGNORECASE)
EXCERPT_CHARS = 1200


class CheckInProgress(Exception):
    pass


def _status(update_type: str, pinned: Optional[str], latest: Optional[str]) -> str:
    if latest is None:
        return "unknown"
    if pinned is None:
        return "unpinned"
    return {"none": "current", "downgrade": "ahead", "unknown": "unknown"}.get(update_type, "update_available")


def _excerpt(notes: str, limit: int = EXCERPT_CHARS) -> str:
    text = re.sub(r"<!--.*?-->", "", notes or "", flags=re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


class UpdateMonitor:
    def __init__(self, *, store: Optional[UpdateStore] = None,
                 components: Optional[list[Component]] = None,
                 notifiers: Optional[list[Notifier]] = None,
                 transport: Optional[httpx.BaseTransport] = None,
                 notify_transport: Optional[httpx.BaseTransport] = None,
                 llm_transport: Optional[httpx.BaseTransport] = None,
                 pin_dirs: Optional[list[Path]] = None):
        self.store = store or UpdateStore()
        self.components = components if components is not None else load_components()
        self.notifiers = notifiers if notifiers is not None else notifiers_from_env()
        self.transport = transport
        self.notify_transport = notify_transport
        self.llm_transport = llm_transport
        self.pin_dirs = pin_dirs
        self._lock = threading.Lock()
        self.last_manual_check: float = 0.0

    # ------------------------------------------------------------------ check
    def check(self, *, trigger: str = "schedule") -> dict:
        if not self._lock.acquire(blocking=False):
            raise CheckInProgress("an update check is already running")
        try:
            return self._check(trigger)
        finally:
            self._lock.release()

    def _check(self, trigger: str) -> dict:
        started = now_iso()
        t0 = time.monotonic()
        self.store.set_meta("last_check", {"started_at": started, "trigger": trigger, "running": True})
        new_alerts: list[dict] = []
        errors: dict[str, str] = {}
        with httpx.Client(timeout=HTTP_TIMEOUT, transport=self.transport, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
            for comp in self.components:
                try:
                    new_alerts += self._check_component(client, comp)
                except SourceError as e:
                    errors[comp.key] = str(e)
                    logger.warning("update check %s: %s", comp.key, e)
                    pin = resolve_pin(comp, self.pin_dirs)
                    self.store.record_check(comp.key, pinned=pin.version, latest=None,
                                            released_at=None, error=str(e))
                except Exception as e:  # a bug for one component must not hide the others
                    errors[comp.key] = f"{type(e).__name__}: {e}"
                    logger.exception("update check %s failed", comp.key)
        result = {"started_at": started, "finished_at": now_iso(), "trigger": trigger,
                  "duration_ms": round((time.monotonic() - t0) * 1000), "ok": not errors,
                  "errors": errors, "new_alerts": len(new_alerts), "running": False}
        self.store.set_meta("last_check", result)
        logger.info("update check (%s): %d components, %d new alerts, %d errors",
                    trigger, len(self.components), len(new_alerts), len(errors))
        return {**result, "alerts": new_alerts}

    def _check_component(self, client: httpx.Client, comp: Component) -> list[dict]:
        pin = resolve_pin(comp, self.pin_dirs)
        release = fetch_latest(client, comp, pin.version)
        cls = classify(pin.version, release.version)
        security = bool(_SECURITY_RE.search(release.notes or ""))
        self.store.record_check(comp.key, pinned=pin.version, latest=release.version,
                                released_at=release.released_at, error=None)
        alerts: list[dict] = []

        if cls.update_type in ("patch", "minor", "major"):
            self.store.upsert_release(comp.key, release.version, released_at=release.released_at,
                                      update_type=cls.update_type, breaking=cls.breaking,
                                      security=security, notes_url=release.notes_url)
            if not self.store.has_alert(comp.key, release.version, "release"):
                alert = self._release_alert(comp, pin, release, cls, security)
                if self._deliver(alert):
                    alerts.append(self._alert_dict(alert))
            else:
                # Already announced: only refresh the summary shown in Engine
                # when the audit notes or prompt changed. Never a second alert.
                self._summary(comp, pin, release, cls)

        for vuln in release.pinned_vulnerabilities:
            kind = f"security:{vuln.get('id') or 'unknown'}"
            if pin.version and not self.store.has_alert(comp.key, pin.version, kind):
                alert = self._vuln_alert(comp, pin, release, vuln, kind)
                if self._deliver(alert):
                    alerts.append(self._alert_dict(alert))
        return alerts

    # ----------------------------------------------------------------- alerts
    def _summary(self, comp: Component, pin: Pin, release: Release, cls) -> tuple[str, bool]:
        """(text, from_llm). Cached per release and per context (our audit notes,
        the prompt), so a retry does not pay twice but a new finding is used."""
        audit, status = audit_notes.for_release(audit_notes.load(), comp.key, release.version)
        ctx = audit_notes.fingerprint(PROMPT_VERSION, audit, status, pin.version or "")
        row = self.store.get_release(comp.key, release.version) or {}
        if row.get("summary") and row.get("summary_ctx") == ctx:
            return row["summary"], True
        text = summarize(label=comp.label, package=comp.package, relevance=comp.relevance,
                         current=pin.version, latest=release.version, update_type=cls.update_type,
                         breaking=cls.breaking, notes=release.notes, audit=audit, status=status,
                         transport=self.llm_transport)
        if text:
            self.store.set_summary(comp.key, release.version, text, ctx)
            return text, True
        if row.get("summary"):
            return row["summary"], True   # LLM unavailable now: keep the older summary
        if audit:
            return f"Our audit: {audit}" + (f"\nStatus: {status}" if status else ""), False
        return _excerpt(release.notes) or "(no release notes published)", False

    def _release_alert(self, comp: Component, pin: Pin, release: Release, cls, security: bool) -> Alert:
        behind = count_between(release.all_versions, pin.version, release.version)
        risk = ", may break the API" if cls.breaking else ""
        title = f"{comp.label} {release.version} available ({cls.update_type}; running {pin.version})"
        summary, from_llm = self._summary(comp, pin, release, cls)
        lines = [f"{comp.label} ({comp.package}): {pin.version} -> {release.version} "
                 f"({cls.update_type}{risk})."]
        meta = []
        if release.released_at:
            meta.append(f"released {release.released_at[:10]}")
        if behind > 1:
            meta.append(f"{behind} releases behind")
        if release.platforms:
            meta.append("platforms: " + ", ".join(release.platforms))
        if meta:
            lines.append("; ".join(meta).capitalize() + ".")
        if security:
            lines.append("The release notes mention a SECURITY fix."
                         + (" This component is reachable through the public Funnel gateway: prioritise it."
                            if comp.public else ""))
        if release.digest and pin.digest and release.pinned_digest_now and release.pinned_digest_now != pin.digest:
            lines.append("Note: the pinned tag has since been re-pushed with a different digest.")
        heading = ("What matters for this app:" if from_llm or summary.startswith("Our audit:")
                   else "Release notes (excerpt):")
        lines += ["", heading, summary, ""]
        if cls.automerge_eligible:
            lines.append("Renovate may automerge this once CI passes; the Mini then redeploys on its next pull.")
        else:
            lines.append("Renovate opens a PR labelled for review. Read the API diff before merging.")
        tags = ["package"] + (["warning"] if cls.breaking else []) + (["rotating_light"] if security else [])
        priority = ("urgent" if comp.public else "high") if security else ("high" if cls.breaking else "default")
        return Alert(component=comp.key, label=comp.label, version=release.version, kind="release",
                     title=title, body="\n".join(lines), priority=priority, url=release.notes_url,
                     tags=tags, details={"current": pin.version, "latest": release.version,
                                         "update_type": cls.update_type, "breaking": cls.breaking,
                                         "automerge_eligible": cls.automerge_eligible,
                                         "security": security, "llm_summary": from_llm})

    def _vuln_alert(self, comp: Component, pin: Pin, release: Release, vuln: dict, kind: str) -> Alert:
        vid = vuln.get("id") or "advisory"
        fixed = ", ".join(vuln.get("fixed_in") or []) or "not stated"
        body = (f"PyPI lists {vid} against {comp.package} {pin.version}, which this deployment runs.\n"
                f"{vuln.get('summary') or ''}\nFixed in: {fixed}. Latest: {release.version}.\n"
                + ("This component is reachable through the public Funnel gateway: upgrade promptly."
                   if comp.public else "Upgrade promptly."))
        return Alert(component=comp.key, label=comp.label, version=pin.version or "", kind=kind,
                     title=f"Security advisory {vid} affects {comp.label} {pin.version}",
                     body=body, priority="urgent", url=vuln.get("link") or release.notes_url,
                     tags=["rotating_light"], details={"advisory": vuln})

    def _deliver(self, alert: Alert) -> bool:
        channels = fan_out(self.notifiers, alert, transport=self.notify_transport) if self.notifiers else {}
        if self.notifiers and not any(v == "ok" for v in channels.values()):
            logger.warning("update alert %s %s not delivered on any channel; will retry next check",
                           alert.component, alert.version)
            return False
        return self.store.add_alert(alert.component, alert.version, alert.kind, title=alert.title,
                                    body=alert.body, priority=alert.priority, channels=channels)

    @staticmethod
    def _alert_dict(alert: Alert) -> dict:
        return {"component": alert.component, "version": alert.version, "kind": alert.kind,
                "title": alert.title, "priority": alert.priority}

    # ------------------------------------------------------------------ state
    def state(self, alerts_limit: int = 20) -> dict:
        """Everything GET /engine/updates shows. No network calls."""
        comps = []
        for comp in self.components:
            pin = resolve_pin(comp, self.pin_dirs)
            row = self.store.get_component(comp.key) or {}
            latest = row.get("latest_version")
            cls = classify(pin.version, latest) if latest else None
            rel = self.store.get_release(comp.key, latest) if latest else None
            comps.append({
                "key": comp.key,
                "label": comp.label,
                "source": comp.source,
                "package": comp.package,
                "pinned": pin.version,
                "pinned_source": pin.source,
                "declared": pin.declared,
                "declared_source": pin.declared_source,
                "redeploy_pending": bool(pin.declared and pin.version and pin.declared != pin.version),
                "pinned_digest": pin.digest,
                "latest": latest,
                "latest_released_at": row.get("latest_released_at"),
                "latest_first_seen_at": row.get("latest_first_seen_at"),
                "status": _status(cls.update_type if cls else "unknown", pin.version, latest),
                "update_type": cls.update_type if cls else None,
                "breaking": cls.breaking if cls else None,
                "automerge_eligible": cls.automerge_eligible if cls else None,
                "security": bool(rel and rel.get("security")),
                "notes_url": rel.get("notes_url") if rel else None,
                "summary": rel.get("summary") if rel else None,
                "last_checked_at": row.get("last_checked_at"),
                "error": row.get("last_error"),
            })
        return {
            "components": comps,
            "last_check": self.store.get_meta("last_check"),
            "alerts": self.store.recent_alerts(alerts_limit),
            "notifiers": [n.name for n in self.notifiers],
            "llm": llm_configured(),
        }


_monitor: Optional[UpdateMonitor] = None
_monitor_lock = threading.Lock()


def get_monitor() -> UpdateMonitor:
    """The process-wide monitor, built from env on first use."""
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = UpdateMonitor()
        return _monitor


def set_monitor(monitor: Optional[UpdateMonitor]) -> None:
    """Replace the process-wide monitor (tests; or None to rebuild from env)."""
    global _monitor
    with _monitor_lock:
        _monitor = monitor

# Keeping the LucidLink bits current

Three LucidLink components are pinned in this repo, and new releases of each
reach the Mini through one pipeline:

| Component | Pinned in | Tracked by |
|---|---|---|
| `lucidlink` Python SDK | `docker/requirements.txt` | Renovate `pip_requirements` + update monitor (PyPI) |
| `lucidlink/lucidlink-api` image | `docker/docker-compose.yml` and `docker-compose.test.yml` (tag + digest) | Renovate `docker-compose` + update monitor (Docker Hub) |
| `lucidlink-mcp` | `.mcp.json` (`uvx lucidlink-mcp==X`) | Renovate regex manager + update monitor (PyPI) |

```
 PyPI / Docker Hub
        │
        ├──► update monitor (engine, every 6h) ──► ntfy / webhook / email, once per release
        │                                           GET /engine/updates for the UI
        │
        └──► Renovate (Mondays) ──► PR ──► CI (.github/workflows/ci.yml)
                                            │
                     patch, or minor on >=1.x: automerge after green CI
                     majors, 0.x minors, digest re-pushes: label needs-review, a human merges
                                            │
                                            ▼
                                          main ──► redeploy.sh on the Mini (launchd, every 5 min)
                                                   build, start, health-check, roll back on failure
```

Nothing installs into the running service. The gateway is public (Tailscale
Funnel, catch-all, see `docs/GATEWAY.md`), so the only way code changes on the
Mini is a commit on `main` that passed CI.

## Update monitor (`docker/updates/`)

A small package in the engine: fastapi, httpx and the stdlib only.

| Module | Job |
|---|---|
| `components.py` | the tracked list (data-driven) and how each pin is read |
| `sources.py` | newest final release from PyPI / Docker Hub, release notes, advisories |
| `versions.py` | parse + classify patch / minor / major; 0.x minors and 0.0.x patches count as breaking |
| `store.py` | SQLite state at `UPDATES_DB` (default `/data/updates.db`, on the existing volume) |
| `summarize.py` | optional LLM summary: "what in this release matters for this app" |
| `notifiers.py` | ntfy, generic webhook, Resend email; fan-out, failures logged |
| `monitor.py` | one check: pins vs registries, alerts once per release |
| `scheduler.py` | `start_scheduler()` / `stop_scheduler()` (daemon thread) |
| `api.py` | `GET /engine/updates`, `POST /engine/updates/check` |
| `redeploy.sh` | **owner-installed** pull-and-redeploy for the Mini (below) |

### What a check does

1. Resolve each pin: `UPDATES_PIN_<KEY>` env override, else the version
   *installed* in the interpreter (for the SDK: the truth of what runs), else
   the pin file. The file's value is also reported as `declared`;
   `redeploy_pending: true` means `main` moved and the Mini has not caught up.
2. Ask the registry for the newest *final* release (no pre-releases, no yanked
   files, no floating Docker tags like `latest` or `1.5`).
3. Classify it against the pin. `automerge_eligible` mirrors `renovate.json`.
4. If newer and never announced: build an alert (LLM summary when
   `LLM_BASE_URL`/`LLM_MODEL` are set, cached per release; otherwise an
   excerpt of the release notes, including a GitHub-hosted changelog section
   when the package links one) and send it to every configured notifier.
5. Also alert once per PyPI security advisory listed against the *pinned*
   version.

"Once" is a UNIQUE row in `alerts`. A release is recorded as announced when
at least one notifier accepted it, or when no notifier is configured. If
every configured notifier failed, the next check tries again.

Priorities: a security mention on a public component (SDK, management API) is
`urgent` (ntfy priority 5); a breaking update is `high`; others `default`.

### Configuration

| Env | Default | |
|---|---|---|
| `UPDATES_DB` | `/data/updates.db` | SQLite state |
| `UPDATE_CHECK_INTERVAL_SEC` | `21600` | `<= 0` disables the schedule |
| `UPDATE_CHECK_INITIAL_DELAY_SEC` | `60` | first check after boot |
| `UPDATES_MIN_MANUAL_INTERVAL_SEC` | `60` | `POST /check` rate limit (429) |
| `UPDATES_PIN_DIR` | — | directory holding pin files (e.g. a read-only mount) |
| `UPDATES_PIN_<KEY>` | — | force a pin, e.g. `UPDATES_PIN_LUCIDLINK_MCP=0.3.1` |
| `UPDATES_COMPONENTS_FILE` | — | JSON list of extra/overriding components |
| `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN` | — | ntfy push (iPhone app); token optional |
| `ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_TOKEN` | — | JSON POST `{"type": "lucidlink.update", ...}` |
| `RESEND_API_KEY`, `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM` | — | email; `TO` is comma-separated |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT` | — | shared with `/agent/query` |

Adding a component: a dict in `DEFAULT_COMPONENTS` or in
`UPDATES_COMPONENTS_FILE`:

```json
[{"key": "fastapi", "label": "FastAPI", "source": "pypi", "package": "fastapi",
  "pin": {"file": "requirements.txt", "kind": "requirements", "name": "fastapi", "installed": "fastapi"},
  "public": true, "relevance": "Web framework of the public file service."}]
```

`source` is `pypi` or `dockerhub`; pin `kind` is `requirements`, `image` or `uvx`.

### API

Both routes must be mounted owner-only (the file service is the public
catch-all). The router applies no auth itself.

`GET /engine/updates[?alerts=N]`

```json
{
  "components": [{
    "key": "lucidlink", "label": "LucidLink Python SDK", "package": "lucidlink",
    "pinned": "0.14.0", "pinned_source": "installed", "declared": "0.14.0",
    "redeploy_pending": false, "latest": "1.0.1", "latest_released_at": "2026-10-24T16:13:11Z",
    "latest_first_seen_at": "...", "status": "update_available", "update_type": "major",
    "breaking": true, "automerge_eligible": false, "security": false,
    "notes_url": "...", "summary": "...", "last_checked_at": "...", "error": null
  }],
  "last_check": {"started_at": "...", "finished_at": "...", "ok": true, "errors": {}, "new_alerts": 0},
  "alerts": [{"component": "lucidlink", "version": "1.0.1", "kind": "release", "title": "...",
              "priority": "high", "channels": {"ntfy": "ok"}, "created_at": "..."}],
  "notifiers": ["ntfy"], "llm": true,
  "scheduler": {"running": true, "interval_sec": 21600}
}
```

`status` is `current`, `update_available`, `ahead` (pin newer than the
registry's latest), `unpinned` or `unknown` (never checked / registry error).

`POST /engine/updates/check` runs a check synchronously and returns
`{"check": {...}, ...same as GET}`. 429 within the rate limit, 409 while a
check is already running.

### Wiring (owner)

`lucidlink_api.py`:

```python
from fastapi import Depends
from updates import router as updates_router, start_scheduler, stop_scheduler

@asynccontextmanager
async def _lifespan(_app):
    start_scheduler()
    yield
    stop_scheduler()
    _ll_shutdown()

app.include_router(updates_router, dependencies=[Depends(owner_auth.require_owner)])
```

`Dockerfile`: `COPY updates ./updates` next to the other `COPY` lines.

`docker-compose.yml` (`lucidlink-fileservice`): the notifier env vars, and the
pin files the image does not contain, mounted read-only:

```yaml
    environment:
      - NTFY_URL=${NTFY_URL:-}
      - NTFY_TOPIC=${NTFY_TOPIC:-}
      - NTFY_TOKEN=${NTFY_TOKEN:-}
      - ALERT_WEBHOOK_URL=${ALERT_WEBHOOK_URL:-}
      - RESEND_API_KEY=${RESEND_API_KEY:-}
      - ALERT_EMAIL_TO=${ALERT_EMAIL_TO:-}
      - ALERT_EMAIL_FROM=${ALERT_EMAIL_FROM:-}
      - UPDATE_CHECK_INTERVAL_SEC=${UPDATE_CHECK_INTERVAL_SEC:-21600}
      - UPDATES_PIN_DIR=/app/pins
    volumes:
      - lucidlink-data:/data
      - ./docker-compose.yml:/app/pins/docker-compose.yml:ro
      - ./requirements.txt:/app/pins/requirements.txt:ro
      - ../.mcp.json:/app/pins/.mcp.json:ro
```

Only the file service runs the monitor; the indexer does not need it.

## Renovate policy (`renovate.json`)

| Update | What happens |
|---|---|
| patch (any version from 0.1) | automerge after CI is green |
| minor, current >= 1.0 | automerge after CI is green |
| minor, current 0.x | PR labelled `needs-review`, `breaking-risk` |
| patch, current 0.0.x | PR labelled `needs-review`, `breaking-risk` |
| major, digest re-push, pin | PR labelled `needs-review` (+ `major-update`) |
| python base image minor/major | waits for approval on the dependency dashboard |

`platformAutomerge` is off: Renovate merges only after it has itself seen
every status check pass, so this holds even without branch protection. Adding
branch protection on `main` that requires `tests` and `image` is still
recommended. The SDK and MCP soak 3 days after release (`minimumReleaseAge`),
the management API image 2 days; security advisories skip the soak.

Validate after editing: `npx --package renovate renovate-config-validator renovate.json`.

## CI (`.github/workflows/ci.yml`)

On every PR and on pushes to `main` / `renovate/**`:

* `tests`: Python 3.12, `pip install -r docker/requirements.txt pytest`,
  an SDK import smoke check, then `python -m pytest docker/tests -q`.
  The lucidlink wheel is manylinux x86_64 only, which `ubuntu-latest` is.
* `image`: `docker build --platform linux/amd64 docker`, then imports the
  engine inside the image. This fails when a new module is not `COPY`ed.

## Redeploy on the Mini (`docker/updates/redeploy.sh`)

**Owner-installed.** It runs on AI-Factory-Mini under launchd, never from the
engine. Each run:

1. `git fetch` and exit if `HEAD` is already `origin/main`;
2. refuse a dirty, diverged or wrong-branch checkout (and notify);
3. tag the running images `lucidlink-rollback/<service>:<old sha>`;
4. fast-forward, `docker compose build` **both** built services (start.sh
   alone rebuilds only the file service), run `docker/start.sh`;
5. wait up to `HEALTH_TIMEOUT` for `/health`, and for `/version` to report
   `git_sha` = the new commit and `sdk` = the `lucidlink==` pin;
6. on failure: `git reset --hard` to the old commit, retag the rollback
   images to `lucidlink-<service>:latest`, `docker compose up -d --no-build`
   (the old compose file also restores the old `lucidlink-api` digest), write
   the bad commit to `~/.lucidlink-redeploy/bad-sha` so it is not retried every
   5 minutes, and notify. A later commit on `main` is deployed normally.

Install:

```sh
# 1. a clean checkout of main that can fetch non-interactively (deploy key)
git -C ~/lucidlink-workspace-manager status

# 2. optional notification settings
mkdir -p ~/.lucidlink-redeploy
cat > ~/.lucidlink-redeploy/env <<'EOF'
NTFY_URL=https://ntfy.sh
NTFY_TOPIC=<your topic>
EOF
chmod 600 ~/.lucidlink-redeploy/env

# 3. try it without changing anything
DRY_RUN=1 ~/lucidlink-workspace-manager/docker/updates/redeploy.sh
```

`~/Library/LaunchAgents/com.markt.lucidlink-redeploy.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.markt.lucidlink-redeploy</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/markt/lucidlink-workspace-manager/docker/updates/redeploy.sh</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/markt/Library/Logs/lucidlink-redeploy.log</string>
  <key>StandardErrorPath</key><string>/Users/markt/Library/Logs/lucidlink-redeploy.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.markt.lucidlink-redeploy.plist
launchctl kickstart -k gui/$(id -u)/com.markt.lucidlink-redeploy   # run now
```

A LaunchAgent runs while `markt` is logged in, which the Mini already needs
for OrbStack. Without auto-login, install it as a LaunchDaemon with
`<key>UserName</key><string>markt</string>`.

Adjust the paths if the checkout lives elsewhere. Logs are also appended to
`~/.lucidlink-redeploy/redeploy.log`.

## The web app's "Upgrade gateway" button

`SdkVersionPanel` calls `requestGatewayUpgrade()`, which POSTs
`{"version": ...}` to `/admin/sdk/upgrade`, `/admin/upgrade`, then
`/version/upgrade`. The gateway implements none of them, so the button always
ends in the 501 "does not expose an SDK upgrade endpoint" error. That is the
right outcome for an in-place upgrade: an endpoint that pip-installs a version
into the running, publicly reachable service would be remote code execution
on a timer, and 0.14 -> 1.0 is a breaking migration that cannot be applied
without code changes anyway. The panel should read `GET /engine/updates`
instead: show the status, the breaking flag and the summary, and link to the
Renovate PR. See the audit for the exact UI change.

## Audit notes

Release notes for LucidLink packages are often a single line, so a summary
written from them alone says "not stated" for most things that matter. The
summarizer is therefore also given our own audit of each release, from reading
the package's code: `docker/updates/audit.json`, keyed by component and version
(`"*"` for notes that apply to every version), each entry with `notes` (what
changes for this app) and `status` (where the upgrade stands here). The
summary treats the audit as authoritative over the release notes and takes the
status into account in its recommendation.

To add a finding without rebuilding, write the same shape to
`/data/updates-audit.json` (or `UPDATES_AUDIT_FILE`); it is merged over the
packaged file per component and version. Summaries are cached per release
together with a fingerprint of the audit, status and prompt: when those change,
the next check rewrites the summary shown in Engine › Status, without sending a
second alert. Without an LLM, an alert carries the audit text itself.

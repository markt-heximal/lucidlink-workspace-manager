"""Update monitor for the LucidLink components this engine depends on.

Watches PyPI and Docker Hub for new releases of the pieces pinned in this repo
(the `lucidlink` SDK, the `lucidlink-mcp` server, the `lucidlink/lucidlink-api`
image), classifies each release against what is pinned, and alerts the owner
once per release through whichever notifiers are configured (ntfy, a generic
webhook, Resend email). See docs/UPDATES.md.

It only *reports*. Pins change through Renovate PRs that CI has to pass, and
the Mini picks merged changes up with docker/updates/redeploy.sh. Nothing in
here installs anything: the gateway is public via Tailscale Funnel, so an
"upgrade" button that pip-installs into the running service is not on offer.

Wiring (in lucidlink_api.py):

    from updates import router as updates_router, start_scheduler, stop_scheduler
    app.include_router(updates_router, dependencies=[Depends(owner_auth.require_owner)])
    # in the lifespan: start_scheduler() before `yield`, stop_scheduler() after
"""
from .api import router
from .monitor import UpdateMonitor, get_monitor, set_monitor
from .scheduler import start_scheduler, stop_scheduler

__all__ = [
    "router",
    "UpdateMonitor",
    "get_monitor",
    "set_monitor",
    "start_scheduler",
    "stop_scheduler",
]

# Tailnet gateway (v0-gateway on AI-Factory-Mini)

> **This project's place on the gateway:** LucidLink is the **catch-all / root backend**.
> Any path that matches no other prefix, including `/`, routes to
> `lucidlink-fileservice:8000` and is reachable at
> `https://ai-factory-mini.tail333a1d.ts.net/` (Funnel = **public internet**).
> Because it's the catch-all, it sits **outside** the `@noauth` key gate — it answers
> every unclaimed path and relies on **its own auth**. (`lucidlink-api` runs separately
> on `:3003`, published directly rather than behind a gateway prefix.)

This is the shared reference for the single tailnet HTTPS gateway that fronts several
projects (Arista, Nutanix, LucidLink, the cognitum V0/seed fleet). A copy of this file
lives in each consuming repo; **the live Caddyfile on the mini is the source of truth** —
keep these copies in sync with it.

## Where it runs

- **Host:** `ai-factory-mini` (macOS), tailnet `tail333a1d.ts.net` (`100.97.164.63`).
  Reach it from another tailnet node with
  `ssh markt@ai-factory-mini.tail333a1d.ts.net` (key auth; first connect needs
  `-o StrictHostKeyChecking=accept-new`).
- **Gateway:** an OrbStack container **`v0-gateway`** (`caddy:2-alpine`) that publishes
  host `:8088` (container `:80`). Config file on the host:
  `/Users/markt/v0-gateway/Caddyfile`, mounted to `/etc/caddy/Caddyfile`.
- **docker CLI on the mini:** `/Users/markt/.orbstack/bin/docker` (not on the default
  non-interactive `PATH` — use the full path over SSH).
- **Tailscale exposure:**
  - `/` on `:443` is **Funnel = public internet** → `127.0.0.1:8088` (the gateway).
  - `:8443` is **tailnet-only** → `127.0.0.1:9770`.

## Base URL & auth

- **Base:** `https://ai-factory-mini.tail333a1d.ts.net`
- **Auth:** prefixed routes require `X-API-Key: gwk_…`. The **catch-all (LucidLink) does
  not** — it is outside the key gate, so LucidLink's own auth is the only thing protecting
  it on the public Funnel origin. Gateway keys live in the Caddyfile `@noauth` block and
  `gw_keys.json` (chmod 600) — **never commit them**.
- `/healthz` is open (returns `ok`).
- **CORS:** the gateway sets `Access-Control-Allow-Origin: *` when the backend doesn't,
  and answers `OPTIONS` preflight with `204`.
- **Prefix stripping:** prefixed routes use Caddy `handle_path`, which strips the matched
  prefix. The catch-all does not strip anything — LucidLink sees the full path.

## Route table (live)

| Prefix (key-gated) | Backend | What |
|---|---|---|
| `/gw/*` | `host.docker.internal:8089` | gateway control / discovery (`/gw/seeds`, `/gw/pair/<name>`) |
| `/anm/*` | `host.docker.internal:9770` | Arista network manager |
| `/ntx/*` | `host.docker.internal:9780` | Nutanix management API |
| `/v0/<name>/*` | `<tailnet-ip>:9000` | V0 appliances (bearer injected) |
| `/csi/<name>/*` | `<ip>:8090` | CSI vitals (presence / breathing) |
| `/seed/cognitum-<id>/*` | `<ip>:80` | cognitum Pi seeds |
| `/healthz` | — | `ok`, no auth |
| `/` (catch-all) | `host.docker.internal:8000` | **LucidLink file service (this project; public)** |

Full key-gated prefix set (the `@noauth` matcher):
`/gw/* /v0/* /csi/* /seed/* /screen/* /lan/* /anm/* /ntx/*`.

## Adding or changing a route

1. `ssh markt@ai-factory-mini.tail333a1d.ts.net`, edit `/Users/markt/v0-gateway/Caddyfile`.
2. **Back up first** (the dir already keeps timestamped `Caddyfile.bak-*` files).
3. If the route should be key-gated, add its prefix to the `@noauth` `path …` line.
4. Add a block **before** the final catch-all `handle { … }` (which is LucidLink — keep it
   last so it stays the fallback):
   ```
   handle_path /<prefix>/* {
       reverse_proxy host.docker.internal:<port>
   }
   ```
5. **Validate**, then **reload by restart** (see below).

## Reloading — restart, not `caddy reload`

The Caddyfile sets `admin off`, so the admin API on `:2019` is disabled and
`caddy reload` **fails** with `connection refused`. The working sequence:

```bash
DOCKER=/Users/markt/.orbstack/bin/docker
cp /Users/markt/v0-gateway/Caddyfile /Users/markt/v0-gateway/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)
# ...edit the Caddyfile...
$DOCKER exec v0-gateway caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
$DOCKER restart v0-gateway          # ~1-2s fleet-wide blip; loads the validated config
curl -s http://127.0.0.1:8088/healthz   # expect: ok
```

A restart briefly interrupts **every** route on this gateway (all V0s, vitals, seeds,
Arista, Nutanix, LucidLink), so always `validate` before restarting.

## Secrets (never commit)

`gwk_…` gateway keys → `Caddyfile` / `gw_keys.json` / `gw_master_key`. Per-backend bearer
tokens are injected by the gateway.

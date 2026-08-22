# portal/

Password-protected login portal that publishes several local apps through one
Cloudflare tunnel hostname. One Flask app ([portal_app.py](portal_app.py),
`127.0.0.1:8100`) owns the only auth layer; every backend binds localhost and
is reachable exclusively through the portal.

## How it works

The backends (arcade, ca_donors, AI_prep, …) all use root-absolute URLs in
their HTML/JS and are **not modified by anything in this folder** — so they
can't live behind path prefixes like `/arcade/`. Instead the portal proxies
the *entire* path space to the backend of the app the visitor selected on
`/_portal/` (stored in their session cookie). One app active per browser at a
time; portal pages are reserved under `/_portal/`.

```
browser ── https (Cloudflare tunnel) ──> portal :8100 ──┬─> omaha_web.py   :5051  (conda env: omaha)
                                                        ├─> run_arcade.py  :8001  (p312)
                                                        ├─> ca_donors.py   :5056  (p312, ca_donors/)
                                                        ├─> run_aiprep.py  :8006  (p312)
                                                        ├─> uvicorn fridge :8007  (rs313)
                                                        ├─> praxis demo    100.79.128.124:8008  (VM, via Tailscale)
                                                        └─> hab7bot web    100.79.128.124:3000  (VM, via Tailscale)
```

Backends default to `127.0.0.1`; an app entry may set `host` in
[config.py](config.py) to proxy a remote backend (praxis runs on the VM and
brings its own guest login, so it needs no blocklist). The proxy strips
`Origin`/`Referer` from forwarded requests so Django CSRF origin-matching
accepts posts made through the portal's public hostname.

## Accounts & roles

Two roles, decided by `OWNER_USERS` in [config.py](config.py):

- **Owners** — `praxis` and `hab7bot`: the **portal layer** never restricts
  them — no method blocks, no path blocklists, no guest-instance rerouting
  (they reach ca_donors on :5056; guests are rerouted to the --guest
  instance on :5057).
- **Guests** — every other username in `users.json` (e.g. `demo`,
  `praxis_guest`, `hab7_guest`): the guest protections below apply
  automatically.

Portal roles govern only what the portal does. Inside each app, that app's
own login/roles apply to everyone equally — the Praxis tile points at the
:8008 demo instance (guest-only by construction), and Compass gives the
read-only guest role to whoever types "demo" on its login page. A portal
owner gets more inside an app only by logging into the app itself with its
real credentials.

Create/rotate any account: `python set_password.py <username> [password]`
(prints the password once; hashes only in `users.json`). A new name is a
guest unless you add it to `OWNER_USERS`. The Flask session secret lives in
`secret_key` (auto-generated on first run; deliberately **not** in `.env`,
which env_sync.py overwrites).

## Guest protection (guests only — owners bypass all of it)

No backend has a guest mode of its own, so the proxy enforces it:

- DELETE/PUT/PATCH are never forwarded to any app (`GUEST_ALLOWED_METHODS`).
- Per-app path blocklists in [config.py](config.py) — for the donor tool that
  means purge/merge/update/exclude/clear, uploads, LinkedIn flows, downloads,
  and the Anthropic/RocketReach spending endpoints all return 403.

For everyone, owners included:

- Login backoff: 5 failed attempts per username or IP → 60 s lockout.
- Sessions: HttpOnly, SameSite=Lax, Secure (set `PORTAL_COOKIE_SECURE=0` only
  for plain-http local browser debugging), 12 h lifetime.
- LLM calls are metered (tracking only — see below).


## The Omaha app

[omaha_web.py](omaha_web.py) is adapted from
`omaha_rl/PokerRL-Omaha/examples/web_FLO_HiLo.py`, with the rule bots replaced
by the trained Deep CFR agent
(`~/poker_ai_data/benchmarks/deepcfr_hu_step170/eval_agentSINGLE.pkl`, CPU
inference) using the loop from `examples/eval_agent_vs_bots.py`. Stacks reset
to the training baseline every hand (the agent's mirrored env requires it);
the cross-hand score is shown as a session total instead.

## API budget bar + metering

The LLM-calling backends (arcade, AI Prep, fridge — and fof, though its
`/api/ask` is blocked anyway) are launched with
`ANTHROPIC_BASE_URL=http://…:8100/_anthropic`, so their Claude API traffic
passes through [anthropic_meter.py](anthropic_meter.py): each response's real
token usage is priced (per-model table in config.py) and accumulated in
`data/usage.db`. Every page shows a bottom bar with live spend as a
**percentage of `BUDGET_USD`** ($5 = 100%) — injected into proxied backend
HTML by the portal.

- **Soft by default** (`HARD_LIMIT = False`): nothing is ever blocked; past
  100% the bar goes red ("over budget"). Set `HARD_LIMIT = True` for a hard
  429 cutoff of LLM calls.
- **Owner usage is untouched**: only the portal's own backend processes have
  `ANTHROPIC_BASE_URL` set. Telegram/Discord bots, VM services, and CLI work
  hit api.anthropic.com directly, unmetered.
- The meter path is key-gated (requests must present the local
  `ANTHROPIC_API_KEY`), so the public tunnel can't be used to relay.
- Reset the budget: `rm data/usage.db` (also clears the access log).
- Omaha calls no LLM at all.

## Monitoring

Everything proxied through the portal is logged to the `requests` table in
`data/usage.db` (timestamp, user, app, method, path, status); LLM spend is in
the `spend` table (timestamp, model, token counts, cost).

```bash
sqlite3 data/usage.db 'SELECT datetime(ts,"unixepoch","localtime"),user,app,method,path,status FROM requests ORDER BY ts DESC LIMIT 30'
sqlite3 data/usage.db 'SELECT datetime(ts,"unixepoch","localtime"),model,input_tokens,output_tokens,round(cost,4) FROM spend ORDER BY ts DESC LIMIT 30'
```

## Run / deploy

**Docker mode (default).** The five backends run as containers defined in
[compose.yaml](compose.yaml): stock `ubuntu:24.04` (same glibc as the host)
with the existing conda envs bind-mounted read-only at their real paths — no
image builds, no pip. Projects are mounted `:ro` where the app doesn't write;
**the ca_donors DuckDBs (`contact_analysis_data/`) are mounted `:ro`**;
container ports publish on 127.0.0.1 only. One-time prerequisite:

```bash
sudo usermod -aG docker $USER   # then log out/in (or: newgrp docker)
```

Then:

```bash
./install_services.sh   # portal + cloudflared as user units, backends via docker compose
./stop_all.sh           # stops + disables everything, tunnel and containers included
docker compose ps       # backend container status (from this directory)
journalctl --user -u portal -f
```

**Bare mode (tested fallback, no Docker):** `./install_services.sh --bare`
runs each backend as its own systemd user unit on the host instead
(`portal-omaha`, `portal-arcade`, `portal-fof`, `portal-fof-guest`,
`portal-aiprep`, `portal-fridge`). The installer disables whichever mode's
units it's not installing, so the two never fight over ports.

### Restarting — the common mistakes

- **Never `sudo ./install_services.sh`** — these are *user* units; under
  sudo, systemctl can't reach your user session bus and fails with
  `Failed to connect to bus: No medium found`. Run it as yourself.
- Scripts aren't on PATH: it's `./stop_all.sh`, not `stop_all.sh`.
- To restart everything: `./stop_all.sh && ./install_services.sh --bare`
  (or no flag for Docker mode).
- Docker mode says "docker is not usable" until the **one-time** group setup
  is done AND you've re-logged: `sudo usermod -aG docker ai1` (sudo is
  correct for this one command), then log out/in — or `newgrp docker` in the
  current terminal — then `./install_services.sh` with no sudo.
- One service only: `systemctl --user restart portal` (or `portal-<app>`).
- Health check: `systemctl --user list-units 'portal*' 'cloudflared*'` and
  open http://127.0.0.1:8100/_portal/.

## Cloudflare setup (one-time)

The tunnel itself already runs here (`cloudflared-tunnel.service` uses the
`CLOUDFLARED_TUNNEL` token from `~/Documents/.env`; `journalctl --user -u
cloudflared-tunnel` should show "Registered tunnel connection"). What's left
is mapping a public hostname to it:

1. Go to [one.dash.cloudflare.com](https://one.dash.cloudflare.com) →
   **Networks → Tunnels**. Your tunnel should show **HEALTHY** (that's this
   machine's connection).
2. Click the tunnel → **Public Hostname** tab → **Add a public hostname**.
3. Pick a subdomain + your domain (e.g. `portal.yourdomain.com`) — this
   **requires a domain added to your Cloudflare account** (Websites → Add a
   site; free plan is fine; you point your domain's nameservers at
   Cloudflare).
4. Service: type **HTTP**, URL **localhost:8100**. Save.
5. Visit `https://portal.yourdomain.com` — Cloudflare terminates TLS and the
   portal login should appear. Nothing on this machine needs to change or
   restart.

**No domain?** A named tunnel can't get a public URL without one. Fallback is
a *quick tunnel* — random URL, new on every restart:
`cloudflared tunnel --url http://localhost:8100` (prints an
`https://….trycloudflare.com` address; swap the unit's ExecStart to this
command if you want it persistent).

Public entry point: the `cloudflared-tunnel.service` unit runs the tunnel with
the `CLOUDFLARED_TUNNEL` token from `~/Documents/.env`. In the Cloudflare Zero
Trust dashboard (Networks → Tunnels → your tunnel → **Public Hostname**), add a
hostname pointing at `http://localhost:8100`. Without a domain on the account,
fall back to a quick tunnel: `cloudflared tunnel --url http://localhost:8100`.

## Adding an app

1. Make it reachable on a `127.0.0.1` port (a launcher script here if the
   project can't be configured — see [run_arcade.py](run_arcade.py)).
2. Add an entry to `APPS` in [config.py](config.py) (`port`, optional `home`
   path, `blocked` regexes).
3. Add a `systemd/portal-<name>.service` unit and list it in
   `install_services.sh` / `stop_all.sh`.
4. Re-run `./install_services.sh` and restart `portal.service`.

## Testing

Smoke suite: [test_portal.py](test_portal.py) — auth, proxying, blocklists,
image filtering. Runs against the live localhost backends via Flask's test
client (no port binding):

```bash
PORTAL_TEST_USER=praxis PORTAL_TEST_PW=<password> \
  /home/ai1/anaconda3/envs/p312/bin/python test_portal.py
```

24 checks, all passing as of 2026-08-21.

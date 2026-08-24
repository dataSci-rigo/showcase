# Vertical test plan

Walks the whole stack bottom-to-top — processes → auth → each app end-to-end
→ metering/bar → tunnel — so every layer is proven before a guest touches it.
Run the layers in order; each assumes the previous one passed.

Roles: `praxis` / `hab7bot` are **owners** (unrestricted). Any other account
in users.json is a **guest** (restricted). Before starting, create the guest
account you'll demo with:

```bash
cd ~/Documents/portal && python set_password.py demo    # prints its password
```

Do one full pass as an owner and one as the guest — the negative checks
below (403s, filtered search) hold **only for the guest**; the owner
deliberately bypasses them.

---

## Layer 0 — deploy

```bash
cd ~/Documents/portal
./install_services.sh            # Docker mode (needs docker group), or:
# ./install_services.sh --bare   # no-Docker fallback
```

**Expect:** installer output lists `portal`, `cloudflared-tunnel`, and either
`portal-backends` (Docker) or the five `portal-*` units, all `active`.

## Layer 1 — processes & ports

```bash
systemctl --user --no-pager list-units 'portal*' 'cloudflared*'
docker compose ps                # Docker mode only: five containers Up
ss -tlnp | grep -E ':(8100|5051|8001|5056|8006|8007)\b'
```

**Expect:** all six ports listening, every one bound to `127.0.0.1` — none on
`0.0.0.0`. If a backend is missing, check `journalctl --user -u portal-backends -f`
(Docker) or `-u portal-<app> -f` (bare). The omaha backend takes up to a couple
of minutes on first start (loading the 152 MB agent).

## Layer 2 — auth wall

In a browser, open `http://127.0.0.1:8100/_portal/`:

1. You are redirected to the login page — no content visible without a session.
2. Try `praxis` with a wrong password → "Wrong username or password."
3. Enter a wrong password 5 times fast → "Too many attempts" lockout; wait
   ~60 s and it clears.
4. Log in as `praxis` → the home grid shows 5 tiles and the API-budget bar
   sits at the bottom.
5. Also confirm from a shell that raw paths are walled:

```bash
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://127.0.0.1:8100/some/page
# expect: 302 .../_portal/login
```

The curl-based negative checks in Layer 3 must run as the **guest** (owners
bypass the blocks by design). Grab a guest session cookie once (the cookie
is flagged Secure, so use it via an explicit header, not a cookie jar):

```bash
COOKIE=$(curl -si -X POST -d 'username=demo&password=PASTE_GUEST_PW' \
  http://127.0.0.1:8100/_portal/login | grep -oP 'session=[^;]+')
echo "$COOKIE"    # must print session=...
```

## Layer 3 — one vertical pass per app

Select each app from the home grid; after each app, return via
`/_portal/` in the address bar.

### 3a. Omaha (no LLM)

1. Tile → table loads, "vs AI" → Start Game.
2. Play 2–3 full hands: fold one, call/raise through showdown on another.
   AI responds in under a second; board/pot/stacks update; "Hand over" panel
   shows the session score; Next Hand deals again.
3. Stacks reset to baseline each hand — the session score line is what carries.

### 3b. Arcade (LLM: letter recognition, vocab)

1. Tile → launcher grid loads with game art.
2. Open a sound game (e.g. sound speller) → audio plays (`/api/tts` works).
3. Open letter-draw, draw a letter, submit → it gets recognized (this is a
   real Haiku vision call — the budget bar should tick up within a minute).
4. Guest-mode negative check (DELETE is never forwarded for guests; the
   guest cookie must have arcade selected — open the tile as the guest
   first, or run `curl -H "Cookie: $COOKIE" http://127.0.0.1:8100/_portal/open/arcade`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
  -H "Cookie: $COOKIE" http://127.0.0.1:8100/api/custom-sets/1
# expect: 403   — repeat logged in as praxis and it is NOT 403 (owner bypass)
```

### 3c. Donor Research (read-only)

1. Tile → `/v2` page loads; run a donor search; open a candidate page.
2. Blocked-route checks (guest cookie, fof selected):

```bash
curl -s -o /dev/null -H "Cookie: $COOKIE" http://127.0.0.1:8100/_portal/open/fof
for p in /api/donor/purge /api/ask /api/context/upload; do
  curl -s -o /dev/null -w "$p %{http_code}\n" -X POST -H "Cookie: $COOKIE" \
    http://127.0.0.1:8100$p; done
# expect: 403 for all three (as praxis they forward — owner bypass)
```

3. Docker mode hard guarantee: the DuckDBs are mounted `:ro` —
   `docker inspect portal-backends-fof-1 | grep -A3 contact_analysis` shows `"RO": true`.

### 3d. AI Prep (LLM: review/quizzes)

1. Tile → dashboard loads.
2. Start a quiz, answer a question; if you use LeetCode Live, submitting
   pseudocode for review is a real Sonnet call — bar ticks up.

### 3e. Fridge Recipes (LLM: extraction/substitutions)

1. Tile → lands on `/demo` and redirects to the demo setup (never a Basic-Auth
   prompt — that would mean it landed on `/`, the personal mode).
2. Build a demo fridge, ask for recipe matches.
3. Personal mode stays locked: browse to `/` manually → the app's own
   Basic-Auth challenge appears; cancel it.

## Layer 4 — metering & the bar

1. After 3b/3d/3e the bar shows a non-zero percentage like `1%` (of the $5
   budget; updates ~once a minute; reload to force it).
2. Ledger sanity:

```bash
sqlite3 ~/Documents/portal/data/usage.db \
  'SELECT datetime(ts,"unixepoch","localtime"),model,input_tokens,output_tokens,round(cost,4) FROM spend ORDER BY ts DESC LIMIT 10'
sqlite3 ~/Documents/portal/data/usage.db \
  'SELECT datetime(ts,"unixepoch","localtime"),user,app,method,path,status FROM requests ORDER BY ts DESC LIMIT 20'
```

**Expect:** spend rows matching the models you triggered
(`claude-haiku-4-5…` for letter-draw, `claude-sonnet-4-6` for review/
extraction), and request rows tagged with the right user + app.

3. Soft-limit confirmation: `HARD_LIMIT = False` in config.py — nothing
   blocks past 100%; the bar just turns red ("over budget"). Reset anytime:
   `rm ~/Documents/portal/data/usage.db` (bar returns to 0%).

## Layer 5 — the tunnel (true guest view)

1. Map the public hostname in Cloudflare Zero Trust → Networks → Tunnels →
   your tunnel → Public Hostname → service `http://localhost:8100`
   (or quick tunnel: `cloudflared tunnel --url http://localhost:8100`).
2. **On your phone, Wi-Fi off (cellular)**: open the public URL.
3. Repeat Layer 2 steps 1–4 and one app from Layer 3 (omaha is the best
   demo) exactly as a guest would — HTTPS padlock, login, bar at the bottom.
4. Log in as the **guest** on the phone while `praxis` is logged in on the
   desktop — both work independently (separate sessions, each with their own
   selected app), and the phone session hits the guest 403s while the
   desktop doesn't.

## Automated suites (any time)

```bash
cd ~/Documents/portal
PORTAL_TEST_USER=praxis PORTAL_TEST_PW=<pw> \
  /home/ai1/anaconda3/envs/p312/bin/python test_portal.py    # owner + guest roles — needs backends running
env $(grep '^ANTHROPIC_API_KEY=' ~/Documents/.env) \
  /home/ai1/anaconda3/envs/p312/bin/python test_meter.py     # 12 checks — mocked upstream, spends nothing
```

(test_portal.py injects its guest account in memory — it never edits
users.json.)

## Teardown

```bash
./stop_all.sh    # stops portal, tunnel, and containers/units
```

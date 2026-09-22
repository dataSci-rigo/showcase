"""Smoke tests for the portal via Flask test_client (backends must be running).

Covers both roles: owners (praxis/hab7bot, unrestricted) and guests (every
other account — restricted). The guest account is injected in-memory; the
real users.json is never touched.

Run:  PORTAL_TEST_USER=praxis PORTAL_TEST_PW=<password> \
      /home/ai1/anaconda3/envs/p312/bin/python test_portal.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from werkzeug.security import generate_password_hash

import config
import portal_app

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    ok += cond
    fail += (not cond)
    print(("PASS" if cond else "FAIL"), name, extra)


TEST_USER = os.environ.get("PORTAL_TEST_USER", "praxis")
TEST_PW = os.environ.get("PORTAL_TEST_PW")
if not TEST_PW:
    sys.exit("Set PORTAL_TEST_USER / PORTAL_TEST_PW to a valid owner account first.")
assert TEST_USER in config.OWNER_USERS, "PORTAL_TEST_USER must be an owner account"

# inject an in-memory guest account
GUEST_PW = "guest-test-pw"
_real_load = portal_app.load_users
portal_app.load_users = lambda: {**_real_load(),
                                 "testguest": generate_password_hash(GUEST_PW)}

# ---------------------------------------------------------------- unauthenticated
c = portal_app.app.test_client()
r = c.get("/")
check("unauth / -> login redirect", r.status_code == 302 and "/_portal/login" in r.location)
r = c.get("/_portal/api/usage")
check("unauth usage api -> 401", r.status_code == 401)
r = c.post("/_portal/login", data={"username": TEST_USER, "password": "nope"})
check("wrong password rejected", b"Wrong username" in r.data)

# ---------------------------------------------------------------- owner session
r = c.post("/_portal/login", data={"username": TEST_USER, "password": TEST_PW})
check("owner login -> home", r.status_code == 302 and "/_portal/" in r.location)
r = c.get("/_portal/")
check("home lists apps", b"Omaha" in r.data and b"Arcade" in r.data and b"AI Prep" in r.data
      and b"Fridge" in r.data and b"Donor" in r.data)
check("no image search tile", b"Image Search" not in r.data)

r = c.get("/some/path")
check("no app selected -> home redirect", r.status_code == 302)

c.get("/_portal/open/omaha")
r = c.get("/")
check("omaha proxied HTML", r.status_code == 200 and b"FL OMAHA HI/LO" in r.data)
check("budget bar injected", b"pbudget" in r.data)

c.get("/_portal/open/arcade")
r = c.get("/")
check("arcade proxied", r.status_code == 200)
r = c.delete("/api/custom-sets/999999")
check("owner DELETE forwarded (not 403)", r.status_code != 403, f"got {r.status_code}")

c.get("/_portal/open/aiprep")
r = c.get("/")
check("aiprep proxied", r.status_code == 200)

c.get("/_portal/open/praxis")
r = c.get("/", follow_redirects=True)
check("praxis (VM :8008) proxied", r.status_code == 200, f"got {r.status_code}")

r = c.get("/_portal/open/fridge", follow_redirects=False)
check("fridge opens on /demo", r.location == "/demo")
r = c.get("/demo", follow_redirects=False)
check("fridge proxied", r.status_code in (200, 302, 307), f"got {r.status_code}")

c.get("/_portal/open/fof")
r = c.get("/v2")
check("fof /v2 proxied", r.status_code == 200)
r = c.get("/download/linkedin_local.py")
check("owner bypasses fof blocklist", r.status_code == 200, f"got {r.status_code}")

r = c.get("/_portal/logout")
r = c.get("/")
check("after logout -> login redirect", r.status_code == 302)

# ---------------------------------------------------------------- guest session
g = portal_app.app.test_client()
r = g.post("/_portal/login", data={"username": "testguest", "password": GUEST_PW})
check("guest login -> home", r.status_code == 302)

g.get("/_portal/open/arcade")
r = g.get("/")
check("guest: arcade works", r.status_code == 200)
r = g.delete("/api/custom-sets/1")
check("guest: DELETE blocked", r.status_code == 403)

g.get("/_portal/open/fof")
r = g.get("/v2")
check("guest: fof read works", r.status_code == 200)
for p in ("/api/donor/purge", "/api/context/upload"):
    r = g.post(p, json={})
    check(f"guest: {p} blocked", r.status_code == 403)
# metered lookups are allowed through to the --guest instance (empty body ->
# app-level 4xx/5xx, but NOT the portal's 403)
r = g.post("/api/ask", json={})
check("guest: /api/ask reaches guest instance (metered, not portal-blocked)",
      r.status_code != 403, f"got {r.status_code}")
r = g.get("/download/linkedin_local.py")
check("guest: /download blocked", r.status_code == 403)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)

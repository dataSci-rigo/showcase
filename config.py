"""Portal configuration: app registry, ports, blocklists, image allowlist."""
import os
import re

PORTAL_HOST = "127.0.0.1"
PORTAL_PORT = 8100

BASE = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE, "users.json")

# Session cookie: Secure by default (public access is via the Cloudflare
# tunnel, which is always HTTPS). Set PORTAL_COOKIE_SECURE=0 only for
# plain-http local debugging in a browser.
COOKIE_SECURE = os.environ.get("PORTAL_COOKIE_SECURE", "1") == "1"
SESSION_HOURS = 12

# Login backoff: after MAX_FAILS consecutive failures for a username or
# source IP, reject logins for LOCK_SECONDS.
MAX_FAILS = 5
LOCK_SECONDS = 60

# ---------------------------------------------------------------------------
# Roles. OWNER_USERS get full, unrestricted access to every proxied app.
# Every other username in users.json is a GUEST: destructive HTTP methods are
# never forwarded and the app blocklists below apply. Add a guest with:
#   python set_password.py <name>
# ---------------------------------------------------------------------------
OWNER_USERS = {"praxis", "hab7bot"}

# ---------------------------------------------------------------------------
# Proxied apps. The portal forwards every non-/_portal/ path to the backend
# of the app currently selected in the visitor's session.
# "blocked" entries are regexes matched against the request path; matches 403
# for guests (owners bypass them).
#
# Guest mode: none of the backends has a guest flag of its own, so protection
# is enforced here — destructive HTTP methods are never forwarded for guests,
# and app-specific dangerous routes are path-blocked below.
# ---------------------------------------------------------------------------
GUEST_ALLOWED_METHODS = {"GET", "HEAD", "POST", "OPTIONS"}  # no DELETE/PUT/PATCH
APPS = {
    "omaha": {
        "title": "Omaha Poker vs AI",
        "desc": "Fixed-Limit Omaha Hi/Lo heads-up against the Deep CFR agent.",
        "port": 5051,
        "blocked": [],
    },
    "arcade": {
        "title": "Arcade",
        "desc": "Kids' learning games — math, spelling, letters, memory.",
        "port": 8001,
        "blocked": [],
    },
    "aiprep": {
        "title": "AI Prep",
        "desc": "Interview practice — quizzes, LeetCode Live, pseudocode review.",
        "port": 8006,
        "blocked": [],
    },
    "hab7bot": {
        "title": "Compass",
        "desc": "Weekly planner demo — type “demo” in the password box for the read-only tour.",
        "host": "100.79.128.124",  # VM web app, over Tailscale
        "port": 3000,
        "blocked": [],  # read-only guest role is enforced inside the app
        # The built Next.js frontend hardcodes the API origin, which is
        # Tailscale-private and unreachable from visitors' browsers. The
        # portal absorbs it: API paths route to the API server, and the
        # absolute origin is rewritten out of served JS/HTML so browser
        # calls become same-origin (through the portal).
        "routes": [(r"^/api/", 8010)],
        "rewrite": {"http://100.79.128.124:8010": ""},
    },
    "praxis": {
        "title": "Praxis",
        "desc": "Praxis demo — has its own guest login (credentials on its login page).",
        "host": "100.79.128.124",  # VM demo instance, over Tailscale
        "port": 8008,
        "blocked": [],
    },
    "fridge": {
        "title": "Fridge Recipes",
        "desc": "What can I cook? Demo fridge + recipe matching.",
        "port": 8007,
        "home": "/demo",  # "/" is personal mode behind the app's own Basic Auth
        "blocked": [],
    },
    "fof": {
        "title": "Donor Research",
        "desc": "CA campaign-donations research tool (guests: read-only + metered lookups).",
        "port": 5056,
        # Guests are proxied to a separate `ca_donors.py --guest` instance,
        # which blocks donor-data mutations itself and meters the external
        # lookups (RocketReach / LinkedIn / DuckDuckGo / Claude) against
        # global 100-use counters (ca_donors_cache/guest_usage.json).
        "guest_port": 5057,
        # Belt-and-braces on top of the guest instance's own guard: still
        # block the destructive/credential paths at the portal, but let the
        # metered lookup routes through.
        "blocked": [
            r"^/api/donor/purge",
            r"^/api/donor/merge",
            r"^/api/donor/update",
            r"^/api/donor/rows/exclude",
            r"^/api/donor/variant/remove",
            r"^/api/donor/[^/]+/clear",
            r"^/api/context/upload",
            r"^/api/linkedin/(launch_vnc|vnc_|save_cookies)",  # credential/VNC flows
            r"^/download/",
        ],
    },
}

for _app in APPS.values():
    _app["blocked_re"] = [re.compile(p) for p in _app["blocked"]]
    _app["routes_re"] = [(re.compile(p), port) for p, port in _app.get("routes", [])]

# ---------------------------------------------------------------------------
# Anthropic API budget. Backends that call Claude are pointed at the portal's
# metering proxy via ANTHROPIC_BASE_URL (set in their systemd units); every
# request's real token usage is priced and accumulated in USAGE_DB and shown
# in the bottom bar against BUDGET_USD.
#
# HARD_LIMIT=False (default): tracking only — apps stay fully functional past
# the budget, the bar just goes red. HARD_LIMIT=True: past-budget LLM calls
# get a 429 and AI features pause until the budget is reset (delete
# data/usage.db).
# ---------------------------------------------------------------------------
BUDGET_USD = 5.00
HARD_LIMIT = False
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
USAGE_DB = os.path.join(DATA_DIR, "usage.db")

# $ per 1M tokens (input, output); cache writes bill 1.25x input,
# cache reads 0.1x input. Matched by model-id prefix.
MODEL_PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
    "_default": (5.00, 25.00),  # unknown models: assume Opus rates
}

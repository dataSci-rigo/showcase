"""
Login portal + single-hostname reverse proxy.

One Flask app, bound to 127.0.0.1:8100 and published through the Cloudflare
tunnel. Owns the only auth layer: session-cookie login checked against
users.json (werkzeug password hashes). Portal pages live under /_portal/;
every other path is forwarded verbatim to the backend of the app the session
has selected. The backends all use root-absolute URLs in their HTML/JS and
must not be modified, which is why the proxy claims the whole path space and
routes by session instead of by path prefix — one active app per browser.

Run:  conda run -n p312 python portal_app.py
"""
import json
import os
import threading
import time

import requests
from dotenv import load_dotenv
from flask import (Flask, Response, abort, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

import anthropic_meter
import config

load_dotenv(os.path.join(config.BASE, ".env"))
# master .env fallback: the meter needs ANTHROPIC_API_KEY to authenticate
# backend requests; portal/.env normally mirrors it via env_sync anyway
load_dotenv(os.path.expanduser("~/Documents/.env"))


def _secret_key():
    # Kept in its own file (not .env): env_sync.py overwrites portal/.env with
    # the master env file, and the key must survive that + restarts so
    # sessions stay valid.
    path = os.path.join(config.BASE, "secret_key")
    if not os.path.exists(path):
        import secrets
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = _secret_key()
app.register_blueprint(anthropic_meter.bp)

with open(os.path.join(config.BASE, "templates", "usage_bar.html")) as _f:
    _USAGE_BAR = _f.read()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=config.SESSION_HOURS * 3600,
)


def load_users():
    with open(config.USERS_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Login backoff — in-memory, keyed by username and by client IP.
# ---------------------------------------------------------------------------
_fails = {}
_fails_lock = threading.Lock()


def _locked(key):
    with _fails_lock:
        n, t = _fails.get(key, (0, 0))
        return n >= config.MAX_FAILS and (time.time() - t) < config.LOCK_SECONDS


def _record_fail(key):
    with _fails_lock:
        n, _ = _fails.get(key, (0, 0))
        _fails[key] = (n + 1, time.time())


def _clear_fails(key):
    with _fails_lock:
        _fails.pop(key, None)


def _client_ip():
    # Cloudflare puts the real visitor IP here; direct localhost hits fall
    # back to remote_addr.
    return request.headers.get("Cf-Connecting-Ip", request.remote_addr or "?")


def logged_in():
    return "user" in session


def is_guest():
    return session.get("user") not in config.OWNER_USERS


# ---------------------------------------------------------------------------
# Portal pages
# ---------------------------------------------------------------------------
@app.route("/_portal/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        ip = _client_ip()
        if _locked(username) or _locked(ip):
            error = "Too many attempts. Wait a minute and try again."
        else:
            users = load_users()
            if username in users and check_password_hash(users[username], password):
                _clear_fails(username)
                _clear_fails(ip)
                session.clear()
                session.permanent = True
                session["user"] = username
                return redirect(url_for("home"))
            _record_fail(username)
            _record_fail(ip)
            error = "Wrong username or password."
    return render_template("login.html", error=error)


@app.route("/_portal/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/_portal/")
def home():
    if not logged_in():
        return redirect(url_for("login"))
    return render_template("home.html", apps=config.APPS, user=session["user"],
                           active=session.get("app"))


@app.route("/_portal/open/<app_key>")
def open_app(app_key):
    if not logged_in():
        return redirect(url_for("login"))
    if app_key not in config.APPS:
        abort(404)
    session["app"] = app_key
    return redirect(config.APPS[app_key].get("home", "/"))


@app.route("/_portal/about")
def about():
    if not logged_in():
        return redirect(url_for("login"))
    return render_template("about.html")


@app.route("/_portal/api/usage")
def api_usage():
    if not logged_in():
        abort(401)
    return {"spent": round(anthropic_meter.total_spent(), 4),
            "budget": config.BUDGET_USD,
            "hard_limit": config.HARD_LIMIT}


# ---------------------------------------------------------------------------
# Catch-all reverse proxy for the selected app.
# ---------------------------------------------------------------------------
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailers", "transfer-encoding",
               "upgrade", "content-length", "content-encoding"}

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@app.route("/", defaults={"path": ""}, methods=_METHODS)
@app.route("/<path:path>", methods=_METHODS)
def proxy(path):
    if not logged_in():
        return redirect(url_for("login"))
    app_key = session.get("app")
    if app_key not in config.APPS:
        return redirect(url_for("home"))
    backend = config.APPS[app_key]

    req_path = "/" + path
    if is_guest():
        if request.method not in config.GUEST_ALLOWED_METHODS:
            abort(403)
        for pat in backend["blocked_re"]:
            if pat.search(req_path):
                abort(403)

    backend_host = backend.get("host", "127.0.0.1")
    backend_port = backend["port"]
    if is_guest() and "guest_port" in backend:
        # dedicated guest instance (e.g. ca_donors --guest: read-only +
        # metered external lookups)
        backend_port = backend["guest_port"]
    for pat, route_port in backend["routes_re"]:
        # path-based port override (e.g. Compass: /api/* -> the API server)
        if pat.search(req_path):
            backend_port = route_port
            break
    url = f"http://{backend_host}:{backend_port}{req_path}"
    # Forward headers, minus hop-by-hop and Host (requests sets it), and minus
    # Origin/Referer — they carry the portal's public origin, which backends
    # doing CSRF origin-matching (Django: praxis) would reject. The portal's
    # own session cookie is stripped; backend cookies (fridge demo session,
    # praxis csrftoken/sessionid) pass through.
    fwd_headers = {k: v for k, v in request.headers
                   if k.lower() not in ("host", "cookie", "origin", "referer") and
                   k.lower() not in _HOP_BY_HOP}
    backend_cookies = "; ".join(
        f"{name}={value}" for name, value in request.cookies.items()
        if name != app.config.get("SESSION_COOKIE_NAME", "session"))
    if backend_cookies:
        fwd_headers["Cookie"] = backend_cookies
    try:
        r = requests.request(
            method=request.method, url=url,
            params=request.args.to_dict(flat=False),
            headers=fwd_headers, data=request.get_data(),
            stream=True, allow_redirects=False,
            timeout=(10, 300),  # long read timeout: ca_donors streams SSE
        )
    except requests.RequestException:
        return (f"<h3>{backend['title']} backend is not running.</h3>"
                f'<p><a href="/_portal/">Back to portal</a></p>', 502)

    backend_origin = f"http://127.0.0.1:{backend_port}"
    resp_headers = []
    for k, v in r.raw.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        if k.lower() == "location" and v.startswith(backend_origin):
            # backends emit absolute self-redirects; keep the browser on the
            # portal's origin
            v = v[len(backend_origin):] or "/"
        resp_headers.append((k, v))

    try:
        anthropic_meter.log_request(session.get("user", "?"), app_key,
                                    request.method, req_path, r.status_code)
    except Exception:
        pass

    # Buffer-and-transform text responses; stream everything else.
    # - HTML gets the API-budget bar injected.
    # - Apps with a "rewrite" map (Compass) get hardcoded private origins
    #   stripped from HTML/JS/CSS so browser API calls stay same-origin.
    ctype = r.headers.get("Content-Type", "")
    is_html = "text/html" in ctype
    needs_rewrite = bool(backend.get("rewrite")) and any(
        t in ctype for t in ("text/html", "javascript", "text/css", "application/json"))
    if r.status_code == 200 and (is_html or needs_rewrite):
        body = r.content
        for old, new in (backend.get("rewrite") or {}).items():
            body = body.replace(old.encode(), new.encode())
        if is_html:
            idx = body.rfind(b"</body>")
            if idx != -1:
                body = body[:idx] + _USAGE_BAR.encode() + body[idx:]
        return Response(body, status=r.status_code, headers=resp_headers)

    return Response(r.iter_content(chunk_size=65536), status=r.status_code,
                    headers=resp_headers)


if __name__ == "__main__":
    app.run(host=config.PORTAL_HOST, port=config.PORTAL_PORT,
            debug=False, threaded=True)

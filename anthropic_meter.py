"""
Anthropic API metering proxy + request accounting for the portal.

Backends that call Claude are launched (by their systemd units) with
  ANTHROPIC_BASE_URL=http://127.0.0.1:8100/_anthropic
so their SDK traffic lands on this blueprint, which forwards it to
api.anthropic.com, reads the real token usage off each response, prices it,
and accumulates the total in data/usage.db. Once BUDGET_USD is spent, further
calls get an Anthropic-shaped 429 and the apps' AI features stop until the
budget is reset (delete data/usage.db or its rows).

Only requests carrying the real local ANTHROPIC_API_KEY are forwarded — the
tunnel exposes this path publicly, but outsiders don't hold the key, so they
can't relay through it.

Nothing outside the portal's backend units sets ANTHROPIC_BASE_URL, so the
owner's bots, VM services, and CLI usage are neither metered nor limited.
"""
import hmac
import json
import os
import sqlite3
import threading
import time

import requests
from flask import Blueprint, Response, jsonify, request

import config

bp = Blueprint("anthropic_meter", __name__)

UPSTREAM = "https://api.anthropic.com"

_db_lock = threading.Lock()

_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailers", "transfer-encoding",
               "upgrade", "content-length", "content-encoding"}
_FWD_REQ_HEADERS = {"x-api-key", "authorization", "anthropic-version",
                    "anthropic-beta", "content-type", "accept"}


def _db():
    con = sqlite3.connect(config.USAGE_DB, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS spend(
        ts REAL, model TEXT, input_tokens INT, output_tokens INT,
        cache_write INT, cache_read INT, cost REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS requests(
        ts REAL, user TEXT, app TEXT, method TEXT, path TEXT, status INT)""")
    return con


def total_spent() -> float:
    with _db_lock:
        con = _db()
        try:
            row = con.execute("SELECT COALESCE(SUM(cost), 0) FROM spend").fetchone()
        finally:
            con.close()
    return float(row[0])


def log_request(user, app_key, method, path, status):
    """Access log for everything proxied through the portal."""
    with _db_lock:
        con = _db()
        try:
            con.execute("INSERT INTO requests VALUES (?,?,?,?,?,?)",
                        (time.time(), user, app_key, method, path, status))
            con.commit()
        finally:
            con.close()


def _price(model: str):
    for prefix, p in config.MODEL_PRICES.items():
        if prefix != "_default" and model.startswith(prefix):
            return p
    return config.MODEL_PRICES["_default"]


def _record(model: str, usage: dict):
    i = usage.get("input_tokens") or 0
    o = usage.get("output_tokens") or 0
    cw = usage.get("cache_creation_input_tokens") or 0
    cr = usage.get("cache_read_input_tokens") or 0
    p_in, p_out = _price(model or "")
    cost = (i * p_in + o * p_out + cw * p_in * 1.25 + cr * p_in * 0.1) / 1e6
    with _db_lock:
        con = _db()
        try:
            con.execute("INSERT INTO spend VALUES (?,?,?,?,?,?,?)",
                        (time.time(), model, i, o, cw, cr, cost))
            con.commit()
        finally:
            con.close()


def _harvest_sse(text: str):
    """Pull model + final usage out of a buffered SSE stream body."""
    model, usage = "", {}
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        if ev.get("type") == "message_start":
            msg = ev.get("message") or {}
            model = msg.get("model") or model
            usage.update(msg.get("usage") or {})
        elif ev.get("type") == "message_delta":
            usage.update(ev.get("usage") or {})
    if usage:
        _record(model, usage)


def _anthropic_error(status, err_type, message):
    return jsonify({"type": "error",
                    "error": {"type": err_type, "message": message}}), status


@bp.route("/_anthropic/<path:path>", methods=["GET", "POST"])
def anthropic_proxy(path):
    supplied = request.headers.get("x-api-key") or ""
    if not supplied:
        auth = request.headers.get("Authorization") or ""
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
    local = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not local or not supplied or not hmac.compare_digest(supplied, local):
        return _anthropic_error(401, "authentication_error",
                                "This metering proxy only forwards requests "
                                "from the portal's own backends.")

    if config.HARD_LIMIT and total_spent() >= config.BUDGET_USD:
        return _anthropic_error(429, "rate_limit_error",
                                f"Portal demo budget (${config.BUDGET_USD:.2f}) "
                                "is exhausted.")

    fwd_headers = {k: v for k, v in request.headers
                   if k.lower() in _FWD_REQ_HEADERS}
    try:
        r = requests.request(request.method, f"{UPSTREAM}/{path}",
                             headers=fwd_headers, data=request.get_data(),
                             stream=True, timeout=(10, 600))
    except requests.RequestException:
        return _anthropic_error(502, "api_error", "Upstream unreachable.")

    resp_headers = [(k, v) for k, v in r.raw.headers.items()
                    if k.lower() not in _HOP_BY_HOP]
    ctype = r.headers.get("Content-Type", "")

    if "text/event-stream" in ctype:
        def relay():
            chunks = []
            try:
                for chunk in r.iter_content(chunk_size=8192):
                    chunks.append(chunk)
                    yield chunk
            finally:
                try:
                    _harvest_sse(b"".join(chunks).decode("utf-8", "replace"))
                except Exception:
                    pass
        return Response(relay(), status=r.status_code, headers=resp_headers)

    body = r.content
    if r.status_code == 200:
        try:
            j = json.loads(body)
            _record(j.get("model", ""), j.get("usage") or {})
        except Exception:
            pass
    return Response(body, status=r.status_code, headers=resp_headers)

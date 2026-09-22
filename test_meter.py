"""Meter + bar tests with a mocked Anthropic upstream and a stub backend."""
import http.server
import json
import os
import sys
import threading

sys.path.insert(0, "/home/ai1/Documents/portal")
import config

config.USAGE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_test.db")
if os.path.exists(config.USAGE_DB):
    os.remove(config.USAGE_DB)

import anthropic_meter
import portal_app

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    ok += cond
    fail += (not cond)
    print(("PASS" if cond else "FAIL"), name, extra)


KEY = os.environ["ANTHROPIC_API_KEY"]
c = portal_app.app.test_client()

# ---- auth gate ----
r = c.post("/_anthropic/v1/messages", json={})
check("meter: no key -> 401", r.status_code == 401)
r = c.post("/_anthropic/v1/messages", json={}, headers={"x-api-key": "wrong"})
check("meter: wrong key -> 401", r.status_code == 401)


# ---- mocked upstream: non-streaming ----
class FakeRaw:
    headers = {"Content-Type": "application/json"}


class FakeResp:
    status_code = 200
    raw = FakeRaw()
    headers = {"Content-Type": "application/json"}
    content = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "usage": {"input_tokens": 1000, "output_tokens": 500},
        "content": [{"type": "text", "text": "hi"}],
    }).encode()

    def iter_content(self, chunk_size=None):
        yield self.content


real_request = anthropic_meter.requests.request
anthropic_meter.requests.request = lambda *a, **k: FakeResp()

r = c.post("/_anthropic/v1/messages", json={"model": "claude-haiku-4-5-20251001"},
           headers={"x-api-key": KEY})
check("meter: forwarded 200", r.status_code == 200)
spent = anthropic_meter.total_spent()
# 1000 in * $1/M + 500 out * $5/M = 0.001 + 0.0025 = $0.0035
check("meter: cost math", abs(spent - 0.0035) < 1e-9, f"spent={spent}")


# ---- mocked upstream: SSE stream ----
class FakeSSE(FakeResp):
    headers = {"Content-Type": "text/event-stream"}
    raw = type("R", (), {"headers": {"Content-Type": "text/event-stream"}})()
    content = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"model":"claude-sonnet-4-6",'
        b'"usage":{"input_tokens":2000,"output_tokens":1}}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":1000}}\n\n'
    )

    def iter_content(self, chunk_size=None):
        yield self.content


anthropic_meter.requests.request = lambda *a, **k: FakeSSE()
r = c.post("/_anthropic/v1/messages", json={"stream": True}, headers={"x-api-key": KEY})
_ = r.data  # drain the stream so the finally-block records usage
# sonnet: 2000*$3/M + 1000*$15/M = 0.006 + 0.015 = 0.021; total 0.0245
spent = anthropic_meter.total_spent()
check("meter: SSE usage harvested", abs(spent - 0.0245) < 1e-9, f"spent={spent}")

# ---- hard limit off: still forwards past budget ----
config.BUDGET_USD = 0.001
anthropic_meter.requests.request = lambda *a, **k: FakeResp()
r = c.post("/_anthropic/v1/messages", json={}, headers={"x-api-key": KEY})
check("meter: soft limit never blocks", r.status_code == 200)
config.HARD_LIMIT = True
r = c.post("/_anthropic/v1/messages", json={}, headers={"x-api-key": KEY})
check("meter: hard limit blocks with 429", r.status_code == 429
      and b"rate_limit_error" in r.data)
config.HARD_LIMIT = False
config.BUDGET_USD = 5.0
anthropic_meter.requests.request = real_request

# ---- usage endpoint + bar on portal pages ----
PW = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pw.json")))
r = c.get("/_portal/api/usage")
check("usage api: unauth 401", r.status_code == 401)
c.post("/_portal/login", data={"username": "praxis", "password": PW["praxis"]})
r = c.get("/_portal/api/usage")
d = r.get_json()
check("usage api: spent/budget", r.status_code == 200 and d["budget"] == 5.0
      and d["hard_limit"] is False and d["spent"] > 0, str(d))
r = c.get("/_portal/")
check("bar on portal home", b"pbudget" in r.data)


# ---- bar injected into proxied backend HTML ----
class StubHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body><h1>stub app</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 5051), StubHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

c.get("/_portal/open/omaha")
r = c.get("/")
check("bar injected into proxied HTML", b"pbudget" in r.data and b"stub app" in r.data)
srv.shutdown()

# ---- access log recorded ----
import sqlite3
con = sqlite3.connect(config.USAGE_DB)
n = con.execute("SELECT COUNT(*) FROM requests WHERE user='praxis' AND app='omaha'").fetchone()[0]
con.close()
check("access log rows", n >= 1, f"rows={n}")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)

"""
Fixed-Limit Omaha Hi/Lo (8-or-better) browser backend for the portal --
heads-up against the trained Deep CFR EvalAgent.

Adapted from omaha_rl/PokerRL-Omaha/examples/web_FLO_HiLo.py (UI + Flask
surface kept intact) with the rule bots replaced by the EvalAgent pattern
proven in examples/eval_agent_vs_bots.py play_match(): the game env is built
from the agent's own env_bldr so the agent's internal mirrored env stays in
lockstep -- reset(deck_state_dict=...) on every deal, notify_of_action() for
every human action, get_action(step_env=True) for the agent's own moves.
Stacks reset to the training baseline every hand (the mirrored env demands
it); the session score is carried across hands in Python instead.

Binds 127.0.0.1 only -- the portal is the sole client and the auth layer.

Run:  conda run -n omaha python omaha_web.py
"""
import os
import sys
import threading
import time
import uuid

os.environ["OMP_NUM_THREADS"] = "1"

_REPO_ROOT = os.path.expanduser("~/Documents/omaha_rl/PokerRL-Omaha")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from flask import (Flask, jsonify, make_response, render_template_string,  # noqa: E402
                   request)

from PokerRL.game.Poker import Poker  # noqa: E402

# 127.0.0.1 when run bare on the host; the Docker deploy sets BIND_HOST=0.0.0.0
# inside the container (the published port stays 127.0.0.1-only either way)
HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = 5051
MODEL_PATH = os.path.expanduser(
    "~/poker_ai_data/benchmarks/deepcfr_hu_step170/eval_agentSINGLE.pkl")

HUMAN_SEAT = 0
BOT_SEAT = 1

RANKS = "23456789TJQKA"
SUITS = "hdsc"


def card_str(c):
    return RANKS[c[0]] + SUITS[c[1]]


def cards_str(cards_2d):
    return "".join(card_str(c) for c in cards_2d if c[0] != Poker.CARD_NOT_DEALT_TOKEN_1D)


def load_eval_agent(path):
    """Same profile-type dispatch as examples/eval_agent_vs_bots.py."""
    from PokerRL.util.file_util import load_pickle
    state = load_pickle(path=path)
    tname = type(state["t_prof"]).__name__
    if tname == "MCCFRProfile":
        from MCCFR.EvalAgentMCCFR import EvalAgentMCCFR
        agent = EvalAgentMCCFR(t_prof=state["t_prof"])
        agent.load_state_dict(state=state)
        return agent
    if tname == "DistilledProfile":
        from DeepCFR.EvalAgentDistilled import EvalAgentDistilled
        agent = EvalAgentDistilled(t_prof=state["t_prof"])
        agent.load_state_dict(state=state)
        return agent
    from DeepCFR.EvalAgentDeepCFR import EvalAgentDeepCFR
    return EvalAgentDeepCFR.load_from_disk(path_to_eval_agent=path)


print(f"[omaha_web] loading eval agent from {MODEL_PATH} ...", flush=True)
_first_agent = load_eval_agent(MODEL_PATH)
ENV_BLDR = _first_agent.env_bldr
GAME_CLS = ENV_BLDR.env_cls
print("[omaha_web] agent loaded.", flush=True)

SMALL_BET = GAME_CLS.SMALL_BET
BIG_BET = GAME_CLS.BIG_BET

LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Parallel tables. Each browser gets its own Game (keyed by an omaha_sid
# cookie); "vs Friend" uses one shared table under the fixed FRIEND_KEY so a
# second device can join with ?seat=1. Every bot table needs its OWN agent
# instance — the EvalAgent mirrors the game in internal state, so sharing one
# across interleaved hands would corrupt it. Evicted tables return their
# agent to a pool to avoid re-loading the snapshot from disk.
# ---------------------------------------------------------------------------
MAX_BOT_TABLES = 4
FRIEND_KEY = "friend"
_agent_pool = [_first_agent]
_tables = {}       # key -> Game
_table_seen = {}   # key -> monotonic last-use, for LRU eviction


def _take_agent():
    if _agent_pool:
        return _agent_pool.pop()
    print("[omaha_web] loading another eval agent for a new table ...", flush=True)
    a = load_eval_agent(MODEL_PATH)
    print("[omaha_web] extra agent loaded.", flush=True)
    return a


def _evict_lru_bot_table():
    candidates = [k for k, g in _tables.items()
                  if k != FRIEND_KEY and g.mode == "bot"]
    if len(candidates) < MAX_BOT_TABLES:
        return
    oldest = min(candidates, key=lambda k: _table_seen.get(k, 0))
    g = _tables.pop(oldest)
    _table_seen.pop(oldest, None)
    if g.agent is not None:
        _agent_pool.append(g.agent)


def _game_for(key):
    """The caller's table: their own first, else the shared friend table."""
    g = _tables.get(key)
    if g is None:
        g = _tables.get(FRIEND_KEY)
        key = FRIEND_KEY
    if g is not None:
        _table_seen[key] = time.monotonic()
    return g


class Game:
    def __init__(self):
        self.env = None
        self.agent = None  # per-table agent (bot mode only)
        self.done = True
        self.mode = None  # "bot" or "friend"
        self.history = []
        self.baseline = [0, 0]
        self.session_net = [0, 0]

    def new_game(self, mode):
        self.env = GAME_CLS(env_args=ENV_BLDR.env_args, is_evaluating=True,
                            lut_holder=ENV_BLDR.lut_holder)
        self.mode = mode
        if mode == "bot" and self.agent is None:
            self.agent = _take_agent()
        self.session_net = [0, 0]
        self.env.reset()
        self._start_hand()

    def _start_hand(self):
        self.baseline = [p.stack + p.current_bet for p in self.env.seats]
        if self.mode == "bot":
            self.agent.reset(deck_state_dict=self.env.cards_state_dict())
        self.done = False
        self.history = []

    def next_hand(self):
        self.env.reset()
        self._start_hand()

    def bot_decision(self):
        action, _ = self.agent.get_action(step_env=True, need_probs=False)
        if action not in self.env.get_legal_actions():
            action = Poker.CHECK_CALL
        return action

    def apply(self, seat, action, notify_agent=False):
        action_str = {Poker.FOLD: "fold", Poker.CHECK_CALL: "call",
                      Poker.BET_RAISE: "raise"}[action]
        street_str = Poker.INT2STRING_ROUND[self.env.current_round]
        self.history.append({"actor": seat, "street": street_str, "action": action_str})
        _obs, _rew, done, _info = self.env.step(action)
        if notify_agent and self.mode == "bot":
            self.agent.notify_of_action(p_id_acted=seat, action_he_did=action)
        self.done = done
        if done:
            for i, p in enumerate(self.env.seats):
                self.session_net[i] += p.stack - self.baseline[i]


app = Flask(__name__)


def state_for(g: Game, seat: int) -> dict:
    env = g.env
    seats = []
    for p in env.seats:
        reveal = (p.seat_id == seat) or g.done or p.is_allin
        seats.append({
            "seat": p.seat_id,
            "stack": p.stack,
            "current_bet": p.current_bet,
            "folded": p.folded_this_episode,
            "is_allin": p.is_allin,
            "cards": cards_str(p.hand) if reveal else "",
            "n_cards": 4,
            "hand_rank": p.hand_rank if g.done else None,
            "is_bot": (g.mode == "bot" and p.seat_id == BOT_SEAT),
        })
    legal = env.get_legal_actions() if (not g.done and env.current_player.seat_id == seat) else []
    return {
        "mode": g.mode,
        "board": cards_str(env.board),
        "pot": env.main_pot + sum(p.current_bet for p in env.seats),
        "round": Poker.INT2STRING_ROUND.get(env.current_round, "-"),
        "n_raises_this_round": env.n_raises_this_round,
        "small_bet": SMALL_BET,
        "big_bet": BIG_BET,
        "current_player": None if g.done else env.current_player.seat_id,
        "legal_actions": legal,
        "seats": seats,
        "done": g.done,
        "session_net": g.session_net,
    }


def _sid():
    return request.cookies.get("omaha_sid")


@app.route("/")
def index():
    resp = make_response(render_template_string(HTML))
    if not _sid():
        resp.set_cookie("omaha_sid", uuid.uuid4().hex, samesite="Lax",
                        max_age=7 * 24 * 3600)
    return resp


@app.route("/api/new_game", methods=["POST"])
def api_new_game():
    body = request.json or {}
    mode = body.get("mode", "bot")
    sid = _sid() or uuid.uuid4().hex
    with LOCK:
        if mode == "friend":
            g = _tables.get(FRIEND_KEY) or Game()
            g.new_game(mode)
            _tables[FRIEND_KEY] = g
            # creator's own key aliases the friend table so their next
            # requests find it before any old bot table of theirs would
            _tables[sid] = g
        else:
            _evict_lru_bot_table()
            g = _tables.get(sid)
            if g is None or g.mode != "bot":
                g = Game()
            g.new_game(mode)
            _tables[sid] = g
        _table_seen[sid] = time.monotonic()
    resp = jsonify({"ok": True})
    if not _sid():
        resp.set_cookie("omaha_sid", sid, samesite="Lax", max_age=7 * 24 * 3600)
    return resp


@app.route("/api/reset_stacks", methods=["POST"])
def api_reset_stacks():
    # kept for UI compat: restarts the session score
    with LOCK:
        g = _game_for(_sid())
        if g is None or g.env is None:
            return jsonify({"error": "No active game"}), 400
        g.new_game(g.mode)
    return jsonify({"ok": True})


@app.route("/api/state")
def api_state():
    with LOCK:
        g = _game_for(_sid())
        if g is None or g.env is None:
            return jsonify({"error": "No active game"}), 404
        seat = int(request.args.get("seat", 0))
        return jsonify(state_for(g, seat))


@app.route("/api/action", methods=["POST"])
def api_action():
    body = request.json or {}
    seat = int(body.get("seat", HUMAN_SEAT))
    action = int(body.get("action"))
    with LOCK:
        g = _game_for(_sid())
        if g is None or g.env is None or g.done:
            return jsonify({"error": "No hand in progress"}), 400
        if g.env.current_player.seat_id != seat:
            return jsonify({"error": "Not your turn"}), 400
        if action not in g.env.get_legal_actions():
            return jsonify({"error": "Illegal action"}), 400
        g.apply(seat, action, notify_agent=True)
        return jsonify(state_for(g, seat))


@app.route("/api/bot_step", methods=["POST"])
def api_bot_step():
    with LOCK:
        g = _game_for(_sid())
        if g is None or g.env is None:
            return jsonify({"error": "No active game"}), 404
        if g.done or g.mode != "bot" or g.env.current_player.seat_id != BOT_SEAT:
            return jsonify(state_for(g, HUMAN_SEAT))
        action = g.bot_decision()
        g.apply(BOT_SEAT, action, notify_agent=False)
        return jsonify(state_for(g, HUMAN_SEAT))


@app.route("/api/next_hand", methods=["POST"])
def api_next_hand():
    with LOCK:
        g = _game_for(_sid())
        if g is None or g.env is None or not g.done:
            return jsonify({"error": "Hand not over"}), 400
        g.next_hand()
    return jsonify({"ok": True})


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#127183;</text></svg>">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Fixed-Limit Omaha Hi/Lo</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#1a5c2a;color:#fff;font-family:system-ui,-apple-system,sans-serif;-webkit-tap-highlight-color:transparent}
body{display:flex;flex-direction:column;align-items:center;padding:8px;max-width:520px;margin:0 auto;gap:5px}

.hdr{width:100%;display:flex;justify-content:space-between;align-items:center;padding:7px 10px;background:rgba(0,0,0,.38);border-radius:8px}
.hdr h1{font-size:1rem;font-weight:800;letter-spacing:2px;color:#a5d6a7}
.hinfo{font-size:.72rem;opacity:.75;text-align:right;line-height:1.5}

.pbox{width:100%;background:rgba(0,0,0,.22);border-radius:10px;padding:10px 12px;border:2px solid transparent;transition:border-color .2s}
.pbox.actor{border-color:#ffeb3b}
.pbox.human{background:rgba(0,0,0,.32)}
.ph{display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap}
.pname{font-weight:700;font-size:.92rem;flex:1}
.pstack{font-size:.85rem;color:#ffd54f;font-weight:600}

.cards{display:flex;gap:5px;flex-wrap:wrap;min-height:60px;align-items:center}
.card{width:44px;height:60px;background:#fff;border-radius:6px;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:1rem;font-weight:800;line-height:1.1;box-shadow:0 2px 6px rgba(0,0,0,.5);color:#111;user-select:none;flex-shrink:0}
.card .s{font-size:1rem;line-height:1}
.card.r{color:#c62828}
.card.hid{background:#1a3a6b;background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.07) 0,rgba(255,255,255,.07) 1px,transparent 1px,transparent 7px)}
.pbet{font-size:.75rem;color:#ff8a65;margin-top:3px}

.board{width:100%;background:rgba(0,0,0,.18);border-radius:10px;padding:10px 14px;text-align:center}
.pot{font-size:1.05rem;font-weight:700;color:#ffd54f;margin-bottom:5px}
.blbl{font-size:.67rem;color:#a5d6a7;text-transform:uppercase;letter-spacing:1px;margin-bottom:5px}
.bcards{display:flex;gap:5px;justify-content:center;flex-wrap:wrap;min-height:60px;align-items:center}
.sbdg{font-size:.66rem;background:rgba(255,255,255,.15);padding:2px 7px;border-radius:10px;margin-left:6px;font-weight:600;vertical-align:middle}

.actarea{width:100%;display:flex;flex-direction:column;gap:7px}
.btnrow{display:flex;gap:7px}
.btn{border:none;border-radius:8px;padding:15px 10px;font-size:.92rem;font-weight:700;cursor:pointer;color:#fff;touch-action:manipulation;flex:1;transition:opacity .1s,transform .08s}
.btn:active{opacity:.72;transform:scale(.96)}
.btn:disabled{opacity:.35;cursor:default}
.fold{background:#b71c1c}.call{background:#1565c0}.rbtn{background:#e65100}
.bnew{background:#2e7d32;width:100%;padding:16px}
.bnxt{background:#4a148c;width:100%;padding:16px}

.res{width:100%;background:rgba(0,0,0,.38);border-radius:10px;padding:14px;text-align:center}
.res h2{font-size:1.05rem;margin-bottom:10px;color:#ffd54f}
.rline{display:flex;justify-content:space-between;padding:5px 0;font-size:.85rem;border-bottom:1px solid rgba(255,255,255,.1)}
.rline.win{color:#86efac}.rline.lose{color:#fca5a5}

.start{width:100%;display:flex;flex-direction:column;gap:14px;padding:16px 0}
.start h2{text-align:center;font-size:1.15rem;color:#a5d6a7}
.start p{font-size:.85rem;opacity:.8;line-height:1.5}
.modebtns{display:flex;gap:8px}
.modebtn{flex:1;padding:14px 8px;border-radius:8px;border:2px solid rgba(255,255,255,.15);background:rgba(0,0,0,.2);color:#fff;font-size:.95rem;font-weight:700;cursor:pointer}
.modebtn.sel{border-color:#ffd54f;background:rgba(255,213,79,.15)}
.friendlink{background:rgba(0,0,0,.3);border-radius:8px;padding:12px;font-size:.85rem;word-break:break-all}

.thinking{text-align:center;padding:14px;opacity:.65;font-size:.88rem}
</style>
</head>
<body>
<div class="hdr">
  <h1>FL OMAHA HI/LO</h1>
  <div class="hinfo" id="hinfo">8-or-Better</div>
</div>
<div id="app" style="width:100%;display:flex;flex-direction:column;gap:5px"></div>

<script>
const params = new URLSearchParams(location.search);
const urlSeat = params.get('seat');
const seat = urlSeat !== null ? parseInt(urlSeat) : 0;
let mode = 'bot';
let G = null;
let botStepTimer = null;
const $ = id => document.getElementById(id);
const app = $('app');

async function post(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
  return r.json();
}

const SUIT = {s:'&#9824;', h:'&#9829;', d:'&#9830;', c:'&#9827;'}, RED = {h:1, d:1};
function cardHtml(rankSuit) {
  const rank = rankSuit.slice(0, -1), suit = rankSuit.slice(-1).toLowerCase();
  return `<div class="card${RED[suit] ? ' r' : ''}">${rank}<span class="s">${SUIT[suit]||suit}</span></div>`;
}
function hiddenCardHtml() { return '<div class="card hid"></div>'; }
function renderCardString(s, hidden, nCards) {
  if (hidden) return Array(nCards).fill(0).map(hiddenCardHtml).join('');
  const cards = [];
  for (let i = 0; i < s.length; i += 2) cards.push(s.slice(i, i+2));
  return cards.map(cardHtml).join('');
}

function netStr(n) { return (n >= 0 ? '+$' + n : '-$' + (-n)); }

function playerBox(p, isMe, isActor) {
  const label = isMe ? 'You' : (p.is_bot ? 'Deep CFR AI' : 'Opponent');
  const hidden = p.cards === '';
  let rankStr = '';
  if (p.hand_rank) {
    rankStr = `<div class="pbet">hi=${p.hand_rank[0]}${p.hand_rank[1] !== null ? ', lo=' + p.hand_rank[1] : ' (no low)'}</div>`;
  }
  return `<div class="pbox${isActor ? ' actor' : ''}${isMe ? ' human' : ''}">
    <div class="ph">
      <span class="pname">${label}${p.folded ? ' (folded)' : ''}${p.is_allin ? ' (all-in)' : ''}</span>
      <span class="pstack">$${p.stack}</span>
    </div>
    <div class="cards">${renderCardString(p.cards, hidden, p.n_cards)}</div>
    ${p.current_bet ? `<div class="pbet">Bet: $${p.current_bet}</div>` : ''}
    ${rankStr}
  </div>`;
}

function needsBotStep(s) {
  return s && s.mode === 'bot' && !s.done && s.current_player === 1;
}

async function botStep() {
  const s = await post('/api/bot_step');
  G = s;
  render();
  if (needsBotStep(G)) botStepTimer = setTimeout(botStep, 400);
}

async function newGame() {
  clearTimeout(botStepTimer);
  await post('/api/new_game', {mode});
  await poll();
}

async function nextHand() {
  clearTimeout(botStepTimer);
  await post('/api/next_hand');
  await poll();
}

async function act(action) {
  clearTimeout(botStepTimer);
  G = await post('/api/action', {seat, action});
  render();
  if (needsBotStep(G)) botStepTimer = setTimeout(botStep, 400);
}

function render() {
  if (!G) { renderStart(); return; }
  const me = G.seats[seat], opp = G.seats[1 - seat];
  $('hinfo').textContent = `FL $${G.small_bet}/$${G.big_bet}  |  ${G.round.toUpperCase()}`;

  let h = '';
  h += playerBox(opp, false, !G.done && G.current_player === opp.seat);
  h += `<div class="board">
    <div class="pot">Pot: $${G.pot}</div>
    <div class="blbl">Board<span class="sbdg">${G.round}${G.n_raises_this_round ? ' &middot; raises: ' + G.n_raises_this_round : ''}</span></div>
    <div class="bcards">${G.board ? renderCardString(G.board, false, 5) : '<span style="opacity:.35">&mdash;</span>'}</div>
  </div>`;
  h += playerBox(me, true, !G.done && G.current_player === me.seat);

  h += '<div class="actarea">';
  if (G.done) {
    h += `<div class="res"><h2>Hand over</h2>
      <div class="rline"><span>You (session)</span><span>${netStr(G.session_net[seat])}</span></div>
      <div class="rline"><span>${opp.is_bot ? 'Deep CFR AI' : 'Opponent'} (session)</span><span>${netStr(G.session_net[1 - seat])}</span></div>
    </div>`;
    h += `<button class="btn bnxt" onclick="nextHand()">Next Hand &#9654;</button>`;
  } else if (G.current_player === seat) {
    const btns = [];
    if (G.legal_actions.includes(0)) btns.push(`<button class="btn fold" onclick="act(0)">Fold</button>`);
    if (G.legal_actions.includes(1)) btns.push(`<button class="btn call" onclick="act(1)">${me.current_bet===opp.current_bet && G.round==='preflop' ? 'Check/Call' : 'Call'}</button>`);
    if (G.legal_actions.includes(2)) btns.push(`<button class="btn rbtn" onclick="act(2)">Raise</button>`);
    h += `<div class="btnrow">${btns.join('')}</div>`;
  } else {
    h += `<div class="thinking">${opp.is_bot ? 'AI is thinking&hellip;' : 'Waiting on opponent&hellip;'}</div>`;
  }
  h += '</div>';
  app.innerHTML = h;
}

function selectMode(m) {
  mode = m;
  renderStart();
}

function renderStart() {
  app.innerHTML = `<div class="start">
    <h2>New Game</h2>
    <div class="modebtns">
      <button class="modebtn${mode==='bot'?' sel':''}" onclick="selectMode('bot')">vs AI</button>
      <button class="modebtn${mode==='friend'?' sel':''}" onclick="selectMode('friend')">vs Friend</button>
    </div>
    ${mode === 'bot' ? `
    <p>Heads-up against a Deep CFR agent trained by self-play. Stacks reset to the baseline every hand; the session score above the buttons tracks who's up.</p>
    ` : `
    <p>You'll play seat 0. Share this page's URL with <code>?seat=1</code> appended with your friend.</p>
    <div class="friendlink">${location.origin}${location.pathname}?seat=1</div>
    `}
    <button class="btn bnew" onclick="newGame()">Start Game</button>
  </div>`;
}

let noGameYet = false;

async function poll() {
  const r = await fetch(`/api/state?seat=${seat}`);
  if (r.status === 404) {
    G = null;
    noGameYet = true;
    if (seat === 0) { renderStart(); }
    else { app.innerHTML = '<div class="thinking">Waiting for seat 0 to start the game&hellip;</div>'; }
    return;
  }
  noGameYet = false;
  G = await r.json();
  render();
  if (needsBotStep(G) && !botStepTimer) botStepTimer = setTimeout(botStep, 400);
}

poll();
setInterval(() => {
  if (needsBotStep(G)) return;
  if (noGameYet && seat === 0) return;
  poll();
}, 1500);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False, threaded=True)

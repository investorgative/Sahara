#!/usr/bin/env python3
"""
SAHARA dashboard + API (tmux session "SaharaWeb", port 8761).

The page itself contains no data. Every /api call needs a token that only the correct passcode hands out
(checked here, on the Pi). Tokens live in memory: restarting the web server locks every browser.
"""
import hmac as _hmac
import os
import re
import secrets
import subprocess
import sys
import threading
import time

from flask import Flask, Response, request, send_from_directory

import sahara_backtest as bt
import sahara_core as core
import sahara_exec as ex

cfg0 = core.load_config()
log = core.setup_logging("web")
app = Flask(__name__, static_folder=None)
STATIC = os.path.join(core.BASE_DIR, "static")
CF_LOG = os.path.join(core.LOG_DIR, "cloudflared.log")

AUTH_LOCK = threading.Lock()
TOKENS, FAILS = {}, {}
TOKEN_TTL = 12 * 3600
LOCKOUT = {6: 60, 7: 300, 8: 900}
PUBLIC_API = {"/api/auth/status", "/api/unlock"}
BT = {"running": False, "stage": "idle", "pct": 0.0, "error": None, "result": None, "started": None}
BT_LOCK = threading.Lock()
OPT = {"p": None}
CACHE, CACHE_LOCK = {}, threading.Lock()


def _init_passcode():
    pc = str(os.environ.get("SAHARA_PASSCODE") or cfg0["web"].get("passcode") or "")
    if pc.isdigit() and 4 <= len(pc) <= 8:
        return pc
    pc = "".join(secrets.choice("0123456789") for _ in range(6))
    try:
        core.update_config({"web": {"passcode": pc}})
    except Exception:
        pass
    print(f"\n*** No valid web passcode in config.json - generated one: {pc}  (change: web.passcode) ***\n", flush=True)
    return pc


PASSCODE = _init_passcode()
AUTOLOCK_MIN = float(cfg0["web"].get("autolock_minutes", 10))


def cfg_now():
    return core.load_config()


def jresp(obj, status=200):
    import json
    return Response(json.dumps(core.clean_json(obj), separators=(",", ":")), status=status, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


def cached(key, ttl, fn):
    with CACHE_LOCK:
        hit = CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with CACHE_LOCK:
        CACHE[key] = (time.time(), val)
    return val


def body():
    return request.get_json(force=True, silent=True) or {}


# ---------------------------------------------------------------- vault (lock screen)
def client_ip():
    peer = request.remote_addr or ""
    if peer in ("127.0.0.1", "::1"):  # behind the Cloudflare tunnel
        return request.headers.get("CF-Connecting-IP") or peer
    return peer


def lockout_for(n):
    return 0 if n < 6 else LOCKOUT.get(n, 3600)


def token_ok(tok):
    if not tok:
        return False
    with AUTH_LOCK:
        exp = TOKENS.get(tok)
        if not exp or exp < time.time():
            TOKENS.pop(tok, None)
            return False
        return True


@app.before_request
def _auth():
    if request.path.startswith("/api/") and request.path not in PUBLIC_API:
        if not token_ok(request.headers.get("X-Sahara-Token", "")):
            return jresp({"error": "locked"}, 401)
    return None


@app.after_request
def _headers(r):
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "no-referrer"
    return r


@app.errorhandler(Exception)
def _err(e):
    code = getattr(e, "code", 500)
    if code == 500 or not isinstance(code, int):
        log.exception(f"web error on {request.path}")
        return jresp({"error": str(e)}, 500)
    return jresp({"error": str(e)}, code)


@app.route("/api/auth/status")
def api_auth_status():
    with AUTH_LOCK:
        f = FAILS.get(client_ip(), {"n": 0, "until": 0})
    return jresp({"attempts": f["n"], "wait": max(0, int(f["until"] - time.time())), "digits": len(PASSCODE),
                  "autolock_min": AUTOLOCK_MIN, "version": core.VERSION})


@app.route("/api/unlock", methods=["POST"])
def api_unlock():
    code = str(body().get("code", ""))
    ip = client_ip()
    with AUTH_LOCK:
        f = FAILS.setdefault(ip, {"n": 0, "until": 0})
        now = time.time()
        if f["until"] > now:
            return jresp({"error": "locked", "wait": int(f["until"] - now), "attempts": f["n"]}, 429)
        if _hmac.compare_digest(code.encode(), PASSCODE.encode()):
            FAILS.pop(ip, None)
            for t in [t for t, e in TOKENS.items() if e < now]:
                TOKENS.pop(t, None)
            tok = secrets.token_urlsafe(32)
            TOKENS[tok] = now + TOKEN_TTL
            return jresp({"token": tok, "autolock_min": AUTOLOCK_MIN})
        f["n"] += 1
        f["until"] = now + lockout_for(f["n"])
        n, wait = f["n"], lockout_for(f["n"])
    if n >= 6:
        log.warning(f"web: {n} wrong passcodes from {ip} - locked for {wait}s")
    return jresp({"error": "wrong", "wait": wait, "attempts": n}, 403)


@app.route("/api/lock", methods=["POST"])
def api_lock():
    with AUTH_LOCK:
        TOKENS.pop(request.headers.get("X-Sahara-Token", ""), None)
    return jresp({"ok": True})


# ---------------------------------------------------------------- pages
@app.route("/")
def index():
    r = send_from_directory(STATIC, "index.html")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(STATIC, p, max_age=86400)


@app.route("/healthz")
def healthz():
    return "ok"


def tunnel_url():
    def f():
        try:
            with open(CF_LOG, "r", errors="ignore") as fh:
                m = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", fh.read()[-20000:])
            return m[-1] if m else None
        except Exception:
            return None
    return cached("tunnel", 20, f)


# ---------------------------------------------------------------- live paper account
def live_engine(cfg):
    st = core.read_json(core.STATE_PATH, None)
    p = core.clean_params(None, cfg["strategy"])
    eng = core.Engine(p, cfg)
    started = core.now_ms()
    if st and st.get("engine"):
        eng.load(st["engine"])
        started = st.get("started", started)
    return eng, started


@app.route("/api/status")
def api_status():
    """small and fast: polled every few seconds"""
    cfg = cfg_now()
    st = core.read_json(core.STATUS_PATH, {}) or {}
    age = (core.now_ms() - st.get("ts", 0)) / 1000 if st else None
    return jresp({"status": st, "age": age, "alive": age is not None and age < max(90, cfg.get("poll_seconds", 10) * 6),
                  "now": core.now_ms(), "tz": cfg["timezone"], "tunnel": tunnel_url(), "symbol": cfg["symbol"],
                  "tf": cfg["timeframe"], "version": core.VERSION,
                  "bt": {k: BT[k] for k in ("running", "stage", "pct", "error")},
                  "exec": {k: _exec_state(k) for k in ("demo", "live")}})


@app.route("/api/overview")
def api_overview():
    cfg = cfg_now()

    def f():
        eng, started = live_engine(cfg)
        st = core.read_json(core.STATUS_PATH, {}) or {}
        b = core.bundle(eng, cfg, started, core.now_ms(), mark=st.get("price"))
        b["meta"] = {"live": True, "symbol": cfg["symbol"], "timeframe": cfg["timeframe"], "balance": eng.start_balance,
                     "risk_pct": cfg["account"]["risk_pct"], "kelly_sizing": cfg["account"].get("kelly_sizing", False)}
        return b
    return jresp(cached("overview", 4, f))


@app.route("/api/events")
def api_events():
    return jresp({"events": (core.read_json(core.EVENTS_PATH, []) or [])[-150:]})


@app.route("/api/log")
def api_log():
    n = min(1000, int(request.args.get("n", 250)))
    try:
        with open(core.LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()[-n:]
    except FileNotFoundError:
        lines = []
    return jresp({"lines": lines})


@app.route("/api/chart")
def api_chart():
    """candles + levels for the live chart (closed candles from the database + the forming one)"""
    cfg = cfg_now()
    bars = max(100, min(5000, int(request.args.get("bars", 800))))
    p = core.clean_params(None, cfg["strategy"])

    def f():
        store = core.Store(None)
        d = store.tail(cfg["symbol"], cfg["timeframe"], bars + core.warmup_bars(p))
        if len(d["ts"]) == 0:
            return {"candles": [], "hh": [], "ll": [], "tl": [], "ts": [], "tf_ms": core.tf_ms(cfg), "agg": 1}
        eng = core.Engine(p, cfg)
        start = int(d["ts"][max(0, len(d["ts"]) - bars)])
        return bt.chart_payload(d, core.indicators(d, p), eng, start, max_bars=bars)
    out = dict(cached(("chart", bars, tuple(sorted(p.items()))), 8, f))
    st = core.read_json(core.STATUS_PATH, {}) or {}
    out["forming"] = st.get("bar")
    out["levels"] = st.get("levels")
    return jresp(out)


# ---------------------------------------------------------------- settings
def _mask(u):
    return ("…" + u[-6:]) if u else ""


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        b = body()
        patch = {}
        if "strategy" in b:
            patch["strategy"] = core.clean_params(b["strategy"], cfg_now()["strategy"])
        acc = {}
        for k, lo, hi in (("risk_pct", 0.05, 25), ("kelly_fraction", 0.05, 1), ("kelly_max_risk_pct", 0.1, 25),
                          ("max_leverage", 1, 100)):
            if k in b:
                acc[k] = min(hi, max(lo, float(b[k])))
        for k in ("kelly_sizing", "lot_rounding"):
            if k in b:
                acc[k] = bool(b[k])
        if acc:
            patch["account"] = acc
        dc = {}
        for k in ("BTCUSDT", "errors"):
            v = (b.get("discord") or {}).get(k)
            if v is not None:
                v = str(v).strip()
                if v and not v.startswith("https://"):
                    return jresp({"error": "a webhook must start with https://"}, 400)
                dc[k] = v
        if dc:
            patch["discord"] = dc
        if patch:
            core.update_config(patch)
            log.info(f"settings changed from the website: {sorted(patch)}")
            if b.get("test_discord"):
                c = cfg_now()["discord"]
                core.discord_send(c.get("BTCUSDT"), "🏜 **SAHARA** trade webhook works.")
                core.discord_send(c.get("errors"), "🏜 **SAHARA** error webhook works.")
    c = cfg_now()
    return jresp({"account": c["account"], "fees": c["fees"], "strategy": core.clean_params(None, c["strategy"]),
                  "timeframe": c["timeframe"], "symbol": c["symbol"], "timezone": c["timezone"],
                  "optimizer": {k: c["optimizer"][k] for k in ("days", "combos", "train_frac", "space")},
                  "discord": {k: {"set": bool(c["discord"].get(k)), "masked": _mask(c["discord"].get(k, ""))}
                              for k in ("BTCUSDT", "errors")}})


# ---------------------------------------------------------------- backtester
def _bt_thread(kw):
    def pr(stage, x):
        BT.update(stage=stage, pct=float(x))
    try:
        res = bt.run(cfg_now(), progress=pr, log=log, **kw)
        core.save_json(bt.BT_RESULT, res)
        BT.update(result=res, error=None)
    except Exception as e:
        log.exception("backtest failed")
        BT.update(error=str(e))
    finally:
        BT.update(running=False, stage="idle", pct=0.0)


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    b = body()
    with BT_LOCK:
        if BT["running"]:
            return jresp({"error": "a backtest is already running"}, 409)
        kw = {"days": max(7, min(4000, int(b.get("days") or 365))),
              "risk_pct": min(25.0, max(0.05, float(b.get("risk") or cfg_now()["account"]["risk_pct"]))),
              "balance": max(1.0, float(b.get("balance") or cfg_now()["account"]["start_balance"])),
              "kelly": bool(b.get("kelly", False))}
        if b.get("params"):
            kw["params"] = core.clean_params(b["params"], cfg_now()["strategy"])
        BT.update(running=True, stage="starting", pct=0.0, error=None, started=core.now_ms())
        threading.Thread(target=_bt_thread, args=(kw,), daemon=True, name="backtest").start()
    return jresp({"ok": True})


@app.route("/api/backtest/result")
def api_backtest_result():
    if BT["result"] is None:
        BT["result"] = core.read_json(bt.BT_RESULT, None)
    return jresp({"result": BT["result"], "running": BT["running"], "stage": BT["stage"], "pct": BT["pct"],
                  "error": BT["error"]})


# ---------------------------------------------------------------- optimizer
def _opt_running():
    p = OPT["p"]
    return p is not None and p.poll() is None


@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    b = body()
    if _opt_running():
        return jresp({"error": "the optimizer is already running"}, 409)
    days = max(60, min(4000, int(b.get("days") or cfg_now()["optimizer"]["days"])))
    combos = max(5, min(3000, int(b.get("combos") or cfg_now()["optimizer"]["combos"])))
    core.save_json(bt.OPT_PROGRESS, {"running": True, "stage": "starting", "done": 0, "total": combos, "ts": core.now_ms()})
    OPT["p"] = subprocess.Popen([sys.executable, os.path.join(core.BASE_DIR, "sahara_backtest.py"), "--optimize", "--days",
                                 str(days), "--combos", str(combos)], cwd=core.BASE_DIR, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    return jresp({"ok": True})


@app.route("/api/optimize/stop", methods=["POST"])
def api_optimize_stop():
    open(bt.OPT_STOP, "w").close()
    return jresp({"ok": True})


@app.route("/api/optimize/status")
def api_optimize_status():
    pr = core.read_json(bt.OPT_PROGRESS, {}) or {}
    if pr.get("running") and not _opt_running() and core.now_ms() - pr.get("ts", 0) > 15000:
        pr["running"] = False
        pr["stage"] = "error" if pr.get("stage") not in ("done", "stopped") else pr["stage"]
        pr.setdefault("error", "the optimizer process ended unexpectedly (see the log)")
    out = {"progress": pr}
    if request.args.get("result"):
        out["result"] = core.read_json(bt.OPT_RESULT, None)
    return jresp(out)


@app.route("/api/optimize/apply", methods=["POST"])
def api_optimize_apply():
    p = core.clean_params(body().get("params"), cfg_now()["strategy"])
    core.update_config({"strategy": p})
    log.info(f"strategy parameters applied from the website: {p}")
    return jresp({"ok": True, "strategy": p})


# ---------------------------------------------------------------- API keys / executors
def _exec_state(kind):
    st = core.read_json(ex.paths(kind)[1], {}) or {}
    ctl = core.read_json(ex.paths(kind)[0], {}) or {}
    return {"keys": ex.keys_info(kind), "armed": bool(ctl.get("armed")) and (kind != "live" or ex.LIVE_ENABLED),
            "enabled": kind != "live" or ex.LIVE_ENABLED, "wallet": st.get("wallet"), "pos": st.get("pos"),
            "error": st.get("error"), "actions": (st.get("actions") or [])[-12:], "ts": st.get("ts")}


def _kind():
    k = body().get("kind")
    if k not in ex.HOSTS:
        raise ValueError("kind must be demo or live")
    return k


@app.route("/api/keys", methods=["POST"])
def api_keys():
    b, kind = body(), _kind()
    key, sec = str(b.get("api_key", "")).strip(), str(b.get("api_secret", "")).strip()
    if (key or sec) and not (re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key) and re.fullmatch(r"[A-Za-z0-9_-]{8,120}", sec)):
        return jresp({"error": "that does not look like a Bybit API key / secret"}, 400)
    ex.save_keys(kind, key, sec)
    if not key:
        core.save_json(ex.paths(kind)[0], {"armed": False})
    log.info(f"{kind} API keys {'saved' if key else 'removed'} from the website")
    return jresp({"ok": True, "state": _exec_state(kind)})


@app.route("/api/keys/test", methods=["POST"])
def api_keys_test():
    kind = _kind()
    try:
        return jresp({"ok": True, "wallet": ex.test_connection(kind)})
    except Exception as e:
        return jresp({"ok": False, "error": str(e)})


@app.route("/api/exec", methods=["POST"])
def api_exec():
    b, kind = body(), _kind()
    action = b.get("action")
    if kind == "live" and not ex.LIVE_ENABLED:
        return jresp({"error": "live trading is not implemented yet - the switch is built but locked"}, 400)
    if action == "arm":
        if not ex.keys_info(kind)["present"]:
            return jresp({"error": "save API keys first"}, 400)
        core.save_json(ex.paths(kind)[0], {"armed": True, "ts": core.now_ms()})
        log.info(f"{kind} executor ARMED from the website")
    elif action == "disarm":
        core.save_json(ex.paths(kind)[0], {"armed": False, "ts": core.now_ms()})
        log.info(f"{kind} executor disarmed from the website")
    elif action == "flatten":
        core.save_json(ex.paths(kind)[0], {"armed": False, "ts": core.now_ms()})
        try:
            ex.Executor(kind, cfg_now(), log).flatten("FLATTEN pressed on the website")
        except Exception as e:
            return jresp({"error": str(e)}, 500)
    else:
        return jresp({"error": "unknown action"}, 400)
    return jresp({"ok": True, "state": _exec_state(kind)})


if __name__ == "__main__":
    port = int(cfg0["web"].get("port", 8761))
    log.info(f"SAHARA web {core.VERSION} on port {port}")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port, threaded=True)

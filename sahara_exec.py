#!/usr/bin/env python3
"""
SAHARA exchange executors.

  DEMO  mirrors every paper trade on Bybit Demo Trading (api-demo.bybit.com) once you saved demo API keys on
        the website (Vault tab) and pressed ARM.
  LIVE  real money (api.bybit.com). The keys can already be stored and the connection tested, but order
        execution is deliberately NOT enabled yet (LIVE_ENABLED = False). Nothing in this file can place a
        real-money order while that flag is False.

API keys live in data/secrets.json (chmod 600, never sent back to the browser, never in git).
"""
import hashlib
import hmac
import json
import math
import os
import threading
import time
import urllib.parse

import requests

import sahara_core as core

HOSTS = {"demo": "https://api-demo.bybit.com", "live": "https://api.bybit.com"}
LIVE_ENABLED = False
RECV = "10000"


class ExecError(RuntimeError):
    pass


def load_keys(kind):
    s = (core.read_json(core.SECRETS_PATH, {}) or {}).get(kind, {})
    return s.get("api_key", ""), s.get("api_secret", "")


def save_keys(kind, key, secret):
    if kind not in HOSTS:
        raise ExecError("unknown account type")
    s = core.read_json(core.SECRETS_PATH, {}) or {}
    if key or secret:
        s[kind] = {"api_key": key.strip(), "api_secret": secret.strip(), "saved": core.now_ms()}
    else:
        s.pop(kind, None)
    core.save_json(core.SECRETS_PATH, s)
    try:
        os.chmod(core.SECRETS_PATH, 0o600)
    except Exception:
        pass


def keys_info(kind):
    s = (core.read_json(core.SECRETS_PATH, {}) or {}).get(kind, {})
    k = s.get("api_key", "")
    return {"present": bool(k and s.get("api_secret")), "masked": (k[:4] + "•" * 8 + k[-3:]) if len(k) > 8 else ("•" * len(k)),
            "saved": s.get("saved")}


def paths(kind):
    return os.path.join(core.DATA_DIR, f"exec_{kind}_control.json"), os.path.join(core.DATA_DIR, f"exec_{kind}_state.json")


class BybitPrivate:
    """Minimal signed Bybit v5 REST client."""

    def __init__(self, key, secret, host):
        if host not in HOSTS.values() and not host.startswith("http://127.0.0.1"):
            raise ExecError(f"host {host} not allowed")
        self.host, self.key, self.secret = host, key or "", secret or ""
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"sahara/{core.VERSION}"
        self.lock = threading.Lock()
        self.time_offset = 0

    def ready(self):
        return bool(self.key and self.secret)

    def _sign(self, ts, payload):
        return hmac.new(self.secret.encode(), f"{ts}{self.key}{RECV}{payload}".encode(), hashlib.sha256).hexdigest()

    def _req(self, method, path, params=None, signed=True, retries=2):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        err = None
        for attempt in range(retries + 1):
            try:
                ts = str(int(time.time() * 1000) + self.time_offset)
                headers = {}
                if method == "GET":
                    qs = urllib.parse.urlencode(params)
                    url, payload, body = self.host + path + ("?" + qs if qs else ""), qs, None
                else:
                    url = self.host + path
                    body = json.dumps(params, separators=(",", ":"))
                    payload = body
                    headers["Content-Type"] = "application/json"
                if signed:
                    if not self.ready():
                        raise ExecError("API key / secret missing")
                    headers.update({"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": RECV,
                                    "X-BAPI-SIGN": self._sign(ts, payload)})
                with self.lock:
                    r = self.s.request(method, url, data=body, headers=headers, timeout=15)
                j = r.json()
                rc = j.get("retCode")
                if rc == 10002 and attempt < retries:
                    self.sync_time()
                    continue
                if rc not in (0, None):
                    raise ExecError(f"{path} retCode={rc} {j.get('retMsg')}")
                return j.get("result", {})
            except ExecError:
                raise
            except Exception as e:
                err = e
                time.sleep(1.0 * (attempt + 1))
        raise ExecError(f"{path} request failed: {err}")

    def sync_time(self):
        try:
            r = self.s.get(self.host + "/v5/market/time", timeout=10).json()
            self.time_offset = int(r["time"]) - int(time.time() * 1000)
        except Exception:
            pass

    def wallet(self):
        res = self._req("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        acc = (res.get("list") or [{}])[0]
        f = lambda x: float(x) if x not in (None, "") else 0.0
        return {"equity": f(acc.get("totalEquity")), "wallet": f(acc.get("totalWalletBalance")),
                "available": f(acc.get("totalAvailableBalance")), "upnl": f(acc.get("totalPerpUPL"))}

    def instrument(self, sym):
        it = self._req("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": sym}, signed=False)["list"][0]
        lf, pf = it["lotSizeFilter"], it["priceFilter"]
        return {"qty_step": float(lf["qtyStep"]), "min_qty": float(lf["minOrderQty"]), "tick": float(pf["tickSize"]),
                "max_lev": float(it.get("leverageFilter", {}).get("maxLeverage") or 50)}

    def positions(self, sym):
        out = []
        for x in self._req("GET", "/v5/position/list", {"category": "linear", "symbol": sym}).get("list", []):
            if float(x.get("size") or 0) > 0:
                out.append({"sym": x["symbol"], "dir": 1 if x["side"] == "Buy" else -1, "size": float(x["size"]),
                            "entry": float(x.get("avgPrice") or 0), "sl": float(x.get("stopLoss") or 0),
                            "upnl": float(x.get("unrealisedPnl") or 0), "lev": x.get("leverage"),
                            "mark": float(x.get("markPrice") or 0), "liq": float(x.get("liqPrice") or 0)})
        return out

    def set_leverage(self, sym, lev):
        try:
            self._req("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": sym, "buyLeverage": str(lev),
                                                             "sellLeverage": str(lev)})
        except ExecError as e:
            if "110043" not in str(e):  # leverage not modified
                raise

    def order(self, sym, side, qty, reduce=False, link=None):
        return self._req("POST", "/v5/order/create", {"category": "linear", "symbol": sym, "side": side, "orderType": "Market",
                                                       "qty": qty, "positionIdx": 0, "reduceOnly": reduce, "orderLinkId": link})

    def set_stop(self, sym, sl):
        return self._req("POST", "/v5/position/trading-stop", {"category": "linear", "symbol": sym, "tpslMode": "Full",
                                                               "positionIdx": 0, "stopLoss": sl, "slTriggerBy": "LastPrice"})

    def cancel_all(self, sym):
        return self._req("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": sym})


def fmt_num(x, step):
    dec = max(0, -int(math.floor(math.log10(step) + 1e-9))) if step < 1 else 0
    return f"{math.floor(x / step + 1e-9) * step:.{dec}f}"


def fmt_px(x, tick):
    dec = max(0, -int(math.floor(math.log10(tick) + 1e-9))) if tick < 1 else 0
    return f"{round(round(x / tick) * tick, dec):.{dec}f}"


def client(kind, host=None):
    k, s = load_keys(kind)
    return BybitPrivate(k, s, host or HOSTS[kind])


def test_connection(kind, host=None):
    """read-only check of stored keys (wallet balance). Safe for LIVE as well."""
    c = client(kind, host)
    if not c.ready():
        raise ExecError("no API key stored")
    c.sync_time()
    return c.wallet()


class Executor:
    """mirrors the engine's events on a Bybit account. Only acts while armed."""

    def __init__(self, kind, cfg, log, host=None):
        self.kind, self.cfg, self.log = kind, cfg, log
        self.sym = cfg["symbol"]
        self.host = host or HOSTS[kind]
        self.ctl_path, self.state_path = paths(kind)
        self.state = core.read_json(self.state_path, None) or {"actions": [], "mine": None, "wallet": None, "pos": None,
                                                               "error": None, "ts": 0}
        self._inst = None
        self._last_tick = 0
        self._key_sig = None
        self.api = None

    # ---- plumbing
    def _api(self):
        k, s = load_keys(self.kind)
        if (k, s) != self._key_sig:
            self._key_sig = (k, s)
            self.api = BybitPrivate(k, s, self.host)
            self.api.sync_time()
            self._inst = None
        return self.api

    def control(self):
        return core.read_json(self.ctl_path, {}) or {}

    def armed(self):
        if self.kind == "live" and not LIVE_ENABLED:
            return False
        return bool(self.control().get("armed")) and self._api().ready()

    def act(self, what, ok=True, detail=""):
        self.state["actions"] = (self.state.get("actions") or [])[-60:] + [
            {"ts": core.now_ms(), "what": what, "ok": ok, "detail": str(detail)[:300]}]
        (self.log.info if ok else self.log.error)(f"{self.kind}: {what} {detail}")
        self.save()

    def save(self):
        self.state["ts"] = core.now_ms()
        self.state["armed"] = bool(self.control().get("armed"))
        core.save_json(self.state_path, self.state)

    def inst(self):
        if self._inst is None:
            try:
                self._inst = self._api().instrument(self.sym)
            except Exception:
                i = self.cfg["instrument"]
                self._inst = {"qty_step": i["qty_step"], "min_qty": i["min_qty"], "tick": i["tick"], "max_lev": 50}
        return self._inst

    # ---- engine events
    def on_event(self, ev):
        if ev["type"] not in ("open", "trail", "close") or not self.armed():
            return
        try:
            if ev["type"] == "open":
                self._open(ev["pos"])
            elif ev["type"] == "trail":
                if self.state.get("mine"):
                    self._api().set_stop(self.sym, fmt_px(ev["new"], self.inst()["tick"]))
                    self.state["mine"]["stop"] = ev["new"]
                    self.act("trail", True, f"stop -> {ev['new']:.1f}")
            elif ev["type"] == "close":
                self.flatten(f"engine closed ({ev['trade']['reason']})")
        except Exception as e:
            self.state["error"] = str(e)
            self.act(ev["type"], False, e)

    def _open(self, p):
        api, ins = self._api(), self.inst()
        if api.positions(self.sym):
            self.act("open", False, "account already has a position in this symbol - not touching it")
            return
        w = api.wallet()
        ec = self.cfg.get(self.kind, {})
        risk_pct = float(ec.get("risk_pct", self.cfg["account"]["risk_pct"]))
        lev = min(float(ec.get("max_leverage", 25)), ins["max_lev"])
        dist = abs(p["entry"] - p["stop"])
        qty = w["equity"] * risk_pct / 100.0 / dist if dist > 0 else 0.0
        qty = min(qty, w["equity"] * lev / p["entry"] * 0.95)
        qs = fmt_num(qty, ins["qty_step"])
        if float(qs) < ins["min_qty"]:
            self.act("open", False, f"size {qty:.5f} below the exchange minimum {ins['min_qty']} - skipped")
            return
        api.set_leverage(self.sym, int(lev))
        side = "Buy" if p["dir"] == 1 else "Sell"
        api.order(self.sym, side, qs, link=f"sahara-{p['id']}-{int(time.time())}")
        self.state["mine"] = {"id": p["id"], "dir": p["dir"], "qty": float(qs), "stop": p["stop"], "ts": core.now_ms()}
        self.save()
        try:
            api.set_stop(self.sym, fmt_px(p["stop"], ins["tick"]))
        except Exception as e:  # a position without a stop must not exist
            self.act("stop rejected - closing the position", False, e)
            self.flatten("stop rejected")
            return
        self.act("open", True, f"{side} {qs} stop {p['stop']:.1f} (risk {risk_pct}% of ${w['equity']:.2f})")

    def flatten(self, why="manual"):
        api = self._api()
        closed = False
        for x in api.positions(self.sym):
            api.order(self.sym, "Sell" if x["dir"] == 1 else "Buy", fmt_num(x["size"], self.inst()["qty_step"]), reduce=True,
                      link=f"sahara-x-{int(time.time() * 1000)}")
            closed = True
        self.state["mine"] = None
        self.act("flatten" if closed else "flat", True, why)

    # ---- periodic reconcile (called by the trader)
    def tick(self, eng, every=30):
        if time.time() - self._last_tick < every:
            return
        self._last_tick = time.time()
        api = self._api()
        self.state["keys"] = api.ready()
        self.state["enabled"] = self.kind != "live" or LIVE_ENABLED
        if not api.ready():
            self.state.update(wallet=None, pos=None)
            self.save()
            return
        try:
            self.state["wallet"] = api.wallet()
            ps = api.positions(self.sym)
            self.state["pos"] = ps[0] if ps else None
            self.state["error"] = None
            if self.armed() and self.state.get("mine"):
                if not ps:
                    self.state["mine"] = None  # stopped out on the exchange
                elif eng.pos is None:
                    self.flatten("engine is flat")
        except Exception as e:
            self.state["error"] = str(e)
            self.log.warning(f"{self.kind}: reconcile failed: {e}")
        self.save()

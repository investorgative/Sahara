#!/usr/bin/env python3
"""
SAHARA core - shared by the paper trader, the backtester, the optimizer and the website.

Strategy "Sahara Impulse Breakout" (BTCUSDT perpetual, Bybit, 30m candles by default)
  * ENTRY   a candle CLOSES above the highest high (below the lowest low) of the previous `entry_n` candles
            AND that candle is an impulse: (high - low) >= `impulse` x ATR(`atr_n`)
            (optional: volume >= `vol_mult` x average volume of `vol_n` candles)
            -> market order at the OPEN of the next candle (taker fee + slippage)
  * STOP    entry -/+ `stop_atr` x ATR.  Position size = risk$ / stop distance.
  * EXIT    the stop trails along the lowest low (highest high) of the last `trail_n` candles.
            There is no take-profit: losers are cut small and fast, winners run for days or weeks.

NO LOOK-AHEAD: every decision is taken on a closed candle and only uses that candle and older ones.
The order created by a decision is filled on a LATER candle. The paper trader and the backtester run
the very same Engine class, candle by candle.
"""
import json
import logging
import math
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import numpy as np
import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

VERSION = "1.0.0"
NAME = "SAHARA"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("SAHARA_DATA") or os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
CONFIG_PATH = os.environ.get("SAHARA_CONFIG") or os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "sahara.db")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
STATUS_PATH = os.path.join(DATA_DIR, "status.json")
EVENTS_PATH = os.path.join(DATA_DIR, "events.json")
SECRETS_PATH = os.path.join(DATA_DIR, "secrets.json")
LOG_PATH = os.path.join(LOG_DIR, "sahara.log")
os.makedirs(LOG_DIR, exist_ok=True)

MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000

DEFAULT_CONFIG = {
    "symbol": "BTCUSDT",
    "category": "linear",
    "api_base": "https://api.bybit.com",
    "timezone": "Europe/Berlin",
    "timeframe": 30,
    "poll_seconds": 10,
    "account": {
        "start_balance": 100.0,
        "risk_pct": 2.0,
        "max_leverage": 25,
        "lot_rounding": True,
        "kelly_sizing": False,
        "kelly_fraction": 0.5,
        "kelly_min_trades": 30,
        "kelly_max_risk_pct": 6.0,
    },
    "fees": {
        "maker": 0.0002,
        "taker": 0.00055,
        "slippage": 0.0001,
        "include_funding": True,
        "funding_interval_hours": 8,
    },
    "instrument": {"qty_step": 0.001, "min_qty": 0.001, "tick": 0.1},
    "strategy": {
        "entry_n": 400,
        "trail_n": 400,
        "atr_n": 20,
        "stop_atr": 1.0,
        "impulse": 2.5,
        "vol_mult": 0.0,
        "vol_n": 100,
        "allow_long": True,
        "allow_short": True,
    },
    "optimizer": {
        "days": 1095,
        "combos": 200,
        "train_frac": 0.6,
        "min_trades": 25,
        "workers": 0,
        "space": {
            "entry_n": [150, 200, 300, 400, 500, 600, 800],
            "trail_mult": [0.5, 0.75, 1.0],
            "stop_atr": [0.75, 1.0, 1.25, 1.5, 2.0],
            "impulse": [0.0, 1.5, 2.0, 2.5, 3.0],
            "vol_mult": [0.0, 2.0, 3.0],
            "atr_n": [14, 20, 30],
        },
    },
    "discord": {"BTCUSDT": "", "errors": "", "notify_start": True},
    "web": {"port": 8761, "passcode": "", "autolock_minutes": 10},
    "demo": {"risk_pct": 2.0, "max_leverage": 25},
    "live": {"risk_pct": 2.0, "max_leverage": 25},
}

STRATEGY_KEYS = ("entry_n", "trail_n", "atr_n", "stop_atr", "impulse", "vol_mult", "vol_n", "allow_long",
                 "allow_short")


# ---------------------------------------------------------------- small helpers
def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def save_json(path, obj):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean_json(obj), f, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path, default=None):
    for _ in range(3):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except Exception:
            time.sleep(0.05)
    return default


def clean_json(o):
    """numpy / nan safe JSON"""
    if isinstance(o, dict):
        return {str(k): clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_json(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return clean_json(o.tolist())
    return o


def load_config():
    """config.json (created with defaults on first run) merged over the defaults."""
    raw = read_json(CONFIG_PATH, None)
    if raw is None:
        raw = {k: DEFAULT_CONFIG[k] for k in ("symbol", "timezone", "timeframe", "account", "fees", "strategy",
                                             "discord", "web")}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except Exception:
            pass
    return deep_merge(DEFAULT_CONFIG, raw)


def update_config(patch):
    """merge `patch` into config.json on disk (keeps everything else)."""
    raw = read_json(CONFIG_PATH, {}) or {}
    raw = deep_merge(raw, patch)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass
    return deep_merge(DEFAULT_CONFIG, raw)


def clean_params(raw, base=None):
    """validated strategy parameters"""
    p = dict(base or DEFAULT_CONFIG["strategy"])
    raw = raw or {}

    def num(k, lo, hi, cast):
        if k in raw and raw[k] is not None:
            try:
                p[k] = cast(min(hi, max(lo, float(raw[k]))))
            except Exception:
                pass

    num("entry_n", 10, 5000, int)
    num("trail_n", 5, 5000, int)
    num("atr_n", 5, 200, int)
    num("stop_atr", 0.2, 10, float)
    num("impulse", 0, 10, float)
    num("vol_mult", 0, 20, float)
    num("vol_n", 10, 1000, int)
    for k in ("allow_long", "allow_short"):
        if k in raw:
            p[k] = bool(raw[k])
    if "trail_mult" in raw and "trail_n" not in raw:
        p["trail_n"] = max(5, int(round(p["entry_n"] * float(raw["trail_mult"]))))
    return {k: p[k] for k in STRATEGY_KEYS}


def now_ms():
    return int(time.time() * 1000)


def tz(cfg):
    try:
        return ZoneInfo(cfg.get("timezone", "Europe/Berlin")) if ZoneInfo else timezone.utc
    except Exception:
        return timezone.utc


def tf_ms(cfg):
    return int(cfg.get("timeframe", 30)) * MIN


def fmt_ts(ms, cfg, f="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ms / 1000, tz(cfg)).strftime(f)


# ---------------------------------------------------------------- logging + discord
def _discord_post(url, payload):
    try:
        if isinstance(payload, str):
            payload = {"content": payload[:1990]}
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def discord_send(url, payload):
    if url and str(url).startswith("http"):
        threading.Thread(target=_discord_post, args=(url, payload), daemon=True).start()


class DiscordHandler(logging.Handler):
    """Every ERROR/CRITICAL record goes to the error webhook (identical messages max once / 5 min)."""

    def __init__(self, source):
        super().__init__(level=logging.ERROR)
        self.source, self._recent, self._lock = source, {}, threading.Lock()

    def emit(self, record):
        try:
            url = (read_json(CONFIG_PATH, {}) or {}).get("discord", {}).get("errors", "")
            if not url:
                return
            msg = record.getMessage()
            key = msg[:200]
            with self._lock:
                if time.time() - self._recent.get(key, 0) < 300:
                    return
                self._recent[key] = time.time()
            tb = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            body = msg + ("\n\n" + tb if tb else "")
            if len(body) > 1700:
                body = body[:500] + "\n...\n" + body[-1150:]
            discord_send(url, f"**🏜 SAHARA {self.source} | {record.levelname}** "
                              f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n```{body}```")
        except Exception:
            pass


def setup_logging(source):
    log = logging.getLogger("sahara")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [" + source + "] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    log.addHandler(DiscordHandler(source))
    import sys

    def hook(t, v, tb):
        log.critical("uncaught exception", exc_info=(t, v, tb))

    sys.excepthook = hook

    def thook(a):
        log.critical(f"uncaught exception in thread {a.thread.name if a.thread else '?'}",
                     exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

    threading.excepthook = thook
    return log


def push_event(ev, keep=400):
    """append to the small event feed shown on the website"""
    evs = read_json(EVENTS_PATH, []) or []
    evs.append(ev)
    save_json(EVENTS_PATH, evs[-keep:])


# ---------------------------------------------------------------- Bybit public data
class Bybit:
    def __init__(self, cfg):
        self.base = cfg["api_base"].rstrip("/")
        self.cat = cfg["category"]
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"sahara-paper/{VERSION}"

    def _get(self, path, params, tries=4):
        err = None
        for attempt in range(tries):
            try:
                r = self.s.get(self.base + path, params=params, timeout=15)
                r.raise_for_status()
                j = r.json()
                if j.get("retCode") != 0:
                    raise RuntimeError(f"retCode={j.get('retCode')} {j.get('retMsg')}")
                return j["result"]
            except Exception as e:
                err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Bybit request failed {path}: {err}")

    def klines(self, sym, interval, start=None, end=None, limit=1000, tries=4):
        p = {"category": self.cat, "symbol": sym, "interval": str(interval), "limit": limit}
        if start is not None:
            p["start"] = int(start)
        if end is not None:
            p["end"] = int(end)
        rows = self._get("/v5/market/kline", p, tries=tries).get("list", [])
        out = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]
        out.sort()
        return out

    def klines_range(self, sym, interval, start, end, progress=None):
        res, cur_end, total = {}, int(end), max(1, int(end) - int(start))
        while True:
            rows = self.klines(sym, interval, start=start, end=cur_end)
            if not rows:
                break
            for r in rows:
                res[r[0]] = r
            first = rows[0][0]
            if progress:
                progress(min(1.0, (end - first) / total))
            if first <= start or len(rows) < 1000:
                break
            cur_end = first - 1
            time.sleep(0.05)
        return [res[k] for k in sorted(res) if start <= k <= end]

    def funding_range(self, sym, start, end):
        res, cur_end = {}, int(end)
        while True:
            rows = self._get("/v5/market/funding/history",
                             {"category": self.cat, "symbol": sym, "startTime": int(start), "endTime": cur_end,
                              "limit": 200}).get("list", [])
            if not rows:
                break
            for r in rows:
                res[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
            oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
            if oldest <= start or len(rows) < 200:
                break
            cur_end = oldest - 1
            time.sleep(0.05)
        return res


class Store:
    """sqlite cache of CLOSED candles (any timeframe) and funding rates."""

    def __init__(self, api=None, path=None):
        self.api = api
        self.path = path or DB_PATH
        self.lock = threading.Lock()
        con = self._con()
        con.execute("CREATE TABLE IF NOT EXISTS candles (sym TEXT, tf INTEGER, ts INTEGER, o REAL, h REAL, l REAL, "
                    "c REAL, v REAL, PRIMARY KEY(sym, tf, ts))")
        con.execute("CREATE TABLE IF NOT EXISTS funding (sym TEXT, ts INTEGER, rate REAL, PRIMARY KEY(sym, ts))")
        con.commit()
        con.close()

    def _con(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def insert(self, sym, tf, rows, closed_before=None):
        lim = now_ms() if closed_before is None else closed_before
        rows = [(sym, int(tf)) + tuple(r) for r in rows if r[0] + tf * MIN <= lim]  # closed candles only
        if rows:
            with self.lock:
                con = self._con()
                con.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?)", rows)
                con.commit()
                con.close()
        return len(rows)

    def bounds(self, sym, tf):
        con = self._con()
        r = con.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE sym=? AND tf=?", (sym, int(tf))).fetchone()
        con.close()
        return r

    def ensure(self, sym, tf, start, end, progress=None):
        """download whatever is missing between start and end (needs the api)"""
        end = min(int(end), now_ms())
        mn, mx, _ = self.bounds(sym, tf)
        segs = []
        if mn is None:
            segs.append((start, end))
        else:
            if start < mn - tf * MIN:
                segs.append((start, mn - 1))
            if end > mx + 2 * tf * MIN:
                segs.append((mx, end))
        for s, e in segs:
            self.insert(sym, tf, self.api.klines_range(sym, tf, s, e, progress=progress))

    def load(self, sym, tf, start, end):
        con = self._con()
        rows = con.execute("SELECT ts,o,h,l,c,v FROM candles WHERE sym=? AND tf=? AND ts>=? AND ts<=? ORDER BY ts",
                           (sym, int(tf), int(start), int(end))).fetchall()
        con.close()
        return rows_to_data(rows)

    def tail(self, sym, tf, n, end=None):
        con = self._con()
        rows = con.execute("SELECT ts,o,h,l,c,v FROM candles WHERE sym=? AND tf=? AND ts<=? ORDER BY ts DESC LIMIT ?",
                           (sym, int(tf), int(end if end is not None else 1 << 62), int(n))).fetchall()
        con.close()
        return rows_to_data(rows[::-1])

    def insert_funding(self, sym, d):
        if d:
            with self.lock:
                con = self._con()
                con.executemany("INSERT OR REPLACE INTO funding VALUES(?,?,?)", [(sym, int(t), float(r)) for t, r in d.items()])
                con.commit()
                con.close()

    def ensure_funding(self, sym, start, end):
        con = self._con()
        mn, mx = con.execute("SELECT MIN(ts), MAX(ts) FROM funding WHERE sym=?", (sym,)).fetchone()
        con.close()
        segs = []
        if mn is None:
            segs.append((start, end))
        else:
            if start < mn - 8 * HOUR:
                segs.append((start, mn - 1))
            if end > mx + 8 * HOUR:
                segs.append((mx + 1, end))
        for s, e in segs:
            self.insert_funding(sym, self.api.funding_range(sym, s, e))

    def load_funding(self, sym, start, end):
        con = self._con()
        rows = con.execute("SELECT ts, rate FROM funding WHERE sym=? AND ts>=? AND ts<=?",
                           (sym, int(start), int(end))).fetchall()
        con.close()
        return {int(t): float(r) for t, r in rows}


def rows_to_data(rows):
    if not rows:
        return {"ts": np.array([], dtype=np.int64), **{k: np.array([]) for k in ("o", "h", "l", "c", "v")}}
    a = np.array(rows, dtype=float)
    return {"ts": a[:, 0].astype(np.int64), "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "v": a[:, 5]}


# ---------------------------------------------------------------- indicators (causal, fixed windows)
def _roll(a, n, fn):
    """fn over the window [i-n+1 .. i]; nan while the window is not full. Each window is computed on its own,
    so the value at candle i is bit-identical no matter where the array starts (live == backtest)."""
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        w = np.lib.stride_tricks.sliding_window_view(a, n)
        out[n - 1:] = fn(w, axis=1)
    return out


def _shift1(a):
    out = np.full(len(a), np.nan)
    out[1:] = a[:-1]
    return out


def indicators(d, p, cache=None):
    """All indicator arrays for candle data d. Index i only uses candles <= i."""
    h, l, c, v = d["h"], d["l"], d["c"], d["v"]

    def memo(key, fn):
        if cache is None:
            return fn()
        if key not in cache:
            cache[key] = fn()
        return cache[key]

    def _tr():
        pc = _shift1(c)
        tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
        if len(tr):
            tr[0] = h[0] - l[0]
        return tr

    tr = memo("tr", _tr)
    atr = memo(("atr", p["atr_n"]), lambda: _roll(tr, p["atr_n"], np.mean))
    hh = memo(("hh", p["entry_n"]), lambda: _shift1(_roll(h, p["entry_n"], np.max)))
    ll = memo(("ll", p["entry_n"]), lambda: _shift1(_roll(l, p["entry_n"], np.min)))
    tl = memo(("tl", p["trail_n"]), lambda: _roll(l, p["trail_n"], np.min))
    ts = memo(("ts", p["trail_n"]), lambda: _roll(h, p["trail_n"], np.max))
    if p.get("vol_mult", 0) > 0:
        vavg = memo(("va", p["vol_n"]), lambda: _roll(v, p["vol_n"], np.mean))
    else:
        vavg = np.zeros(len(c))
    return {"atr": atr, "hh": hh, "ll": ll, "tl": tl, "ts": ts, "vavg": vavg}


def warmup_bars(p):
    return int(max(p["entry_n"] + 1, p["trail_n"], p["atr_n"] + 1, p["vol_n"] if p.get("vol_mult", 0) > 0 else 0) + 5)


# ---------------------------------------------------------------- the engine
class Engine:
    """One account, one instrument, at most one position. Fed candle by candle.

    process(bar, ind, final)
      bar   = (ts, o, h, l, c, v); for final=False it is the candle that is still forming (live only)
      ind   = (atr, hh, ll, tl, ts, vavg) of that candle - only needed when final=True
    Fills and stops are decided the same way for forming and closed candles, so the live result of a
    candle is identical to what the backtester computes from the closed candle afterwards.
    """

    def __init__(self, params, cfg, start_balance=None, risk_pct=None, funding=None, trade_from=0):
        a, f, ins = cfg["account"], cfg["fees"], cfg["instrument"]
        self.p = dict(params)
        self.tf = tf_ms(cfg)
        self.taker, self.maker, self.slip = f["taker"], f["maker"], f.get("slippage", 0.0)
        self.use_funding = bool(f.get("include_funding", True))
        self.fund_iv = int(f.get("funding_interval_hours", 8)) * HOUR
        self.start_balance = float(a["start_balance"] if start_balance is None else start_balance)
        self.risk_pct = float(a["risk_pct"] if risk_pct is None else risk_pct)
        self.max_lev = float(a.get("max_leverage", 25))
        self.lot = bool(a.get("lot_rounding", True))
        self.qty_step, self.min_qty = float(ins["qty_step"]), float(ins["min_qty"])
        self.kelly = bool(a.get("kelly_sizing", False))
        self.kelly_frac = float(a.get("kelly_fraction", 0.5))
        self.kelly_min = int(a.get("kelly_min_trades", 30))
        self.kelly_max = float(a.get("kelly_max_risk_pct", 6.0))
        self.funding = funding  # {ts: rate} or None (live: resolved later by the trader)
        self.trade_from = int(trade_from)  # no new signals before this time (warm-up)
        self.balance = self.start_balance
        self.pos = None
        self.pending = None
        self.trades = []
        self.seen_ts = 0
        self.closed_ts = 0
        self.seq = 0
        self.fund_open = []  # funding payments whose rate is not known yet (live)
        self.events = []
        self.days = []  # [day_start_ms_utc, equity at the first candle of that day (mark to market)]
        self._rs = []  # R of closed trades (for Kelly)
        self.last = None  # last closed bar + indicators (status display)

    # ---- persistence
    KEYS = ("balance", "pos", "pending", "trades", "seen_ts", "closed_ts", "seq", "fund_open", "days",
            "start_balance", "trade_from")

    def export(self):
        return {k: getattr(self, k) for k in self.KEYS}

    def load(self, st):
        for k in self.KEYS:
            if k in st:
                setattr(self, k, st[k])
        self._rs = [t["r"] for t in self.trades]

    # ---- sizing
    def current_risk_pct(self):
        if self.kelly and len(self._rs) >= self.kelly_min:
            k = kelly(self._rs)["full"]
            return max(0.1, min(self.kelly_max, k * 100 * self.kelly_frac)) if k > 0 else 0.1
        return self.risk_pct

    def _ev(self, typ, ts, **kw):
        self.events.append({"type": typ, "ts": int(ts), **kw})

    # ---- main entry point
    def process(self, bar, ind=None, final=True):
        ts, o, h, l, c, v = bar
        if ts > self.seen_ts:  # ---------- first look at this candle: things that happen at its open
            if not self.days or ts // DAY > self.days[-1][0] // DAY:
                eq = self.balance + (self._upnl(o) if self.pos else 0.0)
                self.days.append([int(ts // DAY * DAY), eq])
            if self.pos and self.use_funding and ts % self.fund_iv == 0 and self.pos["entry_ts"] < ts:
                self._funding(ts, o)
            if self.pending:
                pd_ = self.pending
                self.pending = None
                if ts == pd_["signal_ts"] + self.tf:
                    self._open(pd_, ts, o)
                else:
                    self._ev("skip", ts, reason="entry candle missed (data gap or downtime)", dir=pd_["dir"])
            self.seen_ts = ts
        if self.pos:  # ---------- protective stop
            ps = self.pos
            d, st = ps["dir"], ps["stop"]
            px = None
            if d == 1:
                if ps["entry_ts"] != ts and o <= st:
                    px = o * (1 - self.slip)
                elif l <= st:
                    px = st * (1 - self.slip)
            else:
                if ps["entry_ts"] != ts and o >= st:
                    px = o * (1 + self.slip)
                elif h >= st:
                    px = st * (1 + self.slip)
            if px is not None:
                self._close(ts, px, "trail" if ps["trails"] else "stop")
            else:
                if (h - ps["entry"]) * d > ps["mfe"] or (l - ps["entry"]) * d > ps["mfe"]:
                    ps["mfe"] = max((h - ps["entry"]) * d, (l - ps["entry"]) * d)
                if (h - ps["entry"]) * d < ps["mae"] or (l - ps["entry"]) * d < ps["mae"]:
                    ps["mae"] = min((h - ps["entry"]) * d, (l - ps["entry"]) * d)
        if not final or ts <= self.closed_ts:
            return
        # ---------- the candle is closed: trail the stop / look for a new signal
        self.closed_ts = ts
        atr, hh, ll, tl, tsh, vavg = ind
        self.last = (bar, ind)
        if atr != atr or hh != hh or tl != tl:
            return
        p = self.p
        if self.pos:
            ps = self.pos
            if ps["dir"] == 1 and tl > ps["stop"] and tl < c:
                self._trail(ts, tl)
            elif ps["dir"] == -1 and tsh < ps["stop"] and tsh > c:
                self._trail(ts, tsh)
        elif self.pending is None and ts >= self.trade_from:
            d = 1 if c > hh else (-1 if c < ll else 0)
            if d and ((d == 1 and p["allow_long"]) or (d == -1 and p["allow_short"])):
                rng = (h - l) / atr if atr > 0 else 0.0
                ok = rng >= p["impulse"]
                if ok and p["vol_mult"] > 0:
                    ok = vavg > 0 and v >= p["vol_mult"] * vavg
                if ok:
                    self.pending = {"dir": d, "signal_ts": int(ts), "atr": float(atr), "stop_dist": float(p["stop_atr"] * atr),
                                    "level": float(hh if d == 1 else ll), "close": float(c), "rng": float(rng)}
                    self._ev("signal", ts, dir=d, level=self.pending["level"], close=float(c), rng=float(rng))

    # ---- internals
    def _upnl(self, price):
        ps = self.pos
        return (price - ps["entry"]) * ps["dir"] * ps["qty"] - ps["fee_in"] - ps["funding"]

    def equity(self, price):
        return self.balance + (self._upnl(price) if self.pos else 0.0)

    def _open(self, pd_, ts, o):
        d = pd_["dir"]
        entry = o * (1 + self.slip * d)
        dist = pd_["stop_dist"]
        risk_pct = self.current_risk_pct()
        risk_usd = self.balance * risk_pct / 100.0
        qty = risk_usd / dist if dist > 0 else 0.0
        capped = False
        if qty * entry > self.balance * self.max_lev:
            qty = self.balance * self.max_lev / entry
            capped = True
        if self.lot:
            qty = math.floor(qty / self.qty_step + 1e-9) * self.qty_step
            qty = round(qty, 8)
            if qty < self.min_qty:
                self._ev("skip", ts, reason=f"position size below the exchange minimum ({self.min_qty})", dir=d)
                return
        if qty <= 0 or self.balance <= 0:
            self._ev("skip", ts, reason="no balance", dir=d)
            return
        self.seq += 1
        stop = entry - d * dist
        self.pos = {"id": self.seq, "dir": d, "qty": qty, "entry": entry, "entry_ts": int(ts), "signal_ts": pd_["signal_ts"],
                    "stop": stop, "init_stop": stop, "risk_usd": qty * dist, "risk_pct": risk_pct,
                    "fee_in": qty * entry * self.taker, "funding": 0.0, "atr": pd_["atr"], "level": pd_["level"],
                    "bal_before": self.balance, "trails": 0, "mfe": 0.0, "mae": 0.0, "lev_capped": capped,
                    "stops": [[int(ts), stop]]}
        self._ev("open", ts, pos=dict(self.pos))

    def _trail(self, ts, new_stop):
        ps = self.pos
        old = ps["stop"]
        ps["stop"] = float(new_stop)
        ps["trails"] += 1
        ps["stops"].append([int(ts + self.tf), float(new_stop)])
        self._ev("trail", ts, id=ps["id"], dir=ps["dir"], old=old, new=float(new_stop), entry=ps["entry"],
                 qty=ps["qty"], locked_r=(new_stop - ps["entry"]) * ps["dir"] * ps["qty"] / ps["risk_usd"])

    def _funding(self, ts, price):
        ps = self.pos
        item = {"ts": int(ts), "id": ps["id"], "notional": ps["qty"] * price * ps["dir"]}
        if self.funding is not None:
            rate = self.funding.get(int(ts))
            if rate is not None:
                ps["funding"] += item["notional"] * rate
        else:
            self.fund_open.append(item)

    def resolve_funding(self, ts, rate):
        """live: the funding rate of `ts` became known"""
        rest = []
        for it in self.fund_open:
            if it["ts"] != ts:
                rest.append(it)
                continue
            cost = it["notional"] * rate
            if self.pos and self.pos["id"] == it["id"]:
                self.pos["funding"] += cost
            else:
                for t in reversed(self.trades):
                    if t["id"] == it["id"]:
                        t["funding"] += cost
                        t["pnl"] -= cost
                        t["r"] = t["pnl"] / t["risk_usd"] if t["risk_usd"] else 0.0
                        t["bal_after"] -= cost
                        self.balance -= cost
                        break
            self._ev("funding", ts, id=it["id"], rate=rate, cost=cost)
        self.fund_open = rest

    def _close(self, ts, px, reason):
        ps = self.pos
        d, q = ps["dir"], ps["qty"]
        gross = (px - ps["entry"]) * d * q
        fee_out = q * px * self.taker
        fees = ps["fee_in"] + fee_out
        pnl = gross - fees - ps["funding"]
        self.balance += pnl
        r = pnl / ps["risk_usd"] if ps["risk_usd"] else 0.0
        t = {"id": ps["id"], "dir": d, "qty": q, "entry": ps["entry"], "exit": px, "entry_ts": ps["entry_ts"],
             "exit_ts": int(ts), "signal_ts": ps["signal_ts"], "init_stop": ps["init_stop"], "stop": ps["stop"],
             "risk_usd": ps["risk_usd"], "risk_pct": ps["risk_pct"], "gross": gross, "fees": fees,
             "funding": ps["funding"], "pnl": pnl, "r": r, "reason": reason, "bal_before": ps["bal_before"],
             "bal_after": self.balance, "notional": q * ps["entry"], "lev": q * ps["entry"] / ps["bal_before"]
             if ps["bal_before"] > 0 else 0.0, "sl_pct": abs(ps["entry"] - ps["init_stop"]) / ps["entry"] * 100,
             "mfe_r": ps["mfe"] * q / ps["risk_usd"] if ps["risk_usd"] else 0.0,
             "mae_r": ps["mae"] * q / ps["risk_usd"] if ps["risk_usd"] else 0.0, "trails": ps["trails"],
             "stops": ps["stops"]}
        self.trades.append(t)
        self._rs.append(r)
        self.pos = None
        self._ev("close", ts, trade=t)

    def force_close(self, ts, price, reason="end"):
        if self.pos:
            self._close(ts, price * (1 - self.slip * self.pos["dir"]), reason)


def run_backtest(d, params, cfg, start_ms=0, funding=None, balance=None, risk_pct=None, cache=None, close_end=True):
    """d = candle arrays (must include the warm-up candles before start_ms). Returns the finished Engine."""
    ind = indicators(d, params, cache)
    eng = Engine(params, cfg, start_balance=balance, risk_pct=risk_pct, funding=funding if funding is not None else {},
                 trade_from=start_ms)
    ts, o, h, l, c, v = (d[k].tolist() for k in ("ts", "o", "h", "l", "c", "v"))
    ia = [ind[k].tolist() for k in ("atr", "hh", "ll", "tl", "ts", "vavg")]
    n = len(ts)
    i0 = 0
    if start_ms and n:
        i0 = max(0, int(np.searchsorted(d["ts"], start_ms)) - 1)
    proc = eng.process
    a0, a1, a2, a3, a4, a5 = ia
    for i in range(i0, n):
        proc((ts[i], o[i], h[i], l[i], c[i], v[i]), (a0[i], a1[i], a2[i], a3[i], a4[i], a5[i]), True)
    eng.events = []
    if close_end and eng.pos and n:
        eng.force_close(ts[-1], c[-1], "end of test")
        eng.trades[-1]["open_at_end"] = True
    eng.end_ts = ts[-1] + eng.tf if n else start_ms
    eng.end_price = c[-1] if n else 0.0
    return eng


# ---------------------------------------------------------------- statistics
def kelly(rs):
    """Kelly fraction of equity to risk per trade, from a list of R multiples"""
    rs = [r for r in rs if r == r]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    if not wins or not losses:
        return {"full": 0.0, "half": 0.0, "p": len(wins) / len(rs) if rs else 0.0, "b": 0.0, "n": len(rs)}
    p = len(wins) / len(rs)
    aw, al = sum(wins) / len(wins), sum(losses) / len(losses)
    b = aw / al if al > 0 else 0.0
    f = (p - (1 - p) / b) / al if b > 0 else 0.0  # fraction of equity lost when a trade loses 1 average loss
    f = max(0.0, f)
    return {"full": f, "half": f / 2, "p": p, "b": b, "n": len(rs)}


def _streaks(flags):
    bw = bl = cw = cl = 0
    for w in flags:
        if w:
            cw += 1
            cl = 0
        else:
            cl += 1
            cw = 0
        bw, bl = max(bw, cw), max(bl, cl)
    return bw, bl, cw, cl


def compute_stats(trades, cfg, start_balance, start_ms, end_ms, days_eq=None, open_pos=None, mark=None,
                  risk_pct=None, balance=None):
    """every number on the overview. trades = closed trades in exit order."""
    n = len(trades)
    bal = balance if balance is not None else (trades[-1]["bal_after"] if trades else start_balance)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    rs = [t["r"] for t in trades]
    gp, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    cum, peak, dd_r = 0.0, 0.0, 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd_r = max(dd_r, peak - cum)
    # equity curve: realised balance at every close (+ current mark-to-market point)
    curve = [[int(start_ms), start_balance]] + [[t["exit_ts"], t["bal_after"]] for t in trades]
    upnl = 0.0
    if open_pos and mark:
        upnl = (mark - open_pos["entry"]) * open_pos["dir"] * open_pos["qty"] - open_pos["fee_in"] - open_pos["funding"]
    equity = bal + upnl
    curve.append([int(end_ms), equity])
    pk, dd_pct, dd_usd = start_balance, 0.0, 0.0
    for _, e in curve:
        pk = max(pk, e)
        if pk > 0 and (pk - e) / pk * 100 > dd_pct:
            dd_pct, dd_usd = (pk - e) / pk * 100, pk - e
    # daily series (mark to market at the first candle of each UTC day)
    daily_dd = 0.0
    ds = [e for e in (days_eq or []) if e[0] >= start_ms - DAY]
    ds = ds + [[int(end_ms), equity]]
    for i in range(1, len(ds)):
        if ds[i - 1][1] > 0:
            daily_dd = max(daily_dd, (ds[i - 1][1] - ds[i][1]) / ds[i - 1][1] * 100)
        pk2 = max(e for _, e in ds[: i + 1])
        if pk2 > 0:
            dd_pct = max(dd_pct, (pk2 - ds[i][1]) / pk2 * 100)
    days = max((end_ms - start_ms) / DAY, 1e-9)
    growth = equity / start_balance if start_balance > 0 else 1.0
    cagr = (growth ** (365.0 / days) - 1) * 100 if days >= 1 and growth > 0 else 0.0
    geo_month = (growth ** (30.4375 / days) - 1) * 100 if days >= 1 and growth > 0 else 0.0
    months = month_returns(trades, cfg, start_balance, start_ms, end_ms)
    avg_month = sum(m["pct"] for m in months) / len(months) if months else 0.0
    bw, bl, cw, cl = _streaks([t["pnl"] > 0 for t in trades])
    aw_r = sum(t["r"] for t in wins) / len(wins) if wins else 0.0
    al_r = -sum(t["r"] for t in losses) / len(losses) if losses else 0.0
    fees = sum(t["fees"] for t in trades)
    fund = sum(t["funding"] for t in trades)
    k = kelly(rs)
    f = cfg["fees"]
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "winrate": len(wins) / n * 100 if n else 0.0,
        "cum_r": sum(rs), "max_dd_r": dd_r, "avg_r": sum(rs) / n if n else 0.0,
        "start_balance": start_balance, "balance": bal, "equity": equity, "upnl": upnl,
        "risk_pct": risk_pct if risk_pct is not None else cfg["account"]["risk_pct"],
        "fee_maker": f["maker"] * 100, "fee_taker": f["taker"] * 100, "slippage": f.get("slippage", 0) * 100,
        "days": days, "net_pnl": equity - start_balance, "net_pct": (growth - 1) * 100,
        "profit_factor": gp / gl if gl > 0 else (None if gp == 0 else 999.0),
        "annual_return": cagr, "avg_month": avg_month, "geo_month": geo_month, "months": len(months),
        "pos_months": sum(1 for m in months if m["pct"] > 0),
        "best_month": max((m["pct"] for m in months), default=0.0),
        "worst_month": min((m["pct"] for m in months), default=0.0),
        "avg_win": gp / len(wins) if wins else 0.0, "avg_loss": -gl / len(losses) if losses else 0.0,
        "avg_win_r": aw_r, "avg_loss_r": -al_r, "avg_rtr": aw_r / al_r if al_r > 0 else 0.0,
        "best_r": max(rs, default=0.0), "worst_r": min(rs, default=0.0),
        "max_dd_pct": dd_pct, "max_dd_usd": dd_usd, "max_daily_dd": daily_dd,
        "max_consec_wins": bw, "max_consec_losses": bl, "cur_wins": cw, "cur_losses": cl,
        "fees": fees, "funding": fund, "avg_fee": fees / n if n else 0.0,
        "avg_size": sum(t["notional"] for t in trades) / n if n else 0.0,
        "avg_qty": sum(t["qty"] for t in trades) / n if n else 0.0,
        "avg_lev": sum(t["lev"] for t in trades) / n if n else 0.0,
        "avg_hold_h": sum(t["exit_ts"] - t["entry_ts"] for t in trades) / n / HOUR if n else 0.0,
        "avg_hold_win_h": sum(t["exit_ts"] - t["entry_ts"] for t in wins) / len(wins) / HOUR if wins else 0.0,
        "avg_hold_loss_h": sum(t["exit_ts"] - t["entry_ts"] for t in losses) / len(losses) / HOUR if losses else 0.0,
        "avg_sl_pct": sum(t["sl_pct"] for t in trades) / n if n else 0.0,
        "longs": sum(1 for t in trades if t["dir"] == 1), "shorts": sum(1 for t in trades if t["dir"] == -1),
        "long_r": sum(t["r"] for t in trades if t["dir"] == 1), "short_r": sum(t["r"] for t in trades if t["dir"] == -1),
        "kelly_full": k["full"] * 100, "kelly_half": k["half"] * 100, "kelly_p": k["p"] * 100, "kelly_b": k["b"],
        "curve": curve, "r_curve": _r_curve(trades, start_ms),
    }


def _r_curve(trades, start_ms):
    out, cum = [[int(start_ms), 0.0]], 0.0
    for t in trades:
        cum += t["r"]
        out.append([t["exit_ts"], cum])
    return out


def _period_keys(ms, z):
    d = datetime.fromtimestamp(ms / 1000, z)
    iso = d.isocalendar()
    return (f"{d.year}", f"{d.year}-{d.month:02d}", f"{iso[0]}-W{iso[1]:02d}", f"{d.year}-{d.month:02d}-{d.day:02d}")


def month_returns(trades, cfg, start_balance, start_ms, end_ms):
    """calendar months (config timezone) between start and end, incl. months without trades"""
    z = tz(cfg)
    by = {}
    for t in trades:
        by.setdefault(_period_keys(t["exit_ts"], z)[1], []).append(t)
    out, bal = [], start_balance
    d = datetime.fromtimestamp(start_ms / 1000, z).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(max(end_ms - 1, start_ms) / 1000, z)
    while d <= end:
        key = f"{d.year}-{d.month:02d}"
        pnl = sum(t["pnl"] for t in by.get(key, []))
        out.append({"key": key, "start": bal, "pnl": pnl, "pct": pnl / bal * 100 if bal > 0 else 0.0,
                    "trades": len(by.get(key, []))})
        bal += pnl
        d = (d.replace(day=28) + timedelta(days=5)).replace(day=1)
    return out


def journal(trades, cfg, start_balance, start_ms, end_ms):
    """year -> month -> week -> day balances with parent links for drill-down.
    The hierarchy is year > month > day and, in parallel, week > day (ISO weeks cross month borders)."""
    z = tz(cfg)
    end_ms = max(end_ms, start_ms + 1)
    days = {}
    d = datetime.fromtimestamp(start_ms / 1000, z).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp((end_ms - 1) / 1000, z)
    order = []
    while d <= end:
        iso = d.isocalendar()
        key = f"{d.year}-{d.month:02d}-{d.day:02d}"
        days[key] = {"key": key, "y": f"{d.year}", "m": f"{d.year}-{d.month:02d}", "w": f"{iso[0]}-W{iso[1]:02d}",
                     "pnl": 0.0, "r": 0.0, "fees": 0.0, "n": 0, "wins": 0, "ids": []}
        order.append(key)
        d = (d + timedelta(days=1)).replace(hour=0)
    for t in trades:
        k = _period_keys(t["exit_ts"], z)[3]
        if k not in days:
            continue
        x = days[k]
        x["pnl"] += t["pnl"]
        x["r"] += t["r"]
        x["fees"] += t["fees"]
        x["n"] += 1
        x["wins"] += 1 if t["pnl"] > 0 else 0
        x["ids"].append(t["id"])
    out = {"day": [], "week": [], "month": [], "year": []}
    bal = start_balance
    agg = {"week": {}, "month": {}, "year": {}}
    for key in order:
        x = days[key]
        row = {"key": key, "start": bal, "end": bal + x["pnl"], "pnl": x["pnl"], "pct": x["pnl"] / bal * 100 if bal > 0 else 0.0,
               "r": x["r"], "n": x["n"], "wins": x["wins"], "fees": x["fees"], "week": x["w"], "month": x["m"], "year": x["y"]}
        out["day"].append(row)
        for lvl, pk in (("week", x["w"]), ("month", x["m"]), ("year", x["y"])):
            a = agg[lvl].get(pk)
            if a is None:
                a = agg[lvl][pk] = {"key": pk, "start": bal, "end": bal, "pnl": 0.0, "r": 0.0, "n": 0, "wins": 0, "fees": 0.0,
                                    "from": key, "to": key, "year": x["y"]}
                if lvl == "week":
                    a["month"] = x["m"]
                out[lvl].append(a)
            a["end"] = bal + x["pnl"]
            a["pnl"] += x["pnl"]
            a["r"] += x["r"]
            a["n"] += x["n"]
            a["wins"] += x["wins"]
            a["fees"] += x["fees"]
            a["to"] = key
        bal += x["pnl"]
    for lvl in ("week", "month", "year"):
        for a in out[lvl]:
            a["pct"] = a["pnl"] / a["start"] * 100 if a["start"] > 0 else 0.0
    return out


def bundle(eng, cfg, start_ms, end_ms, mark=None, risk_pct=None, with_trades=True):
    """everything the website needs to draw an account (live paper account or a backtest)"""
    st = compute_stats(eng.trades, cfg, eng.start_balance, start_ms, end_ms, days_eq=eng.days, open_pos=eng.pos,
                       mark=mark, risk_pct=risk_pct if risk_pct is not None else eng.risk_pct, balance=eng.balance)
    out = {"stats": st, "journal": journal(eng.trades, cfg, eng.start_balance, start_ms, end_ms),
           "months": month_returns(eng.trades, cfg, eng.start_balance, start_ms, end_ms),
           "open": eng.pos, "pending": eng.pending, "start_ms": int(start_ms), "end_ms": int(end_ms),
           "params": eng.p, "mark": mark}
    if with_trades:
        out["trades"] = eng.trades
    return out


# ---------------------------------------------------------------- discord trade messages
def fmt_discord(ev, cfg, balance=None):
    sym = cfg["symbol"]
    f = lambda x, n=1: f"{x:,.{n}f}"
    if ev["type"] == "open":
        p = ev["pos"]
        side = "🟢 LONG" if p["dir"] == 1 else "🔴 SHORT"
        return {"embeds": [{
            "title": f"🏜 SAHARA · {sym} · {side} opened",
            "color": 0x3FD8C4 if p["dir"] == 1 else 0xFF5A4D,
            "fields": [
                {"name": "Entry", "value": f"`{f(p['entry'])}`", "inline": True},
                {"name": "Stop", "value": f"`{f(p['stop'])}` ({abs(p['entry'] - p['stop']) / p['entry'] * 100:.2f}%)", "inline": True},
                {"name": "Size", "value": f"`{p['qty']:.3f}` BTC (${f(p['qty'] * p['entry'], 0)})", "inline": True},
                {"name": "Risk", "value": f"${p['risk_usd']:.2f} ({p['risk_pct']:.2f}%)", "inline": True},
                {"name": "Breakout level", "value": f"`{f(p['level'])}`", "inline": True},
                {"name": "Balance", "value": f"${p['bal_before']:.2f}", "inline": True}],
            "footer": {"text": fmt_ts(ev["ts"], cfg) + " · paper"}}]}
    if ev["type"] == "trail":
        return (f"🏜 **{sym}** trail #{ev['id']}: stop `{f(ev['old'])}` → `{f(ev['new'])}` "
                f"(locks {ev['locked_r']:+.2f} R)")
    if ev["type"] == "close":
        t = ev["trade"]
        win = t["pnl"] > 0
        return {"embeds": [{
            "title": f"🏜 SAHARA · {sym} · {'LONG' if t['dir'] == 1 else 'SHORT'} closed · {t['r']:+.2f} R",
            "color": 0x3FD8C4 if win else 0xFF5A4D,
            "fields": [
                {"name": "Entry → Exit", "value": f"`{f(t['entry'])}` → `{f(t['exit'])}`", "inline": True},
                {"name": "PnL", "value": f"**${t['pnl']:+.2f}** ({t['r']:+.2f} R)", "inline": True},
                {"name": "Reason", "value": t["reason"], "inline": True},
                {"name": "Fees / funding", "value": f"${t['fees']:.3f} / ${t['funding']:.3f}", "inline": True},
                {"name": "Held", "value": f"{(t['exit_ts'] - t['entry_ts']) / HOUR:.1f} h", "inline": True},
                {"name": "Balance", "value": f"${t['bal_after']:.2f}", "inline": True}],
            "footer": {"text": fmt_ts(ev["ts"], cfg) + " · paper"}}]}
    if ev["type"] == "skip":
        return f"🏜 **{sym}** signal skipped: {ev.get('reason', '')}"
    return None

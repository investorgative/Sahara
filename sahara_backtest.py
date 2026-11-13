#!/usr/bin/env python3
"""
SAHARA backtester + optimizer. Used by the website and from the command line:

  ./venv/bin/python sahara_backtest.py --days 365 --risk 2 --balance 100
  ./venv/bin/python sahara_backtest.py --start 2023-01-01 --end 2024-01-01
  ./venv/bin/python sahara_backtest.py --optimize --days 1095 --combos 200

The backtester feeds the same Engine as the paper trader, candle by candle (no look-ahead).
"""
import argparse
import itertools
import json
import multiprocessing as mp
import os
import random
import sys
import time
from datetime import datetime, timezone

import numpy as np

import sahara_core as core

OPT_PROGRESS = os.path.join(core.DATA_DIR, "optimizer_progress.json")
OPT_RESULT = os.path.join(core.DATA_DIR, "optimizer_result.json")
OPT_STOP = os.path.join(core.DATA_DIR, "optimizer_stop")
BT_RESULT = os.path.join(core.DATA_DIR, "backtest_result.json")
MAX_CHART_BARS = 20000


# ---------------------------------------------------------------- data
def load_data(cfg, start_ms, end_ms, params_list, progress=None, log=None, offline=False):
    """candles from (start - warm-up) to end, plus funding. Downloads what is missing."""
    sym, tf = cfg["symbol"], int(cfg["timeframe"])
    warm = max(core.warmup_bars(p) for p in params_list) * tf * core.MIN
    api = core.Bybit(cfg)
    store = core.Store(api)
    if not offline:
        try:
            store.ensure(sym, tf, start_ms - warm, end_ms, progress=progress)
            if cfg["fees"].get("include_funding", True):
                store.ensure_funding(sym, start_ms - core.DAY, end_ms)
        except Exception as e:
            if store.bounds(sym, tf)[2] == 0:
                raise
            if log:
                log.warning(f"backtest: download failed ({e}) - using the candles already in the database")
    d = store.load(sym, tf, start_ms - warm, end_ms)
    funding = store.load_funding(sym, start_ms - core.DAY, end_ms + core.DAY) if cfg["fees"].get("include_funding", True) else {}
    if len(d["ts"]) == 0:
        raise RuntimeError("no candles available for this period")
    # only closed candles
    keep = d["ts"] + tf * core.MIN <= max(end_ms, d["ts"][0])
    d = {k: v[keep] for k, v in d.items()}
    first = int(d["ts"][0])
    real_start = max(start_ms, first + warm) if first > start_ms - warm else start_ms
    return d, funding, int(real_start)


def chart_payload(d, ind, eng, start_ms, max_bars=MAX_CHART_BARS):
    """candles + strategy levels + trade markers for the website chart"""
    ts = d["ts"]
    i0 = int(np.searchsorted(ts, start_ms))
    sl = slice(i0, len(ts))
    n = len(ts) - i0
    k = max(1, int(np.ceil(n / max_bars)))
    T, O, H, L, C = ts[sl], d["o"][sl], d["h"][sl], d["l"][sl], d["c"][sl]
    hh, ll, tl, tsh = ind["hh"][sl], ind["ll"][sl], ind["tl"][sl], ind["ts"][sl]
    if k > 1:
        m = (n // k) * k
        off = n - m  # drop the oldest partial group
        rs = lambda a: a[off:].reshape(-1, k)
        T, O, H, L, C = rs(T)[:, 0], rs(O)[:, 0], rs(H).max(1), rs(L).min(1), rs(C)[:, -1]
        hh, ll, tl, tsh = rs(hh)[:, -1], rs(ll)[:, -1], rs(tl)[:, -1], rs(tsh)[:, -1]
    sec = (T // 1000).tolist()
    r = lambda a: [None if x != x else round(float(x), 1) for x in a]
    return {"tf_ms": int(eng.tf * k), "agg": k,
            "candles": [[sec[i], float(O[i]), float(H[i]), float(L[i]), float(C[i])] for i in range(len(sec))],
            "hh": r(hh), "ll": r(ll), "tl": r(tl), "ts": r(tsh)}


def run(cfg, days=365, risk_pct=None, balance=None, params=None, start_ms=None, end_ms=None, progress=None,
        log=None, with_chart=True, offline=False, kelly=None):
    """one backtest -> the same bundle the live paper account produces (+ chart)"""
    pr = progress or (lambda stage, x: None)
    cfg = core.deep_merge(cfg, {})
    if kelly is not None:
        cfg["account"]["kelly_sizing"] = bool(kelly)
    p = core.clean_params(params, cfg["strategy"])
    end_ms = int(end_ms or core.now_ms())
    start_ms = int(start_ms or end_ms - days * core.DAY)
    pr("loading candles", 0.02)
    d, funding, start_ms = load_data(cfg, start_ms, end_ms, [p], progress=lambda x: pr("downloading candles", 0.02 + 0.5 * x),
                                     log=log, offline=offline)
    pr("simulating", 0.6)
    t0 = time.time()
    cache = {}
    eng = core.run_backtest(d, p, cfg, start_ms=start_ms, funding=funding, balance=balance, risk_pct=risk_pct, cache=cache)
    end_real = int(d["ts"][-1]) + eng.tf
    pr("statistics", 0.9)
    res = core.bundle(eng, cfg, start_ms, end_real, mark=float(d["c"][-1]))
    res["meta"] = {"days": (end_real - start_ms) / core.DAY, "candles": int((d["ts"] >= start_ms).sum()),
                   "sim_seconds": round(time.time() - t0, 3), "finished": core.now_ms(), "symbol": cfg["symbol"],
                   "timeframe": cfg["timeframe"], "kelly_sizing": bool(cfg["account"].get("kelly_sizing")),
                   "balance": eng.start_balance, "risk_pct": eng.risk_pct,
                   "funding_records": len(funding), "lot_rounding": eng.lot}
    if with_chart:
        res["chart"] = chart_payload(d, core.indicators(d, p, cache), eng, start_ms)
    pr("done", 1.0)
    return res


# ---------------------------------------------------------------- optimizer
_G = {}


def _opt_init(d, funding, cfg, start_ms, split_ms, end_ms):
    _G.update(d=d, funding=funding, cfg=cfg, start=start_ms, split=split_ms, end=end_ms, cache={})


def _seg(trades, a, b):
    ts = [t for t in trades if a <= t["exit_ts"] < b]
    rs = [t["r"] for t in ts]
    months = max((b - a) / core.DAY / 30.4375, 1e-9)
    cum = peak = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r <= 0)
    return {"n": len(rs), "r": cum, "r_month": cum / months, "dd_r": dd, "pf": gp / gl if gl > 0 else 0.0,
            "wr": sum(1 for r in rs if r > 0) / len(rs) * 100 if rs else 0.0}


def score(train, test, full):
    """robust, drawdown-adjusted: the weaker of the two halves (R per month), divided by the square root of
    the worst drawdown (in units of 10 R). A parameter set must work in BOTH halves and not bleed too deep."""
    weak = min(train["r_month"], test["r_month"])
    return weak / (max(full["dd_r"], 10.0) / 10.0) ** 0.5 if weak > 0 else weak


def _opt_eval(raw):
    try:
        g = _G
        p = core.clean_params(raw, g["cfg"]["strategy"])
        if len(g["cache"]) > 60:
            tr = g["cache"].get("tr")
            g["cache"].clear()
            if tr is not None:
                g["cache"]["tr"] = tr
        eng = core.run_backtest(g["d"], p, g["cfg"], start_ms=g["start"], funding=g["funding"], cache=g["cache"])
        tr_, te_, al_ = (_seg(eng.trades, g["start"], g["split"]), _seg(eng.trades, g["split"], g["end"]),
                         _seg(eng.trades, g["start"], g["end"]))
        st = core.compute_stats(eng.trades, g["cfg"], eng.start_balance, g["start"], g["end"], days_eq=eng.days,
                                balance=eng.balance)
        return {"params": p, "train": tr_, "test": te_, "all": al_,
                "avg_month": st["avg_month"], "geo_month": st["geo_month"], "max_dd_pct": st["max_dd_pct"],
                "net_pct": st["net_pct"], "score": score(tr_, te_, al_)}
    except Exception as e:  # pragma: no cover
        return {"params": raw, "error": str(e)}


def sample_space(space, combos, base, seed=None):
    keys = sorted(space)
    total = 1
    for k in keys:
        total *= max(1, len(space[k]))
    rnd = random.Random(seed)
    seen, out = set(), []

    def add(c):
        key = tuple(c[k] for k in keys)
        if key not in seen:
            seen.add(key)
            out.append(c)

    if total <= combos:
        for vals in itertools.product(*[space[k] for k in keys]):
            add(dict(zip(keys, vals)))
    else:
        tries = 0
        while len(out) < combos and tries < combos * 50:
            add({k: rnd.choice(space[k]) for k in keys})
            tries += 1
    res = []
    for c in out:
        raw = dict(base)
        raw.update(c)
        if "trail_mult" in c:
            raw.pop("trail_n", None)
        res.append(raw)
    return res, total


def optimize(cfg, days=None, combos=None, progress_file=OPT_PROGRESS, result_file=OPT_RESULT, log=None, seed=None,
             offline=False, start_ms=None, end_ms=None, workers=None):
    oc = cfg["optimizer"]
    days = int(days or oc["days"])
    combos = int(combos or oc["combos"])
    end_ms = int(end_ms or core.now_ms())
    start_ms = int(start_ms or end_ms - days * core.DAY)
    t0 = time.time()

    def prog(stage, done=0, total=0, best=None):
        core.save_json(progress_file, {"running": stage not in ("done", "error", "stopped"), "stage": stage, "done": done,
                                       "total": total, "best": best, "started": int(t0 * 1000), "ts": core.now_ms(),
                                       "pid": os.getpid()})

    try:
        if os.path.exists(OPT_STOP):
            os.remove(OPT_STOP)
        prog("loading candles")
        base = core.clean_params(None, cfg["strategy"])
        cands, total_space = sample_space(oc["space"], combos, base, seed)
        cands = [dict(base)] + cands  # the current live parameters are always evaluated
        plist = [core.clean_params(c, cfg["strategy"]) for c in cands]
        d, funding, start_ms = load_data(cfg, start_ms, end_ms, plist, log=log, offline=offline)
        end_real = int(d["ts"][-1]) + core.tf_ms(cfg)
        split = int(start_ms + (end_real - start_ms) * float(oc.get("train_frac", 0.6)))
        nw = int(workers if workers is not None else oc.get("workers", 0)) or max(1, (os.cpu_count() or 2) - 1)
        rows, stopped = [], False
        prog("optimizing", 0, len(cands))
        ctx = mp.get_context("fork") if hasattr(os, "fork") else mp.get_context()
        with ctx.Pool(nw, initializer=_opt_init, initargs=(d, funding, cfg, start_ms, split, end_real)) as pool:
            for i, r in enumerate(pool.imap_unordered(_opt_eval, cands, chunksize=1)):
                if "error" not in r:
                    rows.append(r)
                if i % 3 == 0 or i == len(cands) - 1:
                    best = max(rows, key=lambda x: x["score"]) if rows else None
                    prog("optimizing", i + 1, len(cands), best and {"score": best["score"], "params": best["params"]})
                if os.path.exists(OPT_STOP):
                    stopped = True
                    pool.terminate()
                    break
        mt = int(oc.get("min_trades", 25))
        for r in rows:
            r["valid"] = r["all"]["n"] >= mt
        rows.sort(key=lambda x: (x["valid"], x["score"]), reverse=True)
        cur = core.clean_params(None, cfg["strategy"])
        current = next((r for r in rows if r["params"] == cur), None)
        ok = [r for r in rows if r["valid"]]
        res = {"finished": core.now_ms(), "seconds": round(time.time() - t0, 1), "start_ms": start_ms, "split_ms": split,
               "end_ms": end_real, "days": (end_real - start_ms) / core.DAY, "evaluated": len(rows),
               "space_size": total_space, "stopped": stopped, "min_trades": mt, "risk_pct": cfg["account"]["risk_pct"],
               "rows": rows[:60], "current": current,
               "summary": {"profitable_all": sum(1 for r in ok if r["all"]["r"] > 0), "valid": len(ok),
                           "profitable_both": sum(1 for r in ok if r["train"]["r"] > 0 and r["test"]["r"] > 0),
                           "median_r_month": float(np.median([r["all"]["r_month"] for r in ok])) if ok else 0.0,
                           "median_test_r_month": float(np.median([r["test"]["r_month"] for r in ok])) if ok else 0.0}}
        core.save_json(result_file, res)
        prog("stopped" if stopped else "done", len(rows), len(cands),
             rows and {"score": rows[0]["score"], "params": rows[0]["params"]})
        return res
    except Exception as e:
        core.save_json(progress_file, {"running": False, "stage": "error", "error": str(e), "ts": core.now_ms()})
        raise


# ---------------------------------------------------------------- command line
def _date(s):
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def print_report(res, cfg):
    s = res["stats"]
    z = lambda ms: core.fmt_ts(ms, cfg, "%Y-%m-%d")
    print(f"\n=== SAHARA backtest  {cfg['symbol']} {cfg['timeframe']}m   {z(res['start_ms'])} -> {z(res['end_ms'])}"
          f"  ({s['days']:.0f} days) ===")
    print(f"params: {res['params']}")
    print(f"balance ${s['start_balance']:.2f} -> ${s['equity']:.2f}   net {s['net_pct']:+.1f}%   risk/trade {s['risk_pct']}%"
          f"   fees maker {s['fee_maker']:.3f}% taker {s['fee_taker']:.3f}% slippage {s['slippage']:.3f}%")
    print(f"trades {s['trades']}  wins {s['wins']}  losses {s['losses']}  winrate {s['winrate']:.1f}%  PF "
          f"{(s['profit_factor'] or 0):.2f}")
    print(f"cum R {s['cum_r']:+.1f}   avg R {s['avg_r']:+.3f}   avg win {s['avg_win_r']:+.2f} R   avg loss {s['avg_loss_r']:+.2f} R"
          f"   best {s['best_r']:+.1f} R")
    print(f"AVG MONTH {s['avg_month']:+.2f}%  (compounded {s['geo_month']:+.2f}%/month, {s['pos_months']}/{s['months']} months"
          f" positive, best {s['best_month']:+.1f}%, worst {s['worst_month']:+.1f}%)   annual {s['annual_return']:+.0f}%")
    print(f"max DD {s['max_dd_pct']:.1f}% / {s['max_dd_r']:.1f} R   max daily DD {s['max_daily_dd']:.1f}%   max losing streak"
          f" {s['max_consec_losses']}")
    print(f"fees ${s['fees']:.2f}  funding ${s['funding']:.2f}  avg leverage {s['avg_lev']:.2f}x  avg SL {s['avg_sl_pct']:.2f}%"
          f"  avg hold {s['avg_hold_h']:.1f} h")
    print(f"Kelly: full {s['kelly_full']:.1f}%  half {s['kelly_half']:.1f}% of equity per trade"
          f"  (p={s['kelly_p']:.1f}%, payoff {s['kelly_b']:.1f})")
    print("months: " + "  ".join(f"{m['key'][2:]}:{m['pct']:+.0f}%" for m in res["months"]))


def main():
    ap = argparse.ArgumentParser(description="SAHARA backtester / optimizer")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--start", type=str, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", type=str, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--risk", type=float)
    ap.add_argument("--balance", type=float)
    ap.add_argument("--kelly", action="store_true", help="size positions with the Kelly criterion (trailing trades)")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--combos", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--workers", type=int)
    ap.add_argument("--offline", action="store_true", help="do not download, use the candle database as it is")
    ap.add_argument("--params", type=str, help='JSON, e.g. \'{"entry_n":300}\'')
    ap.add_argument("--json", type=str, help="write the full result to this file")
    a = ap.parse_args()
    cfg = core.load_config()
    log = core.setup_logging("optimizer" if a.optimize else "backtest")
    s_ms = _date(a.start) if a.start else None
    e_ms = _date(a.end) if a.end else None
    if s_ms and not e_ms:
        e_ms = core.now_ms()
    if a.optimize:
        try:
            res = optimize(cfg, days=a.days if (a.days != 365 or a.start) else None, combos=a.combos, log=log, seed=a.seed,
                           offline=a.offline, start_ms=s_ms, end_ms=e_ms, workers=a.workers)
        except Exception:
            log.exception("optimizer failed")
            sys.exit(1)
        sm = res["summary"]
        print(f"\n=== SAHARA optimizer: {res['evaluated']} combos in {res['seconds']}s, {res['days']:.0f} days, "
              f"train until {core.fmt_ts(res['split_ms'], cfg, '%Y-%m-%d')} ===")
        print(f"profitable on the whole period: {sm['profitable_all']}/{sm['valid']}   profitable in train AND test: "
              f"{sm['profitable_both']}/{sm['valid']}   median R/month {sm['median_r_month']:.2f}")
        print(f"{'score':>6} {'trainR/mo':>9} {'testR/mo':>9} {'allR':>7} {'ddR':>6} {'n':>4} {'avg mo%':>8} {'DD%':>6}  params")
        for r in res["rows"][:15]:
            p = r["params"]
            print(f"{r['score']:6.2f} {r['train']['r_month']:9.2f} {r['test']['r_month']:9.2f} {r['all']['r']:7.1f} "
                  f"{r['all']['dd_r']:6.1f} {r['all']['n']:4d} {r['avg_month']:8.2f} {r['max_dd_pct']:6.1f}  "
                  f"N={p['entry_n']} trail={p['trail_n']} stop={p['stop_atr']} imp={p['impulse']} vol={p['vol_mult']} atr={p['atr_n']}")
        return
    params = json.loads(a.params) if a.params else None
    res = run(cfg, days=a.days, risk_pct=a.risk, balance=a.balance, params=params, start_ms=s_ms, end_ms=e_ms, log=log,
              with_chart=bool(a.json), offline=a.offline, kelly=True if a.kelly else None)
    print_report(res, cfg)
    if a.json:
        core.save_json(a.json, res)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
SAHARA live paper trader (tmux session "Sahara").

  ./venv/bin/python sahara_trader.py            run
  ./venv/bin/python sahara_trader.py --reset    wipe the paper account and start again

Every `poll_seconds` it asks Bybit for the newest candles. Closed candles go through the same Engine the
backtester uses; the candle that is still forming is only used for things that are certain no matter how the
candle ends (entry at its open, stop hit). After a restart the trader replays every candle it missed.
"""
import argparse
import os
import sys
import time

import numpy as np

import sahara_core as core
import sahara_exec as ex

IND_KEYS = ("atr", "hh", "ll", "tl", "ts", "vavg")


class Trader:
    def __init__(self):
        self.cfg = core.load_config()
        self.log = core.setup_logging("trader")
        self.api = core.Bybit(self.cfg)
        self.store = core.Store(self.api)
        self.sym, self.tfm = self.cfg["symbol"], int(self.cfg["timeframe"])
        self.tf = self.tfm * core.MIN
        self.cfg_mtime = self._mtime()
        self.params = core.clean_params(None, self.cfg["strategy"])
        self.eng = core.Engine(self.params, self.cfg, funding=None)
        self.started = core.now_ms()
        self.forming = None
        self.fail = 0
        self.last_save = 0
        self.execs = [ex.Executor("demo", self.cfg, self.log), ex.Executor("live", self.cfg, self.log)]

    def _mtime(self):
        try:
            return os.path.getmtime(core.CONFIG_PATH)
        except Exception:
            return 0

    # ---------------------------------------------------------- state
    def save(self):
        core.save_json(core.STATE_PATH, {"version": core.VERSION, "started": self.started, "engine": self.eng.export(),
                                         "params": self.eng.p, "saved": core.now_ms()})
        self.last_save = time.time()

    def boot(self):
        st = core.read_json(core.STATE_PATH, None)
        need = core.warmup_bars(self.params)
        now = core.now_ms()
        self.store.ensure(self.sym, self.tfm, now - (need + 50) * self.tf, now)
        if st and st.get("engine"):
            self.eng.load(st["engine"])
            self.started = st.get("started", self.started)
            self.log.info(f"resumed: balance ${self.eng.balance:.2f}, {len(self.eng.trades)} closed trades, "
                          f"position: {'yes' if self.eng.pos else 'no'}")
            self.catch_up()
        else:
            last = self.store.tail(self.sym, self.tfm, 1)
            lt = int(last["ts"][-1]) if len(last["ts"]) else now // self.tf * self.tf - self.tf
            self.eng.closed_ts = self.eng.seen_ts = lt
            self.eng.trade_from = lt + self.tf
            self.started = now
            self.eng.days = [[int(now // core.DAY * core.DAY), self.eng.balance]]
            self.save()
            self.log.info(f"fresh paper account: ${self.eng.balance:.2f}, risk {self.eng.risk_pct}% per trade")
        if self.cfg["discord"].get("notify_start", True):
            core.discord_send(self.cfg["discord"].get(self.sym, ""),
                              f"🏜 **SAHARA** paper trader started · {self.sym} {self.tfm}m · balance "
                              f"${self.eng.balance:.2f} · risk {self.eng.risk_pct}%")

    def reload_config(self):
        m = self._mtime()
        if m == self.cfg_mtime:
            return
        self.cfg_mtime = m
        try:
            cfg = core.load_config()
            p = core.clean_params(None, cfg["strategy"])
            a = cfg["account"]
            if p != self.eng.p:
                self.log.info(f"config changed: strategy parameters now {p}")
                self.store.ensure(self.sym, self.tfm, core.now_ms() - (core.warmup_bars(p) + 50) * self.tf, core.now_ms())
            self.cfg, self.params = cfg, p
            e = self.eng
            e.p = dict(p)
            e.risk_pct = float(a["risk_pct"])
            e.max_lev = float(a.get("max_leverage", 25))
            e.lot = bool(a.get("lot_rounding", True))
            e.kelly, e.kelly_frac = bool(a.get("kelly_sizing", False)), float(a.get("kelly_fraction", 0.5))
            e.kelly_min, e.kelly_max = int(a.get("kelly_min_trades", 30)), float(a.get("kelly_max_risk_pct", 6.0))
            for x in self.execs:
                x.cfg = cfg
        except Exception:
            self.log.exception("could not reload config.json")

    # ---------------------------------------------------------- candles
    def _process_closed(self, upto_ts=None):
        """feed every stored closed candle after eng.closed_ts into the engine"""
        need = core.warmup_bars(self.eng.p)
        first_new = self.eng.closed_ts + self.tf
        d = self.store.load(self.sym, self.tfm, first_new - need * self.tf, upto_ts if upto_ts is not None else 1 << 62)
        ts = d["ts"]
        if not len(ts) or ts[-1] < first_new:
            return 0
        ind = core.indicators(d, self.eng.p)
        i0 = int(np.searchsorted(ts, first_new))
        n = 0
        for i in range(i0, len(ts)):
            bar = (int(ts[i]), float(d["o"][i]), float(d["h"][i]), float(d["l"][i]), float(d["c"][i]), float(d["v"][i]))
            self.eng.process(bar, tuple(float(ind[k][i]) for k in IND_KEYS), True)
            self.handle_events()
            n += 1
        return n

    def catch_up(self):
        n = self._process_closed()
        if n:
            self.log.info(f"caught up {n} candle(s) that closed while the trader was not running")
            self.save()

    def poll(self):
        rows = self.api.klines(self.sym, self.tfm, limit=5, tries=2)
        if not rows:
            return
        closed, forming = rows[:-1], rows[-1]
        if closed and closed[0][0] > self.eng.closed_ts + self.tf:  # more than the short window was missed
            self.store.ensure(self.sym, self.tfm, self.eng.closed_ts, core.now_ms())
        self.store.insert(self.sym, self.tfm, closed, closed_before=forming[0])
        changed = self._process_closed(upto_ts=forming[0] - 1) > 0
        self.forming = forming
        self.eng.process(tuple(forming), None, False)
        if self.handle_events() or changed:
            self.save()
        if self.eng.fund_open:
            self.resolve_funding()

    def resolve_funding(self):
        try:
            oldest = min(f["ts"] for f in self.eng.fund_open)
            rates = self.api.funding_range(self.sym, oldest - core.HOUR, core.now_ms())
            self.store.insert_funding(self.sym, rates)
            for f in list(self.eng.fund_open):
                if f["ts"] in rates:
                    self.eng.resolve_funding(f["ts"], rates[f["ts"]])
                elif core.now_ms() - f["ts"] > 3 * core.DAY:
                    self.eng.resolve_funding(f["ts"], 0.0)
            if self.handle_events():
                self.save()
        except Exception as e:
            self.log.warning(f"funding lookup failed: {e}")

    # ---------------------------------------------------------- events
    def handle_events(self):
        evs, self.eng.events = self.eng.events, []
        hook = self.cfg["discord"].get(self.sym, "")
        for ev in evs:
            t = ev["type"]
            if t == "open":
                p = ev["pos"]
                self.log.info(f"OPEN {'LONG' if p['dir'] == 1 else 'SHORT'} #{p['id']} qty {p['qty']} @ {p['entry']:.1f} "
                              f"stop {p['stop']:.1f} risk ${p['risk_usd']:.2f}")
            elif t == "close":
                x = ev["trade"]
                self.log.info(f"CLOSE #{x['id']} @ {x['exit']:.1f} ({x['reason']}) pnl ${x['pnl']:+.2f} = {x['r']:+.2f} R "
                              f"-> balance ${x['bal_after']:.2f}")
            elif t == "trail":
                self.log.info(f"TRAIL #{ev['id']} stop {ev['old']:.1f} -> {ev['new']:.1f} (locks {ev['locked_r']:+.2f} R)")
            elif t == "signal":
                self.log.info(f"SIGNAL {'LONG' if ev['dir'] == 1 else 'SHORT'}: close {ev['close']:.1f} broke {ev['level']:.1f} "
                              f"with a {ev['rng']:.2f}x ATR candle -> entering at the next open")
            elif t == "skip":
                self.log.warning(f"SKIP: {ev.get('reason')}")
            elif t == "funding":
                self.log.info(f"FUNDING #{ev['id']} rate {ev['rate'] * 100:.4f}% cost ${ev['cost']:+.4f}")
            if t != "funding":
                core.push_event(ev if t != "open" else {**ev, "pos": {k: v for k, v in ev["pos"].items() if k != "stops"}})
            msg = core.fmt_discord(ev, self.cfg)
            if msg:
                core.discord_send(hook, msg)
            for x in self.execs:
                x.on_event(ev)
        return bool(evs)

    # ---------------------------------------------------------- status for the website
    def write_status(self, error=None):
        e, p = self.eng, self.eng.p
        st = {"ts": core.now_ms(), "version": core.VERSION, "symbol": self.sym, "tf": self.tfm, "started": self.started,
              "balance": e.balance, "params": p, "risk_pct": e.current_risk_pct(), "error": error,
              "closed_ts": e.closed_ts, "pending": e.pending, "pos": None, "kelly_sizing": e.kelly}
        f = self.forming
        try:
            need = max(p["entry_n"], p["trail_n"], p["atr_n"] + 1, p["vol_n"]) + 2
            d = self.store.tail(self.sym, self.tfm, need)
            if f and len(d["ts"]) >= p["entry_n"]:
                ts, o, h, l, c, v = f
                hh, ll = float(d["h"][-p["entry_n"]:].max()), float(d["l"][-p["entry_n"]:].min())
                ind = core.indicators(d, p)
                atr = float(ind["atr"][-1])
                vavg = float(ind["vavg"][-1]) if p["vol_mult"] > 0 else 0.0
                rng = (h - l) / atr if atr > 0 else 0.0
                tl = float(min(d["l"][-(p["trail_n"] - 1):].min(), l)) if p["trail_n"] > 1 else l
                tsh = float(max(d["h"][-(p["trail_n"] - 1):].max(), h)) if p["trail_n"] > 1 else h
                st.update(price=c, bar=list(f), next_close=ts + self.tf, equity=e.equity(c),
                          levels={"hh": hh, "ll": ll, "atr": atr, "trail_long": tl, "trail_short": tsh, "vavg": vavg,
                                  "stop_dist": p["stop_atr"] * atr},
                          forming={"rng": rng, "vol": v / vavg if vavg > 0 else None, "above": c > hh, "below": c < ll,
                                   "to_hh_pct": (hh - c) / c * 100, "to_ll_pct": (c - ll) / c * 100})
                if e.pos:
                    ps = dict(e.pos)
                    ps["upnl"] = e._upnl(c)
                    ps["r_now"] = ps["upnl"] / ps["risk_usd"] if ps["risk_usd"] else 0.0
                    ps["locked_r"] = ((ps["stop"] - ps["entry"]) * ps["dir"] * ps["qty"] - ps["fee_in"]) / ps["risk_usd"]
                    ps["next_trail"] = tl if ps["dir"] == 1 else tsh
                    ps["notional"] = ps["qty"] * c
                    ps["lev"] = ps["notional"] / e.balance if e.balance > 0 else 0
                    st["pos"] = ps
        except Exception as x:
            st["error"] = st["error"] or f"status: {x}"
        core.save_json(core.STATUS_PATH, st)

    # ---------------------------------------------------------- main loop
    def run(self):
        self.boot()
        poll = max(3, int(self.cfg.get("poll_seconds", 10)))
        while True:
            t0 = time.time()
            err = None
            try:
                self.reload_config()
                self.poll()
                for x in self.execs:
                    x.tick(self.eng)
                if self.fail >= 3:
                    self.log.info("connection to Bybit is back")
                self.fail = 0
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self.fail += 1
                err = str(e)
                if self.fail == 3:  # one Discord error for an outage, not one per poll
                    self.log.exception("trader loop keeps failing")
                else:
                    self.log.warning(f"loop error ({self.fail}): {e}")
            self.write_status(err)
            if time.time() - self.last_save > 300:
                self.save()
            time.sleep(max(1.0, min(60, poll * (1 + min(self.fail, 5))) - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete the paper account (trades, state, events)")
    a = ap.parse_args()
    if a.reset:
        for p in (core.STATE_PATH, core.STATUS_PATH, core.EVENTS_PATH):
            if os.path.exists(p):
                os.remove(p)
        print("paper account wiped. Start again with ./start.sh")
        return
    t = Trader()
    try:
        t.run()
    except KeyboardInterrupt:
        t.save()
        print("stopped")


if __name__ == "__main__":
    main()

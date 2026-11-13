#!/usr/bin/env python3
"""
Import 1-minute candle archives into Sahara's candle database (optional - the bot downloads from Bybit by itself).

Useful for research on very long histories or on a machine that cannot reach api.bybit.com:
  * Binance USD-M futures archives  https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1m/
  * Binance funding archives        https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/
  * Bybit archives                  https://public.bybit.com/kline_for_metatrader4/BTCUSDT/

  python tools/import_candles.py --binance "raw/BTCUSDT-1m-*.zip" --funding "raw/fund-*.zip"
  python tools/import_candles.py --bybit-mt4 "bybit/*.csv.gz" --mt4-utc-offset 3
"""
import argparse, glob, gzip, io, os, sys, zipfile, csv
from datetime import datetime, timezone
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sahara_core as core


def read_binance(pattern):
    rows = []
    for f in sorted(glob.glob(pattern)):
        z = zipfile.ZipFile(f)
        for line in io.TextIOWrapper(z.open(z.namelist()[0])):
            p = line.split(",")
            if not p[0].isdigit():
                continue
            rows.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
    return rows


def read_bybit_mt4(pattern, utc_offset_h):
    rows = []
    for f in sorted(glob.glob(pattern)):
        for p in csv.reader(io.TextIOWrapper(gzip.open(f))):
            t = datetime.strptime(p[0], "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc)
            rows.append((int(t.timestamp() * 1000) - utc_offset_h * 3_600_000, float(p[1]), float(p[2]), float(p[3]),
                         float(p[4]), float(p[5])))
    return rows


def aggregate(rows, tf):
    a = np.array(sorted(set(rows)), dtype=float)
    ts = a[:, 0].astype(np.int64)
    g = ts // (tf * 60_000)
    idx = np.flatnonzero(np.r_[True, g[1:] != g[:-1]])
    out = []
    for s, e in zip(idx, np.r_[idx[1:], len(ts)]):
        out.append((int(g[s] * tf * 60_000), a[s, 1], a[s:e, 2].max(), a[s:e, 3].min(), a[e - 1, 4], a[s:e, 5].sum()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binance")
    ap.add_argument("--bybit-mt4")
    ap.add_argument("--mt4-utc-offset", type=int, default=3)
    ap.add_argument("--funding")
    ap.add_argument("--symbol")
    a = ap.parse_args()
    cfg = core.load_config()
    sym, tf = a.symbol or cfg["symbol"], int(cfg["timeframe"])
    store = core.Store(None)
    rows = []
    if a.binance:
        rows = read_binance(a.binance)
    if a.bybit_mt4:
        rows = read_bybit_mt4(a.bybit_mt4, a.mt4_utc_offset)
    if rows:
        bars = aggregate(rows, tf)
        bars = bars[:-1]  # the last group may be incomplete
        n = store.insert(sym, tf, bars)
        print(f"{sym}: imported {n} {tf}m candles  {core.fmt_ts(bars[0][0], cfg)} -> {core.fmt_ts(bars[-1][0], cfg)}")
    if a.funding:
        d = {}
        for f in sorted(glob.glob(a.funding)):
            z = zipfile.ZipFile(f)
            for line in io.TextIOWrapper(z.open(z.namelist()[0])):
                p = line.strip().split(",")
                if p[0].isdigit():
                    d[int(p[0]) // 3_600_000 * 3_600_000] = float(p[-1])
        store.insert_funding(sym, d)
        print(f"{sym}: imported {len(d)} funding records")


if __name__ == "__main__":
    main()

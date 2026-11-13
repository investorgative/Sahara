# 🏜 SAHARA — impulse-breakout trend trader for BTCUSDT (Bybit perpetual, 30m)

Paper trader + dashboard + backtester + optimizer for a Raspberry Pi. The paper trader and the backtester
run the **same** `Engine` class candle by candle, so a paper trade and a backtest trade come from the same code.

## The strategy in four lines
1. **Entry** – a 30m candle *closes* above the highest high (below the lowest low) of the previous **400 candles**
   (≈ 8 days) **and** that candle is an impulse: its range is ≥ **2.5 × ATR(20)**. Market order at the *next* candle's open.
2. **Stop** – entry ∓ **1.0 × ATR**. Position size = 2 % of the balance ÷ stop distance (typical stop 0.3–0.8 %, leverage 2–8×).
3. **Exit** – no take-profit. The stop trails along the lowest low (highest high) of the last 400 candles, moved only at candle closes.
4. Losers are cut after minutes or hours for about −1.25 R (1 R + fees). Winners run for days or weeks and pay 10–200 R.

All orders are market / stop orders, so the **taker fee (0.055 %) is charged on both sides**, plus 0.01 % slippage per
fill, plus real funding payments every 8 h. The maker fee (0.02 %) is in the fee model for limit orders, but this
strategy does not use any.

## What the backtest says
Research data: Binance USDT-perpetual 1-minute candles Jan 2020 – Nov 2026 aggregated to 30m (this development
machine cannot reach Bybit's API, and Bybit's public candle archive ends in Jan 2025). $100 start, 2 % risk, all
fees, slippage, funding, Bybit lot size (0.001 BTC).

| Period | Trades | Win rate | Total R | **Avg month** | Compounded / month | Green months | Max DD | $100 → |
|---|---|---|---|---|---|---|---|---|
| last 365 days | 42 | 11.9 % | +101 | **+15.0 %** | +9.9 % | 4 / 13 | 37.6 % | $311 |
| last 730 days | 77 | 11.7 % | +136 | **+9.8 %** | +5.8 % | 8 / 25 | 37.1 % | $390 |
| last 1095 days | 122 | 10.7 % | +177 | **+8.9 %** | +5.2 % | 11 / 37 | 58.4 % | $624 |
| 2020-01 → 2026-11 | 272 | 12.1 % | +783 | **+18.0 %** | +9.6 % | 27 / 83 | 64.0 % | $178,872 |

Per calendar year (each started with a fresh $100): 2020 +345 R · 2021 +54 R · 2022 +38 R · 2023 +138 R · 2024 +39 R ·
2025 +48 R · 2026 +96 R. No losing year.

**Read this before you trust the headline number**
* "Avg month" is the plain average of monthly returns. It clears 10 % over the last year and over the full history,
  but **not over the last 2 and 3 years (9.8 % and 8.9 %)**. The account's real growth rate is the compounded column: ~5–10 % a month.
* This is a lottery-shaped edge: **about 1 trade in 8 wins**, two months out of three are red, losing streaks of 13–33
  trades and **drawdowns of 40–65 %** are in the history. A few huge trades make the whole year.
* Walk-forward check (choose the best parameters on past years only, trade the next year, 2021–2026): **+294 R ≈ 4.2 R
  per month**, i.e. roughly 7–9 % a month at 2 % risk. Expect that, not the in-sample 18 %.
* Robustness: 288 of 288 parameter sets on a coarse grid and 301 of 301 random optimizer sets were profitable after
  costs over 2020–2026; 299 of 301 were profitable in both the older and the newer part. The default sits in the middle of that plateau.
* Bybit vs Binance candles on identical gap-free windows: Jan–Dec 2023 **+64.0 R vs +61.1 R**, Apr–Nov 2024 **−7.4 R vs −3.9 R**;
  71 of 77 trades entered on the same candle. Run the backtest on your Pi to get the numbers on Bybit's own candles.
* Past results do not guarantee anything. Paper first, then demo, then (maybe) small real money.

### Verified
* **No look-ahead:** the data was cut off at 40 random points; all 2,364 already-closed trades were identical to the full run.
* **Paper trader = backtester:** an accelerated live simulation (the real trader class, 31,536 polls with forming
  candles built from 1-minute data, 77 crashes/restarts with up to 6 h downtime) produced exactly the same 14 trades,
  prices, fees and funding as the backtester.
* **Executor:** tested against a signature-checking mock of the Bybit v5 API (entry, stop, trailing, close, reconcile).
  The live executor ignores an "armed" flag while `LIVE_ENABLED = False`.

## Files
| File | Purpose |
|---|---|
| `sahara_core.py` | config, Bybit data + sqlite cache, indicators, the Engine, statistics, journal, Kelly, Discord |
| `sahara_trader.py` | live paper trader — tmux session **Sahara** |
| `sahara_web.py` | dashboard + API on port **8761** — tmux session **SaharaWeb** |
| `sahara_backtest.py` | backtester + optimizer (website and command line) |
| `sahara_exec.py` | Bybit demo executor (and the locked live executor), API-key storage |
| `static/index.html` | the dashboard (vault lock screen, live, backtest, optimizer, control) |
| `setup.sh` / `start.sh` / `stop.sh` | install / start the 3 tmux sessions / stop |
| `tools/import_candles.py` | optional: import Binance/Bybit candle archives for long-history research |

## Setup on the Pi (Raspberry Pi OS Bookworm, 64-bit recommended)
```bash
cd ~
git clone https://github.com/investorgative/Sahara.git sahara
cd sahara
git checkout claude/nifty-ramanujan-95bvxl     # until this branch is merged into main
chmod +x setup.sh && ./setup.sh                # venv, packages, cloudflared, asks for the website passcode
./start.sh                                     # starts Sahara, SaharaWeb, SaharaCloud
```
Dashboard on your network: `http://<pi-ip>:8761`. The public `trycloudflare.com` address is printed by `start.sh` and
is behind the **PUBLIC** chip in the header (click = copy). A quick tunnel gets a new random address whenever
`SaharaCloud` restarts.

### Discord
Dashboard → **CONTROL** → Discord → paste the two webhooks → SAVE & TEST (or `nano config.json` → `"discord"`).
* `BTCUSDT` – every open, stop trail and close.
* `errors` – every ERROR from trader, web server, backtester and optimizer (same message at most once per 5 min).

Discord: Server settings → Integrations → Webhooks → New webhook → copy URL. One per channel.

### Daily use
```bash
tmux attach -t Sahara         # paper trader      (leave with Ctrl+B, then D)
tmux attach -t SaharaWeb      # web server
tmux attach -t SaharaCloud    # cloudflare tunnel (shows the public URL)
./stop.sh && ./start.sh       # restart everything
git pull && ./stop.sh && ./start.sh                                    # update
./stop.sh && ./venv/bin/python sahara_trader.py --reset && ./start.sh  # wipe the paper account
```
Autostart after reboot: `crontab -e`, add `@reboot sleep 30 && /home/<user>/sahara/start.sh`.

### Command line
```bash
./venv/bin/python sahara_backtest.py --days 365 --risk 2 --balance 100
./venv/bin/python sahara_backtest.py --days 365 --kelly
./venv/bin/python sahara_backtest.py --optimize --days 1095 --combos 200
```

## The website
* **Vault** – every visit and reload starts at the lock screen. The passcode (4–8 digits) is checked on the Pi; the page
  holds no data and every API call needs a token only a correct code hands out. From the 6th wrong code the wait
  escalates (1, 5, 15, 60 min). Auto-lock after 10 idle minutes (`web.autolock_minutes`). Change the code:
  `nano config.json` → `"web": {"passcode": "..."}` → restart.
* **Header** – ticker, live price, timeframe with candle countdown, clock and date, PAPER / DEMO / LIVE mode switch, bot heartbeat.
* **LIVE** – account overview and every statistic, equity curve ($ or R) with monthly bars, strategy status (what the
  bot waits for, which conditions are met), live chart with breakout levels, trades and stop paths, open position,
  event feed, journal (year → month → week → day, default: 4 weeks), trade log, log.
* **BACKTEST** – period, risk, balance, Kelly on/off; the result is shown in the same interface. First run downloads the candles from Bybit.
* **OPTIMIZER** – random search with a train/test split; every row can be backtested or applied to the paper trader.
* **CONTROL** – API keys for demo and live, arm/disarm/flatten for demo, risk and strategy settings, Discord webhooks.

### Kelly criterion
The backtester reports full and half Kelly from the trade list. With "Kelly sizing" on, each trade risks
`kelly_fraction` × Kelly computed **only from trades already closed** (starts after 30 trades, capped at
`kelly_max_risk_pct`). On the last year Kelly sizing did worse than fixed 2 % (deeper drawdown) — it is an option, not a recommendation.

### Demo and live trading
* **Demo:** create an API key inside Bybit *Demo Trading* (Contract: Orders + Positions), paste it under CONTROL, TEST
  CONNECTION, then ARM DEMO. Every paper trade is mirrored: market entry, stop-loss on the position, trailing, close.
  If Bybit rejects the stop, the position is closed immediately. Sized with the demo account's equity (`demo.risk_pct`).
* **Live:** the header switch, key storage and connection test are built. Execution is switched off in code
  (`LIVE_ENABLED = False` in `sahara_exec.py`). Use a key **without withdrawal permission**.
* Keys are stored in `data/secrets.json` (chmod 600), never sent back to the browser and never in git.

## Settings (`config.json`, created on first start)
`account.risk_pct` (2), `account.start_balance` (100), `account.max_leverage` (25), `account.lot_rounding` (true),
`fees.maker` (0.0002), `fees.taker` (0.00055), `fees.slippage` (0.0001), `fees.include_funding` (true),
`strategy.*` (entry_n 400, trail_n 400, atr_n 20, stop_atr 1.0, impulse 2.5, vol_mult 0), `optimizer.space`, `timezone` (Europe/Berlin).

Chart library: TradingView Lightweight Charts 4.1.3 (Apache-2.0), bundled in `static/`.

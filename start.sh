#!/usr/bin/env bash
# Starts SAHARA: paper trader (tmux "Sahara"), dashboard on :8761 ("SaharaWeb"),
# Cloudflare quick tunnel ("SaharaCloud"). Every process restarts itself if it crashes.
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || { echo "venv missing - run ./setup.sh first"; exit 1; }
PORT=$("$PY" -c "import sahara_core as c;print(c.load_config()['web'].get('port',8761))")
mkdir -p "$DIR/data/logs"
if ! tmux has-session -t Sahara 2>/dev/null; then
  tmux new-session -d -s Sahara "cd '$DIR' && while true; do '$PY' sahara_trader.py; echo 'trader exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session Sahara      (paper trader)"
else echo "tmux session Sahara already running"; fi
if ! tmux has-session -t SaharaWeb 2>/dev/null; then
  tmux new-session -d -s SaharaWeb "cd '$DIR' && while true; do '$PY' sahara_web.py; echo 'web exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session SaharaWeb   (dashboard http://localhost:$PORT)"
else echo "tmux session SaharaWeb already running"; fi
if command -v cloudflared >/dev/null 2>&1; then
  if ! tmux has-session -t SaharaCloud 2>/dev/null; then
    : > "$DIR/data/logs/cloudflared.log"
    tmux new-session -d -s SaharaCloud "cd '$DIR' && while true; do cloudflared tunnel --no-autoupdate --url http://localhost:$PORT 2>&1 | tee -a data/logs/cloudflared.log; echo 'tunnel exited - restarting in 10s'; sleep 10; done"
    echo "started tmux session SaharaCloud (quick tunnel - URL appears in a few seconds)"
    sleep 8
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$DIR/data/logs/cloudflared.log" | tail -1)
    [ -n "$URL" ] && echo "public URL: $URL" || echo "public URL: run 'tmux attach -t SaharaCloud' or look at the PUBLIC chip in the dashboard header"
  else echo "tmux session SaharaCloud already running"; fi
else
  echo "cloudflared not installed - tunnel not started (dashboard still works on the LAN)"
fi

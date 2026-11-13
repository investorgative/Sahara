#!/usr/bin/env bash
# SAHARA one-time setup (Raspberry Pi OS / Debian / Ubuntu)
set -e
cd "$(dirname "$0")"
echo "== system packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip tmux curl
echo "== python venv"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
mkdir -p data/logs
echo "== cloudflared (free quick tunnel)"
if ! command -v cloudflared >/dev/null 2>&1; then
  ARCH=$(dpkg --print-architecture)
  case "$ARCH" in arm64|amd64|armhf) ;; *) ARCH="";; esac
  if [ -n "$ARCH" ]; then
    curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" \
      && sudo dpkg -i /tmp/cloudflared.deb || echo "WARN: cloudflared install failed - install it yourself"
  else
    echo "WARN: unknown architecture - install cloudflared yourself"
  fi
fi
chmod +x start.sh stop.sh
[ -f config.json ] || ./venv/bin/python -c "import sahara_core; sahara_core.load_config()"
CUR=$(./venv/bin/python -c "import json;print(json.load(open('config.json')).get('web',{}).get('passcode',''))")
if [ -z "$CUR" ]; then
  while true; do
    read -r -s -p "Choose the website passcode (4-8 digits, input hidden): " PC; echo
    if [[ "$PC" =~ ^[0-9]{4,8}$ ]]; then break; fi
    echo "digits only, 4 to 8 of them"
  done
  PC="$PC" ./venv/bin/python - <<'PY'
import json, os
c = json.load(open("config.json")); c.setdefault("web", {})["passcode"] = os.environ["PC"]
json.dump(c, open("config.json", "w"), indent=2)
PY
  chmod 600 config.json
  echo "== passcode saved in config.json (this file is not in git)"
fi
echo
echo "Setup complete."
echo "1) ./start.sh   -> starts the tmux sessions Sahara, SaharaWeb, SaharaCloud"
echo "2) open the dashboard, tab CONTROL -> paste your Discord webhooks (or: nano config.json)"

#!/usr/bin/env bash
for s in Sahara SaharaWeb SaharaCloud; do
  tmux kill-session -t "$s" 2>/dev/null && echo "stopped $s" || echo "$s not running"
done

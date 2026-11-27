#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# If passing --list-chats or --test, run directly without caffeinate
if [[ "${1:-}" == "--list-chats" || "${1:-}" == "--test" ]]; then
  exec python3 ./bridge.py "$@"
fi

# Keep Mac from idle-sleeping while this dedicated terminal stays open
exec caffeinate -i python3 ./bridge.py "$@"

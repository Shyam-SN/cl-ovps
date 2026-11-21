#!/bin/zsh
set -e
cd "$(dirname "$0")/../.."
python3 web/aes_power_compare/server.py &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM
sleep 1
open "http://127.0.0.1:8765"
wait "$server_pid"

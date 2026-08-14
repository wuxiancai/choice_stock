#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; PID_FILE="$ROOT_DIR/data/choice-stock.pid"
[[ -f "$PID_FILE" ]] || { echo "服务未运行"; exit 0; }
PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then kill "$PID"; echo "已停止 PID $PID"; else echo "PID 文件已过期"; fi
rm -f "$PID_FILE"

#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
[[ -x .venv/bin/python ]] || { echo "请先执行 bash deploy.sh" >&2; exit 1; }
set -a; [[ -f .env ]] && source .env; set +a
HOST="${APP_HOST:-0.0.0.0}"; PORT="${APP_PORT:-8012}"; PID_FILE="data/choice-stock.pid"; LOG_FILE="data/choice-stock.log"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then echo "服务已运行，PID $(cat "$PID_FILE")"; exit 0; fi
mkdir -p data
nohup .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"; sleep 1
kill -0 "$(cat "$PID_FILE")" 2>/dev/null || { tail -80 "$LOG_FILE"; exit 1; }
echo "已启动：http://127.0.0.1:${PORT}（日志：${LOG_FILE}）"

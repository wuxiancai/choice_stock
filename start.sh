#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
[[ -x .venv/bin/python ]] || { echo "请先执行 bash deploy.sh" >&2; exit 1; }
set -a; [[ -f .env ]] && source .env; set +a
HOST="${APP_HOST:-0.0.0.0}"; PORT="${APP_PORT:-8012}"; PID_FILE="data/choice-stock.pid"; LOG_FILE="data/choice-stock.log"
mkdir -p data
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "服务已运行，PID ${PID}"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

LISTENER_PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
for LISTENER_PID in $LISTENER_PIDS; do
  LISTENER_CWD="$(lsof -a -p "$LISTENER_PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
  LISTENER_COMMAND="$(ps -p "$LISTENER_PID" -o command= 2>/dev/null || true)"
  if [[ "$LISTENER_CWD" == "$ROOT_DIR" && "$LISTENER_COMMAND" == *"uvicorn app.main:app"* ]]; then
    echo "$LISTENER_PID" > "$PID_FILE"
    echo "服务已运行，PID ${LISTENER_PID}"
    exit 0
  fi
  echo "端口 ${PORT} 已被 PID ${LISTENER_PID} 占用：${LISTENER_COMMAND}" >&2
  echo "请先停止该程序，或通过 APP_PORT 指定其他端口。" >&2
  exit 1
done

nohup .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
SERVER_PID=$!
sleep 1
kill -0 "$SERVER_PID" 2>/dev/null || { tail -80 "$LOG_FILE"; exit 1; }
LISTENER_PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
[[ " $LISTENER_PIDS " == *" $SERVER_PID "* ]] || { echo "服务未能监听端口 ${PORT}" >&2; tail -80 "$LOG_FILE"; exit 1; }
curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null || { echo "服务健康检查失败" >&2; tail -80 "$LOG_FILE"; exit 1; }
echo "$SERVER_PID" > "$PID_FILE"
echo "已启动：http://127.0.0.1:${PORT}（日志：${LOG_FILE}）"

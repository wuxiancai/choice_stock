#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "需要 Python 3.12+。请先安装 python3.12，或以 PYTHON_BIN=/path/to/python3.12 bash deploy.sh 指定。" >&2; exit 1
fi
"$PYTHON_BIN" - <<'PY'
import sys
assert sys.version_info >= (3, 12), f"当前版本 {sys.version.split()[0]}，需要 Python 3.12+"
PY
cd "$ROOT_DIR"
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p data
if [[ ! -f .env ]]; then cp .env.example .env; chmod 600 .env; echo "已创建 .env，请填入 TUSHARE_TOKEN（不会提交到 Git）。"; fi
chmod +x start.sh stop.sh deploy.sh
if [[ "$(uname -s)" == "Linux" ]] && command -v systemctl >/dev/null 2>&1; then
  echo "Ubuntu systemd 安装：sudo bash deploy.sh --systemd"
fi
if [[ "${1:-}" == "--systemd" ]]; then
  [[ "$(uname -s)" == "Linux" ]] || { echo "--systemd 仅适用于 Linux" >&2; exit 1; }
  INSTALL_DIR="${INSTALL_DIR:-/opt/choice-stock}"
  if [[ "$ROOT_DIR" != "$INSTALL_DIR" ]]; then
    echo "请先将项目放到 $INSTALL_DIR，或以 INSTALL_DIR=$ROOT_DIR sudo bash deploy.sh --systemd 安装服务。" >&2; exit 1
  fi
  sed "s|/opt/choice-stock|$INSTALL_DIR|g" deploy/systemd/choice-stock.service | sudo tee /etc/systemd/system/choice-stock.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now choice-stock.service
  echo "systemd 服务已启用：sudo systemctl status choice-stock"
fi
echo "部署完成。编辑 .env 后执行 ./start.sh"

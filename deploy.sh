#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

is_python_312_or_newer() {
  command -v "$1" >/dev/null 2>&1 && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1
}

privileged() {
  if [[ "$EUID" -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

install_python_312() {
  case "$(uname -s)" in
    Darwin)
      if ! command -v brew >/dev/null 2>&1; then
        echo "未找到 Homebrew，正在安装…"
        NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        if [[ -x /opt/homebrew/bin/brew ]]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
        if [[ -x /usr/local/bin/brew ]]; then eval "$(/usr/local/bin/brew shellenv)"; fi
      fi
      echo "正在通过 Homebrew 安装 Python 3.12…"
      brew install python@3.12
      PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"
      ;;
    Linux)
      [[ -r /etc/os-release ]] && . /etc/os-release || true
      if [[ "${ID:-}" != "ubuntu" && "${ID_LIKE:-}" != *"ubuntu"* ]]; then
        echo "当前 Linux 发行版未支持自动安装 Python 3.12；请设置 PYTHON_BIN 后重试。" >&2; exit 1
      fi
      echo "正在通过 apt 安装 Python 3.12 与构建依赖…"
      privileged apt-get update
      if ! apt-cache show python3.12 >/dev/null 2>&1; then
        privileged apt-get install -y software-properties-common
        privileged add-apt-repository -y ppa:deadsnakes/ppa
        privileged apt-get update
      fi
      privileged apt-get install -y python3.12 python3.12-venv python3.12-dev build-essential
      PYTHON_BIN="python3.12"
      ;;
    *) echo "不支持的系统：$(uname -s)" >&2; exit 1 ;;
  esac
}

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -n "$PYTHON_BIN" ]]; then
  if ! is_python_312_or_newer "$PYTHON_BIN"; then
    echo "PYTHON_BIN=$PYTHON_BIN 不是可用的 Python 3.12+ 解释器，改用自动安装。"
    PYTHON_BIN=""
  fi
else
  for candidate in python3.12 python3 python; do
    if is_python_312_or_newer "$candidate"; then PYTHON_BIN="$candidate"; break; fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then install_python_312; fi
if ! is_python_312_or_newer "$PYTHON_BIN"; then
  echo "Python 3.12 安装后仍不可用，请检查 PATH 或以 PYTHON_BIN 指定解释器。" >&2; exit 1
fi
echo "使用解释器：$($PYTHON_BIN --version)"
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

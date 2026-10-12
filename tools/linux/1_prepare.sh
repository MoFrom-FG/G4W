#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APP="$ROOT/runtime/app"
VENV="$APP/.venv"
WHEELS="$ROOT/runtime/wheels"
MIRROR="${G4W_PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"

# 优先使用包内便携解释器；缺失时回退系统 python3
if [ -x "$ROOT/runtime/python/bin/python3" ]; then
  BASE_PY="$ROOT/runtime/python/bin/python3"
else
  BASE_PY="$(command -v python3)"
  echo "[G4W] 提示：未找到 runtime/python，回退系统解释器 $BASE_PY"
fi
echo "[G4W] 基础解释器：$BASE_PY ($("$BASE_PY" -V 2>&1))"

if [ ! -x "$VENV/bin/python" ]; then
  echo "[G4W] 创建运行环境：$VENV"
  if ! "$BASE_PY" -m venv "$VENV" 2>/dev/null; then
    echo "[G4W] venv 不可用，改用 virtualenv"
    "$BASE_PY" -m pip install --user --break-system-packages -q -i "$MIRROR" virtualenv
    "$BASE_PY" -m virtualenv -q "$VENV"
  fi
fi
PY="$VENV/bin/python"
DEPS=("requests>=2.28" "beautifulsoup4>=4.12" "bottle>=0.12" "simple-websocket-server>=0.4" "aiohttp>=3.9" psutil)
if [ -d "$WHEELS" ] && ls "$WHEELS"/*.whl >/dev/null 2>&1; then
  echo "[G4W] 使用离线依赖：$WHEELS"
  "$PY" -m pip install -q --no-index --find-links "$WHEELS" "${DEPS[@]}"
else
  echo "[G4W] 从 PyPI 安装依赖：$MIRROR"
  "$PY" -m pip install -q -i "$MIRROR" "${DEPS[@]}"
fi
"$PY" -c "import requests, bs4, bottle, aiohttp, psutil, simple_websocket_server; print('[G4W] 依赖导入正常')"
source "$HERE/env.sh"
cd "$G4W_HOME"
"$G4W_PY" -m G4W prepare
echo "[G4W] 环境准备完成，下一步：tools/linux/2_key.sh"

#!/usr/bin/env bash
# G4W Linux 环境契约（所有脚本共用，不要直接执行）
G4W_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export G4W_ROOT
export G4W_HOME="$G4W_ROOT/runtime/G4W-main"
export G4W_APP_DIR="$G4W_ROOT/runtime/app"
export G4W_STATE_DIR="$G4W_ROOT/runtime/G4W-data"
export G4W_WORKSPACE_ROOT="$G4W_ROOT"
export G4W_VECTOR_INDEX_DIR="$G4W_ROOT/runtime/G4W-vector-index"
export PYTHONPATH="$G4W_HOME:$G4W_APP_DIR"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1

# 看板端口：环境变量 > runtime/dashboard-port 文件 > 默认 18180
if [ -z "${G4W_DASHBOARD_PORT:-}" ]; then
  if [ -f "$G4W_ROOT/runtime/dashboard-port" ]; then
    G4W_DASHBOARD_PORT="$(tr -d '[:space:]' < "$G4W_ROOT/runtime/dashboard-port")"
  fi
  export G4W_DASHBOARD_PORT="${G4W_DASHBOARD_PORT:-18180}"
fi
export G4W_DASHBOARD_HOST="${G4W_DASHBOARD_HOST:-127.0.0.1}"

G4W_PY="$G4W_APP_DIR/.venv/bin/python"
if [ ! -x "$G4W_PY" ]; then
  echo "[G4W] 运行环境不存在：$G4W_PY" >&2
  echo "[G4W] 请先执行 tools/linux/1_prepare.sh" >&2
  return 1 2>/dev/null || exit 1
fi
export G4W_PY

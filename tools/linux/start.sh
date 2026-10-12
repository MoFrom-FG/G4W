#!/usr/bin/env bash
# 主服务（Conductor+Worker）：默认前台；--bg 后台并写日志
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/env.sh"
cd "$G4W_HOME"
LOG="$G4W_ROOT/runtime/g4w.service.console.log"
if [ "${1:-}" = "--bg" ]; then
  echo "[G4W] 后台启动主服务，日志：$LOG"
  nohup "$G4W_PY" -u -m G4W start >>"$LOG" 2>&1 &
  echo $! >"$G4W_ROOT/runtime/g4w-main.pid"
  sleep 3
  bash "$HERE/status.sh"
  exit 0
fi
exec "$G4W_PY" -u -m G4W start

#!/usr/bin/env bash
# 看板：默认前台运行（能看到日志）；--bg 后台运行（写日志+pid，供验收/常驻）
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/env.sh"
cd "$G4W_HOME"
PORT="${G4W_DASHBOARD_PORT:-18180}"
HOST="${G4W_DASHBOARD_HOST:-127.0.0.1}"
URL="http://127.0.0.1:$PORT"
LOG="$G4W_ROOT/runtime/g4w-dashboard.log"
PIDF="$G4W_ROOT/runtime/g4w-dashboard.pid"
CMD=("$G4W_PY" -B -u -m G4W.dashboard.server --host "$HOST" --port "$PORT")

open_browser() {
  if command -v wslview >/dev/null 2>&1; then (wslview "$URL" >/dev/null 2>&1 &)
  elif command -v explorer.exe >/dev/null 2>&1; then (explorer.exe "$URL" >/dev/null 2>&1 &)
  elif command -v xdg-open >/dev/null 2>&1; then (xdg-open "$URL" >/dev/null 2>&1 &) ; fi
}

if [ "${1:-}" = "--bg" ]; then
  if curl -s -o /dev/null --max-time 2 "$URL/"; then echo "[G4W] 看板已在运行：$URL"; exit 0; fi
  echo "[G4W] 后台启动看板（bind $HOST:$PORT），日志：$LOG"
  nohup "${CMD[@]}" >>"$LOG" 2>&1 &
  echo $! >"$PIDF"
  for _ in $(seq 1 24); do sleep 0.5; curl -s -o /dev/null --max-time 2 "$URL/" && break; done
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL/" || echo 000)"
  echo "[G4W] 自检 HTTP：$CODE  （浏览器打开 $URL；Windows 侧同地址可直接访问）"
  [ "$CODE" = "200" ] && open_browser
  tail -3 "$LOG" 2>/dev/null
  exit 0
fi

echo "[G4W] 看板地址：$URL（Ctrl+C 停止；要常驻用 --bg）"
open_browser
exec "${CMD[@]}"

#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/env.sh"
cd "$G4W_HOME"
echo "[G4W] 停止主服务…"
"$G4W_PY" -m G4W stop || echo "[G4W] stop 返回非零（可能本来就没在跑）"
for pat in "G4W.dashboard.server"; do
  pids="$(pgrep -f "$pat" || true)"
  if [ -n "$pids" ]; then echo "[G4W] 停止看板 pid: $pids"; kill $pids 2>/dev/null || true; fi
done
rm -f "$G4W_ROOT/runtime/g4w-dashboard.pid" "$G4W_ROOT/runtime/g4w-main.pid"
sleep 1
PORT="${G4W_DASHBOARD_PORT:-18180}"
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"; then echo "[G4W] 警告：$PORT 仍有响应"; else echo "[G4W] 看板已停"; fi

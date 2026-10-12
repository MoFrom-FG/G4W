#!/usr/bin/env bash
# 保持 WSL 存活 + 看板守护（推荐：开机自启跑这个）
#   - 每 20 秒检查看板端口，无响应则自动重启看板服务
#   - 本进程不退出，使 WSL 发行版保持运行（否则空闲会被回收）
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/env.sh"
PORT="$G4W_DASHBOARD_PORT"
LOG="$G4W_ROOT/runtime/keepalive.log"
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >>"$LOG" 2>/dev/null || true; }

echo "[G4W] WSL 守护进程已启动（保持发行版运行 + 看板自愈），端口 $PORT"
echo "[G4W] 看板：http://127.0.0.1:$PORT/   （本窗口保持打开；日志 runtime/keepalive.log）"
log "keepalive 启动，端口 $PORT"

systemctl --user start g4w-dashboard.service >/dev/null 2>&1 || true
FAILS=0
while true; do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/" || echo 000)"
  if [ "$CODE" = "200" ]; then
    FAILS=0
  else
    FAILS=$((FAILS + 1))
    log "看板无响应（$CODE），第 $FAILS 次"
    if [ "$FAILS" -ge 2 ]; then
      if command -v systemctl >/dev/null 2>&1; then
        systemctl --user restart g4w-dashboard.service >/dev/null 2>&1 || true
      fi
      bash "$HERE/dashboard.sh" --bg >/dev/null 2>&1 || true
      log "已尝试重启看板服务"
      FAILS=0
    fi
  fi
  sleep 20
done

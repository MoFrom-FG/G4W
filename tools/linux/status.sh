#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/env.sh"
PORT="$G4W_DASHBOARD_PORT"
echo "[G4W] 根目录      : $G4W_ROOT"
printf "[G4W] 便携解释器  : "; if [ -x "$G4W_ROOT/runtime/python/bin/python3" ]; then "$G4W_ROOT/runtime/python/bin/python3" -V; else echo "缺失（回退系统 python3）"; fi
printf "[G4W] 应用环境    : "; [ -x "$G4W_PY" ] && echo "$G4W_PY" || echo "缺失（请跑 1_prepare.sh）"
pids="$(pgrep -f -- "G4W.dashboard.server" || true)"
if [ -n "$pids" ]; then echo "[G4W] 看板进程    : 运行中 ($(echo $pids | tr '\n' ' '))"; else echo "[G4W] 看板进程    : 未运行"; fi
pids="$(pgrep -f -- "m G4W start" || true)"
if [ -n "$pids" ]; then echo "[G4W] 主服务进程  : 运行中 ($(echo $pids | tr '\n' ' '))"; else echo "[G4W] 主服务进程  : 未运行（bash tools/linux/start.sh）"; fi
if command -v systemctl >/dev/null 2>&1; then
  echo "[G4W] systemd 单元: 看板=$(systemctl --user is-active g4w-dashboard.service 2>/dev/null || echo -) 主服务=$(systemctl --user is-active g4w-main.service 2>/dev/null || echo -)"
fi
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/" || echo 000)"
echo "[G4W] 看板 HTTP   : $CODE  http://127.0.0.1:$PORT/"
printf "[G4W] 模型密钥    : "; [ -f "$G4W_APP_DIR/mykey.py" ] && echo "已配置" || echo "未配置（bash tools/linux/2_key.sh）"
COUNT="$(ls "$G4W_STATE_DIR/accounts"/*.json 2>/dev/null | wc -l | tr -d ' ')"
printf "[G4W] 微信登录    : "; if [ "${COUNT:-0}" -gt 0 ]; then echo "已登录（$COUNT 个账号文件）"; else echo "未登录（bash tools/linux/4_login.sh）"; fi
[ -f "$G4W_ROOT/runtime/g4w-dashboard.log" ] && { echo "[G4W] 看板日志尾部:"; tail -4 "$G4W_ROOT/runtime/g4w-dashboard.log"; }

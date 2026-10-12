#!/usr/bin/env bash
# 注册 G4W 为 systemd 用户服务（WSL/Linux 通用）
#   install-systemd.sh                    只装看板（端口沿用 runtime/dashboard-port 或 18180）
#   install-systemd.sh --port 18181       指定看板端口
#   install-systemd.sh --with-main        同时装主服务
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR" "$ROOT/runtime"
PORT=""
WITH_MAIN="no"
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:-}"; shift 2 ;;
    --with-main) WITH_MAIN="yes"; shift ;;
    *) echo "[G4W] 未知参数：$1"; exit 2 ;;
  esac
done
if [ -n "$PORT" ]; then
  printf '%s\n' "$PORT" > "$ROOT/runtime/dashboard-port"
  echo "[G4W] 看板端口已设为 $PORT（写入 runtime/dashboard-port）"
fi

install_unit() {
  sed "s|__G4W_ROOT__|$ROOT|g" "$HERE/systemd/$1" > "$UNIT_DIR/$1"
  chmod 0644 "$UNIT_DIR/$1"
  echo "[G4W] 已写入 $UNIT_DIR/$1"
}
install_unit g4w-dashboard.service
[ "$WITH_MAIN" = "yes" ] && install_unit g4w-main.service

systemctl --user daemon-reload
if loginctl enable-linger "$USER" 2>/dev/null; then
  echo "[G4W] 已开启 linger（用户服务可随发行版启动自启）"
else
  echo "[G4W] 警告：enable-linger 失败，服务不会自动启动"
fi
systemctl --user enable g4w-dashboard.service
[ "$WITH_MAIN" = "yes" ] && systemctl --user enable g4w-main.service
systemctl --user reset-failed g4w-dashboard.service 2>/dev/null || true
systemctl --user restart g4w-dashboard.service
sleep 4
PORT_NOW="$(tr -d '[:space:]' < "$ROOT/runtime/dashboard-port" 2>/dev/null || echo 18180)"
echo -n "[G4W] 看板服务: "; systemctl --user is-active g4w-dashboard.service
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT_NOW/" || echo 000)"
echo "[G4W] 自检 HTTP：$CODE  http://127.0.0.1:$PORT_NOW/"
echo "[G4W] 管理：systemctl --user {status|restart|stop} g4w-dashboard.service ｜ 日志：journalctl --user -u g4w-dashboard.service -f"

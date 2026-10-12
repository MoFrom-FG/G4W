#!/usr/bin/env bash
# G4W Linux 便携包 —— 卸载服务注册（默认保留个人数据）
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[G4W] 停止并移除 systemd 用户服务…"
systemctl --user stop g4w-main.service g4w-dashboard.service 2>/dev/null || true
systemctl --user disable g4w-main.service g4w-dashboard.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/g4w-main.service" "$HOME/.config/systemd/user/g4w-dashboard.service"
systemctl --user daemon-reload 2>/dev/null || true
pkill -f -- "G4W.dashboard.server" 2>/dev/null || true
pkill -f -- "m G4W start" 2>/dev/null || true
echo "[G4W] 已停止进程并移除单元。"
echo "[G4W] 个人数据保留在：$HERE/runtime/G4W-data（含账号/记忆/待办）"
echo "[G4W] 彻底删除整包：rm -rf \"$HERE\""

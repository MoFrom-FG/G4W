#!/usr/bin/env bash
# G4W Linux 便携包 —— 安装（解压后执行一次）
#   ./install.sh            只准备运行环境
#   ./install.sh --systemd  额外注册 systemd 用户服务（看板+主服务，随系统自启）
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_SYSTEMD="no"
[ "${1:-}" = "--systemd" ] && WITH_SYSTEMD="yes"

echo "=================================================="
echo " G4W Linux 便携包"
echo " 安装位置：$HERE"
echo "=================================================="
bash "$HERE/tools/linux/1_prepare.sh"

if [ "$WITH_SYSTEMD" = "yes" ]; then
  bash "$HERE/tools/linux/install-systemd.sh" --with-main || true
fi

cat <<'TIP'

--------------------------------------------------
环境就绪。接下来（按顺序）：

  1) 填模型密钥      bash tools/linux/2_key.sh
  2) 填名字/称呼     bash tools/linux/3_env.sh
  3) 微信扫码登录    bash tools/linux/4_login.sh
  4) 启动看板        bash tools/linux/dashboard.sh --bg
                     （浏览器打开 http://127.0.0.1:<端口>，端口见 runtime/dashboard-port）
  5) 启动微信主体    bash tools/linux/start.sh --bg
  6) 随时看状态      bash tools/linux/status.sh
     全部停止       bash tools/linux/stop.sh

也可以直接开看板，在浏览器向导里完成 1)~3)：
     bash tools/linux/dashboard.sh --bg   →   http://127.0.0.1:<端口>

可选：向量语义检索（约 1-2 GB）
     bash tools/linux/5_embedding.sh

完整说明见 README-Linux.md
--------------------------------------------------
TIP

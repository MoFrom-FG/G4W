# G4W Linux 便携包 · 使用说明

> 与 Windows 便携包同体验：**双击/运行一个入口 → 浏览器里走完向导 → 直接进入看板**。
> 不需要手动建环境、不需要手动装依赖 —— 这些都在向导的「① 安装环境」里自动完成。

## 一、启动（唯一入口）

**Linux / WSL 终端**
```bash
cd G4W-Linux
./G4W
```

**Windows（WSL）双击**
```
tools\wsl\G4W.cmd            （可带参数：G4W.cmd ~/G4W-Linux Ubuntu）
```

两种方式的动作完全一样：

| 当前状态 | 启动器行为 |
|---|---|
| 还没有运行环境 | 启动**初始化向导**（浏览器自动打开），① 里一键装环境 |
| 已有运行环境 | 直接启动**看板** |

向导地址：`http://127.0.0.1:<端口>`（端口 = `runtime/dashboard-port`，缺省 18180）

## 二、向导四步（都在浏览器里）

| 步骤 | 说明 |
|---|---|
| ① 安装环境 | 自动创建**独立 venv**：`runtime/app/.venv`，并从包内 `runtime/wheels` 离线装依赖（缺失时走 PyPI 镜像）。进度实时显示在页面 |
| ② 配置 API Key | DeepSeek Key，写入 `runtime/app/mykey.py` |
| ③ 环境配置 | 你的名字 / 日常称呼 / 性别 / 机器人名字 / 默认模型 → 写入 `runtime/G4W-main/.env` |
| ④ 微信扫码登录 | 点「获取二维码」，手机微信扫码；登录成功后**自动切换到看板** |

> 未完成四步时，直接访问看板会被向导页拦住（本机调试可用 `/?skip_setup=1`）。
> 只想看板、暂时不配密钥：向导里先完成 ①，再 `http://127.0.0.1:<端口>/?skip_setup=1`。

## 三、日常操作

```bash
cd G4W-Linux

bash tools/linux/status.sh            # 一屏状态：解释器/venv/看板/主服务/HTTP/密钥/登录
bash tools/linux/start.sh --bg        # 启动微信主体（也可在看板「服务与终端」页点启动）
bash tools/linux/stop.sh              # 停止主服务 + 看板
bash tools/linux/dashboard.sh --bg    # 只重启看板
bash tools/linux/doctor.sh            # 环境自检
tail -f runtime/g4w-dashboard.log     # 看板日志
```

## 四、命令速查

| 目的 | 命令 |
|---|---|
| 启动（自动判断向导/看板） | `./G4W` |
| 配置密钥（终端方式） | `bash tools/linux/2_key.sh` |
| 配置环境（终端方式） | `bash tools/linux/3_env.sh` |
| 微信扫码（终端方式） | `bash tools/linux/4_login.sh` |
| 只准备环境（免浏览器） | `bash tools/linux/1_prepare.sh` |
| 改看板端口 | `bash tools/linux/install-systemd.sh --port 18181` |
| 向量语义检索（可选，Linux 适配中） | `bash tools/linux/5_embedding.sh` |

## 五、服务化（可选，随系统自启）

```bash
bash install.sh --systemd               # 安装环境 + 注册 systemd 用户服务（看板+主服务）
# 或环境已就绪时：
bash tools/linux/install-systemd.sh --with-main
systemctl --user status  g4w-dashboard.service
systemctl --user restart g4w-main.service
journalctl --user -u g4w-main.service -f
```

## 六、排错

| 现象 | 处理 |
|---|---|
| 浏览器打不开 | `wsl -l -v` 看发行版是否 Running；重跑 `./G4W` |
| 端口被占用/访问被拒 | `bash tools/linux/install-systemd.sh --port 18181` 换端口后重开 `./G4W` |
| 向导 ① 一直转 | 看 `runtime/wizard-prepare.log`（网络不畅时会走镜像重试） |
| 显示 `setup_required` | 说明四步没走完；查 `curl -s 127.0.0.1:<端口>/api/setup/status` |
| 微信不回消息 | `status.sh` 看密钥/登录是否 ✓；主服务需 `start.sh --bg` 或面板启动 |
| 看板过一阵掉线 | WSL 空闲会停机：保持 `./G4W` 窗口打开，或用 `tools/linux/keepalive.sh` |

## 七、注意

- 包放 **WSL 原生文件系统**（`~/G4W-Linux`），不要放 `/mnt/c`
- 同一微信账号不要在 Windows 版与 Linux 版同时登录（会互踢）
- `runtime/G4W-data`、`runtime/app/mykey.py`、`runtime/G4W-main/.env` 是个人运行数据，**不要进发布包**
- 目录结构（与 Windows 包一致）：
  ```
  G4W-Linux/
  ├── G4W                      # 启动器（向导/看板自动判断）
  ├── install.sh uninstall.sh  # 可选：命令行安装 / 服务化
  ├── tools/linux/*.sh         # 全部运维脚本
  ├── tools/wsl/G4W.cmd        # Windows 双击入口
  └── runtime/{python, wheels, app, G4W-main, G4W-data}
  ```

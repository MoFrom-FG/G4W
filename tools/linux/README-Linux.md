# G4W Linux 便携包 · 使用说明

> 与 Windows 便携包同体验：**运行一个入口 → 浏览器里走完向导 → 直接进入看板**。
> 依赖全部用包内离线 wheel 安装，环境由 G4W 自己维护，不动系统 Python。

## 零、解压后能直接用吗？

能。两条路，建议走第一条：

| 方式 | 命令 | 说明 |
|---|---|---|
| **推荐：一次装好** | `bash install.sh` | 用包内 `runtime/wheels` 的离线 wheel 建 venv；全量包还自带 CPython 3.12.14 → **不需要外网** |
| 也可以直接跑 | `./G4W` | 环境未就绪时自动打开浏览器向导，「① 安装环境」里一键装（同样优先用离线 wheel） |

两个包的区别：

| 包 | 需要宿主 python3.12 | 大小 | 说明 |
|---|---|---|---|
| `G4W-3.1.6-linux-x64.tar.gz` | 不需要（自带 CPython 3.12.14） | ~117 MB | 解压即用，推荐 |
| `G4W-3.1.6-linux-x64-slim.tar.gz` | **需要**（`python3.12` 在 PATH 或 `/usr/local/bin/python3`） | ~15 MB | 机器上已有 python3.12 时用这个 |

三步上手：

```bash
tar -xzf G4W-3.1.6-linux-x64.tar.gz
cd G4W-3.1.6-linux-x64
bash install.sh      # 约 1–2 分钟；不要用 sudo，venv 属于当前用户
./G4W                # 打开向导 / 直接进看板
```

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
- `runtime/G4W-data`、`runtime/app/mykey.py`、`runtime/G4W-main/.env` 是个人运行数据
- 目录结构（与 Windows 包一致）：

```
G4W-3.1.6-linux-x64/
├── G4W                  # 启动器（向导 / 看板）
├── install.sh           # 一次性安装环境（可 --systemd）
├── README-Linux.md      # 本文件
├── runtime/
│   ├── python/          # 自带 CPython（slim 包没有）
│   ├── wheels/          # 离线依赖
│   ├── app/             # GA 底座 + venv（安装后生成）
│   ├── G4W-main/        # G4W 主程序
│   └── G4W-data/        # 你的运行数据（首次运行生成）
└── tools/linux/         # 1_prepare / 2_key / 3_env / 4_login / start / stop / dashboard / status / doctor
```

## 八、把看板放到公网？先读这一段

**看板默认只监听 `127.0.0.1`（本机）**，这是刻意的安全默认：

- 它自带的是**单用户登录**，不是面向公网的防护；
- 默认 **HTTP 明文**，没有 TLS；
- `runtime/G4W-data` 里存着微信登录 token、记忆、聊天记录与日志 —— 看板一旦裸暴露，等于把这些一起端出去。

### 三种安全姿势（按省事程度排序）

1. **SSH 隧道（最推荐，零暴露）**

```bash
ssh -N -L 18180:127.0.0.1:18180 you@your-server
# 本机浏览器打开 http://127.0.0.1:18180
```

2. **反向代理 + HTTPS + 二次认证**
   - nginx / caddy 反代到 `127.0.0.1:18180`，强制 HTTPS；
   - 前面再挂一层口令 / 一次性验证码（nginx `auth_basic`、authelia 之类）；
   - 只放行自己的 IP：`allow <你的IP>; deny all;`。
3. **Cloudflare 隧道或橙云 + 源站证书**
   - `cloudflared tunnel` 最省事，源站不用开任何入站端口；
   - 用橙云代理时，SSL 模式设为 **Full (strict)**，并在源站装证书（Let's Encrypt 或 CF Origin Certificate）。

### 明确不要做的事

- ❌ 把 `18180` 直接映射到公网，或用 `--host 0.0.0.0` 且不配防火墙
- ❌ 在 HTTP 明文下登录看板
- ❌ 给「微信主服务」开入站端口 —— 它只需要**出网**（连 iLink），不需要任何入站

### 暴露前先做这几件事

```bash
ss -ltnp | grep 18180                  # 确认监听地址仍是 127.0.0.1
chmod 700 runtime/G4W-data runtime/app  # 数据与密钥目录只给自己
tail -n 100 runtime/g4w-dashboard.log   # 看看有没有异常登录尝试
```

- 登录失败排查看 `runtime/g4w-dashboard.log`；怀疑泄露就改看板口令并重启。
- **微信扫码登录需要交互控制台**（可能要输入验证码）：远程机器请 `ssh -t` 登录后跑 `bash tools/linux/4_login.sh`，或直接在浏览器向导第④步里扫。

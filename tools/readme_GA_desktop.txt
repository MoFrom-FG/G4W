注意：这是 G4W 内置底座 GenericAgent Desktop 的组件说明。
首次使用 G4W 请阅读根目录 README.md，并按 1—5 号脚本配置；不要把 GenericAgent.exe 当作 G4W 的主要启动入口。

================ 中文 ================
GenericAgent Desktop — Windows 便携版（自包含）

无需安装 Python、无需联网装依赖、无需源码仓库——全部已内置。

前置条件
- Windows 10/11 x64
- Microsoft Edge WebView2 运行时（Win11 一般自带；缺失时首次运行会提示安装）
- Windows terminal

使用
1. 解压到任意目录（路径建议不含特殊字符）。
2. 双击 GenericAgent.exe。
3. 首次启动会自动离线准备运行环境（建虚拟环境、装依赖），界面显示进度，完成后进入主界面。
4. 之后启动直接秒进。

说明
- 想真正对话，仍需在程序里配置模型 / API Key。
- 如果 Windows Defender 防火墙拦截 `127.0.0.1` / `localhost` 回环连接，程序可能无法启动或连接本机服务。请允许 `GenericAgent.exe` 和 `runtime\python\python.exe` 通过防火墙，然后重启桌面端。
- 首次准备完成后不要移动本文件夹；若已移动，删除 runtime\app\.venv 后重新启动即可重建。
- runtime\ 是内置运行环境与源码，正常使用无需改动。

================ English ================
GenericAgent Desktop — Windows Portable (self-contained)

No Python install, no internet for dependencies, no source checkout — everything is bundled.

Requirements
- Windows 10/11 x64
- Microsoft Edge WebView2 runtime (usually preinstalled on Win11; you are prompted if missing)
- Windows terminal

Usage
1. Extract anywhere (a path without special characters is recommended).
2. Double-click GenericAgent.exe.
3. The first launch prepares the runtime offline (creates a venv, installs deps) with a
   progress UI, then opens the main window.
4. Subsequent launches start instantly.

Notes
- To actually chat, configure a model / API key inside the app.
- If Windows Defender Firewall blocks the `127.0.0.1` / `localhost` loopback connection, the app may fail to start or reach its local service. Allow `GenericAgent.exe` and `runtime\python\python.exe` through the firewall, then restart the desktop app.
- Do not move this folder after the first setup; if you did, delete runtime\app\.venv and relaunch to rebuild.
- runtime\ holds the bundled runtime and source; no need to touch it.

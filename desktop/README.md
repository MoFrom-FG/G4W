# G4W 控制中心（桌面壳）

把看板封装成桌面应用：双击即用（WebView2，GA 便携包同款方案），托盘驻留，退出干净无残留。

## 使用（成品形态）

- 构建产物 `G4W.exe`（约 10-15MB）放在 **G4W 根目录**（与 `runtime\` 平级），双击即用
- 依赖：Win10/11 系统自带的 WebView2 运行时（无需安装任何东西）
- 行为：
  - 自动启动看板后端（无控制台窗口）→ 就绪后弹窗加载看板
  - 点 **X** = 最小化到系统托盘（看板继续后台运行）
  - 右键托盘图标 → **退出（关闭看板）** = 彻底退出并清理后端进程
  - 重复双击 = 提示已在运行（单实例锁）
  - 若 18180 已有手动启动的看板在跑 → 直接复用，退出时不关它

## 构建（仅开发者需要）

```bat
desktop\build.bat   ← 用 GA venv 里的 PyInstaller 构建，产物 desktop\dist\G4W.exe
```

源码（shell.py / loading.html / icon.ico / build.bat）随 G4W 发布；
exe 产物不进仓库，构建后放到便携包根目录。

## 开发调试

```bash
# 以 18190 端口避免干扰正在跑的看板（shell.py 直接跑，不走 exe）：
G4W_DASHBOARD_PORT=18190 runtime\app\.venv\Scripts\python.exe desktop\shell.py
```


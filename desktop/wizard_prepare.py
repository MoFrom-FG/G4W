# -*- coding: utf-8 -*-
"""向导 ①环境准备子进程（等价 tools\\1_prepare_G4W_ga.bat 的核心逻辑）。

用法：python wizard_prepare.py <G4W根目录>
输出：步骤进度到 stdout（向导页流式显示）；退出码 0=成功。
"""
import os
import subprocess
import sys

ROOT = os.path.abspath(sys.argv[1])
PY = os.path.join(ROOT, "runtime", "python", "python.exe")
VENV = os.path.join(ROOT, "runtime", "app", ".venv")
WHEELS = os.path.join(ROOT, "runtime", "wheels")
VENV_PY = os.path.join(VENV, "Scripts", "python.exe")

ENV = os.environ.copy()
ENV.update({
    "GA_APP_DIR": os.path.join(ROOT, "runtime", "app"),
    "G4W_HOME": os.path.join(ROOT, "runtime", "G4W-main"),
    "G4W_STATE_DIR": os.path.join(ROOT, "runtime", "G4W-data"),
    "G4W_WORKSPACE_ROOT": ROOT,
    "PYTHONPATH": ";".join([os.path.join(ROOT, "runtime", "G4W-main"),
                            os.path.join(ROOT, "runtime", "app")]),
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
})


def step(text):
    print(text, flush=True)


def run(cmd, **kw):
    step("$ " + " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, env=ENV, **kw)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    return result


def main():
    if not os.path.isfile(PY):
        print("[错误] 未找到内置 Python：runtime\\python\\python.exe", flush=True)
        return 1
    if not os.path.isdir(WHEELS):
        print("[错误] 未找到离线依赖目录：runtime\\wheels", flush=True)
        return 1
    if not os.path.isfile(VENV_PY):
        step("[1/3] 创建 GA 主环境…")
        run([PY, "-m", "venv", VENV])
    else:
        step("[1/3] GA 主环境已存在")
    step("[2/3] 安装离线依赖…")
    run([VENV_PY, "-m", "pip", "install", "--disable-pip-version-check",
         "--no-index", "--find-links", WHEELS,
         "requests>=2.28", "beautifulsoup4>=4.12", "bottle>=0.12",
         "simple-websocket-server>=0.4", "aiohttp>=3.9", "psutil"])
    step("[3/3] 初始化 G4W…")
    run([VENV_PY, "-B", "-m", "G4W", "prepare"], cwd=os.path.join(ROOT, "runtime", "G4W-main"))
    step("[完成] 环境准备就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())

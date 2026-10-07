# -*- coding: utf-8 -*-
"""G4W 时间线服务启动器 —— 看板「服务与终端」页的内建服务入口。

背景：这个入口以前指向 `runtime\\G4W-data\\timeline\\start_timeline_serve.py`。那是某个
安装里手工放进数据目录的脚本（硬编码了本机绝对路径、写死某个会话的 G4W-context.json，
而且绑 0.0.0.0）。`G4W-data` 按发布红线不进包，所以用户在服务页点「启动」必然失败
（找不到脚本）。现在入口改成包内模块：

    python -m G4W.features.timeline_serve

所有路径都从包位置与 Config/环境变量推导，不含任何机器相关常量；默认只绑 127.0.0.1
（要公网暴露请显式设 G4W_TIMELINE_HOST=0.0.0.0，并自己加认证层）。

环境变量：
    G4W_TIMELINE_HOST   监听地址，默认 127.0.0.1
    G4W_TIMELINE_PORT   监听端口，默认 18181
    G4W_TIMELINE_BUILD  设为 1 时启动前强制重建站点（默认仅在 index.html 缺失时构建）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _write_log(state_dir: Path, payload: dict) -> None:
    """把启动结果写进 <state_dir>/timeline/serve-startup.log（服务页展示的就是它）。"""
    try:
        path = state_dir / "timeline" / "serve-startup.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as exc:  # 日志失败绝不能影响服务本身
        print(f"[timeline-serve] log write failed: {type(exc).__name__}: {exc}", flush=True)


def main() -> int:
    from ..core.config import Config
    from ..core.records import TimelineStore
    from .timeline_publish import TimelinePublisher

    cfg = Config.load()
    state_dir = Path(cfg.state_dir)
    timeline_dir = state_dir / "timeline"
    store = TimelineStore(
        timeline_dir / "timeline-facts.json",
        state_dir / "legacy-import" / "timeline" / "timeline-facts.json",
    )
    publisher = TimelinePublisher(
        store, timeline_dir, locale=cfg.timeline_locale
    )   # theme 交给 publisher 在 build 时读 .env（默认 default，可在看板时间轴页切换）

    host = os.environ.get("G4W_TIMELINE_HOST") or "127.0.0.1"
    try:
        port = int(os.environ.get("G4W_TIMELINE_PORT") or 18181)
    except ValueError:
        port = 18181

    if os.environ.get("G4W_TIMELINE_BUILD") == "1" or not (timeline_dir / "site" / "index.html").is_file():
        try:
            built = publisher.build()
            print(f"[timeline-serve] site built: {built.get('siteDir')}", flush=True)
        except Exception as exc:
            print(f"[timeline-serve] site build skipped: {type(exc).__name__}: {exc}", flush=True)

    result = publisher.serve(host, port)
    result = dict(result or {})
    result["host"] = host
    result["port"] = port
    result["stateDir"] = str(state_dir)
    _write_log(state_dir, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)

    if not result.get("ok"):
        return 1
    # 常驻：看板通过端口 18181 判活
    while True:
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())

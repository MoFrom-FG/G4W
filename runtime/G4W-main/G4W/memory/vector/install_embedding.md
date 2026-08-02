# install_embedding · ST 外挂安装（TASK-D）

## 入口
- 批处理：在 G4W 产品根目录运行 `5_embedding_for_G4W.bat`（**先询问** y/N）
- Python：`python -m G4W.memory.vector.install_embedding`

## 固定版本（产品钉扎）
| 项 | 值 |
|----|-----|
| 后端 | `st`（Sentence-Transformers 薄 HTTP） |
| 模型 | `Qwen3-Embedding-0.6B` |
| dim | `1024` |
| 端口 | `8081`（`base_url=http://127.0.0.1:8081`，避开 CPA/8080） |
| 服务入口 | `server.py` + `.venv` + `start_embed.bat` |
| 体积提示 | 约 2–4 GB（torch + ST + 权重） |

## 目录布局（`runtime/G4W-embedding`）
```
G4W-embedding/
  server.py                 # 薄 HTTP（/health + /v1/embeddings）
  start_embed.bat           # embed_lifecycle 可发现（兼容旧名 start_tei.bat）
  .venv/                    # 独立 venv（不污染 GA）
  requirements.txt
  models/Qwen3-Embedding-0.6B/
  vector_config.json
  README.md
```

## 写 config 字段
安装路径写入（`write_config` / `mark_installed_if_ready`）：
- `installed`：仅当 venv+server（或 bat）+ model 就绪时可为 `true`；scaffold 为 `false`
- `enabled`：**始终保持 false**（开启走 `/vector on`）
- `model`, `dim`, `port`, `base_url`, `backend=st`
- `pid`, `embed_health` / `tei_health`：清空/null（不在安装时启动）

## CLI
```text
--yes              确认（bat 已询问后传入）
--dry-run          只打印动作，不写盘、不下载
--scaffold-only    目录 + server.py + start_embed.bat + installed=false
--mark-only        布局就绪则 installed=true
--port N           默认 8081
--root PATH        runtime 或 embedding 根覆盖（测试用）
```

## 不做
- 不在安装时启动长期 embed 服务
- 不在 CI 真下载大模型（无自动多 GB 拉取；需人工/后续扩展）
- 不装进 GA 主 `.venv`；不默认 Docker / TEI
- 不改 chat / LM Studio 路径；install 不把 `enabled` 设为 true

## 与生命周期关系
- 探测/启动：`embed_lifecycle.discover_embed_launchers` / `ensure_embed_running`
- 兼容别名：`tei_lifecycle` shim 仍 re-export 旧名一周期
- 总闸：`vector_config.vector_enabled()` = installed ∧ enabled

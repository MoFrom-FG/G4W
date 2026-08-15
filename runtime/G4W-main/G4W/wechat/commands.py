import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from G4W.knowledge.commands import handle_kb_command


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


COMMAND_GROUPS = [
    ("📁", "工作区与线程", [
        ("📍", "/bind", "绑定当前聊天到工作区目录"),
        ("📊", "/status", "查看当前工作区、线程、模型和上下文状态"),
        ("🆕", "/new", "切换到新的线程草稿"),
        ("🔄", "/reread", "让当前线程重新读取最新指令"),
        ("🔀", "/switch <threadId>", "切换到指定线程"),
        ("⏹️", "/stop", "停止当前线程里的运行任务"),
        ("⏰", "/checkin <min>-<max>", "设置主动 check-in 间隔，单位分钟"),
        ("📣", "/turn status, /turn on, /turn off", "查看、开启或关闭中间 turn 回复显示"),
        ("🐾", "/worker_turn status, /worker_turn on, /worker_turn off", "查看、开启或关闭 worker 中间进度汇报"),
        ("🧾", "/input status, /input on, /input off", "查看、开启或关闭完整LLM Input快照"),
        ("🧩", "/chunk <number>", "调整微信短回复合并的最小字符数"),
    ]),
    ("👤", "个人配置", [
        ("👤", "/name <userName>", "设置或查看用户名字"),
        ("🎭", "/identity <identity>", "设置或查看用户身份/日常称呼"),
        ("👤", "/gender <female|male|neutral>", "设置或查看用户性别"),
("📋", "/todo add|list|done|del|cancel", "管理个人任务清单(checkin 时主动提醒)"),
        ("👤", "/botname <botName>", "设置或查看机器人名字"),
    ]),
    ("⚡️", "能力", [
        ("🤖", "/model", "查看当前模型"),
        ("🤖", "/model <id>", "切换到指定模型"),
        ("❓", "/help", "显示当前微信可用指令"),
        ("🗜️", "/l4compress", "手动触发 L4 长程记忆深压"),
        ("📚", "/kb list|remove 编号|rebuild", "管理独立知识库文档"),
        ("🧲", "/vector status|on|off|meta|rebuild", "向量外挂总闸；meta 指纹；rebuild 后台从 L4 重建索引"),
    ]),
]


def parse_command(text: str):
    normalized = str(text or "").strip()
    if not normalized.startswith("/"):
        return None
    head, _, tail = normalized[1:].partition(" ")
    return head.strip().lower(), tail.strip()


def help_text() -> str:
    lines = ["💡 可用指令："]
    for group_emoji, group_name, commands in COMMAND_GROUPS:
        lines.extend(["", f"{group_emoji} 【{group_name}】"])
        lines.extend(f"  {emoji} {command} - {summary}" for emoji, command, summary in commands)
    return "\n".join(lines)


_VECTOR_BAT_HINT = (
    "尚未安装向量外挂。请在 G4W 产品根目录运行 "
    "`5_embedding_for_G4W.bat`，"
    "安装完成后再执行 /vector on。"
)


def _import_vector_config_api():
    """Import frozen TASK-A public API. Raises ImportError/AttributeError if A not ready."""
    import importlib
    import sys

    name = "G4W.memory.vector.vector_config"
    mod = sys.modules.get(name)
    if mod is None:
        mod = importlib.import_module(name)
    for attr in ("load_config", "vector_enabled", "set_vector_enabled"):
        if not hasattr(mod, attr):
            raise AttributeError(f"vector_config missing {attr}")
    return mod


def _import_stop_tei():
    """Prefer embed_lifecycle.stop_embed; then tei/vector_config stop_tei; else no-op."""
    import importlib
    import sys

    for name, attr in (
        ("G4W.memory.vector.embed_lifecycle", "stop_embed"),
        ("G4W.memory.vector.tei_lifecycle", "stop_tei"),
        ("G4W.memory.vector.tei_lifecycle", "stop_embed"),
        ("G4W.memory.vector.vector_config", "stop_tei"),
    ):
        try:
            mod = sys.modules.get(name)
            if mod is None:
                mod = importlib.import_module(name)
            stop = getattr(mod, attr, None)
            if callable(stop):
                return stop
        except Exception:
            continue
    return lambda: {
        "ok": True,
        "stopped": False,
        "reason": "stop_embed placeholder (lifecycle not merged)",
    }


def format_vector_status(cfg: dict, effective_enabled: bool) -> str:
    """Human-readable /vector status from config dict + effective gate + index meta."""
    import os as _os

    path = cfg.get("path") or cfg.get("config_path") or ""
    if not path:
        try:
            vc = _import_vector_config_api()
            path = str(vc.config_path())
        except Exception:
            path = "(unknown)"
    env_raw = str(_os.environ.get("G4W_VECTOR_ADDON", "") or "").strip()
    env_kill = env_raw.lower() in ("0", "false", "off", "no", "disable", "disabled")
    gate_note = "installed 且 enabled"
    if env_kill:
        gate_note = f"env G4W_VECTOR_ADDON={env_raw or '0'} 强制关（覆盖 json）"
    health = cfg.get("embed_health")
    if health is None:
        health = cfg.get("tei_health")
    lines = [
        "🧲 向量外挂状态",
        f"有效总闸：{'开' if effective_enabled else '关'}（{gate_note}）",
        f"enabled：{bool(cfg.get('enabled'))}",
        f"installed：{bool(cfg.get('installed'))}",
        f"backend：{cfg.get('backend') or 'st'}",
        f"model：{cfg.get('model') or '-'}",
        f"dim：{cfg.get('dim') if cfg.get('dim') is not None else '-'}",
        f"base_url：{cfg.get('base_url') or '-'}",
        f"port：{cfg.get('port') if cfg.get('port') is not None else '-'}",
        f"pid：{cfg.get('pid') if cfg.get('pid') is not None else '-'}",
        f"embed_health：{health if health is not None else '-'}",
        f"tei_health：{cfg.get('tei_health') if cfg.get('tei_health') is not None else '-'}（compat）",
        f"config path：{path}",
        f"updated_at：{cfg.get('updated_at') or '-'}",
    ]
    if env_kill:
        lines.append(
            f"环境覆盖：G4W_VECTOR_ADDON={env_raw or '0'} → 有效总闸强制关"
            "（即使 json 中 installed∧enabled=true）"
        )
    if not cfg.get("installed"):
        lines.append(f"提示：{_VECTOR_BAT_HINT}")
        # The minimal GA environment intentionally keeps numpy and the vector
        # index stack out of the main venv.  With no installed addon there can
        # be no usable index metadata, so importing the heavy metadata/rebuild
        # modules only produces a misleading "No module named numpy" warning.
        lines.append("index meta：skipped（向量外挂尚未安装）")
        return "\n".join(lines)

    # Live probe: config fields (pid/embed_health) are historical snapshots and
    # must NOT be trusted as current state. Probe port, process and a real
    # inference request so a dead/half-dead server is reported as such.
    try:
        from G4W.memory.vector.embed_lifecycle import (
            _pid_alive,
            _pids_listening_on_port,
            embed_health,
        )

        port = cfg.get("port")
        try:
            port_i = int(port) if port is not None else 0
        except (TypeError, ValueError):
            port_i = 0
        base = str(cfg.get("base_url") or "").strip()
        listening = _pids_listening_on_port(port_i) if port_i else []
        cfg_pid = cfg.get("pid")
        probe = embed_health(base_url=base, timeout_s=3.0, verify_inference=True)
        lines.append("—— 实时探测 ——")
        lines.append(f"端口 {port_i} 监听进程：{listening if listening else '无'}")
        if cfg_pid is not None:
            lines.append(
                f"config pid {cfg_pid} 存活：{'是' if _pid_alive(cfg_pid) else '否（已失效）'}"
            )
        probe_ok = bool(probe.get("ok"))
        lines.append(
            f"embedding 推理探针：{'✅ 可用' if probe_ok else '❌ 不可用'}"
            f"（{probe.get('detail') or probe.get('method') or '-'}）"
        )
        if not listening and not probe_ok:
            lines.append("结论：服务未运行，向量检索不可用；agent 检索时会尝试按需拉起")
    except Exception as exc:
        lines.append(f"实时探测：unavailable ({type(exc).__name__}: {exc})")

    # Index coverage: how fresh is the retrievable data (transcript vs L4).
    try:
        import json as _json

        from G4W.memory.vector.sandbox_paths import resolve_vector_index_dir

        meta_path = Path(resolve_vector_index_dir()) / "meta.json"
        if meta_path.is_file():
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))

            def _fmt_ts(ts):
                try:
                    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    return str(ts)

            lines.append("—— 索引覆盖 ——")
            lines.append(
                f"transcript 原文最后入库：{_fmt_ts(meta.get('last_transcript_upsert_at'))}"
                f"（{meta.get('last_transcript_upsert_run_id') or '-'}）"
            )
            lines.append(
                f"L4 insight 最后入库：{_fmt_ts(meta.get('last_l4_upsert_at'))}"
                f"（{meta.get('last_l4_upsert_run_id') or '-'}）"
            )
        else:
            lines.append("索引覆盖：meta.json 不存在（尚无索引）")
    except Exception as exc:
        lines.append(f"索引覆盖：unavailable ({type(exc).__name__}: {exc})")
    # TASK-F: index fingerprint / mismatch / rebuild hint
    try:
        from G4W.memory.vector import index_meta as _im

        lines.append("—— 索引指纹 ——")
        lines.extend(_im.meta_status_lines())
        hint = _im.format_mismatch_hint()
        if hint:
            lines.append(hint)
    except Exception as meta_err:
        lines.append(f"index meta：unavailable ({type(meta_err).__name__}: {meta_err})")
    try:
        from G4W.memory.vector import index_rebuild as _ir

        st = _ir.rebuild_state()
        if st.get("status") and st.get("status") != "idle":
            lines.append(f"rebuild_job：{st.get('status')} error={st.get('error') or '-'}")
    except Exception:
        pass
    return "\n".join(lines)


def handle_vector_command(args: str) -> str:
    """
    /vector status|on|off|meta|rebuild — hot switch + TASK-F index meta/rebuild.
    Depends on TASK-A API (vector_config); stop_tei from C or placeholder.
    """
    raw = (args or "").strip()
    parts = raw.split(None, 1)
    mode = (parts[0].lower() if parts else "status") or "status"
    rest = parts[1].strip() if len(parts) > 1 else ""

    if mode not in ("status", "on", "off", "meta", "rebuild"):
        return (
            "用法：/vector status | /vector on | /vector off | /vector meta | /vector rebuild"
        )

    # TASK-F: meta / rebuild do not require full vector_config for read paths,
    # but status/on/off still do.
    if mode == "meta":
        try:
            from G4W.memory.vector import index_meta as _im
            from G4W.memory.vector import index_rebuild as _ir

            lines = ["📇 向量索引 meta"]
            lines.extend(_im.meta_status_lines())
            hint = _im.format_mismatch_hint()
            if hint:
                lines.append(hint)
            lines.append("")
            lines.append(_ir.format_rebuild_status())
            return "\n".join(lines)
        except Exception as error:
            return f"⚠️ 读取索引 meta 失败：{type(error).__name__}: {error}"

    if mode == "rebuild":
        # Optional: /vector rebuild dry → dry_run report only (sync, no switch)
        try:
            from G4W.memory.vector import index_rebuild as _ir

            sub = rest.lower()
            if sub in ("dry", "dry_run", "--dry"):
                summary = _ir.build_index_from_l4(dry_run=True, switch_live=False)
                return (
                    "✅ dry_run 重建预览（未写 live）\n"
                    + "\n".join(f"{k}：{v}" for k, v in summary.items())
                )
            if sub in ("status", "st"):
                return _ir.format_rebuild_status()
            accepted = _ir.start_rebuild_async()
            body = _ir.format_rebuild_status()
            if accepted.get("accepted"):
                return f"✅ 已接受后台重建任务（源=L4 official，保留 tier_records）。\n\n{body}"
            return f"⚠️ 未能启动重建：{accepted}\n\n{body}"
        except Exception as error:
            return f"⚠️ 索引重建失败：{type(error).__name__}: {error}"

    try:
        vc = _import_vector_config_api()
        load_config = vc.load_config
        set_vector_enabled = vc.set_vector_enabled
        vector_enabled = vc.vector_enabled
    except Exception as error:
        return f"⚠️ 总闸未就绪：向量配置模块不可用（{type(error).__name__}: {error}）"

    if mode == "status":
        try:
            cfg = dict(load_config() or {})
            effective = bool(vector_enabled())
            return format_vector_status(cfg, effective)
        except Exception as error:
            return f"⚠️ 读取向量配置失败：{type(error).__name__}: {error}"

    if mode == "on":
        try:
            cfg = dict(set_vector_enabled(True) or {})
            # set may return full config; reload for consistency if needed
            if "enabled" not in cfg and "installed" not in cfg:
                cfg = dict(load_config() or {})
            installed = bool(cfg.get("installed"))
            effective = bool(vector_enabled())
            body = format_vector_status(cfg, effective)
            if not installed:
                return f"✅ 已写入 enabled=true，但外挂未安装，有效总闸仍为关。\n{_VECTOR_BAT_HINT}\n\n{body}"
            return f"✅ 向量外挂已开启（不在此命令内拉起 embed 服务；由 worker/保活按需启动）。\n\n{body}"
        except Exception as error:
            return f"⚠️ 开启向量外挂失败：{type(error).__name__}: {error}"

    # mode == "off"
    try:
        cfg = dict(set_vector_enabled(False) or {})
        if "enabled" not in cfg and "installed" not in cfg:
            cfg = dict(load_config() or {})
        stop_info = None
        try:
            stop_tei = _import_stop_tei()
            stop_info = stop_tei()
        except Exception as stop_err:
            stop_info = {"ok": False, "error": f"{type(stop_err).__name__}: {stop_err}"}
        effective = bool(vector_enabled())
        body = format_vector_status(cfg, effective)
        stop_line = f"embed stop：{stop_info}" if stop_info is not None else "embed stop：skipped"
        return f"✅ 向量外挂已关闭。\n{stop_line}\n\n{body}"
    except Exception as error:
        return f"⚠️ 关闭向量外挂失败：{type(error).__name__}: {error}"



class CommandRouter:
    def __init__(self, service):
        self.service = service

    def execute(self, binding: dict, text: str) -> str:
        parsed = parse_command(text)
        if not parsed:
            return ""
        name, args = parsed
        sender_id = binding["senderId"]
        binding_key = self.service.conversations.binding_key(binding["accountId"], sender_id)
        if name == "help":
            return help_text()
        if name == "switch":
            return "Conductor版每个微信用户固定使用唯一主会话，不再切换到其他用户会话。"
        if name == "turn":
            mode = args.lower() or "status"
            if mode == "status":
                return self.service.turn_progress.status_text(binding_key)
            if mode in ("on", "off"):
                state = self.service.turn_progress.set(binding_key, mode == "on")
                return self.service.turn_progress.status_text(binding_key)
            return "用法：/turn status | /turn on | /turn off"
        if name == "worker_turn":
            mode = args.lower() or "status"
            if mode == "status":
                return self.service.worker_turn.status_text(binding_key)
            if mode in ("on", "off"):
                self.service.worker_turn.set(binding_key, mode == "on")
                return self.service.worker_turn.status_text(binding_key)
            return "用法：/worker_turn status | /worker_turn on | /worker_turn off"
        if name == "input":
            mode = args.lower() or "status"
            if mode == "status":
                return self.service.input_capture.status_text(binding_key)
            if mode in ("on", "off"):
                self.service.input_capture.set(binding_key, mode == "on")
                return self.service.input_capture.status_text(binding_key)
            return "用法：/input status | /input on | /input off"
        if name == "bind":
            if not args:
                current = self.service.conversations.get_binding(binding_key) or {}
                return f"当前工作目录：{current.get('workspaceRoot') or self.service.config.workspace_root}"
            path = Path(args).expanduser().resolve()
            if not path.is_dir():
                return f"⚠️ 目录不存在：{path}"
            self.service.conversations.update_binding(binding_key, workspaceRoot=str(path))
            self.service.controller.reset_session(sender_id, archive_history=False)
            return f"📁 已绑定工作目录：{path}"
        if name == "status":
            workers = self.service.workers.list_for(binding_key)
            running = sum(item.get("status") == "running" for item in workers)
            schedules = self.service.schedules.list_for(binding_key)
            reminder_count = sum(item.get("status") == "scheduled" and item.get("kind") == "reminder" for item in schedules)
            xiaoyi_jobs = self.service.xiaoyi.list_for(binding_key)
            current_binding = self.service.conversations.get_binding(binding_key) or {}
            supervision = self.service.supervision.status(sender_id)
            cache = self.service.cache_metrics.status(sender_id)
            outbox_health = self.service.outbox_health()
            checkin = self.service.checkins.status(binding_key)
            next_checkin_at = float(checkin.get("nextAt", 0) or 0)
            next_checkin_text = (
                datetime.fromtimestamp(next_checkin_at, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
                if checkin.get("enabled") and next_checkin_at > 0 else "未安排"
            )
            history_path = self.service.config.conversations_dir / re.sub(r"[^a-zA-Z0-9._-]+", "_", sender_id) / "conductor" / "history" / "current.json"
            try:
                history_raw = history_path.read_text(encoding="utf-8")
                history_messages = len(json.loads(history_raw))
                history_chars = len(history_raw)
            except Exception:
                history_messages = history_chars = 0
            try:
                attachment_pending = sum(item.get("status") == "pending" for item in self.service.channel.attachment_retries.read().get("jobs", []))
            except Exception:
                attachment_pending = 0
            return "\n".join([
                "📊 G4W 状态",
                f"账号：{binding.get('accountId', '')}",
                f"工作目录：{current_binding.get('workspaceRoot') or self.service.config.workspace_root}",
                f"总管模型：{current_binding.get('conductorModel') or self.service.config.conductor_model}",
                f"Worker：{len(workers)}（运行中 {running}）",
                f"定时提醒：{reminder_count}",
                f"小艺任务：{len(xiaoyi_jobs)}（待完成 {sum(item.get('status') not in ('completed', 'failed', 'cancelled') for item in xiaoyi_jobs)}）",
                f"监督状态：{(supervision.get('session') or {}).get('state', '未开启')}",
                f"随机check-in：{'已开启' if checkin.get('enabled') else '未开启'}",
                f"下次随机check-in：{next_checkin_text}",
                f"微信发送线程：{'正常' if outbox_health.get('alive') else '已停止'}"
                + (f"（最近错误：{outbox_health.get('lastError')}）" if outbox_health.get('lastError') else ""),
                f"等待下次context_token补发：{self.service.deferred.count(sender_id)}",
                f"中间turn回复：{'开启' if self.service.turn_progress.get(binding_key) else '关闭'}",
                f"完整Input快照：{'开启' if self.service.input_capture.get(binding_key) else '关闭'}",
                f"Conductor history：{history_messages} messages / {history_chars} chars",
                f"附件下载重试：{attachment_pending}",
                f"最近上下文：{self.service.config.recent_pairs} 轮",
                f"缓存命中：最近 {cache.get('lastRatio', 0):.1%} / 近{cache.get('rollingSamples', 0)}次LLM调用 {cache.get('rollingRatio', 0):.1%}",
                f"缓存拆分：首Turn {cache.get('firstTurn', {}).get('ratio', 0):.1%}（{cache.get('firstTurn', {}).get('samples', 0)}次） / 工具Turn {cache.get('toolTurn', {}).get('ratio', 0):.1%}（{cache.get('toolTurn', {}).get('samples', 0)}次）",
                f"Clean history：{cache.get('cleanHistoryUserRounds', 0)}个用户轮 / {cache.get('cleanHistoryMessageCount', 0)}条可见消息 / {cache.get('cleanHistoryMode') or '尚无样本'}",
                f"System指纹：{cache.get('systemFingerprint') or '(尚无样本)'}",
            ])
        if name == "workers":
            workers = self.service.workers.list_for(binding_key)
            if not workers:
                return "当前没有Worker。"
            return "\n".join(
                f"- {item['id']} | {item.get('capabilityId')} | {item.get('status')} | run {item.get('runIndex')} | {item.get('progress') or item.get('summary') or ''}"
                for item in workers
            )
        if name == "worker":
            if not args:
                return "用法：/worker <id>"
            try:
                detail = self.service.controller.get_worker(sender_id, args)
            except Exception as error:
                return f"⚠️ {error}"
            return json.dumps(detail, ensure_ascii=False, indent=2)
        if name == "stop":
            return self.service.controller.cancel_active(sender_id)["message"]
        if name == "chunk":
            if not args:
                return f"当前微信短分片合并阈值：{self.service.channel.get_min_chunk_chars()} 字。用法：/chunk <1-3800>"
            try:
                value = int(args)
            except ValueError:
                return "用法：/chunk <1-3800>"
            if not 1 <= value <= 3800:
                return "⚠️ 分片阈值必须在 1-3800 之间。"
            updated = self.service.channel.set_min_chunk_chars(value)
            return f"✅ 微信短分片合并阈值已设为 {updated} 字。"
        if name == "checkin":
            if not args:
                return json.dumps(self.service.checkins.status(binding_key), ensure_ascii=False, indent=2)
            if args.lower() in ("off", "0", "disable"):
                self.service.controller.update_checkin_config(sender_id, enabled=False)
                return "⏰ 已关闭随机主动check-in。"
            match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", args)
            if not match:
                return "用法：/checkin <最小分钟>-<最大分钟>，或 /checkin off"
            configured = self.service.controller.update_checkin_config(sender_id, int(match.group(1)), int(match.group(2)), True)
            return f"⏰ 随机check-in已设为 {configured['minimumMinutes']}-{configured['maximumMinutes']} 分钟。"
        if name == "reminders":
            jobs = self.service.schedules.list_for(binding_key)
            active = [job for job in jobs if job.get("status") == "scheduled"]
            if not active:
                return "当前没有待触发提醒或check-in。"
            return "\n".join(f"- {job['id']} | {job['kind']} | {job['content']} | due={job['dueAt']}" for job in active)
        if name == "todo":
            return self._handle_todo(sender_id, args)
        if name == "xiaoyi":
            if args:
                try:
                    return json.dumps(self.service.xiaoyi.get(binding_key, args), ensure_ascii=False, indent=2)
                except Exception as error:
                    return f"⚠️ {error}"
            jobs = self.service.xiaoyi.list_for(binding_key)
            if not jobs:
                return "当前没有小艺任务。"
            return "\n".join(f"- {item['jobId']} | {item.get('status')} | {item.get('prompt', '')[:80]}" for item in jobs[:20])

        if name == "location":
            latest = self.service.locations.latest()
            if not latest:
                return "当前没有位置记录。"
            movements = self.service.locations.movements(3)
            lines = ["📍 最近位置", json.dumps(latest, ensure_ascii=False, indent=2)]
            if movements:
                lines.extend(["", "最近重大移动：", json.dumps(movements, ensure_ascii=False, indent=2)])
            return "\n".join(lines)
        if name == "l4":
            return json.dumps(self.service.l4.status(sender_id), ensure_ascii=False, indent=2)
        if name == "supervise":
            return json.dumps(self.service.supervision.status(sender_id), ensure_ascii=False, indent=2)
        if name == "reread":
            return self.service.controller.reread(sender_id)
        if name in ("new", "compact"):
            self.service.controller.reset_session(sender_id, archive_history=True, clean_start=(name == "new"))
            if name == "new":
                return "🆕 已开启干净的总管模型会话；可见 transcript 和长期记忆仍保留。"
            return "🗜️ 已归档旧模型history；下次消息会从长期记忆和最近20轮可见聊天重建上下文。"
        if name == "model":
            if args:
                try:
                    updated = self.service.controller.set_model(sender_id, args)
                    return f"🤖 已切换总管模型：{updated['model']}（索引 {updated['modelNo']}）。"
                except Exception as error:
                    return f"⚠️ 模型切换失败：{error}"
            try:
                # 与 /model <编号> 共用同一真实 Conductor session；不存在时自动创建。
                agent = self.service.controller.session(sender_id).agent
                all_llms = list(agent.list_llms())
                current_no = agent.llm_no
                current_name = next(
                    (model_name for i, model_name, is_current in all_llms if is_current),
                    agent.get_llm_name(),
                )
                parts = [f"🟢 当前模型:\n  [{current_no}] {current_name}"]
                others = [(i, model_name) for i, model_name, is_current in all_llms if not is_current]
                if others:
                    parts.append(
                        f"📋 其他可用模型 ({len(others)}):\n"
                        + "\n".join(f"  [{i}] {model_name}" for i, model_name in others)
                    )
                return (
                    "🤖 模型列表\n━━━━━━━━━━━━━━━━━\n"
                    + "\n\n".join(parts)
                    + "\n━━━━━━━━━━━━━━━━━\n💡 /model <编号> 切换模型"
                )
            except Exception as error:
                return f"⚠️ 模型列表加载失败：{error}"
        if name in ("name", "identity", "gender", "botname"):
            field = {"name": "userName", "identity": "userIdentity", "gender": "userGender", "botname": "botName"}[name]
            if not args:
                profile = self.service.controller.identity_profile(sender_id)
                return f"{field}：{profile.get(field) or '未单独设置'}"
            if name == "gender" and args not in ("female", "male", "neutral"):
                return "用法：/gender <female|male|neutral>"
            updated = self.service.controller.update_identity(sender_id, field, args)
            return f"✅ 已更新 {field}：{updated.get(field)}"
        if name == "l4compress":
            result = self.service.l4.command(binding_key, sender_id, args)
            if result.get("status") == "usage":
                return result["message"]
            if result.get("l4Started"):
                return f"🗜️ L4 {result.get('automatic') and 'auto' or '增量深压'} 已触发，正在通过GA Worker进行语义记忆维护。\n完成后会再汇报结果。"
            return json.dumps(result, ensure_ascii=False, indent=2)
        if name == "kb":
            return handle_kb_command(args)
        if name == "vector":
            return handle_vector_command(args)
        return "未知指令。\n\n" + help_text()

    def _handle_todo(self, sender_id: str, args: str) -> str:
        """/todo add|list|done <id>|del <id>|cancel <id> — 个人任务清单。"""
        parts = (args or "").split()
        if not parts or parts[0].lower() in ("list", "ls"):
            return self.service.todos.render_menu(sender_id)
        op = parts[0].lower()
        try:
            if op == "add":
                rest = (args or "")[len(parts[0]):].strip()
                due = None
                repeat_minutes = 0
                due_match = re.search(r"--due\s+(\S+)", rest)
                if due_match:
                    due = due_match.group(1)
                    rest = rest.replace(due_match.group(0), "").strip()
                repeat_match = re.search(r"--repeat\s+(\d+)", rest)
                if repeat_match:
                    repeat_minutes = int(repeat_match.group(1))
                    rest = rest.replace(repeat_match.group(0), "").strip()
                if not rest:
                    return "用法：/todo add <内容> [--due 2026-08-15T12:00] [--repeat 分钟]"
                item = self.service.todos.add(sender_id, rest, due_at=due,
                                              recurrence_seconds=repeat_minutes * 60 if repeat_minutes else 0)
                extra = ""
                if due:
                    extra += " · 到点提醒"
                if repeat_minutes:
                    extra += f" · 每 {repeat_minutes} 分钟重复"
                return f"✅ 已添加任务 [{item['id']}] {item['text']}{extra}"
            if op == "done":
                if len(parts) < 2:
                    return "用法：/todo done <id> [完成说明]"
                todo_id = parts[1]
                confirm = " ".join(parts[2:])
                item = self.service.todos.done(sender_id, todo_id, confirm)
                return f"✅ 已完成 [{item['id']}] {item['text']}"
            if op == "del":
                if len(parts) < 2:
                    return "用法：/todo del <id>"
                self.service.todos.delete(sender_id, parts[1])
                return f"🗑️ 已删除任务 {parts[1]}"
            if op == "cancel":
                if len(parts) < 2:
                    return "用法：/todo cancel <id>"
                item = self.service.todos.cancel(sender_id, parts[1])
                return f"↩️ 已取消 [{item['id']}] {item['text']}"
            return "用法：/todo add <内容> [--due 时间] [--repeat 分钟] · /todo list · /todo done <id> · /todo del <id> · /todo cancel <id>"
        except KeyError:
            return "⚠️ 找不到该任务编号,用 /todo list 查看"
        except ValueError as error:
            return f"⚠️ {error}"

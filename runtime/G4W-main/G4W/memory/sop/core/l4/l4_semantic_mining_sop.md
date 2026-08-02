# G4W L4 语义挖掘 SOP

## 目的

从 G4W 微信对话记录中挖掘可长期保留的用户记忆。该流程依赖 GA/Worker 的语义判断，用来替代只靠关键词扫描的 `/l4compress`。

## 入口

唯一入口是 G4W 确定性组件 `L4MaintenanceService` / `l4_safe.prepare_run` 生成的 `run_manifest.json`。

Worker 只执行语义挖掘，不自行准备 run，也不自行 finalize。manifest 会明确列出允许读取的文件和允许写入的目录。不要搜索整个磁盘，不要读取无关历史。

## 输入

只能读取 `run_manifest.json` 中列出的文件：

- `user_only_files`：主要挖掘输入，只包含用户消息。
- `chunk_files`：从 `user_only` 生成的日级摘要，可用于导航，但不能作为正式证据。
- `source_transcripts`：原始证据定位文件；只有需要精确上下文时才读取。

严禁读取或写入：

- `memory/users`
- `memory/operational`
- `transcripts` 作为写入目标
- `assistant-replies`
- 全局 `all_histories.txt`
- GA 全局记忆，包括 `runtime/app/memory`

## 输出

必须把以下所有文件写入 manifest 指定的 `allowed_output_dir`：

- `active_knowledge.candidate.json`
- `emotion_events.candidate.json`
- `incremental_markers.candidate.json`
- `README.candidate.md`
- `memory_brief.candidate.md`
- `proposed_updates.candidate.md`
- `subagent_report.md`

只有确定性 validator 可以把这些 candidate 合并进正式 `history_insight`。

## 证据规则

每条正式 insight 都必须包含：

```json
{
  "timestamp": "来源用户消息时间戳",
  "speaker": "user",
  "source_transcript": "必须与 manifest.source_transcripts 中列出的路径完全一致",
  "user_only_source": "必须与 manifest.user_only_files 中列出的路径完全一致",
  "snippet": "简短、精确的用户侧原文证据片段",
  "confidence": 0.8
}
```

只有用户消息明确支持该 insight 时，才使用 `confidence >= 0.8`。低置信度观察写入 `proposed_updates.candidate.md`，不要写进正式 candidate JSON 列表。

## 语义挖掘标准

做语义阅读，不做关键词扫描。目标是接近 GA 2026-06-26 质量：

- 用户画像：稳定偏好、行为模式、稳定自我描述。
- 持续项目：项目名、状态、计划、阻塞点、下一步。
- Agent 能力沉淀：用户明确要求的规则、已验证工作流、集成经验。
- 记忆教训：Agent 过去忘记过、记错过、应当保留的内容。
- 情绪事件：语气变化、挫败、缓解、感谢、失望、压力。
- 活动事项：用户生活中正在进行的事项。
- 已消失事项：有时间边界、很可能已经结束的事项。
- 待验证事项：未来需要确认的内容。

不要保存普通闲聊摘要、助手自我描述、陪伴填充语、吃饭提醒或重复噪声。

## active_knowledge 候选结构

使用稳定 key，让 validator 能更新旧条目，而不是无限追加：

```json
{
  "_meta": {
    "generated": "ISO 时间",
    "source": "GA subagent semantic mining",
    "run_id": "manifest.run_id"
  },
  "user_profile": {
    "behavior_patterns": [
      {
        "stable_key": "sleep_irregularity",
        "summary": "简短、可长期保留的发现",
        "timestamp": "...",
        "speaker": "user",
        "source_transcript": "...",
        "user_only_source": "...",
        "snippet": "...",
        "confidence": 0.9
      }
    ],
    "preferences": []
  },
  "ongoing_projects": [],
  "agent_capabilities_learned": [],
  "memory_lessons": []
}
```

列表项必须是对象，不能是裸字符串。每个对象都需要证据字段。

## 情绪事件

`emotion_events.candidate.json`：

```json
{
  "_meta": {"run_id": "..."},
  "events": [
    {
      "category": "frustration|relief|gratitude|fatigue|pressure|surprise|disappointment|other",
      "intensity": 1,
      "interpretation": "为什么这是一个语气/情绪事件",
      "timestamp": "...",
      "speaker": "user",
      "source_transcript": "...",
      "user_only_source": "...",
      "snippet": "...",
      "confidence": 0.85
    }
  ]
}
```

情绪判断看语气和上下文，不只看负面词。

## incremental_markers 候选

`incremental_markers.candidate.json` 只写语义状态。真实游标由 validator 负责。

```json
{
  "activities": {},
  "gone_things": [],
  "pending_checks": []
}
```

## 报告文件

`README.candidate.md` 是给微信 Agent 看的人工报告。包含扫描窗口、计数、关键发现和产物文件列表。

`memory_brief.candidate.md` 是默认注入 prompt 的唯一摘要。控制在 600-1000 个中文字符以内。

`proposed_updates.candidate.md` 写有价值但不自动合并到 `users/operational` 的建议。

`subagent_report.md` 必须写明：

- manifest 路径和 run id
- 读取过的文件
- 写出的文件
- 验证风险或低置信度观察
- 给主 Agent 的简短最终总结
